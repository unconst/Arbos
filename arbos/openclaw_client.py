"""OpenClaw gateway client — workspace-scoped runs, optional session reuse."""

import json
import threading
import time
import uuid

from arbos.config import (
    OPENCLAW_GATEWAY_WS_URL,
    OPENCLAW_GATEWAY_HTTP_URL,
    OPENCLAW_AUTH_TOKEN,
    PI_TIMEOUT,
)
from arbos.log import log


# Session registry for future connection reuse: (workspace_id, thread_id) -> session handle.
# For now we do one-shot per request; this is here for long-lived sessions later.
_sessions: dict[tuple[int, int], object] = {}
_sessions_lock = threading.Lock()


def close_session(workspace_id: int, thread_id: int) -> None:
    """Close and remove a session when a goal is deleted or idle. Call from goals/bot when appropriate."""
    with _sessions_lock:
        key = (workspace_id, thread_id)
        if key in _sessions:
            sess = _sessions.pop(key)
            if hasattr(sess, "close"):
                try:
                    sess.close()
                except Exception:
                    pass
            log(f"openclaw session closed ws={workspace_id} t={thread_id}")


def run_openclaw_once(
    prompt: str,
    workspace: int,
    thread_id: int,
    cwd: str,
    on_text=None,
    on_activity=None,
    call_label: str = "",
) -> tuple[bool, str, list[str], str]:
    """Run one agent turn via OpenClaw gateway. One workspace per run; cwd is the sandbox root.

    Returns (success, result_text, raw_lines, stderr_or_error).
    """
    msg_id = f"arbos-{uuid.uuid4().hex[:12]}"
    raw_lines: list[str] = []
    result_text = ""
    error_msg = ""

    # Context: workspace isolation — gateway should restrict tools to this cwd
    context = {
        "cwd": cwd,
        "workspace_id": workspace,
        "thread_id": thread_id,
    }

    if OPENCLAW_GATEWAY_HTTP_URL:
        success, result_text, raw_lines, error_msg = _run_via_http(
            prompt, context, msg_id, on_text, on_activity, call_label
        )
    else:
        success, result_text, raw_lines, error_msg = _run_via_websocket(
            prompt, context, msg_id, on_text, on_activity, call_label
        )

    return success, result_text, raw_lines, error_msg


def _run_via_http(
    prompt: str,
    context: dict,
    msg_id: str,
    on_text,
    on_activity,
    call_label: str,
) -> tuple[bool, str, list[str], str]:
    """HTTP POST /message fallback (no streaming)."""
    import requests

    url = OPENCLAW_GATEWAY_HTTP_URL.rstrip("/") + "/message"
    payload = {
        "content": prompt,
        "user": f"arbos-{context.get('workspace_id', 0)}-{context.get('thread_id', 0)}",
        "channel": "discord",
        "context": context,
    }
    headers = {"Content-Type": "application/json"}
    if OPENCLAW_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {OPENCLAW_AUTH_TOKEN}"

    label = f" [{call_label}]" if call_label else ""
    log(f"openclaw HTTP request{label} msg_id={msg_id} cwd={context.get('cwd', '')[:60]}")

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=PI_TIMEOUT)
        raw_lines.append(json.dumps({"request": payload, "status": resp.status_code}))
        if resp.status_code != 200:
            return False, "", raw_lines, f"HTTP {resp.status_code}: {resp.text[:500]}"
        data = resp.json()
        result_text = data.get("reply", data.get("text", "")) or ""
        if on_text and result_text:
            on_text(result_text)
        return True, result_text, raw_lines, ""
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:300]}"
        log(f"openclaw HTTP error{label}: {err}")
        return False, "", raw_lines, err


def _run_via_websocket(
    prompt: str,
    context: dict,
    msg_id: str,
    on_text,
    on_activity,
    call_label: str,
) -> tuple[bool, str, list[str], str]:
    """WebSocket chat: send one message, collect response and tool events."""
    import websocket

    url = OPENCLAW_GATEWAY_WS_URL
    if OPENCLAW_AUTH_TOKEN:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={OPENCLAW_AUTH_TOKEN}"

    label = f" [{call_label}]" if call_label else ""
    log(f"openclaw WS connect{label} msg_id={msg_id} cwd={context.get('cwd', '')[:60]}")

    result_text = ""
    streaming_parts: list[str] = []
    error_msg = ""
    success = False
    raw_lines: list[str] = []
    done = threading.Event()

    def _emit_text():
        s = "".join(streaming_parts)
        if on_text and s:
            on_text(s)

    def on_message(ws, message: str):
        nonlocal result_text, success, error_msg
        raw_lines.append(message)
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        typ = msg.get("type", "")
        payload = msg.get("payload", msg)

        if typ == "response":
            text = payload.get("text", "") if isinstance(payload, dict) else ""
            if text:
                streaming_parts.append(text)
                result_text = text
                _emit_text()
            success = True
            done.set()
        elif typ == "tool_call":
            if on_activity and isinstance(payload, dict):
                tool = payload.get("tool", "?")
                args = payload.get("args", {})
                detail = str(args)[:80] if args else ""
                on_activity(f"{tool}: {detail}" if detail else f"{tool}...")
        elif typ == "tool_result":
            if on_activity and isinstance(payload, dict):
                tool = payload.get("tool", "?")
                on_activity(f"{tool}: done")
        elif typ == "error":
            if isinstance(payload, dict):
                error_msg = payload.get("message", str(payload))[:500]
            else:
                error_msg = str(payload)[:500]
            success = False
            done.set()

    def on_error(ws, err):
        nonlocal error_msg
        if err:
            err_str = str(err)
            errno_val = getattr(err, "errno", None)
            if errno_val == 61 or "Connection refused" in err_str or "ConnectionRefusedError" in err_str:
                error_msg = (
                    f"OpenClaw gateway not running at {OPENCLAW_GATEWAY_WS_URL}. "
                    "Start the OpenClaw gateway, or set OPENCLAW_GATEWAY_WS_URL in .env."
                )
            else:
                error_msg = err_str[:500]
        done.set()

    def on_close(ws, close_status_code, close_msg):
        if not done.is_set():
            done.set()

    chat_msg = {
        "type": "chat",
        "id": msg_id,
        "payload": {
            "text": prompt,
            "context": context,
            "options": {},
        },
    }
    payload_json = json.dumps(chat_msg)

    def on_open(ws):
        try:
            ws.send(payload_json)
        except Exception as e:
            nonlocal error_msg
            error_msg = str(e)[:500]
            done.set()

    ws = websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    thread = threading.Thread(
        target=lambda: ws.run_forever(ping_interval=30, ping_timeout=10),
        daemon=True,
    )
    thread.start()

    if not done.wait(timeout=PI_TIMEOUT):
        error_msg = f"OpenClaw timeout (no response for {PI_TIMEOUT}s)"
        log(f"openclaw WS timeout{label} msg_id={msg_id}")
        try:
            ws.close()
        except Exception:
            pass

    if not result_text and streaming_parts:
        result_text = "".join(streaming_parts)

    return success, result_text, raw_lines, error_msg
