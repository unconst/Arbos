"""Chutes translation proxy — Anthropic Messages API <-> OpenAI Chat Completions."""

import json
import re
import uuid
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from arbos.config import (
    CHUTES_BASE_URL, CHUTES_POOL, CHUTES_ROUTING_AGENT, CHUTES_ROUTING_BOT,
    CLAUDE_MODEL, LLM_API_KEY, PROXY_PORT, PROXY_TIMEOUT,
)
from arbos.log import log
from arbos.state import token_lock, token_usage

app = FastAPI(title="Chutes Proxy")


def _convert_tools_to_openai(anthropic_tools: list[dict]) -> list[dict]:
    out = []
    for t in anthropic_tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


def _convert_messages_to_openai(
    messages: list[dict], system: str | list | None = None
) -> list[dict]:
    out: list[dict] = []

    if system:
        if isinstance(system, list):
            text_parts = [b["text"] for b in system if b.get("type") == "text"]
            system = "\n\n".join(text_parts)
        if system:
            out.append({"role": "system", "content": system})

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            out.append({"role": role, "content": str(content)})
            continue

        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        image_parts: list[dict] = []

        for block in content:
            btype = block.get("type", "")

            if btype == "text":
                text_parts.append(block["text"])

            elif btype == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

            elif btype == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = "\n".join(
                        b.get("text", "") for b in result_content if b.get("type") == "text"
                    )
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "content": str(result_content),
                })

            elif btype == "image":
                source = block.get("source", {})
                if source.get("type") == "base64":
                    image_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{source.get('media_type', 'image/png')};base64,{source['data']}"
                        },
                    })

        if role == "assistant":
            oai_msg: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                oai_msg["content"] = "\n".join(text_parts)
            else:
                oai_msg["content"] = None
            if tool_calls:
                oai_msg["tool_calls"] = tool_calls
            out.append(oai_msg)

        elif role == "user":
            if tool_results:
                for tr in tool_results:
                    out.append(tr)
            if text_parts or image_parts:
                if image_parts:
                    content_blocks = [{"type": "text", "text": t} for t in text_parts] + image_parts
                    out.append({"role": "user", "content": content_blocks})
                elif text_parts:
                    out.append({"role": "user", "content": "\n".join(text_parts)})
        else:
            out.append({"role": role, "content": "\n".join(text_parts) if text_parts else ""})

    return out


def _build_openai_request(body: dict, *, routing: str = "agent") -> dict:
    routing_model = CHUTES_ROUTING_BOT if routing == "bot" else CHUTES_ROUTING_AGENT
    oai: dict[str, Any] = {
        "model": routing_model,
        "messages": _convert_messages_to_openai(
            body.get("messages", []),
            system=body.get("system"),
        ),
    }
    if "max_tokens" in body:
        oai["max_tokens"] = body["max_tokens"]
    if body.get("tools"):
        oai["tools"] = _convert_tools_to_openai(body["tools"])
        oai["tool_choice"] = "auto"
    if body.get("temperature") is not None:
        oai["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oai["top_p"] = body["top_p"]
    if body.get("stream"):
        oai["stream"] = True
        oai["stream_options"] = {"include_usage": True}
    return oai


def _openai_response_to_anthropic(oai_resp: dict, model: str) -> dict:
    choice = oai_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")

    content_blocks: list[dict] = []
    if message.get("content"):
        content_blocks.append({"type": "text", "text": message["content"]})
    for tc in (message.get("tool_calls") or []):
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": tc["function"]["name"],
            "input": args,
        })

    if finish == "tool_calls":
        stop_reason = "tool_use"
    elif finish == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"

    usage = oai_resp.get("usage", {})
    return {
        "id": oai_resp.get("id", f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_openai_to_anthropic(oai_response: httpx.Response, model: str):
    msg_id = f"msg_{uuid.uuid4().hex}"
    yield _sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    block_idx = 0
    in_text_block = False
    tool_calls_accum: dict[int, dict] = {}
    stop_reason = "end_turn"
    usage = {"input_tokens": 0, "output_tokens": 0}
    logged_stream_model = False

    async for line in oai_response.aiter_lines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if not logged_stream_model and chunk.get("model"):
            log(f"proxy: stream model={chunk['model']}")
            logged_stream_model = True

        if chunk.get("usage"):
            u = chunk["usage"]
            usage["input_tokens"] = u.get("prompt_tokens", usage["input_tokens"])
            usage["output_tokens"] = u.get("completion_tokens", usage["output_tokens"])

        choices = chunk.get("choices", [])
        if not choices:
            continue

        delta = choices[0].get("delta", {})
        finish = choices[0].get("finish_reason")

        if finish == "tool_calls":
            stop_reason = "tool_use"
        elif finish == "length":
            stop_reason = "max_tokens"
        elif finish == "stop":
            stop_reason = "end_turn"

        if delta.get("content"):
            if not in_text_block:
                yield _sse_event("content_block_start", {
                    "type": "content_block_start",
                    "index": block_idx,
                    "content_block": {"type": "text", "text": ""},
                })
                in_text_block = True
            yield _sse_event("content_block_delta", {
                "type": "content_block_delta",
                "index": block_idx,
                "delta": {"type": "text_delta", "text": delta["content"]},
            })

        if delta.get("tool_calls"):
            if in_text_block:
                yield _sse_event("content_block_stop", {
                    "type": "content_block_stop", "index": block_idx,
                })
                block_idx += 1
                in_text_block = False
            for tc in delta["tool_calls"]:
                tc_idx = tc.get("index", 0)
                if tc_idx not in tool_calls_accum:
                    tool_calls_accum[tc_idx] = {
                        "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": "",
                        "block_idx": block_idx,
                    }
                    yield _sse_event("content_block_start", {
                        "type": "content_block_start",
                        "index": block_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_calls_accum[tc_idx]["id"],
                            "name": tool_calls_accum[tc_idx]["name"],
                            "input": {},
                        },
                    })
                    block_idx += 1
                args_chunk = tc.get("function", {}).get("arguments", "")
                if args_chunk:
                    tool_calls_accum[tc_idx]["arguments"] += args_chunk
                    yield _sse_event("content_block_delta", {
                        "type": "content_block_delta",
                        "index": tool_calls_accum[tc_idx]["block_idx"],
                        "delta": {"type": "input_json_delta", "partial_json": args_chunk},
                    })

    with token_lock:
        token_usage["input"] += usage["input_tokens"]
        token_usage["output"] += usage["output_tokens"]

    if in_text_block:
        yield _sse_event("content_block_stop", {
            "type": "content_block_stop", "index": block_idx,
        })
    for tc in tool_calls_accum.values():
        yield _sse_event("content_block_stop", {
            "type": "content_block_stop", "index": tc["block_idx"],
        })

    yield _sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": usage["output_tokens"]},
    })
    yield _sse_event("message_stop", {"type": "message_stop"})


