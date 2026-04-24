# Akopia — MCP Integration

**Audience:** end-users wiring akopia into their AI tools —
Claude Desktop, Claude Code, Cursor, Continue.dev, or any other MCP
client.

## 1. What MCP is

The **Model Context Protocol** (MCP) is a small JSON-RPC protocol
that lets AI clients call external "tools" (functions) and read
"resources" (files/data) from local or remote servers. Spec at
[modelcontextprotocol.io](https://modelcontextprotocol.io).

akopia ships one such server — the `mcp-server` container in the
compose file. It exposes the same knowledge-base operations the REST
API exposes, but over MCP so AI clients can call them as tools. The
implementation is a thin proxy: every MCP tool call becomes an HTTP
call to the concentrador.

## 2. The tools akopia exposes

Source of truth: `mcp_server/main.py::list_tools`. Current catalogue:

| Tool name               | Purpose                                                                   |
|-------------------------|---------------------------------------------------------------------------|
| `search_semantic`       | Vector search via Qdrant. Best for meaning / paraphrase queries.          |
| `search_lexical`        | BM25 search via Meilisearch. Best for exact keywords / IDs / error codes. |
| `search_images_by_text` | CLIP image search. Finds diagrams/screenshots matching a text prompt.     |
| `list_sources`          | List every registered data source (git repos, folders).                   |
| `add_git_source`        | Register a new Git repo for indexing.                                     |
| `trigger_sync`          | Force immediate resync of a source.                                       |
| `get_file`              | Fetch metadata for one indexed file.                                      |
| `get_status`            | System health: Qdrant / Meili / Redis state + queue depth.                |

### Schemas (abridged — full JSON Schema in `mcp_server/main.py`)

```jsonc
// search_semantic
{
  "query": "string (required) — natural-language search",
  "modality": "text | image | audio_transcript | video_transcript",
  "repo": "string — filter by repo name",
  "path_prefix": "string — filter by path",
  "top_k": "integer (default 10)",
  "score_threshold": "number (default 0.0)"
}
// returns: {"results": [{path, snippet, score, chunk_id, source_id, ...}], ...}

// search_lexical
{
  "query": "string (required)",
  "repo": "string", "path_prefix": "string", "modality": "string",
  "limit": "integer (default 20)", "offset": "integer (default 0)"
}

// search_images_by_text — thin wrapper that forces modality=image and proxies to search_semantic.

// add_git_source
{
  "name": "string (required) — e.g. myorg/myrepo",
  "git_url": "string (required) — clone URL",
  "branch": "string (default main)",
  "sync_cron": "string (default */5 * * * *)",
  "file_patterns": ["string"]  // default ["**/*"]
}

// trigger_sync:    { "source_id": "src-git-abc123" }
// get_file:        { "source_id": "...", "path": "docs/foo.md" }
// list_sources / get_status: {}
```

### Transport

The server speaks **SSE** (Server-Sent Events), not stdio. It exposes:

- `GET /sse` — the SSE endpoint for the MCP session
- `POST /messages/` — the reverse channel for client→server messages
- `GET /health` — unauthenticated liveness

Default port: **8082** (mapped in `docker-compose.yml` via
`${MCP_PORT:-8082}`). Some MCP clients (looking at you, older Claude
Desktop builds) only support stdio. For those you can wrap the server
with a stdio→SSE bridge, or use `mcp-proxy`
(`pip install mcp-proxy`) — but most modern clients speak SSE
directly.

## 3. Connection setup by client

In every snippet below, replace `<YOUR-TOKEN>` with the value of
`AKOPIA_BEARER_TOKEN` from your `.env`, and replace `localhost:8082` with
the host/port where your MCP server actually runs.

### 3.1 Claude Desktop

Config path:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "akopia": {
      "transport": "sse",
      "url": "http://localhost:8082/sse",
      "headers": {
        "Authorization": "Bearer <YOUR-TOKEN>"
      }
    }
  }
}
```

Restart Claude Desktop after saving. The tools appear in the paper-clip
(attachments) menu; you will see `search_semantic`, `search_lexical`,
etc.

### 3.2 Claude Code

Config path: project-local `.claude/mcp.json` or user-wide
`~/.claude/mcp.json`. Same schema:

```json
{
  "mcpServers": {
    "akopia": {
      "transport": "sse",
      "url": "http://localhost:8082/sse",
      "headers": {
        "Authorization": "Bearer <YOUR-TOKEN>"
      }
    }
  }
}
```

Start a new Claude Code session after editing. `/mcp` inside the CLI
lists active servers and their tool count.

### 3.3 Cursor

Config path: `~/.cursor/mcp.json`. Cursor uses the same MCP schema as
Claude Desktop:

```json
{
  "mcpServers": {
    "akopia": {
      "url": "http://localhost:8082/sse",
      "headers": {
        "Authorization": "Bearer <YOUR-TOKEN>"
      }
    }
  }
}
```

Reload the Cursor window (`Cmd/Ctrl+Shift+P` → "Developer: Reload
Window"). Tools become available to the Composer.

### 3.4 Continue.dev

Edit `~/.continue/config.json` (or the workspace `./.continue/config.json`):

```json
{
  "experimental": {
    "modelContextProtocolServers": [
      {
        "transport": {
          "type": "sse",
          "url": "http://localhost:8082/sse",
          "headers": {
            "Authorization": "Bearer <YOUR-TOKEN>"
          }
        }
      }
    ]
  }
}
```

Continue will reload automatically on save.

## 4. Authentication

The MCP server uses **one shared secret end-to-end**: the value of the
`AKOPIA_BEARER_TOKEN` env var. It is enforced on **both** sides:

1. **Inbound.** A Starlette middleware (`BearerAuthMiddleware` in
   `mcp_server/main.py`) rejects every request missing a valid
   `Authorization: Bearer <token>` header with HTTP `401`. This covers
   the MCP JSON-RPC POSTs, the SSE handshake at `/sse`, and the
   reverse-channel POSTs at `/messages/`. Only `/health` is public
   (needed by compose / k8s liveness probes).
2. **Outbound.** The server attaches the same token to every upstream
   call to the concentrador, so the concentrador's `verify_token`
   dependency also passes.

### 4.1 Configuring the token

Set `AKOPIA_BEARER_TOKEN` in the environment of the `mcp-server` container.
The `docker-compose.yml` already reads it from your `.env`:

```bash
# .env
AKOPIA_BEARER_TOKEN=$(openssl rand -hex 32)
```

Restart the container after a change:

```bash
docker compose restart mcp-server concentrador
```

### 4.2 Client-side configuration

Every MCP client must send the token as a bearer header on every call.
Real snippets per client:

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS, `~/.config/Claude/claude_desktop_config.json` on Linux):

```json
{
  "mcpServers": {
    "akopia": {
      "transport": "sse",
      "url": "http://localhost:8082/sse",
      "headers": {
        "Authorization": "Bearer <YOUR-AKOPIA_BEARER_TOKEN>"
      }
    }
  }
}
```

**Claude Code** (`~/.claude/mcp.json` or project-local `.claude/mcp.json`):

```json
{
  "mcpServers": {
    "akopia": {
      "transport": "sse",
      "url": "http://localhost:8082/sse",
      "headers": {
        "Authorization": "Bearer <YOUR-AKOPIA_BEARER_TOKEN>"
      }
    }
  }
}
```

**Cursor** (`~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "akopia": {
      "url": "http://localhost:8082/sse",
      "headers": {
        "Authorization": "Bearer <YOUR-AKOPIA_BEARER_TOKEN>"
      }
    }
  }
}
```

**Continue.dev** (`~/.continue/config.json`):

```json
{
  "experimental": {
    "modelContextProtocolServers": [
      {
        "transport": {
          "type": "sse",
          "url": "http://localhost:8082/sse",
          "headers": {
            "Authorization": "Bearer <YOUR-AKOPIA_BEARER_TOKEN>"
          }
        }
      }
    ]
  }
}
```

If a client cannot set outbound headers (rare but possible with older
builds), front the server with a reverse proxy that injects the header
(§5.2) or use `mcp-proxy` as a shim.

### 4.3 Permissive-dev fallback

If `AKOPIA_BEARER_TOKEN` is **unset** when the server starts, the middleware
enters **permissive mode**: it logs a prominent WARNING on every request
but lets them through. This keeps `docker compose up` on a laptop
friction-free for a new contributor who hasn't set `.env` yet.

**This fallback is for local development only.** Before you expose port
8082 to anything other than `localhost` (Cloudflare Tunnel, LAN access,
a reverse proxy) — always set a non-empty token. The log line to look
for:

```
mcp-server WARNING AKOPIA_BEARER_TOKEN is unset — MCP server running in PERMISSIVE mode
```

If you see that in a production deployment, that's a misconfiguration,
not a feature.

**Fail-closed with `AKOPIA_STRICT_AUTH=1`.** To turn that warning into a
hard failure — so the misconfiguration cannot ship to prod silently —
set `AKOPIA_STRICT_AUTH=1` in the MCP server's environment. With strict
auth enabled **and** no token configured, the server refuses to start:
it raises `StrictAuthMissingTokenError` during import, the container
exits non-zero, and any orchestrator (compose, k8s) shows a crash loop
instead of a permissive listener. The matrix:

| `AKOPIA_STRICT_AUTH` | `AKOPIA_BEARER_TOKEN` | Behaviour                        |
|---------------------|----------------------|----------------------------------|
| unset / `0`         | unset                | permissive mode + loud WARNING   |
| unset / `0`         | set                  | enforce 401 on missing / bad token |
| `1`                 | unset                | **refuse to start** (fail closed) |
| `1`                 | set                  | enforce 401 on missing / bad token |

Production deploys should set **both** `AKOPIA_STRICT_AUTH=1` and a
non-empty `AKOPIA_BEARER_TOKEN`. The same switch applies to the
`concentrador` service for parity.

### 4.4 Token rotation

Rotate by:

1. Generate a new value: `openssl rand -hex 32`.
2. Update `.env`.
3. `docker compose restart concentrador mcp-server`.
4. Update every client config from §4.2 and restart the clients.

The middleware reads the env var per-request, so step 3 picks up the
new value on the next container start with no code changes. A
reverse-proxy gate (§5.2) needs its config updated in the same rotation
window. See `docs/operations.md` §Token-rotation for a fuller runbook.

## 5. Remote vs local deployment

### Local (default, dev laptop)

Run `docker compose up -d`. The MCP server listens on
`localhost:8082` and every client in §3 points there. No TLS, no
hardening; fine for a single developer.

### Remote, behind Cloudflare Tunnel

If your akopia runs on a homelab server and you want Claude
Desktop on your laptop to reach it:

```
Laptop (Claude Desktop)
  └─(HTTPS/SSE)─▶ kb.example.com                 (Cloudflare edge)
                   └─(tunnel)─▶ mcp-server:8082  (your homelab)
