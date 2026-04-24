# Akopia — RAG Integration

**Audience:** a developer building a RAG-powered app who wants to use
akopia as the retrieval layer.

**One-sentence framing.** akopia is a self-hosted retrieval layer
for your RAG pipeline — it ingests, chunks, embeds, indexes, and serves
search. **You bring the LLM.** The project does not ship an LLM, does
not call one, does not depend on one; every search endpoint returns
structured hits that your app feeds into whichever model you prefer
(Claude, GPT, Gemini, Ollama-hosted Llama, whatever).

If you were hoping for a "ChatGPT over your docs" out of the box, this
is not that product. The piece you are missing is ~60 lines of glue
code. The rest of this manual walks through those 60 lines.

## 1. Mental model

Two pipelines run concurrently. Keep them separate in your head.

```
Ingestion pipeline (one-time + continuous, background)
─────────────────────────────────────────────────────────
[ source adapter ]─▶ change-events ─▶ [ extractor ]─▶ extract-results
                                                           │
                                              chunker + router
                                                           ▼
                                               embedding-jobs
                                                           │
                                                   [ embedder ]
                                                           ▼
                                               embedding-results
                                                           │
                                                   ┌───────┴────────┐
                                                   ▼                ▼
                                                Qdrant        Meilisearch
                                                (vectors)     (lexical + snippets)

Retrieval pipeline (per-query, synchronous)
─────────────────────────────────────────────────────────
[ your app ]
     │ 1. user asks "how do we handle refunds?"
     ▼
[ concentrador /v1/search/... ]
     │ 2. query → embed(query) if semantic
     │ 3. Qdrant / Meili search
     ▼
[ hits with {path, snippet, score, content_modified_at, ...} ]
     │ 4. assemble prompt with snippets + cite paths
     ▼
[ YOUR LLM (Claude/GPT/Ollama/...) ]
     │ 5. LLM writes grounded answer with citations
     ▼
[ user ]
```

You do not have to orchestrate the ingestion side per-query. It runs
in the background and keeps Qdrant/Meili up to date. Your RAG loop
lives entirely in the retrieval pipeline.

## 2. The three retrieval modes

| Endpoint              | Backend     | Returns                          | Use when                                                            |
|-----------------------|-------------|----------------------------------|----------------------------------------------------------------------|
| `/v1/search/lexical`  | Meilisearch | BM25 hits, typo-tolerant         | Exact terms, identifiers, function names, error codes, product SKUs |
| `/v1/search/semantic` | Qdrant      | Cosine similarity over vectors   | Meaning, paraphrase, "this thing but phrased differently"           |
| `/v1/rag/ask`         | Both (hybrid) + re-rank | Pre-assembled context + sources  | General QA where you want the platform to do the mixing for you     |

`/v1/rag/ask` is a **context assembly** endpoint. It does hybrid
retrieval (semantic + lexical in parallel), merges, re-ranks by a
weighted score, then trims to a `max_context_chars` budget and returns
a markdown-shaped blob + the list of source paths. **It does not call
an LLM.** You still do the last mile.

### Decision table

```
Your query looks like...                        →  Reach for
──────────────────────────────────────────────────────────────
"FooBarClient.authenticate()"                   →  lexical
"PROD-4812 crashed with EAGAIN"                 →  lexical
"what does our refund policy say about        →  semantic
 transactions cancelled after 24h?"
"how do we handle late-arriving SLA tickets"    →  semantic
"give me everything on refunds, answer it too"  →  /v1/rag/ask
"I don't know, surprise me"                     →  /v1/rag/ask
```

Rule of thumb: if the user's query contains the literal string that
should appear in the answer (an ID, a class name, an error), use
lexical. Otherwise use semantic. Use `/v1/rag/ask` when you want the
platform to handle the merge for you.

## 3. A minimal RAG loop in ~60 lines

No framework. Just `httpx` and your LLM SDK. Adjust imports for your
choice of LLM.

