# DeepSeek Search API

Cited web-answer API over DeepSeek's server-side web search. One HTTP call turns a question into an answer with sources — the retrieval stack (search, page reading, extraction) runs entirely on DeepSeek's side via the `deepseek_web_search` server tool. **No SearXNG, no SERP keys, no scraping code.**

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/deepseek-search)

## How it works

The service makes a single-turn Chat Completions call to DeepSeek with `tools: [{"type": "deepseek_web_search"}]`:

- `depth: "quick"` (default) — one search pass with citations.
- `depth: "deep"` — thinking-enabled deep search: the model runs up to ~20 server-side tool calls (search → open page → find in page) for a more researched answer.
- Citations come back in `message.annotations` and are normalized to `{title, url, cited_text?}` objects.
- Encrypted server-tool blocks (`server_tool_calls` + `signature` payloads) are **stripped** from every client response — the API is stateless and the client never replays them.
- `usage` (tokens + search/fetch counts) is surfaced on every answer so you can watch spend.

## The one variable

| Variable | Required | Meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` | yes | Your DeepSeek API key from [platform.deepseek.com](https://platform.deepseek.com). All usage bills to **your** account. |

Deploy form description: *your DeepSeek API key from platform.deepseek.com — usage billed to your account: ~$0.75 per 1,000 searches + token usage.*

The service boots healthy without the key and answers `/health`; any search call returns a clear 401-shaped error envelope until the key is set.

## Endpoints

### `POST /search-answer`

```bash
curl -s https://your-app.up.railway.app/search-answer \
  -H "Content-Type: application/json" \
  -d '{"question": "What did the DeepSeek 2026-11-13 release add to the API?", "depth": "quick"}'
```

Response:

```json
{
  "answer": "...",
  "citations": [
    {"title": "DeepSeek API Docs - News", "url": "https://api-docs.deepseek.com/news/news261113", "cited_text": "..."}
  ],
  "tools_used": 2,
  "usage": {"input_tokens": 412, "output_tokens": 655, "search_calls": 1, "fetch_calls": 1, "image_search_calls": 0}
}
```

Deep mode (more tool calls, higher cost — think of it as research mode):

```bash
curl -s https://your-app.up.railway.app/search-answer \
  -H "Content-Type: application/json" \
  -d '{"question": "Compare the pricing of the newest small-context coding models", "depth": "deep"}'
```

Optional `freshness_hint` (e.g. `"last 30 days"`) is appended to the prompt as recency steering.

### `POST /search-answer/stream`

Server-Sent Events. Order: `start` → `delta` (answer text, one event per chunk) → `citations` → `usage` → `done`. Error and refusal conditions are emitted as `error` / `refusal` events.

```bash
curl -N https://your-app.up.railway.app/search-answer/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the current stable Python version?", "depth": "quick"}'
```

### `GET /health`

`200 {"ok": true}` — dotless route, no external calls. Used as the Railway healthcheck probe.

## Error envelope

All errors use one shape: `{"error": {"message", "type", "code"}}`.

| Situation | Status | Notes |
|---|---|---|
| Key not set on the deployment | 401 | `code: missing_api_key` — set the variable and redeploy |
| DeepSeek rejects the key | 401 | passthrough of the upstream message |
| Provider content-safety block (400 Content Exists Risk) | 422 | `type: content_blocked` |
| Model safety refusal (`finish_reason: content_filter`) | 422 | `{refusal: true, category, explanation, answer, citations}` |
| Rate limited after retries | 429 | `Retry-After` header included |
| Upstream 408/429/5xx after retries, or 200-with-body-error | 502 | upstream details in the message |

Retries: 408/429/5xx and network errors are retried twice with backoff before surfacing.

## Cost (billed to your DeepSeek account)

| Item | Price |
|---|---|
| Web search / web fetch (server tool) | $0.75 per 1,000 uses |
| deepseek-flash input tokens | $0.30 per 1M peak / $0.15 per 1M off-peak (cache hits ~1/50th of miss) |
| deepseek-flash output tokens | $1.20 per 1M peak / $0.60 per 1M off-peak |
| Typical quick answer | ~1-3 searches + a few thousand tokens = roughly $0.002-0.005 |
| Deep answer | up to ~20 searches + longer generation = roughly $0.02-0.05 |

Peak hours: 01:00-04:00 and 06:00-10:00 UTC Mon-Fri; all other times are off-peak (half price on tokens). A quick answer is typically ~1-3 searches, a deep answer up to ~20.

## Why one service instead of a Perplexity-clone stack

Self-hosted Perplexica/Vane-style templates on this marketplace ship a bundled SearXNG service, a persistent ~4 GB index volume, and a separate LLM key. This template is **one container, no volume, one key**: DeepSeek runs the retrieval, bills per use, and the answer arrives with normalized citations. If you want a chat UI with browsing, use those; if you want a clean API your code can call, this is the smaller, simpler surface.

## Operational notes

- Stateless: nothing to persist, no volume, safe to restart/redeploy at any time.
- Concurrency: DeepSeek allows 2,500 concurrent requests per account — no client-side locking needed.
- `SAFETY_CHECK_LEVEL` (optional, `low`/`high`) enables DeepSeek's server-side safety review (`safety_check`). Public-facing apps should enable it — content-safety review and AI-labeling duties under the DeepSeek ToS belong to the deployer.
- `DEEPSEEK_BASE_URL` and `DEEPSEEK_MODEL` (optional overrides, defaults `https://api.deepseek.com` / `deepseek-flash`).
- The DeepSeek web-tools API is new (days old); if DeepSeek changes shapes, re-pin and check the [web tools guide](https://api-docs.deepseek.com/guides/web_tools).

## License

MIT
