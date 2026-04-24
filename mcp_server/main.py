"""Akopia MCP Server - exposes knowledge base tools via MCP protocol.

Uses SSE transport for compatibility with Claude and other MCP clients.
Proxies all search/source operations to the Concentrador API.

Inbound auth: a Starlette middleware rejects every request missing a
valid ``Authorization: Bearer <AKOPIA_BEARER_TOKEN>`` header with HTTP
401. Only ``/health`` is exempt so orchestrators (compose, k8s) can
probe liveness without embedding a secret.

Two operating modes:
  * ``AKOPIA_STRICT_AUTH=1`` — fail closed. Importing this module raises
    ``StrictAuthMissingTokenError`` if ``AKOPIA_BEARER_TOKEN`` is unset.
    Intended for production.
  * ``AKOPIA_STRICT_AUTH`` unset (default) — permissive dev mode. When
    the token is unset, the middleware LOGS A PROMINENT WARNING and
    allows requests. Only safe on localhost.

Token comparison uses ``hmac.compare_digest`` (constant-time) to avoid
leaking byte-level timing information about the expected value.

See ``docs/mcp-integration.md`` §4 for deployment guidance.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Optional

import httpx
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("mcp-server")

CONCENTRADOR_URL = os.getenv(
    "CONCENTRADOR_URL",
    "http://concentrador.akopia.svc.cluster.local:8080"
)
BEARER_TOKEN = Config.BEARER_TOKEN

# Paths that skip bearer-token enforcement. Keep this set deliberately
# small — every entry is an unauthenticated surface.
PUBLIC_PATHS: frozenset[str] = frozenset({"/health"})


class StrictAuthMissingTokenError(RuntimeError):
    """Raised at startup when AKOPIA_STRICT_AUTH is truthy but no bearer
    token is configured. Fail-closed: refuse to start rather than
    silently accept anonymous traffic."""


def _strict_auth_enabled() -> bool:
    """Parse ``AKOPIA_STRICT_AUTH`` the same way :class:`common.config.Config` does."""
    return os.getenv("AKOPIA_STRICT_AUTH", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _token_configured() -> bool:
    return bool(os.getenv("AKOPIA_BEARER_TOKEN") or os.getenv("BEARER_TOKEN"))


server = Server("akopia")
sse = SseServerTransport("/messages/")

http = httpx.AsyncClient(
    timeout=30,
    headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
)


async def api_call(method: str, path: str, json_data: dict | None = None) -> dict:
    """Call the Concentrador API."""
    url = f"{CONCENTRADOR_URL}/v1{path}"
    if method == "GET":
        resp = await http.get(url, params=json_data)
    else:
        resp = await http.post(url, json=json_data)
    resp.raise_for_status()
    return resp.json()


# --- MCP Tools ---

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_semantic",
            description="Semantic search across the knowledge base using embeddings. Supports filtering by modality (text, image, audio_transcript, video_transcript), repository, and path prefix.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query in natural language"},
                    "modality": {"type": "string", "enum": ["text", "image", "audio_transcript", "video_transcript"], "description": "Filter by content type"},
                    "repo": {"type": "string", "description": "Filter by repository name"},
                    "path_prefix": {"type": "string", "description": "Filter by file path prefix"},
                    "top_k": {"type": "integer", "default": 10, "description": "Max results to return"},
                    "score_threshold": {"type": "number", "default": 0.0, "description": "Minimum similarity score"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_lexical",
            description="BM25 full-text search with typo tolerance. Best for exact keyword matches, function names, variable names, error messages.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords"},
                    "repo": {"type": "string", "description": "Filter by repository"},
                    "path_prefix": {"type": "string", "description": "Filter by path prefix"},
                    "modality": {"type": "string", "enum": ["text", "audio_transcript", "video_transcript"]},
                    "limit": {"type": "integer", "default": 20},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_images_by_text",
            description="Search images using text descriptions via CLIP. Finds diagrams, screenshots, photos matching a text query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text description of the image to find"},
                    "repo": {"type": "string"},
                    "path_prefix": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                    "score_threshold": {"type": "number", "default": 0.2},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_sources",
            description="List all registered data sources (git repos and NAS folders) with their sync status.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="add_git_source",
            description="Register a new Git repository as a data source for indexing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Source name (e.g., org/repo)"},
                    "git_url": {"type": "string", "description": "Git clone URL"},
                    "branch": {"type": "string", "default": "main"},
                    "sync_cron": {"type": "string", "default": "*/5 * * * *"},
                    "file_patterns": {"type": "array", "items": {"type": "string"}, "default": ["**/*"]},
                },
                "required": ["name", "git_url"],
            },
        ),
        Tool(
            name="trigger_sync",
            description="Force immediate synchronization of a data source. Useful after pushing code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string", "description": "Source ID to sync"},
                },
                "required": ["source_id"],
            },
        ),
        Tool(
            name="get_file",
            description="Get metadata and optionally content of an indexed file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["source_id", "path"],
            },
        ),
        Tool(
            name="get_status",
            description="Get system health status including Qdrant, Meilisearch, Redis, and queue depth.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name == "search_semantic":
            result = await api_call("POST", "/search/semantic", arguments)
        elif name == "search_lexical":
            result = await api_call("POST", "/search/lexical", arguments)
        elif name == "search_images_by_text":
            args = {**arguments, "modality": "image"}
            result = await api_call("POST", "/search/semantic", args)
        elif name == "list_sources":
            result = await api_call("GET", "/sources")
        elif name == "add_git_source":
            result = await api_call("POST", "/sources", {**arguments, "type": "git"})
        elif name == "trigger_sync":
            source_id = arguments["source_id"]
            result = await api_call("POST", f"/sources/{source_id}/sync")
        elif name == "get_file":
            params = {"source_id": arguments.get("source_id"), "path_prefix": arguments.get("path")}
            result = await api_call("GET", "/files", params)
        elif name == "get_status":
            result = await api_call("GET", "/status")
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        import json
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"API error {e.response.status_code}: {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


# --- Starlette app with SSE ---

async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())


async def health(request):
    return JSONResponse({"status": "ok"})


# --- Inbound auth middleware ---------------------------------------------
#
# Mirrors concentrador.main.verify_token — same env var, same Bearer
# scheme — but runs as middleware so it covers every route (including
# the MCP SSE handshake and the POST reverse channel) without touching
# the MCP library internals.
#
# Matrix:
#   AKOPIA_STRICT_AUTH=1 + no token → refuse to start (raised at import).
#   AKOPIA_STRICT_AUTH=1 + token    → normal enforcement.
#   no strict + no token           → permissive mode (warn loudly, allow).
#   no strict + token + no header  → 401.
#   no strict + token + wrong tok  → 401.
#   no strict + token + correct    → pass through.
#   path in PUBLIC_PATHS           → always pass (health probes).

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Enforce ``Authorization: Bearer $AKOPIA_BEARER_TOKEN`` on every request.

    The token is resolved lazily from the environment on each request.
    This lets tests (and token rotation) change the value without
    re-instantiating the middleware, at the cost of one env lookup per
    request — negligible next to the SSE roundtrip.

    Token comparison uses ``hmac.compare_digest`` to prevent an attacker
    from measuring how many bytes of their guess matched the prefix of
    the real token.
    """

    async def dispatch(self, request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # Re-resolve every call so rotating AKOPIA_BEARER_TOKEN (and
        # restarting the pod) doesn't require code changes, AND so the
        # test suite can ``monkeypatch.setenv`` the value mid-run.
        expected = os.getenv("AKOPIA_BEARER_TOKEN") or os.getenv("BEARER_TOKEN") or ""

        if not expected:
            # Permissive mode — loud warning, no enforcement. Intended
            # for `docker compose up` on a laptop. Production deploys
            # MUST set AKOPIA_BEARER_TOKEN (and AKOPIA_STRICT_AUTH=1 to
            # fail closed) — see docs/mcp-integration.md §4.
            logger.warning(
                "AKOPIA_BEARER_TOKEN is unset — MCP server running in PERMISSIVE "
                "mode. Anyone who can reach this port gets full Akopia access. "
                "Set AKOPIA_BEARER_TOKEN + AKOPIA_STRICT_AUTH=1 before exposing "
                "the server beyond localhost."
            )
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"detail": "Missing Bearer token"}, status_code=401)
        token = auth.split(" ", 1)[1].strip()
        # Constant-time comparison. ``hmac.compare_digest`` accepts
        # str or bytes as long as both sides match; encode both to make
        # intent explicit and to survive any non-ASCII surprises.
        if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            return JSONResponse({"detail": "Invalid token"}, status_code=401)

        return await call_next(request)