```python
# rag.py
import os
import httpx

KB_URL   = os.getenv("KB_URL", "http://localhost:8080")
KB_TOKEN = os.environ["AKOPIA_BEARER_TOKEN"]

HEADERS = {"Authorization": f"Bearer {KB_TOKEN}"}


async def retrieve(question: str, top_k: int = 8,
                   max_age_days: int | None = None) -> list[dict]:
    """Hybrid retrieval. Returns a list of {path, snippet, score, ...} dicts."""
    async with httpx.AsyncClient(timeout=30) as c:
        sem = c.post(f"{KB_URL}/v1/search/semantic",
                     headers=HEADERS,
                     json={"query": question, "top_k": top_k,
                           "max_age_days": max_age_days,
                           "freshness_boost": 0.15})
        lex = c.post(f"{KB_URL}/v1/search/lexical",
                     headers=HEADERS,
                     json={"query": question, "limit": top_k,
                           "max_age_days": max_age_days})
        s_resp, l_resp = await sem, await lex

    sem_hits = s_resp.json()["results"]
    lex_hits = l_resp.json()["results"]

    # Dedupe by path, keep the best-scoring copy, prefer semantic scores.
    by_path: dict[str, dict] = {}
    for h in sem_hits:
        by_path[h["path"]] = {**h, "score": h.get("score", 0) * 0.7}
    for h in lex_hits:
        p = h["path"]
        if p in by_path:
            by_path[p]["score"] += 0.3      # lexical presence boost
        else:
            by_path[p] = {**h, "score": 0.3}

    return sorted(by_path.values(), key=lambda x: x["score"], reverse=True)[:top_k]


def build_prompt(question: str, hits: list[dict]) -> str:
    context_blocks = []
    for i, h in enumerate(hits, 1):
        path = h.get("path", "unknown")
        snippet = h.get("snippet", "").strip()
        if not snippet:
            continue
        context_blocks.append(f"[{i}] {path}\n{snippet}")
    context = "\n\n---\n\n".join(context_blocks)

    return f"""You are a careful assistant. Answer the user question using
ONLY the context below. Cite sources as [1], [2], ... matching the
numbered blocks. If the context does not contain the answer, say so.

Context:
{context}

Question: {question}

Answer:"""


async def ask(question: str) -> str:
    hits = await retrieve(question, top_k=8)
    prompt = build_prompt(question, hits)
    # Call whatever LLM you like. Two common shapes:
    return await call_claude(prompt)     # or call_openai(prompt), call_ollama(prompt)
```

LLM call variants (pick one):

```python
# Claude (anthropic SDK)
from anthropic import AsyncAnthropic
_claude = AsyncAnthropic()
async def call_claude(prompt: str) -> str:
    msg = await _claude.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text

# OpenAI
from openai import AsyncOpenAI
_openai = AsyncOpenAI()
async def call_openai(prompt: str) -> str:
    resp = await _openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content

# Ollama (self-hosted, HTTP)
async def call_ollama(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post("http://localhost:11434/api/generate",
                         json={"model": "llama3.1:8b", "prompt": prompt, "stream": False})
        return r.json()["response"]
```

That is the whole loop. Everything that follows is refinement.

## 4. Using `/v1/rag/ask` instead

If you do not want to write the merge/rerank yourself, the
concentrador has it built in:

```python
async with httpx.AsyncClient(timeout=30) as c:
    r = await c.post(f"{KB_URL}/v1/rag/ask",
                     headers=HEADERS,
                     json={"question": "how do refunds work?",
                           "max_context_chars": 6000,
                           "top_k": 15,
                           "semantic_weight": 0.7,
                           "max_age_days": 365})
payload = r.json()
context = payload["context"]      # markdown-shaped, per-file sections with scores
sources = payload["sources"]      # [{path, source_id, score, chars}, ...]
stats   = payload["stats"]        # {semantic_hits, lexical_hits, context_chars, ...}
```

