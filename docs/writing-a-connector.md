# Akopia — Writing a Source Adapter

**Audience:** developers adding a new source type (SFTP, Notion API,
S3, a database CDC feed, …).

**Terminology.** The codebase calls them **source adapters**. You may
hear "connector" or "fetcher" colloquially — they mean the same
thing. The rest of this document uses **adapter**.

A source adapter answers two questions about some origin of data:

1. **What exists there?** (`discover()` enumerates one or more
   logical `Source` records.)
2. **When does it change?** (`watch()` yields `ChangeEvent`s while
   running.)

Plus a small bonus:

3. **Can you hand me the bytes?** (`read()` for extractors that need
   to re-fetch later.)

Everything else — Redis publishing, idempotency keys, metrics, health
probes, graceful shutdown, supervision with exponential backoff — is
handled by `BaseSourceAdapter`. You inherit it and you're done.

## 1. The contract

Exact shape from `common/protocols.py`:

```python
@runtime_checkable
class SourceAdapter(Protocol):
    plugin_id: str

    @abstractmethod
    async def configure(self, config: dict) -> None: ...

    @abstractmethod
    async def discover(self) -> AsyncIterator[Source]: ...

    @abstractmethod
    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]: ...

    @abstractmethod
    async def read(self, source: Source, path: str) -> bytes: ...

    async def health(self) -> HealthReport: ...   # default: HEALTHY
    async def close(self) -> None: ...            # default: no-op
```

Responsibilities of each method:

- **`plugin_id`** — class attribute. Matches the `type:` value in
  `akopia.yaml` and the entry-point name in `pyproject.toml`. The
  `BaseSourceAdapter.__init__` raises `TypeError` if you forget it.
- **`configure(config)`** — called once at startup with the dict from
  the `config:` block of your `akopia.yaml` entry. Validate here; raise
  `ValueError` with a clear message on missing/invalid keys. Do not
  open network connections here — save them for `start()` or lazy.
- **`discover()`** — async-iterate every logical `Source` your
  instance manages. A git adapter yields one `Source` per repo, a
  folder adapter yields one for the root, a web-deep adapter yields
  one for the site. Called once at startup.
- **`watch(source)`** — long-running. Yield a `ChangeEvent` for every
  detected change. Exit cleanly when `self._shutdown.is_set()`. The
  base wraps this in `_watch_loop` which retries with exponential
  backoff (1s → 60s) if you crash.
- **`read(source, path)`** — synchronous-feeling byte fetch for a
  single logical path. Extractors call this when a
  `ContentRef(kind="path" | "url")` needs resolving. Must be
  idempotent and return bytes whose SHA-256 still matches the
  `content_hash` you stamped in the event.

`ContentExtractor` has its own protocol (same file) — a different
manual.

## 2. Why subclass `BaseSourceAdapter`

Reading `common/base_adapter.py` tells the whole story. The base
handles:

- **Redis client lifecycle.** Lazy-connects on `start()`; injectable
  in tests via the `redis_client=` constructor kwarg.
- **Stream topology.** `ensure_stream_and_group(change-events,
  cg-router)` on first call.
- **Signal handlers.** `SIGTERM` / `SIGINT` → `self._shutdown.set()`.
- **Supervision.** One task per source from `discover()`, each
  wrapped in a try/except that bounces with backoff on crashes.
