"""Text chunking utilities for Akopia.

Uses `tiktoken` (cl100k_base) for real token counts when available, with a
safe `chars/4` fallback so the module still loads in minimal envs.

Two strategies:
    * ``recursive`` — LangChain-style recursive character splitter.
        Tries a list of separators from coarse (``\n\n``) to fine
        (character-level) and only descends when the current chunk is
        still oversized. Preserves the most semantic boundary possible.
    * ``paragraph`` — paragraph-first greedy accumulator (the pre-0.2.0
        behavior). Kept for backward compatibility.

Both strategies honor ``overlap_tokens``: each chunk (except the first)
starts with the last N tokens of the previous chunk.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

__all__ = [
    "chunk_text",
    "detect_modality",
    "recursive_split",
    "count_tokens",
    "ChunkerConfig",
    "get_chunker_config",
    "reset_chunker_config",
]

logger = logging.getLogger(__name__)


# ── Config ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChunkerConfig:
    """Effective chunker settings in-process.

    Distinct from ``common.akopia_config.ChunkerConfig`` (the pydantic wire
    model) so consumers can construct overrides without importing the
    whole pydantic tree. Mirror of the same three fields.
    """

    strategy: str = "recursive"
    chunk_size_tokens: int = 512
    overlap_tokens: int = 50


_DEFAULT_CHUNKER = ChunkerConfig()


@lru_cache(maxsize=1)
def get_chunker_config() -> ChunkerConfig:
    """Resolve the effective chunker config once per process.

    Precedence: akopia.yaml (``core.chunker.*``) > env vars (``AKOPIA_CHUNK_*``)
    > defaults (recursive / 512 / 50). Cached via lru_cache — call
    ``reset_chunker_config()`` if tests need to reload.
    """
    strategy = os.getenv("AKOPIA_CHUNK_STRATEGY")
    size_env = os.getenv("AKOPIA_CHUNK_SIZE_TOKENS")
    overlap_env = os.getenv("AKOPIA_CHUNK_OVERLAP_TOKENS")

    chunk_size: Optional[int] = int(size_env) if size_env else None
    overlap: Optional[int] = int(overlap_env) if overlap_env else None

    try:
        from common.config_loader import load_config
        cfg = load_config()
        ccfg = getattr(cfg.core, "chunker", None)
        if ccfg is not None:
            strategy = strategy or ccfg.strategy
            if chunk_size is None:
                chunk_size = ccfg.chunk_size_tokens
            if overlap is None:
                overlap = ccfg.overlap_tokens
    except Exception as e:
        logger.debug("akopia.yaml not loaded for chunker config (%s); using env/defaults", e)

    return ChunkerConfig(
        strategy=strategy or _DEFAULT_CHUNKER.strategy,
        chunk_size_tokens=chunk_size if chunk_size is not None else _DEFAULT_CHUNKER.chunk_size_tokens,
        overlap_tokens=overlap if overlap is not None else _DEFAULT_CHUNKER.overlap_tokens,
    )


def reset_chunker_config() -> None:
    """Clear the cached config. Primarily for tests."""
    get_chunker_config.cache_clear()


# ── Tokenization ─────────────────────────────────────────────────────

_ENCODING = None
_ENCODING_LOADED = False


def _get_encoding():
    """Lazy-load cl100k_base encoding. None if tiktoken is unavailable."""
    global _ENCODING, _ENCODING_LOADED
    if _ENCODING_LOADED:
        return _ENCODING
    try:
        import tiktoken  # type: ignore
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENCODING = None
    _ENCODING_LOADED = True
    return _ENCODING


def count_tokens(text: str) -> int:
    """Return token count for ``text`` using cl100k_base when available,
    falling back to ``max(1, len(text)//4)`` otherwise.
    """
    if not text:
        return 0
    enc = _get_encoding()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // 4)


def _encode(text: str) -> list[int] | None:
    enc = _get_encoding()
    if enc is None:
        return None
    return enc.encode(text)


def _decode(tokens: list[int]) -> str:
    enc = _get_encoding()
    if enc is None:
        raise RuntimeError("tiktoken not available")
    return enc.decode(tokens)


# ── Modality detection ──────────────────────────────────────────────

def detect_modality(path: str) -> str:
    """Detect file modality from extension.

    Returns only ``"text"`` or ``"image"`` — these are the modalities
    wired end-to-end. PDFs resolve to text (the pdf-text extractor
    emits text). Audio/video are treated as text for now; extending
    Modality is step 1 of docs/adding-a-modality.md.
    """
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    image_exts = {"png", "jpg", "jpeg", "gif", "bmp", "tiff", "tif", "webp", "ico"}
    if ext in image_exts:
        return "image"
    return "text"


# ── Recursive splitter ───────────────────────────────────────────────

_DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


def recursive_split(
    text: str,
    max_tokens: int,
    overlap_tokens: int = 0,
    separators: Optional[list[str]] = None,
) -> list[str]:
    """Recursive character text splitter.

    Tries ``separators`` in order. At each level, the text is split and
    adjacent pieces are greedily merged into chunks under ``max_tokens``.
    Any merged chunk still over budget is recursed into with the next
    separator. If the finest separator (``""``) still fails, the chunk is
    sliced by tokens directly.

    Overlap is applied at the end (see ``_apply_overlap``).
    """
    if not text.strip():
        return []
    separators = separators if separators is not None else list(_DEFAULT_SEPARATORS)
    pieces = _recursive_split_inner(text, max_tokens, separators)
    if overlap_tokens > 0:
        pieces = _apply_overlap(pieces, overlap_tokens)
    return pieces


def _split_on_separator(text: str, sep: str) -> list[str]:
    if sep == "":
        # Will be handled by hard token slicing; don't split here.
        return [text]
    if sep == "\n\n":
        # Collapse runs of blank lines.
        return re.split(r"\n\s*\n", text)
    return text.split(sep)


def _merge_pieces(pieces: list[str], sep: str, max_tokens: int) -> list[str]:
    """Greedily merge adjacent ``pieces`` joined by ``sep`` while the
    running token count stays ≤ ``max_tokens``. Pieces too large on their
    own are emitted as-is (caller will recurse).
    """
    joiner = sep if sep not in ("", ) else ""
    merged: list[str] = []
    current = ""
    for p in pieces:
        if not p:
            continue
        candidate = current + joiner + p if current else p
        if count_tokens(candidate) <= max_tokens:
            current = candidate
        else:
            if current:
                merged.append(current)
            current = p
    if current:
        merged.append(current)
    return merged


def _hard_token_slice(text: str, max_tokens: int) -> list[str]:
    """Last-resort: slice the text by raw tokens. Uses tiktoken if
    available, else chars/4 approximation."""
    enc_tokens = _encode(text)
    if enc_tokens is not None:
        out: list[str] = []
        step = max(1, max_tokens)
        for i in range(0, len(enc_tokens), step):
            out.append(_decode(enc_tokens[i:i + step]))
        return out
    max_chars = max(1, max_tokens * 4)
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _recursive_split_inner(
    text: str,
    max_tokens: int,
    separators: list[str],
) -> list[str]:
    if count_tokens(text) <= max_tokens:
        return [text] if text.strip() else []

    if not separators:
        # No more separators — slice hard.
        return _hard_token_slice(text, max_tokens)

    sep = separators[0]
    remaining = separators[1:]

    if sep == "":
        return _hard_token_slice(text, max_tokens)

    pieces = _split_on_separator(text, sep)
    merged = _merge_pieces(pieces, sep, max_tokens)

    result: list[str] = []
    for m in merged:
        if count_tokens(m) <= max_tokens:
            if m.strip():
                result.append(m)
        else:
            # Recurse into this merged block with finer separators.
            result.extend(_recursive_split_inner(m, max_tokens, remaining))
    return result


# ── Paragraph strategy (legacy) ──────────────────────────────────────


def _paragraph_split(text: str, max_tokens: int) -> list[str]:
    """Original paragraph-first greedy splitter (pre-0.2.0 behavior)."""
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if not para:
            continue
        candidate = current + "\n\n" + para if current else para
        if count_tokens(candidate) <= max_tokens:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if count_tokens(para) > max_tokens:
                sentences = re.split(r"(?<=[.!?])\s+", para)
                sub = ""
                for sent in sentences:
                    sub_cand = sub + " " + sent if sub else sent
                    if count_tokens(sub_cand) <= max_tokens:
                        sub = sub_cand
                    else:
                        if sub:
                            chunks.append(sub)
                        # Sentence itself oversized → hard slice.
                        if count_tokens(sent) > max_tokens:
                            chunks.extend(_hard_token_slice(sent, max_tokens))
                            sub = ""
                        else:
                            sub = sent
                current = sub
            else:
                current = para

    if current:
        chunks.append(current)
    return chunks


# ── Overlap ─────────────────────────────────────────────────────────


def _apply_overlap(chunks: list[str], overlap_tokens: int) -> list[str]:
    """Prepend the last ``overlap_tokens`` tokens of chunk[i-1] onto
    chunk[i]. Uses real tokens when tiktoken is available, else a
    character approximation."""
    if overlap_tokens <= 0 or len(chunks) < 2:
        return chunks

    enc = _get_encoding()
    out = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = chunks[i - 1]
        if enc is not None:
            prev_tokens = enc.encode(prev)
            tail = enc.decode(prev_tokens[-overlap_tokens:]) if prev_tokens else ""
        else:
            tail_chars = overlap_tokens * 4
            tail = prev[-tail_chars:] if len(prev) > tail_chars else prev
        # Join with a separator only if both sides are non-empty and
        # wouldn't trivially concatenate mid-word.
        sep = "" if (not tail or tail.endswith(("\n", " ")) or chunks[i].startswith(("\n", " "))) else " "
        out.append((tail + sep + chunks[i]).lstrip())
    return out


# ── Public entry point ──────────────────────────────────────────────


def chunk_text(
    text: str,
    max_tokens: Optional[int] = None,
    overlap_tokens: Optional[int] = None,
    source_id: str = "",
    path: str = "",
    repo: Optional[str] = None,
    strategy: Optional[str] = None,
) -> list[dict]:
    """Split text into overlapping chunks.

    Any arg left as ``None`` is resolved from ``get_chunker_config()``
    (which reads ``core.chunker.*`` from akopia.yaml with env + default
    fallback). Callers that want to override just one knob can pass that
    one explicitly and let the rest fall through to config.

    Args:
        text: Raw input text.
        max_tokens: Hard upper bound on tokens per chunk. Default:
            ``core.chunker.chunk_size_tokens`` (512).
        overlap_tokens: Tokens of previous-chunk tail to prepend to each
            subsequent chunk (RAG recall). Default:
            ``core.chunker.overlap_tokens`` (50).
        source_id: Stamped into each chunk's ``chunk_id``.
        path: File path, stamped into ``chunk_id`` and ``path``.
        repo: Optional repo hint passed through unchanged.
        strategy: ``"recursive"`` (default) or ``"paragraph"``.

    Returns:
        List of chunk dicts compatible with ``Chunk(**c)``.
    """
    if not text.strip():
        return []

    cfg = get_chunker_config()
    max_tokens = cfg.chunk_size_tokens if max_tokens is None else max_tokens
    overlap_tokens = cfg.overlap_tokens if overlap_tokens is None else overlap_tokens
    strategy = cfg.strategy if strategy is None else strategy

    if strategy == "paragraph":
        pieces = _paragraph_split(text, max_tokens)
        if overlap_tokens > 0:
            pieces = _apply_overlap(pieces, overlap_tokens)
    elif strategy == "recursive":
        pieces = recursive_split(text, max_tokens, overlap_tokens)
    else:
        raise ValueError(f"unknown chunk strategy: {strategy!r}")

    result = []
    total = len(pieces)
    for i, chunk in enumerate(pieces):
        chunk_id = f"{source_id}:{path}:{i}"
        snippet = chunk[:4000]
        result.append({
            "chunk_id": chunk_id,
            "content": chunk,
            "path": path,
            "repo": repo,
            "chunk_index": i,
            "total_chunks": total,
            "snippet": snippet,
        })
    return result