Then hand `context` to your LLM directly:

```python
prompt = f"""Use the context below to answer. Cite file paths you use.

{context}

Question: how do refunds work?
Answer:"""
```

The endpoint groups results by file path, keeps the best-scoring chunk
per file, and truncates at `max_context_chars`. It is deliberately
opinionated. If you want different merge logic, use the raw search
endpoints (§3).

### Freshness in one call

Both the raw endpoints and `/v1/rag/ask` accept `max_age_days` and
`freshness_boost`. Same semantics everywhere:

- `max_age_days` — hard filter. Drop docs older than N days.
- `freshness_boost` — β ∈ [0,1]. `final = (1-β)·similarity + β·exp(-age_days/180)`.

Rule of thumb: for customer-facing "current policy" questions, set
`max_age_days=365` + `freshness_boost=0.2`. For historical research,
leave both off.

## 5. Context window management

Three failure modes to avoid.

**(a) "Shove it all in."** Pulling `top_k=50` and concatenating is
wasteful: you pay in tokens and you dilute the signal. Start with
`top_k=8`, push to `top_k=15` only if answers feel incomplete. The
`/v1/rag/ask` endpoint's default (`top_k=15`, `max_context_chars=6000`)
is a reasonable ceiling for a single-turn answer. If your LLM has
a 200k window that is not a reason to fill it.

**(b) Near-duplicates.** `folder` and `git` sources can index the same
document under different paths (a file and its symlink, a doc and an
old branch). Dedupe by `(path, snippet[:200])` if you see the same
content appear twice:

```python
seen = set()
deduped = []
for h in hits:
    key = (h["path"], (h.get("snippet") or "")[:200])
    if key in seen: continue
    seen.add(key)
    deduped.append(h)
```

**(c) No citation.** Always include `path` in the prompt. Ask the LLM
to emit bracket-style citations. This turns "the LLM hallucinated" into
"the LLM cited path X — go look." It is cheap and worth it every time.

## 6. Embedding model alignment

The single biggest footgun in RAG.

**Rule:** the model that embeds your *query* must be the same model
that embedded your *corpus*. If the concentrador embedded everything
with `nomic-embed-text-v1.5` (the default) and you embed your query
with `all-MiniLM-L6-v2`, the vectors live in different spaces and
cosine similarity is meaningless. Results will look random.

If you use `/v1/search/semantic` you are safe — the concentrador
embeds the query with the same model that embedded the corpus, by
construction. It reads `core.embeddings.text.model` from `akopia.yaml`
and calls the embeddings service at `/embed`. You do not have to
think about this.

You only have a problem if you embed queries client-side (e.g. doing
your own Qdrant queries against `akopia_text` directly). Don't. Go through
the concentrador.

## 7. Language considerations

akopia's default text model is `nomic-embed-text-v1.5` — English
first, workable for Romance languages and most Latin-script content,
weak on Asian languages.

Hints, by corpus language:

- **English only** — keep the default, no action needed.
- **Mixed (English + Spanish, English + Portuguese, etc.)** —
  `nomic-embed-text-v1.5` handles these acceptably. Try it first,
  measure recall on your test queries before reaching for a heavier
  model.
- **Spanish only** — same story. The model is trained on enough
  multilingual data to be competent.
- **Chinese / Japanese / Arabic / anything non-Latin script** — switch
  to a multilingual model. `BAAI/bge-m3` via Ollama is the common
  choice. Edit `core.embeddings.text` in `akopia.yaml`:

```yaml
core:
  embeddings:
    text:
      provider: ollama
      model: bge-m3
      url: "${OLLAMA_URL}"
```

Then **purge the existing index** (`DELETE /v1/sources/{id}/index`)
and re-embed; a new model means a new vector space. See
`docs/operations.md` §Upgrade-path for the exact sequence.

## 8. Anti-patterns to avoid

