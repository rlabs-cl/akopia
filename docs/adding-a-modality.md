# Akopia — Adding a New Modality

**Audience:** developers adding a whole new content category — audio,
video, 3D scans, whatever — to the pipeline.

**Hard truth up front.** A modality touches every layer. Adding a
source adapter is self-contained (§docs/writing-a-connector.md).
Adding an extractor is self-contained too. Adding a **modality**
means changes in `common/models.py`, at least one extractor, the
embedder service, Qdrant collection setup, the router, and usually
the search endpoints. Budget accordingly.

## 1. Definition

A **modality** is a category of content with its own vector space
and retrieval path. Today the enum at `common/models.py::Modality`
has **exactly two values** — both fully wired end-to-end:

```python
class Modality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
```

- **`text`** — the default path. Extractors produce `text`;
  embeddings go to `akopia_text` (Qdrant, 768-d); Meilisearch is a lexical
  companion. **PDFs live here**: the `pdf-text` extractor turns a
  `.pdf` into text before it reaches the embedder.
- **`image`** — CLIP-based. Embeddings go to `akopia_image` (Qdrant,
  512-d).

Earlier revisions of the enum also listed `audio`, `video`, and `pdf`
— those were cargo-cult. PDFs always flowed as text once extracted,
and audio/video never had a working embedder branch. The enum was
pruned in 0.2 so that having an entry here means the wiring is
genuinely there; adding a new modality today means **both** extending
the enum **and** completing the end-to-end checklist below.

## 2. Why this is harder than adding an adapter or extractor

Three non-obvious places branch on modality:

- **Router (`concentrador/main.py`)** and its newer sibling
  (`concentrador/router.py`). The legacy path's
  `create_embedding_jobs()` is a modality switch:

  ```python
  if event.modality.value == "text":
      ...
  if event.modality.value == "image":
      ...
  ```

  and `build_extract_job()` (new path) explicitly bails on `IMAGE`:

  ```python
  if event.modality == Modality.IMAGE:
      return None    # images bypass extractors, go straight to CLIP
  ```

- **Embedder service (`embeddings/main.py`)** hard-codes the
  image path:

  ```python
  if job.modality.value == "image":
      paths = [c.content for c in job.chunks]
      vectors = await embed_images(paths)
      model_name = "Qdrant/clip-ViT-B-32-vision"
  else:
      texts = [c.content for c in job.chunks]
      vectors = await embed_texts(texts)
  ```

  The `EmbedderBackend` protocol (`embeddings/backends/__init__.py`)
  assumes text: `async def embed(self, texts: list[str]) -> list[list[float]]`.
  It is not modality-aware.

- **Qdrant collections
  (`concentrador/index_manager.py::_ensure_qdrant_collection`)** are
  created per modality with a hardcoded dimension:

  ```python
  await self._ensure_qdrant_collection(Config.QDRANT_TEXT_COLLECTION,  768)
  await self._ensure_qdrant_collection(Config.QDRANT_IMAGE_COLLECTION, 512)
  ```

  and `upsert()` picks collection by modality (raising
  `UnsupportedModalityError` for anything outside the known set):

  ```python
  if emb.modality == "image":
      collection = Config.QDRANT_IMAGE_COLLECTION
  elif emb.modality in ("text", "audio_transcript", "video_transcript"):
      collection = Config.QDRANT_TEXT_COLLECTION
  else:
      raise UnsupportedModalityError(...)
  ```

  So "add a branch here" is a required step of the checklist — the
  old silent fallback to `akopia_text` is gone.

## 3. Worked example: adding `audio`

We'll walk the full checklist. Today `Modality` has only `text` and
`image`, so step 1 is extending the enum itself.

### 3.1 Model layer — `common/models.py`

Add the new value:

```python
class Modality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"     # new
```

Also check `EmbeddingModality`:

```python
class EmbeddingModality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO_TRANSCRIPT = "audio_transcript"
    VIDEO_TRANSCRIPT = "video_transcript"
```

