"""In-tree source adapter plugins.

Each module in this package is a ``BaseSourceAdapter`` subclass ready
for registration via ``PluginRegistry.register_source_adapter()`` (tests,
in-repo runners) or via ``[project.entry-points]`` when published as a
wheel.

Currently shipping:

- ``adapters.git`` — ``GitAdapter`` (replaces the old ``adapter_git``
  pod). Provider-abstracted (GitHub / GitLab / Gitea) via config.
- ``adapters.folder`` — ``FolderAdapter`` (replaces the out-of-tree
  ``adapter-nas``). Polls a filesystem path with include/exclude globs.
- ``adapters.web_single`` — ``WebSingleAdapter``. One URL polled on cron,
  conditional GET with ETag/Last-Modified.
- ``adapters.web_deep`` — ``WebDeepAdapter``. BFS crawl from a root with
  max_depth + robots.txt + rate limit.

Web adapters emit raw HTML bytes; the ``html`` extractor (see
``extractors.html``) converts HTML → clean text downstream. Adapters
deliberately do not parse or clean HTML themselves.

New adapters (``s3``, ``db-live`` …) should land here alongside the
existing ones. See ``docs/plugin-contracts.md`` §Layer 3.
"""
from __future__ import annotations

from adapters.folder import FolderAdapter
from adapters.git import GitAdapter
from adapters.web_deep import WebDeepAdapter
from adapters.web_single import WebSingleAdapter

__all__ = [
    "FolderAdapter",
    "GitAdapter",
    "WebDeepAdapter",
    "WebSingleAdapter",
]