- **Don't embed the query with one model and the corpus with another.**
  See §6. If in doubt, go through `/v1/search/semantic`.
- **Don't strip source paths from the prompt.** Citations are your
  main defence against hallucination. Always include `path` per chunk.
- **Don't concatenate snippets without delimiters.** LLMs can't tell
  where one chunk ends and the next begins. Use `---` or numbered
  headings (`[1]`, `[2]`) — the `/v1/rag/ask` response uses `### path`
  markdown headers for exactly this reason.
- **Don't set `freshness_boost` above 0.5** unless you have a reason.
  Above 0.5 the freshness signal dominates similarity and you get
  "recent but irrelevant" results.
- **Don't hammer the endpoints per-token.** One retrieval per user
  question, not per generated token. If you are streaming, retrieve
  once, feed the context, then stream.
- **Don't assume `/v1/rag/ask` cites sources automatically.** It
  returns the source list in a separate field. If your LLM is told
  to cite, tell it: prompt engineering still applies.
- **Don't lean on semantic alone for code search.** Function names and
  import paths are exact tokens; Meili finds them instantly, Qdrant
  struggles because embedding models squash identifiers. Hybrid is
  the right default for a code corpus.

## 9. Streaming answers

`/v1/rag/ask` does not stream today. It returns JSON when context
assembly is complete (which takes ~50-300 ms for a typical corpus).
If you want streamed tokens to the user, the shape is:

```
1. await /v1/rag/ask → context + sources       (50-300 ms)
2. stream from your LLM with context in prompt  (LLM-dependent)
3. render tokens to the user as they arrive
```

Streaming from the LLM is your responsibility — every SDK supports it
(Anthropic's `messages.stream`, OpenAI's `stream=True`, Ollama's
`stream=True`). akopia's role ends at step 1.

A server-side streaming RAG endpoint (`text/event-stream` with
incremental context + LLM tokens) is on the roadmap but not shipped.
If you need it today, write a thin wrapper in your own app: call
`/v1/rag/ask`, then open the LLM stream, and forward SSE events.

## 10. Handling "no results"

The endpoints always return HTTP 200 with an empty `results` list when
nothing matches. Your RAG loop should treat empty retrieval as "I
don't know":

```python
hits = await retrieve(question)
if not hits:
    return ("I couldn't find anything about that in the knowledge "
            "base. Try rephrasing, or check if the relevant source "
            "is actually indexed.")
```

Do **not** send the LLM an empty context and hope. It will happily
hallucinate an answer. The one-line guard above is the fix.

If the user is consistently getting empty results for things that
obviously exist, see `docs/troubleshooting.md` — usually it's a source
not registered, an auth misconfiguration, or the embedder down.

## 11. Putting it together: a Slack-bot shaped example

```python
# slackbot_rag.py
async def on_message(user_question: str, user_id: str) -> str:
    hits = await retrieve(user_question, top_k=10, max_age_days=365)
    if not hits:
        return "I don't see anything about that in the KB."

    prompt = build_prompt(user_question, hits[:6])
    answer = await call_claude(prompt)

    # Append a compact source footer — a Slack-flavoured "Sources:" block.
    sources = "\n".join(f"• `{h['path']}`" for h in hits[:4])
    return f"{answer}\n\n*Sources:*\n{sources}"
```

The pattern generalises: retrieve → prompt → LLM → return with
citations. Everything else (rate limits, auth, formatting) is glue
code specific to your surface.

## 12. See also

- `docs/api-reference.md` — full HTTP reference for the endpoints
  used above.
- `docs/mcp-integration.md` — the same search tools surfaced over
  MCP for AI IDE / chat clients.
- `docs/configuration.md` — tuning the corpus side (sources,
  extractors, embedders).
- `docs/operations.md` §Upgrade-path — how to switch embedding models
  without breaking retrieval.
- `docs/troubleshooting.md` — when search returns 0 hits for the
  obvious match.
