"""
DeepSeek Search API - cited web answers over DeepSeek's server-side web search.

One HTTP call turns a question into an answer with sources: the retrieval
stack (search, page reading, extraction) runs entirely on DeepSeek's side
via the `deepseek_web_search` server tool. No SearXNG, no SERP keys, no
scraping code.

Endpoints:
    GET  /health              liveness probe (no external calls)
    POST /search-answer       {question, depth: quick|deep} -> cited answer
    POST /search-answer/stream  same, as Server-Sent Events

Billing flows to the deployer's own DeepSeek account: ~$0.75 per 1,000
searches (fetches the same) plus token usage. `usage` is surfaced on every
response so callers can watch spend.
"""

import asyncio
import json
import os
from typing import Any, Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------- config

DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
SAFETY_CHECK_LEVEL = os.environ.get("SAFETY_CHECK_LEVEL", "").strip().lower()
if SAFETY_CHECK_LEVEL not in ("low", "high"):
    SAFETY_CHECK_LEVEL = ""

MAX_RETRIES = 2
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
UPSTREAM_TIMEOUT = httpx.Timeout(300.0, connect=15.0)

app = FastAPI(
    title="DeepSeek Search API",
    description="Cited web answers from DeepSeek's server-side web search. No SERP keys, no SearXNG.",
    version="1.0.0",
)


def _api_key() -> Optional[str]:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    return key or None


# ---------------------------------------------------------------- helpers

def error_response(status: int, message: str, type_: str = "api_error",
                   code: Optional[str] = None, retry_after: Optional[int] = None) -> JSONResponse:
    """Uniform OpenAI-compatible error envelope: {error: {message, type, code}}."""
    headers = {}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "code": code}},
        headers=headers,
    )


def sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class SearchRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000,
                          description="The question to answer with web sources.")
    depth: str = Field("quick", pattern="^(quick|deep)$",
                       description="quick = one search pass; deep = thinking-enabled multi-step search (up to ~20 tool calls, higher cost).")
    freshness_hint: Optional[str] = Field(None, max_length=200,
                                          description="Optional recency steering, e.g. 'last 30 days'. Appended to the prompt as guidance.")


def build_payload(question: str, depth: str, freshness_hint: Optional[str], stream: bool) -> dict:
    content = question
    if freshness_hint:
        content = f"{question}\n\n(Prefer sources that are {freshness_hint}.)"
    payload: dict = {
        "model": MODEL,
        "messages": [{"role": "user", "content": content}],
        "tools": [{"type": "deepseek_web_search"}],
        "stream": stream,
    }
    if depth == "deep":
        # Thinking + search = deep search (multi-step, up to ~20 server tool calls).
        payload["thinking"] = {"type": "enabled"}
    if SAFETY_CHECK_LEVEL:
        payload["safety_check"] = {"level": SAFETY_CHECK_LEVEL}
    # NOTE: no tool_choice (naming deepseek_web_search is a 400), no
    # temperature/top_p (ignored when search/thinking is active).
    return payload


def extract_citations(message: dict, content: str) -> list:
    """Collect citations from message.annotations (Chat Completions shape:
    annotations[].url_citation). Tolerates the Messages-API-style
    web_search_result_location blocks if they ever appear here."""
    out: list = []
    seen: set = set()
    for ann in (message.get("annotations") or []):
        if not isinstance(ann, dict):
            continue
        uc = ann.get("url_citation") or ann.get("web_search_result_location") or {}
        if not isinstance(uc, dict):
            continue
        url = uc.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        item = {"title": uc.get("title") or url, "url": url}
        start, end = uc.get("start_index"), uc.get("end_index")
        if isinstance(start, int) and isinstance(end, int) and content and 0 <= start < end <= len(content):
            item["cited_text"] = content[start:end]
        out.append(item)
    return out


def extract_usage(usage: dict) -> dict:
    stu = usage.get("server_tool_use") or {}
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "search_calls": stu.get("tool_search") or 0,
        "fetch_calls": stu.get("tool_fetch") or 0,
        "image_search_calls": stu.get("tool_image_search") or 0,
    }