- **Publishing.** `_publish_event()` overwrites `source_id` /
  `source_type` on every event with the instance identity (you can
  set them if you want, they'll be stamped over), then calls
  `event.compute_idempotency_key()` before pushing to the
  `change-events` stream.
- **Metrics.** `events_published`, `errors`, `discover_calls`.
- **Convenience factories.** `self._make_change_event(...)` fills in
  source identity + idempotency for you. `self._sha256(data)` for
  content hashing.

You get all of that by writing `class FooAdapter(BaseSourceAdapter):`.

## 3. Worked example: a minimal SFTP adapter

We'll build an adapter that watches one SFTP directory and emits
`ChangeEvent`s on add / modify / delete. The pattern mirrors
`adapters/folder.py` exactly — if you haven't read that file yet,
read it now. It's the reference "minimal" implementation.

### 3.1 Directory layout

```
adapters/
  sftp.py            # the adapter class
tests/
  test_sftp_adapter.py
```

In-tree plugins live under `adapters/` next to the existing ones. For
a third-party plugin (see §7), use whatever package name you want.

### 3.2 The class skeleton

```python
# adapters/sftp.py
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import AsyncIterator, Optional

import asyncssh   # third-party; add to [project.optional-dependencies]

from common.base_adapter import BaseSourceAdapter
from common.models import (
    ChangeEvent,
    ContentRef,
    Modality,
    Operation,
    Source,
)

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SECONDS = 300


def _modality_for(path: str) -> Modality:
    ext = PurePosixPath(path).suffix.lower()
    # Same shape as adapters/folder.py::_MODALITY_BY_EXT — trimmed for brevity.
    return Modality.TEXT


class SftpAdapter(BaseSourceAdapter):
    plugin_id = "sftp"

    async def configure(self, config: dict) -> None:
        for key in ("host", "username", "path"):
            if key not in config:
                raise ValueError(f"sftp: missing required config key {key!r}")
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 22))
        self.username: str = config["username"]
        self.password: Optional[str] = config.get("password") or None
        self.private_key: Optional[str] = config.get("private_key") or None
        self.root: str = config["path"]
        self.poll_seconds: int = int(config.get("poll_seconds", _DEFAULT_POLL_SECONDS))

        # Per-source state: {relpath: (mtime_epoch, size)}
        self._state: dict[str, tuple[float, int]] = {}
```

### 3.3 `discover()`

Mirrors `adapters/folder.py`: one SFTP root, one `Source`.

```python
    async def discover(self) -> AsyncIterator[Source]:
        yield Source(
            source_id=self.instance_id,
            type=self.plugin_id,    # free-form string; matches akopia.yaml type
            name=f"sftp://{self.host}{self.root}",
        )
```

`Source.type` is a plain `str` (not an enum) — use your `plugin_id`.
The base class also stamps `source_type = self.plugin_id` on emitted
events, so the two stay aligned.

### 3.4 `watch()` — the change-detection pattern

Copy the pattern from `adapters/folder.py::watch`. It's the
"reference minimal" approach — polling, state map, compare, emit.

```python
    async def watch(self, source: Source) -> AsyncIterator[ChangeEvent]:
        first_pass = True
        while not self._shutdown.is_set():
            if not first_pass:
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(), timeout=self.poll_seconds
                    )
                    break  # shutdown signalled during wait
                except asyncio.TimeoutError:
                    pass
            first_pass = False

            current = await self._scan()

            # Adds + modifies
            for relpath, (mtime, size) in current.items():
                prev = self._state.get(relpath)
                if prev is None:
                    ev = await self._event_for_upsert(relpath, size, Operation.ADD)
                    if ev is not None:
                        yield ev
                elif prev != (mtime, size):
                    ev = await self._event_for_upsert(relpath, size, Operation.MODIFY)
                    if ev is not None:
                        yield ev

            # Deletes
            gone = set(self._state.keys()) - set(current.keys())
            for relpath in gone:
                yield self._make_change_event(
                    path=relpath,
                    operation=Operation.DELETE,
                    modality=_modality_for(relpath),
                    size_bytes=0,
                )

            self._state = current
```

### 3.5 Deciding ADD / MODIFY / DELETE

Three common strategies, pick one (or combine):

- **mtime + size tuple** (what `folder` and our SFTP example use).
  Cheap, accurate enough for filesystem-like sources.
- **ETag / Last-Modified** (what `web-single` uses). Lets you ask
  the server whether anything changed without downloading the body.
- **Content hash compare** (what the `git` adapter falls back on for
  file-level accuracy). Expensive but definitive.

Whatever you use, always emit **one event per detected change**, and
set `operation` accordingly. Rename is a first-class operation too —
set `old_path=` if the source tells you.

### 3.6 `content_modified_at` — do not skip this

Every `ChangeEvent` you emit should carry `content_modified_at` as a
**tz-aware UTC `datetime`**. Downstream search uses it for the
`max_age_days` hard filter and the `freshness_boost` soft re-rank
(see `docs/configuration.md#8-freshness`). Missing values are
tolerated (a neutral score is applied) but cost you recency quality.

Reference implementations:

- `adapters/folder.py::_file_mtime_utc` — `datetime.fromtimestamp(st.st_mtime_ns / 1e9, tz=timezone.utc)`.
- `adapters/git.py::_last_commit_time_for_path` — parses `git log
  -1 --format=%cI -- <path>` as ISO 8601.
- `adapters/web_single.py::_parse_last_modified` — RFC-2822 via
  `email.utils.parsedate_to_datetime`, coerced to UTC.

Your SFTP version (sketch):

```python
    async def _event_for_upsert(
        self, relpath: str, size: int, op: Operation
    ) -> ChangeEvent | None:
        body = await self._fetch(relpath)          # see §3.8
        content_hash = hashlib.sha256(body).hexdigest()
        event = self._make_change_event(
            path=relpath,
            operation=op,
            modality=_modality_for(relpath),
            content_hash=content_hash,
            size_bytes=size,
            content_ref=ContentRef(
                kind="inline_bytes",
                bytes_b64=base64.b64encode(body).decode("ascii"),
            ) if size < 1_048_576 else None,   # inline if <1 MiB
        )
        event.content_modified_at = datetime.fromtimestamp(
            self._state_mtime_for(relpath), tz=timezone.utc,
        )
        return event
```

### 3.7 Rate limiting + backoff

For any external API (SFTP, REST, etc.):

- Use an `asyncio.Semaphore` or a token-bucket if the service has a
  per-second quota.
- Catch transient errors in your `watch()` body and `continue` — the
  base's `_watch_loop` will *also* retry if you let an exception
  propagate, but catching inline preserves per-source progress.
- For permanent errors (auth failure, 4xx), log ERROR and let the
  next poll try again — the supervisor will not tear you down.
- See `adapters/web_deep.py` for a rate-limited async client
  (`_parse_rate_limit`, `_min_request_spacing`).

### 3.8 `read()`

Called by extractors when `ContentRef.kind == "path"` or `"url"` and
they need the payload. For SFTP:

```python
    async def read(self, source: Source, path: str) -> bytes:
        async with self._ssh() as conn:
            async with conn.start_sftp_client() as sftp:
                full = self.root.rstrip("/") + "/" + path.lstrip("/")
                async with sftp.open(full, "rb") as fh:
                    return await fh.read()
```

Guard against `..` escape exactly like `adapters/folder.py::read`.

## 4. Registering the plugin

Two places:

### 4.1 `pyproject.toml` entry point

```toml
[project.entry-points."akopia.source_adapter"]
sftp = "adapters.sftp:SftpAdapter"
```

The group name `akopia.source_adapter` is defined in
`common/registry.py::SOURCE_ADAPTER_GROUP`. The entry-point **name**
(`sftp`) must match your class's `plugin_id` class attribute — the
registry logs a warning if they diverge.

Rebuild the editable install (`pip install -e .`) or the docker
image so Python's `importlib.metadata` sees the new entry point.

### 4.2 `akopia.yaml`

```yaml
sources:
  - id: ops-drops
    type: sftp
    config:
      host: sftp.example.com
      port: 22
      username: "${SFTP_USER}"
      password: "${SFTP_PASSWORD}"
      path: /uploads
      poll_seconds: 120
```

`type: sftp` triggers the registry to resolve your class via the
entry point. The `config:` dict is handed verbatim to
`configure()` — shape is entirely up to your plugin.

## 5. Tests

Follow `tests/test_folder_adapter.py` or `tests/test_git_adapter.py`.
Both use an in-file `_FakeRedis` that collects `(stream, payload)`
tuples instead of talking to Redis, plus a one-shot `watch()` helper
that runs one pass and then sets `_shutdown` to unwind the loop:

```python
class _FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.groups: list[tuple[str, str]] = []
        self.closed = False
    async def connect(self) -> None: ...
    async def ensure_stream_and_group(self, stream, group):
        self.groups.append((stream, group))
    async def publish(self, stream, payload):
        self.published.append((stream, payload))
    async def close(self): self.closed = True
```

Typical assertion shapes (copied from `test_folder_adapter.py`):

```python
events = await _collect_events(adapter)
assert [e.operation for e in events] == [Operation.ADD, Operation.ADD]
assert {e.path for e in events} == {"hello.md", "notes.txt"}
assert all(e.content_modified_at is not None for e in events)
```

No `respx`/httpx mocking is needed for filesystem-like adapters; for
HTTP-backed ones see `tests/test_web_adapters.py` (it uses `respx`,
declared in `dev` optional-deps).

## 6. Running end-to-end in docker-compose

Copy one of the `plugin-*` services in `docker-compose.yml` and point
its `command:` at your new type:

```yaml
  plugin-adapter-sftp:
    build:
      context: .
      dockerfile: Dockerfile.plugin
    command: ["kb", "run", "adapter", "ops-drops"]    # id from akopia.yaml
    environment:
      <<: *kb-env
      SFTP_USER: ${SFTP_USER}
      SFTP_PASSWORD: ${SFTP_PASSWORD}
    volumes:
      - ./akopia.yaml:/app/akopia.yaml:ro
    depends_on:
      - redis
```

`kb run adapter <id>` is wired in `scripts/kb_cli.py`; the runner
instantiates your class via the registry and calls `.start(config)`.

## 7. Packaging as a third-party plugin (out-of-tree)

One of the stated goals of the platform (see `common/registry.py`):
you can publish a plugin as a standalone Python package without
touching akopia's code. Minimal pyproject.toml for a third-party
adapter package:

```toml
[project]
name = "akopia-adapter-sftp"
version = "0.1.0"
dependencies = [
  "akopia",
  "asyncssh>=2.14",
]

[project.entry-points."akopia.source_adapter"]
sftp = "akopia_sftp:SftpAdapter"
```

Install it into the same Python environment as akopia
(`pip install akopia-adapter-sftp` inside the
`Dockerfile.plugin` image, or extend the image with your own
`FROM akopia/plugin:latest` layer). The registry picks it up
next startup — no core changes.

For a reusable container pattern:

```dockerfile
FROM akopia/plugin:latest
COPY . /opt/sftp-plugin
RUN pip install --no-cache-dir /opt/sftp-plugin
```

Then reference it from docker-compose using that image name. Same
approach works for extractors (`akopia.content_extractor` group).

## 8. Debugging

- **Container logs** — `docker compose logs plugin-adapter-sftp --tail
  200 -f`. `BaseSourceAdapter` logs the plugin + instance id on every
  warning. Look for `discovered zero sources`, `watch loop crashed`,
  `configure` errors.
- **Queue depth** — `curl -s localhost:8080/v1/status | jq` returns
  `queue_depth` (pending embeddings) and `dead_letter_count`. If
  `queue_depth` grows and never drains, the embedder is stuck or
  down.
- **Dead-letter stream** — the concentrador publishes failed
  extractor/embedder work to the `dead-letter` Redis stream. The
  `dlq_drainer` background task retries with exponential backoff
  (1 / 5 / 15 min) up to 3 attempts. Inspect with
  `redis-cli XRANGE dead-letter - +` inside the redis container, or
  watch the count via `/v1/status`.
- **Isolated unit run** — for fast iteration, run your adapter class
  against a temp directory / local mock in a test, don't spin up the
  full compose stack until the unit is green.

## 9. See also

- `docs/configuration.md` — operator view of `akopia.yaml`.
- `docs/plugin-contracts.md` — RFC behind the Protocol definitions.
- `docs/adding-a-modality.md` — adding a whole new content type (not
  just a new source of existing modalities).
- `common/protocols.py` — authoritative contracts.
- `common/base_adapter.py` — what you inherit.
- `adapters/folder.py` — the shortest full implementation.