```

Cloudflare Zero Trust can sit in front and gate the tunnel hostname
with Access policies (email SSO, service tokens, etc.). Point Claude
Desktop's `url` at `https://kb.example.com/sse` instead of localhost.

### Remote, behind a reverse proxy (Traefik / Caddy)

Either proxy can terminate TLS, check a bearer token, then forward
SSE to the mcp-server. A Caddy example:

```caddy
kb.example.com {
    @authorized header Authorization "Bearer <YOUR-TOKEN>"
    handle @authorized {
        reverse_proxy mcp-server:8082
    }
    respond 401
}
```

Since v1 the MCP server **also** checks the token itself (§4), so the
proxy rule is defense-in-depth — belt-and-suspenders if an attacker
ever bypasses the proxy by reaching the container directly. Keep the
proxy check for TLS termination, rate limiting, and fast-path 401s;
keep the in-process check as the final authority. See
`docs/operations.md` for a fuller Traefik example.

## 6. Example prompts that exercise the tools

Paste these into the client after wiring is complete to verify it
works.

```
# sanity check
What's the health status of my akopia instance?
# → calls get_status

# list sources
What data sources do I have indexed?
# → calls list_sources

# semantic
Find anything in my knowledge base about rate limiting strategies.
# → calls search_semantic (likely with query="rate limiting strategies")

# lexical
Find every mention of the literal identifier "AKOPIA_ROUTER_USE_EXTRACTORS".
# → calls search_lexical (the identifier is an exact token)

# scoped
Search inside repo "yourorg/yourrepo" for how chunking works.
# → search_semantic with repo filter

# indexing control
Add the GitHub repo https://github.com/modelcontextprotocol/servers
as a new source and trigger a sync.
# → add_git_source, then trigger_sync on the returned source_id

# code-flavoured hybrid
Find both the exact function name "build_embedding_jobs" and anything
that discusses the embedding job construction pipeline.
# → search_lexical for the exact name, search_semantic for the concept
```