def _chutes_headers() -> dict:
    return {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }


@app.get("/health")
async def proxy_health():
    return {"status": "ok"}


@app.get("/")
async def proxy_root():
    return {
        "proxy": "chutes",
        "pool": CHUTES_POOL,
        "agent_routing": CHUTES_ROUTING_AGENT,
        "bot_routing": CHUTES_ROUTING_BOT,
        "status": "running",
    }


_CONTEXT_LENGTH_RE = re.compile(
    r"maximum context length is (\d+) tokens.*?(\d+) output tokens.*?(\d+) input tokens",
    re.DOTALL,
)
PROXY_MAX_RETRIES = 3


def _parse_context_length_error(error_msg: str) -> tuple[int, int, int] | None:
    m = _CONTEXT_LENGTH_RE.search(error_msg)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _maybe_reduce_max_tokens(oai_request: dict, error_msg: str) -> bool:
    parsed = _parse_context_length_error(error_msg)
    if not parsed:
        return False
    ctx_limit, _req_output, input_tokens = parsed
    headroom = ctx_limit - input_tokens
    if headroom < 1024:
        return False
    new_max = max(1024, headroom - 64)
    old_max = oai_request.get("max_tokens", 0)
    if new_max >= old_max:
        return False
    oai_request["max_tokens"] = new_max
    log(f"proxy: reduced max_tokens {old_max} -> {new_max} (ctx_limit={ctx_limit}, input={input_tokens})")
    return True