This enum encodes **what the embedder produced** rather than the
source modality. If you embed audio as a transcript (whisper→text),
use `AUDIO_TRANSCRIPT` and the existing text vector space. If you
embed the audio itself (wav2vec, CLAP, etc.), add a new value like
`AUDIO = "audio"`.

### 3.2 Extractor

You have a choice for how to represent audio content:

- **Transcribed text** (ASR). The extractor runs whisper/vosk/etc.,
  yields chunks with `modality=Modality.TEXT` containing transcript
  text. You get keyword search for free, reuse the text embedder and
  the `akopia_text` collection. **Not a new modality** — just an
  extractor that targets TEXT. See §6.
- **Audio embeddings directly** (CLAP, wav2vec2). Each chunk's
  `content` is a path or reference to the audio segment; a new
  embedder backend turns it into a vector. **This is a new
  modality.**

For the "new modality" path, the extractor emits chunks with
`modality=Modality.AUDIO` and a `content` field that the embedder
understands (e.g. a filesystem path to a segmented clip).

```python
# extractors/audio_segments.py
class AudioSegmenter(BaseExtractor):
    plugin_id = "audio-segments"
    version = "0.1.0"
    priority = 10

    supported_mime_types = ["audio/mpeg", "audio/wav", "audio/x-wav"]
    supported_extensions = [".mp3", ".wav", ".flac"]

    async def extract(self, content_ref, event):
        raw_path = content_ref.path or await self._dump_to_tmp(content_ref)
        segments = split_audio(raw_path, seconds=30)  # your segmenter
        text_parts = [f"[audio segment {i}: {s.path}]" for i, s in enumerate(segments)]
        return ExtractedContent(
            source_event_id=event.event_id,
            text="\n".join(text_parts),
            metadata={"format": "audio", "segment_paths": [s.path for s in segments]},
            extractor_id=self.plugin_id,
            extractor_version=self.version,
        )
```

**Trade-off to document in your PR:** inline segment paths on the
`ExtractedContent.metadata` or emit them as *attached* `ChangeEvent`s
(see `ExtractedContent.attached` in `common/models.py`). Attached
events go back through the router with `depth += 1` and get their own
embedder pipeline, which matches how we'd eventually handle video
keyframes.

### 3.3 Embedder backend — pick an option

The `EmbedderBackend` protocol is text-first. For `image`,
`embeddings/main.py` bypasses the protocol entirely and uses
`fastembed.ImageEmbedding` directly in `embed_images_sync`. You have
two options when adding a new modality:

#### Option A — extend the protocol

Widen `EmbedderBackend` to be modality-aware:

```python
@runtime_checkable
class EmbedderBackend(Protocol):
    async def embed(
        self,
        items: list[str],
        modality: Modality = Modality.TEXT,
    ) -> list[list[float]]: ...
    def supports(self, modality: Modality) -> bool: ...
    def dim(self, modality: Modality = Modality.TEXT) -> int: ...
    def model_id(self, modality: Modality = Modality.TEXT) -> str: ...
```

Every existing backend (`FastEmbedBackend`, `OllamaBackend`) has to be
updated to return a sensible `supports()` and dispatch on modality.
Clean but invasive.

#### Option B — parallel code path (recommended first)

Follow the image pattern. Add an `embed_audio_sync` / `embed_audio` in
`embeddings/main.py`, and another branch in `process_job`:

```python
if job.modality.value == "image":
    vectors = await embed_images([c.content for c in job.chunks])
    model_name = "Qdrant/clip-ViT-B-32-vision"
elif job.modality.value == "audio":
    vectors = await embed_audio([c.content for c in job.chunks])
    model_name = "laion/clap-htsat-unfused"
else:
    vectors = await embed_texts([c.content for c in job.chunks])
    model_name = _load_text_backend().model_id()
```

Less elegant, no protocol churn, fastest path to a working end-to-end
demo. **Recommended for the first new modality.** If you end up with
4+ modalities, refactor to Option A — the switch stops scaling.

### 3.4 Qdrant collection

Three changes in `concentrador/index_manager.py`:

1. Add a constant in `common/config.py`:

   ```python
   QDRANT_AUDIO_COLLECTION: str = "kb_audio"
   ```

2. Create the collection in `IndexManager.initialize`:

   ```python
   await self._ensure_qdrant_collection(Config.QDRANT_TEXT_COLLECTION,  768)
   await self._ensure_qdrant_collection(Config.QDRANT_IMAGE_COLLECTION, 512)
   await self._ensure_qdrant_collection(Config.QDRANT_AUDIO_COLLECTION, 512)  # CLAP
   ```

   The dimension is model-specific: nomic-embed-text is 768, CLIP is
   512, CLAP is 512, wav2vec2-base is 768. Make it a config field on
   `core.embeddings.audio.dim` rather than hardcoding if you expect
   users to swap models.

3. Teach `upsert()` to pick by modality:

   ```python
   if emb.modality == "image":
       collection = Config.QDRANT_IMAGE_COLLECTION
   elif emb.modality == "audio":
       collection = Config.QDRANT_AUDIO_COLLECTION
   else:
       collection = Config.QDRANT_TEXT_COLLECTION
   ```

   When modality count reaches 3+, switch to a lookup dict keyed by
   modality string.

### 3.5 Router dispatch

Two places currently branch on modality:

- `concentrador/router.py::build_extract_job` rejects
  `Modality.IMAGE`. Add `Modality.AUDIO` to the pass-through list
  (the audio extractor owns it) or leave it — default behaviour is
  "dispatch to extractor layer if an extractor matches", which is
  what you want once `AudioSegmenter` is registered.

- `concentrador/main.py::create_embedding_jobs` is the legacy path
  (only runs when `AKOPIA_ROUTER_USE_EXTRACTORS` is off). It already
  branches on audio/video and emits `EmbeddingModality.AUDIO_TRANSCRIPT`
  based on `event.derived_content.transcript_segments`. For the **new
  modality** path (not transcript-reuse), add a new branch:

  ```python
  if event.modality.value == "audio":
      chunks = [Chunk(chunk_id=..., content=seg_path, ...)
                for seg_path in seg_paths]
      jobs.append(EmbeddingJob(
          ..., modality=EmbeddingModality.AUDIO, chunks=chunks,
      ))
  ```

  and add `AUDIO` to `EmbeddingModality` if you didn't already.

### 3.6 Search endpoints

`/v1/search/semantic` in `concentrador/main.py` accepts a `modality`
filter and passes it through to
`IndexManager.search_semantic(modality=...)`. The index manager uses
`modality` as a Qdrant payload filter on the **single** `akopia_text`
collection today (or `akopia_image` via the upsert branch). For a new
modality with its own collection, you have two options:

- **Add a new endpoint** — `POST /v1/search/audio` that queries only
  `kb_audio`. Cleanest; keeps the ranking math per-modality sane.
- **Parameterise the existing endpoint** — make `IndexManager`
  resolve collection by the `modality` filter. Requires cross-
  collection ranking if callers pass no `modality` (you'd have to
  query all collections and merge). Probably not worth it.

For the first new modality, ship a new endpoint. Revisit the
unified-search story once you have ≥3 modalities live.

### 3.7 `akopia.yaml` config

Example for the audio modality:

```yaml
sources:
  - id: podcasts
    type: folder
    config:
      path: /data/podcasts
      include: ["*.mp3", "*.wav"]

extractors:
  - type: audio-segments
    config:
      segment_seconds: 30

core:
  embeddings:
    text: { provider: fastembed, model: nomic-embed-text-v1.5, quantized: true }
    image: { enabled: false }
    audio:                                   # new block
      enabled: true
      provider: fastembed
      model: laion/clap-htsat-unfused
      dim: 512
```

The schema in `common/akopia_schema.json` currently requires
`core.embeddings.{text, image}` and no others. Loosen it:

- Change `required: ["text", "image"]` to `required: ["text"]`.
- Add `audio` to the properties block (same structure as `image`).
- Update the Pydantic model in `common/kb_config.py`:
  `EmbeddingsConfig.audio: Optional[AudioEmbeddingConfig] = None`.