If the client does not actually invoke tools (some models need a
gentle nudge), prompt more directly: *"use the search_lexical tool to
find …"*.

## 7. Troubleshooting

### "Tool not found" / tools don't appear in the client

1. **Check the server is reachable.** `curl http://localhost:8082/health`
   should return `{"status": "ok"}`. If not, the container isn't up —
   `docker compose ps mcp-server` and `docker compose logs mcp-server
   --tail 50`.
2. **Wrong transport.** Most MCP clients now require an explicit
   `"transport": "sse"` (or use URL heuristics). If the client silently
   falls back to stdio it will try to launch the server as a
   subprocess, find nothing to launch, and report "tool not found."
3. **Client didn't restart.** Claude Desktop, Cursor, and Continue
   reload MCP config only on startup/reload. Restart the app.

### `401 Unauthorized` from the MCP server

The server proxies your request to the concentrador with the
**server's** `AKOPIA_BEARER_TOKEN` (set via env in the container). If the
concentrador rejects it, the token in your `.env` doesn't match the
token the concentrador booted with. Two likely causes:

- You regenerated the token in `.env` but never `docker compose
  restart concentrador mcp-server`.
- Two `.env` files drifted (root vs compose working directory). Check
  `docker inspect mcp-server --format '{{range .Config.Env}}{{println .}}{{end}}' | grep BEARER`.