@app.post("/v1/messages")
async def proxy_messages(request: Request):
    body = await request.json()
    stream = body.get("stream", False)
    model = body.get("model", CLAUDE_MODEL)
    routing = "bot" if model == "bot" else "agent"
    oai_request = _build_openai_request(body, routing=routing)
    routing_label = CHUTES_ROUTING_BOT if routing == "bot" else CHUTES_ROUTING_AGENT

    if stream:
        last_error_msg = ""
        for attempt in range(1, PROXY_MAX_RETRIES + 1):
            try:
                client = httpx.AsyncClient(timeout=httpx.Timeout(PROXY_TIMEOUT))
                resp = await client.send(
                    client.build_request(
                        "POST", f"{CHUTES_BASE_URL}/chat/completions",
                        json=oai_request, headers=_chutes_headers(),
                    ),
                    stream=True,
                )
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    await resp.aclose()
                    await client.aclose()
                    last_error_msg = error_body.decode()[:500]
                    log(f"proxy: chutes returned {resp.status_code} (attempt {attempt}/{PROXY_MAX_RETRIES}): {last_error_msg[:300]}")

                    if resp.status_code == 400 and _maybe_reduce_max_tokens(oai_request, last_error_msg):
                        continue
                    if attempt < PROXY_MAX_RETRIES:
                        continue

                    return JSONResponse(status_code=502, content={
                        "type": "error", "error": {
                            "type": "api_error",
                            "message": f"Chutes routing failed ({resp.status_code}): {last_error_msg[:300]}",
                        },
                    })

                async def generate(resp=resp, cl=client):
                    try:
                        log(f"proxy: streaming [{routing}] via {routing_label}")
                        async for event in _stream_openai_to_anthropic(resp, model):
                            yield event
                    finally:
                        await resp.aclose()
                        await cl.aclose()

                return StreamingResponse(
                    generate(), media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )
            except httpx.TimeoutException:
                last_error_msg = f"timed out after {PROXY_TIMEOUT}s"
                log(f"proxy: {last_error_msg} (attempt {attempt}/{PROXY_MAX_RETRIES})")
                if attempt < PROXY_MAX_RETRIES:
                    continue
                return JSONResponse(status_code=502, content={
                    "type": "error", "error": {
                        "type": "api_error",
                        "message": f"Chutes routing {last_error_msg}",
                    },
                })
            except Exception as exc:
                last_error_msg = str(exc)[:300]
                log(f"proxy: error (attempt {attempt}/{PROXY_MAX_RETRIES}): {last_error_msg}")
                if attempt < PROXY_MAX_RETRIES:
                    continue
                return JSONResponse(status_code=502, content={
                    "type": "error", "error": {
                        "type": "api_error",
                        "message": f"Chutes routing error: {last_error_msg}",
                    },
                })

    else:
        oai_request.pop("stream", None)
        oai_request.pop("stream_options", None)
        last_error_msg = ""
        for attempt in range(1, PROXY_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(PROXY_TIMEOUT)) as client:
                    resp = await client.post(
                        f"{CHUTES_BASE_URL}/chat/completions",
                        json=oai_request, headers=_chutes_headers(),
                    )
                if resp.status_code != 200:
                    last_error_msg = resp.text[:500]
                    log(f"proxy: chutes returned {resp.status_code} (attempt {attempt}/{PROXY_MAX_RETRIES}): {last_error_msg[:300]}")

                    if resp.status_code == 400 and _maybe_reduce_max_tokens(oai_request, last_error_msg):
                        continue
                    if attempt < PROXY_MAX_RETRIES:
                        continue

                    return JSONResponse(status_code=502, content={
                        "type": "error", "error": {
                            "type": "api_error",
                            "message": f"Chutes routing failed ({resp.status_code}): {last_error_msg[:300]}",
                        },
                    })
                oai_data = resp.json()
                actual_model = oai_data.get("model", "?")
                u = oai_data.get("usage", {})
                if u:
                    with token_lock:
                        token_usage["input"] += u.get("prompt_tokens", 0)
                        token_usage["output"] += u.get("completion_tokens", 0)
                log(f"proxy: response [{routing}] via {routing_label} model={actual_model}")
                return JSONResponse(content=_openai_response_to_anthropic(oai_data, model))
            except httpx.TimeoutException:
                last_error_msg = f"timed out after {PROXY_TIMEOUT}s"
                log(f"proxy: {last_error_msg} (attempt {attempt}/{PROXY_MAX_RETRIES})")
                if attempt < PROXY_MAX_RETRIES:
                    continue
                return JSONResponse(status_code=502, content={
                    "type": "error", "error": {
                        "type": "api_error",
                        "message": f"Chutes routing {last_error_msg}",
                    },
                })
            except Exception as exc:
                last_error_msg = str(exc)[:300]
                log(f"proxy: error (attempt {attempt}/{PROXY_MAX_RETRIES}): {last_error_msg}")
                if attempt < PROXY_MAX_RETRIES:
                    continue
                return JSONResponse(status_code=502, content={
                    "type": "error", "error": {
                        "type": "api_error",
                        "message": f"Chutes routing error: {last_error_msg}",
                    },
                })


@app.post("/v1/messages/count_tokens")
async def proxy_count_tokens(request: Request):
    body = await request.json()
    rough = sum(len(json.dumps(m)) for m in body.get("messages", [])) // 4
    rough += len(json.dumps(body.get("tools", []))) // 4
    rough += len(str(body.get("system", ""))) // 4
    return JSONResponse(content={"input_tokens": max(rough, 1)})


def start_proxy():
    """Run the Chutes translation proxy in-process on a background thread."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=PROXY_PORT, log_level="warning",
    )
    server = uvicorn.Server(config)
    server.run()
