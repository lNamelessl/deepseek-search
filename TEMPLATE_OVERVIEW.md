[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/deepseek-search)

DeepSeek Search API — a cited web-answer HTTP API over DeepSeek's server-side web search. Send `POST /search-answer` with a question and get back an answer with normalized citations (`title`, `url`, `cited_text`), a tool-call count, and per-answer usage. One container, no volume, one deploy-time variable: your DeepSeek API key. There is no SearXNG service, no SERP API keys, and no scraping code — the retrieval stack runs on DeepSeek's side.

**Endpoints.** `POST /search-answer` with `{"question": "...", "depth": "quick" | "deep"}` — quick is one search pass with citations; deep enables thinking so the model runs up to ~20 server-side tool calls (search, open page, find in page) for a researched answer. `POST /search-answer/stream` produces the same result as Server-Sent Events (`delta` answer chunks, then `citations` and `usage` events). `GET /health` is the healthcheck probe (no external calls).

**Cost — billed to your own DeepSeek account.** Web search and web fetch are $0.75 per 1,000 uses. deepseek-flash tokens are $0.30 per 1M input / $1.20 per 1M output at peak (01:00-04:00 and 06:00-10:00 UTC Mon-Fri), half that off-peak, with cache hits around 1/50th of the miss price. A typical quick answer costs roughly $0.002-0.005 (1-3 searches plus tokens); a deep answer up to roughly $0.02-0.05 (up to ~20 searches plus longer generation). The `usage` object on every response shows tokens and search/fetch counts so spend is visible per call.

**The one variable.** The deploy form asks for `DEEPSEEK_API_KEY` — your key from platform.deepseek.com. All usage bills to your account. The service boots healthy without it (healthcheck passes) and any search call returns a clear 401-shaped error envelope until it is set. Nothing else to configure: the port is handled automatically and there are no volumes or databases.

**Why this instead of a Perplexity-clone stack.** Self-hosted Perplexica/Vane-style templates ship a bundled SearXNG service, a persistent multi-GB index volume, and a separate LLM key. This template is one service with one key: DeepSeek runs retrieval server-side and bills per use, so there is no index to maintain and no second credential to provision. Encrypted server-tool blocks are stripped from client responses — callers only ever see the answer, citations, and usage.

**Error handling.** A uniform `{error: {message, type, code}}` envelope covers a missing key (401), an upstream-rejected key (401 passthrough), provider content-safety blocks (422), model refusals (422 with an explanation), rate limits (429 with `Retry-After`), and upstream failures after automatic retries (502). Transient upstream 408/429/5xx errors and network errors are retried with backoff before surfacing.

# Deploy and Host

## About Hosting

You are hosting a single small FastAPI service (uvicorn on python:3.12-slim, pinned dependencies, runs as a non-root user) that proxies one DeepSeek Chat Completions call with the `deepseek_web_search` server tool enabled. It is stateless: no database, no volume, nothing to back up — restarts and redeploys are always safe. Concurrency is bounded by DeepSeek's account limit (2,500 simultaneous requests), not by the container. The container idles at well under 100 MB RAM. The only configuration is the required `DEEPSEEK_API_KEY` variable (your key from platform.deepseek.com — billed to your account at ~$0.75 per 1,000 searches plus token usage); optional `SAFETY_CHECK_LEVEL` enables DeepSeek's server-side safety review for public-facing apps, which the DeepSeek ToS expects of public deployers.

## Why Deploy

- One service, no volume, one variable: everything retrieval-related (search, page reading, extraction) is DeepSeek's server-side tooling — there is no SearXNG to babysit, no index volume to size, no SERP API keys to buy, and no scraping code to break when a site changes its markup.
- Cited answers out of the box: citations are normalized to `{title, url, cited_text}` objects, so clients render sources without parsing provider-specific annotation shapes.
- Cost visibility: every response carries `usage` (input/output tokens plus search/fetch counts), and deep vs quick is an explicit request parameter so expensive research mode is opt-in.
- Production-shaped errors: uniform envelope, 429s with `Retry-After`, automatic retries with backoff, and provider content-safety outcomes surfaced as clean 422s instead of raw upstream bodies.
- Streaming included: the same question can be answered over SSE with answer deltas followed by final citations and usage events.

## Common Use Cases

- An answer API for apps and agents: your frontend or automation sends questions and renders the returned answer plus citations, without building a retrieval pipeline.
- A drop-in research endpoint: point any HTTP client at `depth: "deep"` for multi-step search reports on a single question (up to ~20 server tool calls per answer).
- Fact-checking and enrichment in workflows: enrich tickets, notes, or dashboards with a cited answer per record via the JSON API.
- A cheaper Perplexica-style alternative for API consumers: no SearXNG/SERP plumbing to host — retrieval is billed per use to your own DeepSeek account.
- Streamed answers for chat-style UX: subscribe to `/search-answer/stream` and render answer deltas live, then attach the sources event.

## Dependencies for

### Deployment Dependencies

- A DeepSeek API key from platform.deepseek.com — the one required deploy-form variable (`DEEPSEEK_API_KEY`). All search and token usage bills to your DeepSeek account: web search and fetch are $0.75 per 1,000 uses; deepseek-flash tokens are $0.30/1M input and $1.20/1M output at peak (01:00-04:00, 06:00-10:00 UTC Mon-Fri), half off-peak.
- Outbound internet access from the service to api.deepseek.com (default on Railway; no inbound ports beyond the auto-provisioned HTTPS domain).
- Nothing else: no database, no volume, no SearXNG, no SERP keys. The image pins fastapi, uvicorn, and httpx; python:3.12-slim is the base.
