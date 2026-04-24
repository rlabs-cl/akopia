"""MCP server bearer-token middleware enforcement.

Covers the matrix from ``mcp_server.main.BearerAuthMiddleware`` +
``_enforce_strict_auth_or_warn``:
  • /health is public — always 200 regardless of token state.
  • No token configured → permissive mode (200, but a WARNING is logged).
  • Token configured + missing Authorization → 401.
  • Token configured + wrong scheme → 401.
  • Token configured + wrong value → 401.
  • Token configured + correct Bearer token → request passes (200 from
    our synthetic /echo route).
  • ``AKOPIA_STRICT_AUTH=1`` + no token → import raises
    ``StrictAuthMissingTokenError`` (fail closed).
  • ``AKOPIA_STRICT_AUTH=1`` + token set → normal enforcement.
  • Constant-time compare ``hmac.compare_digest`` is used (behavioural
    equivalence check: matching token passes, mismatched token rejects).
"""
from __future__ import annotations

import importlib
import logging

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def _clean_strict_auth(monkeypatch):
    """Default every test into non-strict mode. Tests that need strict
    auth opt-in explicitly via ``monkeypatch.setenv``."""
    monkeypatch.delenv("AKOPIA_STRICT_AUTH", raising=False)
    yield


def _load_middleware():
    # Re-import on every test so env changes take effect for module-level
    # startup logging / strict-auth enforcement. The middleware itself
    # reads env per-request, so the class is stable across tests.
    from mcp_server import main as mcp_main
    importlib.reload(mcp_main)
    return mcp_main


def _build_test_app() -> Starlette:
    """Build a minimal Starlette app with the real middleware around a
    synthetic ``/echo`` route and a public ``/health``.

    We avoid importing the full MCP app because it opens an
    ``httpx.AsyncClient`` and registers SSE handlers that complicate
    synchronous TestClient runs. The middleware is the only surface
    under test.
    """
    from mcp_server.main import BearerAuthMiddleware

    async def echo(request):
        return JSONResponse({"ok": True})

    async def health(request):
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/echo", echo, methods=["GET", "POST"]),
            Route("/", echo, methods=["GET", "POST"]),  # MCP JSON-RPC lands here
        ],
        middleware=[Middleware(BearerAuthMiddleware)],
    )


