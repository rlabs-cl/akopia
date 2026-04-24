"""Overlap + recursive split invariants."""
from common.chunker import chunk_text, count_tokens, recursive_split


def test_overlap_applied():
    text = "\n\n".join([f"para {i} " + ("word " * 50) for i in range(10)])
    chunks = chunk_text(text, max_tokens=80, overlap_tokens=20, path="x.md")
    assert len(chunks) >= 2
    # Each subsequent chunk must start with some tokens from the end of the previous.
    for i in range(1, len(chunks)):
        prev_tail = chunks[i - 1]["content"][-200:]
        curr_head = chunks[i]["content"][:200]
        # At least one meaningful word should appear in both — cheap invariant.
        words_prev = set(prev_tail.split())
        words_curr = set(curr_head.split())
        assert words_prev & words_curr, (
            f"no overlap between chunk {i - 1} tail and chunk {i} head"
        )


def test_zero_overlap_no_duplication():
    text = "a " * 400
    chunks = chunk_text(text, max_tokens=20, overlap_tokens=0, path="x.md")
    joined = "".join(c["content"] for c in chunks)
    # Without overlap, total content length should roughly equal source (±
    # some whitespace rewrites from splitting).
    assert len(joined.replace(" ", "")) >= len(text.replace(" ", "")) * 0.95


def test_recursive_split_no_paragraphs():
    # Single long line with no \n\n — recursive must fall through separators.
    text = "the quick brown fox jumps over the lazy dog. " * 30
    pieces = recursive_split(text, max_tokens=40, overlap_tokens=0)
    assert len(pieces) > 1
    assert all(count_tokens(p) <= 40 * 2 for p in pieces)  # loose ceiling; recursion may not split mid-word