### 3.8 Tests

Three levels:

- **Unit: extractor.** Point `AudioSegmenter.extract()` at a tiny
  WAV fixture; assert `ExtractedContent.metadata["segment_paths"]`
  is populated. Pattern from `tests/test_extractors.py`.
- **Integration: router.** Construct a `ChangeEvent(modality=AUDIO)`,
  run it through `Router.build_extract_job` and
  `Router.build_embedding_jobs`. Assert jobs are produced with
  `EmbeddingModality.AUDIO`. Pattern from `tests/test_router.py`.
- **Smoke.** Drop a 3-second clip in the data folder, `docker compose
  up`, wait, `POST /v1/search/audio` with a text query (only works if
  your chosen model is cross-modal; otherwise query audio→audio).
  Assert a non-zero result count.

### 3.9 Docs

- Update `docs/architecture.md`'s modality matrix (search for the
  three-layer diagram). Add `audio` as a first-class modality.
- Update `docs/configuration.md`'s §7 ("Embeddings") with the audio
  block.
- Update `akopia.yaml.example` in `examples/`.

## 4. Files you will touch — PR checklist

| File | Reason |
|---|---|
| `common/models.py` | New `Modality` / `EmbeddingModality` enum value (if not present). |
| `common/config.py` | New `QDRANT_<MODALITY>_COLLECTION` constant. |
| `common/kb_config.py` | New Pydantic model for the per-modality embedder block. |
| `common/akopia_schema.json` | Corresponding JSON Schema additions. |
| `extractors/<new>.py` | Extractor(s) that emit chunks with the new modality. |
| `pyproject.toml` | Entry point for the new extractor under `akopia.content_extractor`. |
| `embeddings/main.py` | New `embed_<modality>` function and branch in `process_job`. Or widen `EmbedderBackend` (Option A). |
| `embeddings/backends/__init__.py` | Optional: new provider for the modality. |
| `concentrador/index_manager.py` | `_ensure_qdrant_collection` call, `upsert()` branch, search method if new endpoint. |
| `concentrador/main.py` | New `/v1/search/<modality>` endpoint (recommended). Update `create_embedding_jobs` legacy path if applicable. |
| `concentrador/router.py` | Ensure `build_extract_job` and `build_embedding_jobs` handle the new modality correctly. |
| `examples/akopia.yaml.example` | Commented-out example block. |
| `docs/architecture.md`, `docs/configuration.md` | Modality matrix + embedder section. |
| `tests/` | Unit + integration + smoke. |
| `docker-compose.yml` | If the new embedder is externalized (e.g. GPU whisper service), add the service. |

## 5. What to test before merge

- `docker compose up --build` ingests a file of the new modality
  end-to-end.
- `GET /v1/status` reports `dead_letter_count == 0` after the ingest
  completes.
- The new Qdrant collection appears in `qdrant:6333/collections`
  with the right dimension.
- Search returns the ingested file at top-1 for an obviously
  matching query.
- `pytest tests/ -x` is green (existing text/image paths didn't
  regress).

## 6. When NOT to add a modality

If your new content is **text-derived** — OCR'd from images, ASR'd
from audio, captioned from video — do NOT add a new modality. Add an
**extractor** that targets `Modality.TEXT`. You reuse the text
embedder, the `akopia_text` collection, the existing search endpoints,
and the existing RAG pipeline. This is what `pdf-text` does for PDFs
today, and what an `ocr` extractor would do for images: the source
modality is whatever it is, but the extractor coerces the payload
into TEXT before it hits the embedder.

Reach for a new modality only when you genuinely need a **separate
vector space** — because the content can't be usefully represented
as text, or because cross-modal retrieval (query one modality, search
another) is the point.

## 7. See also

- `docs/architecture.md` — the three-layer diagram.
- `docs/plugin-contracts.md` — RFC.
- `docs/writing-a-connector.md` — the much simpler adapter story.
- `common/models.py`, `concentrador/router.py`,
  `embeddings/main.py`, `concentrador/index_manager.py` — the four
  files that matter most.