def map_upstream_error(status: int, body: dict) -> JSONResponse:
    """Map DeepSeek errors to the documented client envelope."""
    err = body.get("error") if isinstance(body, dict) else None
    upstream_msg = ""
    if isinstance(err, dict):
        upstream_msg = err.get("message") or ""
    elif isinstance(err, str):
        upstream_msg = err
    elif isinstance(body, dict):
        upstream_msg = str(body.get("message") or "")

    if status == 401:
        return error_response(401, upstream_msg or "Upstream rejected the configured DEEPSEEK_API_KEY.",
                              "invalid_request_error", "invalid_api_key")
    if status == 400 and "risk" in upstream_msg.lower():
        return error_response(422, upstream_msg or "The request was blocked by the provider's content safety review.",
                              "content_blocked", "content_exists_risk")
    if status == 400:
        return error_response(400, upstream_msg or "Upstream rejected the request.", "invalid_request_error", "bad_request")
    if status == 429:
        return error_response(429, upstream_msg or "Upstream rate limit reached, retries exhausted.",
                              "rate_limit_error", "rate_limited", retry_after=5)
    if status in RETRYABLE_STATUS:
        return error_response(502, upstream_msg or f"Upstream unavailable (HTTP {status}), retries exhausted.",
                              "api_error", "upstream_unavailable")
    return error_response(502, upstream_msg or f"Upstream returned HTTP {status}.", "api_error", "upstream_error")