def _enforce_strict_auth_or_warn() -> None:
    """Run once at import. Two branches:

      * ``AKOPIA_STRICT_AUTH`` truthy + no token → raise and refuse to
        start. Any orchestrator watching the process will log the
        traceback and restart, making the misconfiguration loud.
      * Otherwise + no token → emit a prominent warning. The log trail
        clearly shows the permissive-mode gap even if the middleware
        never runs (e.g. a test harness that imports the app but
        doesn't issue requests).
    """
    if _token_configured():
        return
    if _strict_auth_enabled():
        raise StrictAuthMissingTokenError(
            "AKOPIA_STRICT_AUTH=1 but AKOPIA_BEARER_TOKEN is unset. "
            "Either configure AKOPIA_BEARER_TOKEN, or unset AKOPIA_STRICT_AUTH "
            "for permissive-dev mode. Refusing to start (fail closed)."
        )
    logger.warning(
        "AKOPIA_BEARER_TOKEN is unset at startup — MCP server will run in "
        "PERMISSIVE mode. This is only safe for local development; do NOT "
        "expose the port to a network you don't control. Set AKOPIA_STRICT_AUTH=1 "
        "in production to fail closed instead."
    )


_enforce_strict_auth_or_warn()


starlette_app = Starlette(
    routes=[
        Route("/health", health),
        Route("/sse", handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
    ],
    middleware=[Middleware(BearerAuthMiddleware)],
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(starlette_app, host="0.0.0.0", port=8082)