### "No results" for a query you know should match

Not an MCP problem — it's a data problem. Check via the REST API
first, bypassing MCP, using the same parameters:

```bash
curl -X POST http://localhost:8080/v1/search/lexical \
  -H "Authorization: Bearer $AKOPIA_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"<your-term>","limit":5}' | jq
```

If the REST call also returns nothing, see `docs/troubleshooting.md`
§8. If REST returns hits but MCP does not, file an issue — the proxy
is supposed to be transparent.

### Server starts, accepts the SSE connection, then hangs

Usually means the server can reach the client but can't reach the
concentrador (the tool call itself times out). Confirm with
`docker compose logs mcp-server --tail 30` and look for
`httpx.ConnectError` / `httpx.ReadTimeout`. Fix the network between
the two containers (both must be on the same compose network) or the
concentrador's health (`curl http://localhost:8080/health`).

## 8. See also

- `docs/api-reference.md` — the HTTP endpoints the MCP server proxies.
- `docs/rag-integration.md` — using those same endpoints from your
  own code instead of via an AI client.
- `docs/configuration.md` — the `AKOPIA_BEARER_TOKEN` env var and the
  compose port mapping for `mcp-server`.
- `docs/operations.md` §Reverse-proxy — locking down `:8082` for
  remote clients.
- `mcp_server/main.py` — authoritative list of exposed tools.