async def call_deepseek_nonstream(client: httpx.AsyncClient, key: str, payload: dict):
    """POST /chat/completions with retries on 408/429/5xx and network errors.

    Returns (status, body_dict_or_None, error_message_or_None)."""
    last_network_error: Optional[str] = None
    last_status: Optional[int] = None
    last_body: Optional[dict] = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
        try:
            resp = await client.post(
                f"{DEEPSEEK_BASE}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
        except httpx.RequestError as exc:
            last_network_error = f"Upstream connection failed: {exc.__class__.__name__}"
            continue
        last_status = resp.status_code
        try:
            last_body = resp.json()
        except ValueError:
            last_body = None
        if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
            ra = resp.headers.get("retry-after")
            if ra and ra.strip().isdigit():
                await asyncio.sleep(min(int(ra), 30))
            continue
        return last_status, last_body, None
    if last_network_error and last_status is None:
        return None, None, last_network_error
    return last_status, last_body, None


# ---------------------------------------------------------------- routes

@app.get("/")
async def root():
    return {
        "service": "deepseek-search-api",
        "endpoints": ["POST /search-answer", "POST /search-answer/stream", "GET /health"],
        "model": MODEL,
        "depths": ["quick", "deep"],
    }


@app.get("/health")
async def health():
    # Dotless path, no external calls: safe as a Railway healthcheck probe.
    return {"ok": True, "service": "deepseek-search-api"}


@app.post("/search-answer")
async def search_answer(req: SearchRequest):
    key = _api_key()
    if not key:
        return error_response(
            401,
            "DEEPSEEK_API_KEY is not set on this deployment. Add it under Variables (your key from platform.deepseek.com), then redeploy.",
            "invalid_request_error",
            "missing_api_key",
        )

    payload = build_payload(req.question, req.depth, req.freshness_hint, stream=False)
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        status, body, net_err = await call_deepseek_nonstream(client, key, payload)

    if net_err:
        return error_response(502, net_err, "api_error", "upstream_unreachable")
    if status is None:
        return error_response(502, "Upstream returned no response.", "api_error", "upstream_error")
    if body is None:
        return error_response(502, "Upstream returned a non-JSON response.", "api_error", "upstream_bad_response")

    if status != 200:
        return map_upstream_error(status, body)

    # DeepSeek documents that generation failures can arrive as HTTP 200
    # with an `error` object in the body - inspect the body, not just the code.
    if body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return error_response(502, msg or "Upstream reported an error in a 200 response.",
                              "api_error", "upstream_body_error")

    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    finish_reason = choice.get("finish_reason")

    if finish_reason == "content_filter":
        return JSONResponse(status_code=422, content={
            "refusal": True,
            "category": None,
            "explanation": message.get("refusal") or "The request was declined by the provider's safety review.",
            "answer": "",
            "citations": [],
        })

    citations = extract_citations(message, content)
    usage = extract_usage(body.get("usage") or {})
    tools_used = len(message.get("server_tool_calls") or [])
    if tools_used == 0:
        tools_used = (usage.get("search_calls") or 0) + (usage.get("fetch_calls") or 0) + (usage.get("image_search_calls") or 0)

    return {
        "answer": content,
        "citations": citations,
        "tools_used": tools_used,
        "usage": usage,
    }


@app.post("/search-answer/stream")
async def search_answer_stream(req: SearchRequest):
    key = _api_key()
    if not key:
        return error_response(
            401,
            "DEEPSEEK_API_KEY is not set on this deployment. Add it under Variables (your key from platform.deepseek.com), then redeploy.",
            "invalid_request_error",
            "missing_api_key",
        )

    payload = build_payload(req.question, req.depth, req.freshness_hint, stream=True)

    async def generate():
        yield sse_event("start", {"model": MODEL, "depth": req.depth})
        content_parts: list = []
        annotations: list = []
        usage: Optional[dict] = None
        refusal_text: Optional[str] = None
        finish_reason: Optional[str] = None
        emitted_any = False

        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            # Pre-flight retries only while nothing has been emitted to the client.
            for attempt in range(MAX_RETRIES + 1):
                if attempt:
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                try:
                    resp = await client.post(
                        f"{DEEPSEEK_BASE}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {key}"},
                    )
                except httpx.RequestError as exc:
                    if attempt < MAX_RETRIES:
                        continue
                    yield sse_event("error", {"message": f"Upstream connection failed: {exc.__class__.__name__}",
                                              "type": "api_error", "code": "upstream_unreachable"})
                    yield sse_event("done", {"ok": False})
                    return
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES and not emitted_any:
                    ra = resp.headers.get("retry-after")
                    if ra and ra.strip().isdigit():
                        await asyncio.sleep(min(int(ra), 30))
                    continue
                break

            if resp.status_code != 200:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                mapped = map_upstream_error(resp.status_code, body)
                try:
                    err_payload = json.loads(mapped.body)
                except ValueError:
                    err_payload = {"error": {"message": "Upstream error.", "type": "api_error", "code": "upstream_error"}}
                yield sse_event("error", err_payload)
                yield sse_event("done", {"ok": False})
                return

            emitted_any = True
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    # Body-level error on a 200 stream.
                    if chunk.get("error"):
                        yield sse_event("error", {"message": "Upstream reported an error mid-stream.",
                                                  "type": "api_error", "code": "upstream_body_error"})
                        yield sse_event("done", {"ok": False})
                        return
                    choices = chunk.get("choices") or [{}]
                    choice = choices[0] if choices else {}
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text = delta["content"]
                        content_parts.append(text)
                        yield sse_event("delta", {"text": text})
                    if delta.get("refusal"):
                        refusal_text = delta["refusal"]
                    delta_ann = delta.get("annotations")
                    if delta_ann:
                        annotations.extend(delta_ann)
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
            except httpx.RequestError as exc:
                yield sse_event("error", {"message": f"Stream interrupted: {exc.__class__.__name__}",
                                          "type": "api_error", "code": "stream_interrupted"})
                yield sse_event("done", {"ok": False})
                return

        if finish_reason == "content_filter":
            yield sse_event("refusal", {"refusal": True, "category": None,
                                        "explanation": refusal_text or "The request was declined by the provider's safety review."})
        else:
            full_content = "".join(content_parts)
            final_message = {"annotations": annotations}
            yield sse_event("citations", {"citations": extract_citations(final_message, full_content)})
            yield sse_event("usage", {"usage": extract_usage(usage or {})})
        yield sse_event("done", {"ok": finish_reason != "content_filter"})

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