def test_health_is_public_without_token(monkeypatch):
    monkeypatch.delenv("AKOPIA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    _load_middleware()
    client = TestClient(_build_test_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_permissive_mode_when_token_unset(monkeypatch, caplog):
    monkeypatch.delenv("AKOPIA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    _load_middleware()
    client = TestClient(_build_test_app())
    with caplog.at_level(logging.WARNING, logger="mcp-server"):
        resp = client.get("/echo")
    assert resp.status_code == 200
    # Loud warning is required — the whole point of permissive mode is
    # that it screams in the logs.
    assert any("PERMISSIVE mode" in rec.message for rec in caplog.records)


def test_missing_header_returns_401(monkeypatch):
    monkeypatch.setenv("AKOPIA_BEARER_TOKEN", "s3cret")
    _load_middleware()
    client = TestClient(_build_test_app())
    resp = client.get("/echo")
    assert resp.status_code == 401
    assert "Bearer" in resp.json()["detail"]


def test_wrong_scheme_returns_401(monkeypatch):
    monkeypatch.setenv("AKOPIA_BEARER_TOKEN", "s3cret")
    _load_middleware()
    client = TestClient(_build_test_app())
    resp = client.get("/echo", headers={"Authorization": "Basic s3cret"})
    assert resp.status_code == 401


def test_wrong_token_returns_401(monkeypatch):
    monkeypatch.setenv("AKOPIA_BEARER_TOKEN", "s3cret")
    _load_middleware()
    client = TestClient(_build_test_app())
    resp = client.get("/echo", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid token"


def test_correct_token_passes(monkeypatch):
    monkeypatch.setenv("AKOPIA_BEARER_TOKEN", "s3cret")
    _load_middleware()
    client = TestClient(_build_test_app())
    resp = client.get("/echo", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_mcp_root_requires_auth(monkeypatch):
    """MCP JSON-RPC posts to '/' — that path must also be behind auth."""
    monkeypatch.setenv("AKOPIA_BEARER_TOKEN", "s3cret")
    _load_middleware()
    client = TestClient(_build_test_app())
    resp = client.post("/", json={"jsonrpc": "2.0", "method": "x"})
    assert resp.status_code == 401
    resp = client.post("/", json={}, headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200


def test_health_still_public_when_token_set(monkeypatch):
    monkeypatch.setenv("AKOPIA_BEARER_TOKEN", "s3cret")
    _load_middleware()
    client = TestClient(_build_test_app())
    resp = client.get("/health")
    assert resp.status_code == 200


def test_bearer_token_fallback_to_legacy_env(monkeypatch):
    """Accept BEARER_TOKEN (legacy var) when AKOPIA_BEARER_TOKEN is unset."""
    monkeypatch.delenv("AKOPIA_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("BEARER_TOKEN", "legacy")
    _load_middleware()
    client = TestClient(_build_test_app())
    resp = client.get("/echo", headers={"Authorization": "Bearer legacy"})
    assert resp.status_code == 200
    resp = client.get("/echo", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


# --- AKOPIA_STRICT_AUTH --------------------------------------------------

def test_strict_auth_starts_with_token(monkeypatch):
    """STRICT_AUTH=1 + token set → server starts normally + enforces 401."""
    monkeypatch.setenv("AKOPIA_STRICT_AUTH", "1")
    monkeypatch.setenv("AKOPIA_BEARER_TOKEN", "s3cret")
    mcp_main = _load_middleware()  # must not raise
    assert mcp_main.starlette_app is not None

    client = TestClient(_build_test_app())
    resp = client.get("/echo", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    resp = client.get("/echo")
    assert resp.status_code == 401


def test_strict_auth_refuses_to_start_without_token(monkeypatch):
    """STRICT_AUTH=1 + no token → import raises, container crash-loops."""
    monkeypatch.setenv("AKOPIA_STRICT_AUTH", "1")
    monkeypatch.delenv("AKOPIA_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("BEARER_TOKEN", raising=False)
    with pytest.raises(Exception) as excinfo:
        _load_middleware()
    # Our specific exception type carries the fail-closed intent.
    from mcp_server.main import StrictAuthMissingTokenError  # type: ignore
    assert isinstance(excinfo.value, StrictAuthMissingTokenError) or (
        "AKOPIA_STRICT_AUTH" in str(excinfo.value)
    )


# --- hmac.compare_digest behavioural equivalence ------------------------

def test_compare_digest_used_for_token_check(monkeypatch):
    """Behavioural equivalence: the matching token passes, every
    non-matching token of the same length is rejected. This can't prove
    constant-time on its own, but it does verify we swapped the plain
    ``==`` for a comparison that still returns the right booleans — the
    regression guard an attacker-observable wall-clock test cannot give
    you in a unit-test harness.
    """
    monkeypatch.setenv("AKOPIA_BEARER_TOKEN", "s3cret-32-chars-long-padding-xxxx")
    _load_middleware()
    client = TestClient(_build_test_app())

    # Exact match passes.
    resp = client.get(
        "/echo",
        headers={"Authorization": "Bearer s3cret-32-chars-long-padding-xxxx"},
    )
    assert resp.status_code == 200

    # Same length but wrong content → 401.
    resp = client.get(
        "/echo",
        headers={"Authorization": "Bearer XXXXXX-32-chars-long-padding-xxxx"},
    )
    assert resp.status_code == 401

    # Different length (shorter) → 401; compare_digest returns False
    # without leaking length info via early-return.
    resp = client.get(
        "/echo",
        headers={"Authorization": "Bearer s3cret"},
    )
    assert resp.status_code == 401

    # Also verify the middleware actually imports hmac.compare_digest
    # (defence in depth — catches a future regression that swaps it back
    # to ``==`` without changing externally observable behaviour).
    import mcp_server.main as mcp_main
    import inspect
    src = inspect.getsource(mcp_main.BearerAuthMiddleware)
    assert "hmac.compare_digest" in src
