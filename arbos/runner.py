"""Claude subprocess management — command building, execution, step runner, streaming."""

import json
import os
import selectors
import subprocess
import time
import threading
from pathlib import Path

from arbos.config import (
    WORKING_DIR, PROVIDER, CLAUDE_MODEL, LLM_API_KEY, LLM_BASE_URL,
    PROXY_PORT, IS_ROOT, MAX_RETRIES, CLAUDE_TIMEOUT,
    workspace_dir, make_run_dir, step_msg_file,
)
from arbos.log import log, fmt_duration, fmt_tokens, reset_tokens, get_tokens
from arbos.redact import redact_secrets
from arbos.prompt import log_chat, format_tool_activity
from arbos.discord_api import send_new, edit_text
from arbos.state import (
    tls, token_lock, token_usage, claude_semaphore,
    child_procs, child_procs_lock,
)


def claude_cmd(prompt: str, extra_flags: list[str] | None = None) -> list[str]:
    cmd = ["claude", "-p", prompt]
    if not IS_ROOT:
        cmd.append("--dangerously-skip-permissions")
    cmd.extend(["--output-format", "stream-json", "--verbose"])
    if extra_flags:
        cmd.extend(extra_flags)
    return cmd


def write_claude_settings():
    """Point Claude Code at the active provider (OpenRouter direct or Chutes proxy)."""
    settings_dir = WORKING_DIR / ".claude"
    settings_dir.mkdir(exist_ok=True)

    if PROVIDER == "openrouter":
        env_block = {
            "ANTHROPIC_API_KEY": LLM_API_KEY,
            "ANTHROPIC_BASE_URL": LLM_BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        target_label = LLM_BASE_URL
    else:
        proxy_url = f"http://127.0.0.1:{PROXY_PORT}"
        env_block = {
            "ANTHROPIC_API_KEY": "chutes-proxy",
            "ANTHROPIC_BASE_URL": proxy_url,
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        target_label = proxy_url

    settings = {
        "model": CLAUDE_MODEL,
        "permissions": {
            "allow": [
                "Bash(*)", "Read(*)", "Write(*)", "Edit(*)",
                "Glob(*)", "Grep(*)", "WebFetch(*)", "WebSearch(*)",
                "TodoWrite(*)", "NotebookEdit(*)", "Task(*)",
            ],
        },
        "env": env_block,
    }
    (settings_dir / "settings.local.json").write_text(json.dumps(settings, indent=2))
    log(f"wrote .claude/settings.local.json (provider={PROVIDER}, model={CLAUDE_MODEL}, target={target_label})")


def claude_env(workspace: int = 0, thread_id: int = 0) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DISCORD_BOT_TOKEN", None)
    env["ARBOS_ROOT"] = str(WORKING_DIR)
    if workspace:
        env["ARBOS_WORKSPACE"] = str(workspace)
    if thread_id:
        env["ARBOS_THREAD_ID"] = str(thread_id)
    if PROVIDER == "openrouter":
        env["ANTHROPIC_API_KEY"] = LLM_API_KEY
        env["ANTHROPIC_BASE_URL"] = LLM_BASE_URL
        env["ANTHROPIC_AUTH_TOKEN"] = ""
    else:
        env["ANTHROPIC_API_KEY"] = "chutes-proxy"
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{PROXY_PORT}"
        env["ANTHROPIC_AUTH_TOKEN"] = ""
    return env


def run_claude_once(cmd, env, on_text=None, on_activity=None, cwd=None):
    """Run a single claude subprocess, return (returncode, result_text, raw_lines, stderr).

    Kills the process if no output is received for CLAUDE_TIMEOUT seconds.
    """
    run_dir = Path(cwd) if cwd else WORKING_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd, cwd=run_dir, env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    with child_procs_lock:
        child_procs.add(proc)

    result_text = ""
    complete_texts: list[str] = []
    streaming_tokens: list[str] = []
    raw_lines: list[str] = []
    timed_out = False
    last_activity = time.monotonic()

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)

    try:
        while True:
            ready = sel.select(timeout=min(CLAUDE_TIMEOUT, 30))
            if not ready:
                if time.monotonic() - last_activity > CLAUDE_TIMEOUT:
                    log(f"claude timeout: no output for {CLAUDE_TIMEOUT}s, killing pid={proc.pid}")
                    if on_activity:
                        on_activity(f"⚠ timed out (no output for {CLAUDE_TIMEOUT}s)")
                    proc.kill()
                    timed_out = True
                    break
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            last_activity = time.monotonic()
            raw_lines.append(line)
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type", "")
            if etype == "assistant":
                msg = evt.get("message", {})
                for block in msg.get("content", []):
                    btype = block.get("type", "")
                    if btype == "text" and block.get("text"):
                        if evt.get("model_call_id"):
                            complete_texts.append(block["text"])
                            streaming_tokens.clear()
                        else:
                            streaming_tokens.append(block["text"])
                            if on_text:
                                on_text("".join(streaming_tokens))
                    elif btype == "tool_use" and on_activity:
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        on_activity(format_tool_activity(tool_name, tool_input))
                if PROVIDER == "openrouter":
                    u = msg.get("usage", {})
                    if u:
                        with token_lock:
                            token_usage["input"] += u.get("input_tokens", 0)
                            token_usage["output"] += u.get("output_tokens", 0)
            elif etype == "item.completed":
                item = evt.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    complete_texts.append(item["text"])
                    streaming_tokens.clear()
                    if on_text:
                        on_text(item["text"])
            elif etype == "result":
                result_text = evt.get("result", "")
                if PROVIDER == "openrouter":
                    u = evt.get("usage", {})
                    if u:
                        with token_lock:
                            token_usage["input"] += u.get("input_tokens", 0)
                            token_usage["output"] += u.get("output_tokens", 0)
    finally:
        sel.unregister(proc.stdout)
        sel.close()

    if complete_texts and len(complete_texts) > 1:
        seen = set()
        unique = []
        for t in complete_texts:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        if len(unique) > 1:
            result_text = "\n\n".join(unique)
    if not result_text:
        if complete_texts:
            result_text = complete_texts[-1]
        elif streaming_tokens:
            result_text = "".join(streaming_tokens)

    if timed_out:
        stderr_output = "(timed out)"
    else:
        stderr_output = proc.stderr.read() if proc.stderr else ""

    returncode = proc.wait()
    with child_procs_lock:
        child_procs.discard(proc)
    return returncode, result_text, raw_lines, stderr_output


def run_agent(cmd: list[str], phase: str, output_file: Path,
              on_text=None, on_activity=None, workspace: int = 0, thread_id: int = 0) -> subprocess.CompletedProcess:
    claude_semaphore.acquire()
    try:
        env = claude_env(workspace=workspace, thread_id=thread_id)
        cwd = str(workspace_dir(workspace)) if workspace else None
        flags = " ".join(a for a in cmd if a.startswith("-"))

        returncode, result_text, raw_lines, stderr_output = 1, "", [], "no attempts made"

        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1 and on_activity:
                on_activity(f"⚠ retry attempt {attempt}/{MAX_RETRIES}...")
            log(f"{phase}: starting (attempt={attempt}/{MAX_RETRIES}) flags=[{flags}]")
            t0 = time.monotonic()

            returncode, result_text, raw_lines, stderr_output = run_claude_once(
                cmd, env, on_text=on_text, on_activity=on_activity, cwd=cwd,
            )
            elapsed = time.monotonic() - t0

            output_file.write_text(redact_secrets("".join(raw_lines)))
            log(f"{phase}: finished rc={returncode} {fmt_duration(elapsed)}")

            if returncode != 0:
                stderr_snip = (stderr_output or "").strip()[:300]
                log(f"{phase}: stderr={stderr_snip or '(empty)'}")
                if attempt < MAX_RETRIES:
                    delay = min(2 ** attempt, 30)
                    log(f"{phase}: retrying in {delay}s (attempt {attempt}/{MAX_RETRIES})")
                    if on_activity:
                        on_activity(f"⚠ Claude error (rc={returncode}), retrying in {delay}s...")
                    time.sleep(delay)
                    continue

            return subprocess.CompletedProcess(
                args=cmd, returncode=returncode,
                stdout=result_text, stderr=stderr_output,
            )

        log(f"{phase}: all {MAX_RETRIES} retries exhausted")
        if on_activity:
            on_activity(f"⚠ all {MAX_RETRIES} retries exhausted")
        output_file.write_text(redact_secrets("".join(raw_lines)))
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode,
            stdout=result_text, stderr=stderr_output,
        )
    finally:
        claude_semaphore.release()


def extract_text(result: subprocess.CompletedProcess) -> str:
    output = result.stdout or ""
    if not output.strip():
        output = result.stderr or "(no output)"
    return output


def run_step(prompt: str, step_number: int, workspace: int = 0, thread_id: int = 0, goal_step: int = 0) -> bool:
    run_dir = make_run_dir(workspace=workspace, thread_id=thread_id)
    t0 = time.monotonic()

    log_file = run_dir / "logs.txt"
    tls.log_fh = open(log_file, "a", encoding="utf-8")

    smf = step_msg_file(workspace, thread_id)
    smf.parent.mkdir(parents=True, exist_ok=True)

    step_label = f"Goal Step {goal_step}" if goal_step else f"Step {step_number}"
    step_msg_id: int | None = None
    last_edit = 0.0

    step_msg_id = send_new(thread_id, f"{step_label}: starting...")
    if step_msg_id:
        smf.write_text(json.dumps({
            "msg_id": step_msg_id, "channel_id": thread_id, "text": f"{step_label}: starting...",
        }))
    else:
        smf.unlink(missing_ok=True)

    def _edit_step_msg(text: str, *, force: bool = False):
        nonlocal last_edit
        if not step_msg_id:
            return
        now = time.time()
        if not force and now - last_edit < 3.0:
            return
        edit_text(thread_id, step_msg_id, text)
        smf.write_text(json.dumps({"msg_id": step_msg_id, "channel_id": thread_id, "text": text}))
        last_edit = now

    reset_tokens()

    _last_activity = [""]
    _heartbeat_stop = threading.Event()
    _rollout_log_buf: list[str] = [""]

    def _on_activity(status: str):
        _last_activity[0] = status
        log(f"rollout activity: {status}")
        elapsed_s = time.monotonic() - t0
        inp, out = get_tokens()
        tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
        _edit_step_msg(f"{step_label} ({fmt_duration(elapsed_s)}{tok})\n{status}")

    def _on_text(text: str):
        """Stream rollout content to PM2 logs (stdout)."""
        if not text:
            return
        _rollout_log_buf[0] += text
        while "\n" in _rollout_log_buf[0]:
            line, _rollout_log_buf[0] = _rollout_log_buf[0].split("\n", 1)
            if line.strip():
                log(redact_secrets(line))

    def _heartbeat():
        while not _heartbeat_stop.wait(timeout=10):
            elapsed_s = time.monotonic() - t0
            inp, out = get_tokens()
            tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
            status = _last_activity[0] or "working..."
            _edit_step_msg(f"{step_label} ({fmt_duration(elapsed_s)}{tok})\n{status}", force=True)

    success = False
    error_info = ""
    result = None
    try:
        log(f"run dir {run_dir}")

        preview = prompt[:200] + ("..." if len(prompt) > 200 else "")
        log(f"prompt preview: {preview}")

        log(f"workspace={workspace} thread={thread_id} step {goal_step}: executing")

        threading.Thread(target=_heartbeat, daemon=True).start()

        result = run_agent(
            claude_cmd(prompt),
            phase=f"ws{workspace}/t{thread_id}",
            output_file=run_dir / "output.txt",
            on_text=_on_text,
            on_activity=_on_activity,
            workspace=workspace,
            thread_id=thread_id,
        )
        if _rollout_log_buf[0].strip():
            log(redact_secrets(_rollout_log_buf[0]))

        rollout_text = redact_secrets(extract_text(result))
        (run_dir / "rollout.md").write_text(rollout_text)
        log(f"rollout saved ({len(rollout_text)} chars)")

        elapsed = time.monotonic() - t0
        success = result.returncode == 0

        if not success:
            stderr_snip = redact_secrets((result.stderr or "").strip())[:300]
            if stderr_snip == "(timed out)":
                error_info = f"Claude timed out (no output for {CLAUDE_TIMEOUT}s)"
            elif stderr_snip:
                error_info = f"Claude exited with code {result.returncode}: {stderr_snip}"
            else:
                error_info = f"Claude exited with code {result.returncode} (no stderr)"
            if not rollout_text.strip():
                error_info += " — no output was produced"
            log(f"step error: {error_info}")

        log(f"step {'succeeded' if success else 'failed'} in {fmt_duration(elapsed)}")
        return success
    except Exception as exc:
        error_info = f"Exception during step: {type(exc).__name__}: {str(exc)[:250]}"
        log(f"step exception: {error_info}")
        import traceback
        log(traceback.format_exc()[:500])
        return False
    finally:
        _heartbeat_stop.set()
        fh = getattr(tls, "log_fh", None)
        if fh:
            fh.close()
            tls.log_fh = None
        try:
            elapsed = fmt_duration(time.monotonic() - t0)
            rollout = (run_dir / "rollout.md").read_text() if (run_dir / "rollout.md").exists() else ""
            status = "done" if success else "failed"

            agent_text = ""
            if smf.exists():
                try:
                    st = json.loads(smf.read_text())
                    saved = st.get("text", "")
                    prefix = f"{step_label}: starting..."
                    if saved != prefix and not saved.startswith(f"{step_label} ("):
                        agent_text = saved
                except (json.JSONDecodeError, KeyError):
                    pass

            elapsed_s = time.monotonic() - t0
            inp, out = get_tokens()
            tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
            parts = [f"{step_label} ({elapsed}, {status}{tok})"]
            if error_info:
                parts.append(f"⚠ {error_info}")
            if agent_text:
                parts.append(agent_text)
            if rollout.strip():
                parts.append(rollout.strip()[:1500])
            elif not success and not error_info:
                parts.append("(no output from Claude)")
            final = "\n\n".join(parts)

            _edit_step_msg(final, force=True)
            log_chat(workspace, "bot", final[:1000])
            smf.unlink(missing_ok=True)
        except Exception as exc:
            log(f"step message finalize failed: {str(exc)[:120]}")


def run_agent_streaming(channel_id: int, msg_id: int, prompt: str, workspace: int = 0, cwd: str | None = None) -> str:
    """Run Claude Code CLI and stream output by editing a Discord message.
    Message format matches step logs: Arbos: (41.5s | tokens | $cost)\n{activity}
    """
    if PROVIDER == "openrouter":
        cmd = claude_cmd(prompt)
    else:
        cmd = claude_cmd(prompt, extra_flags=["--model", "bot"])

    if cwd is None:
        cwd = str(workspace_dir(workspace)) if workspace else None
    reset_tokens()
    current_text = ""
    activity_status = ""
    last_edit_ts = 0.0
    t0 = time.monotonic()
    _heartbeat_stop = threading.Event()

    def _build_status_body() -> str:
        """Same format as step logs: Arbos: (41.5s | 89.8k in / 0 out | $0.4488)\n{activity}"""
        elapsed_s = time.monotonic() - t0
        inp, out = get_tokens()
        tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
        status_line = f"Arbos: ({fmt_duration(elapsed_s)}{tok})"
        status = activity_status or current_text or "working..."
        display = (status[-1900:] if len(status) > 1900 else status).strip()
        if display:
            return f"{status_line}\n{redact_secrets(display)}"
        return status_line

    def _edit(force: bool = False):
        nonlocal last_edit_ts
        now = time.time()
        if not force and now - last_edit_ts < 1.0:
            return
        body = _build_status_body()
        edit_text(channel_id, msg_id, body)
        last_edit_ts = now

    def _heartbeat():
        while not _heartbeat_stop.wait(timeout=1):
            _edit(force=True)

    def _on_text(text: str):
        nonlocal current_text
        current_text = text
        _edit()

    def _on_activity(status: str):
        nonlocal activity_status
        activity_status = status
        _edit()

    claude_semaphore.acquire()
    heartbeat_thread = threading.Thread(target=_heartbeat, daemon=True)
    heartbeat_thread.start()
    _edit(force=True)
    try:
        env = claude_env(workspace=workspace)

        for attempt in range(1, MAX_RETRIES + 1):
            current_text = ""
            activity_status = ""
            last_edit_ts = 0.0

            returncode, result_text, raw_lines, stderr_output = run_claude_once(
                cmd, env, on_text=_on_text, on_activity=_on_activity, cwd=cwd,
            )

            if result_text.strip():
                current_text = result_text
                break

            if returncode != 0:
                stderr_snip = redact_secrets((stderr_output or "").strip())[:200]
                if attempt < MAX_RETRIES:
                    delay = min(2 ** attempt, 30)
                    err_detail = f": {stderr_snip}" if stderr_snip else ""
                    activity_status = f"⚠ Error (rc={returncode}{err_detail}), retrying in {delay}s... (attempt {attempt}/{MAX_RETRIES})"
                    _edit(force=True)
                    time.sleep(delay)
                    continue
                else:
                    log(f"run_agent_streaming: all {MAX_RETRIES} retries exhausted, rc={returncode}")
            break

        _heartbeat_stop.set()
        _edit(force=True)

        if not current_text.strip():
            stderr_snip = redact_secrets((stderr_output or "").strip())[:300]
            elapsed_s = time.monotonic() - t0
            inp, out = get_tokens()
            tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
            status_line = f"Arbos: ({fmt_duration(elapsed_s)}{tok})"
            if stderr_snip:
                edit_text(channel_id, msg_id, f"{status_line}\n(no output)\n⚠ {stderr_snip}")
            else:
                edit_text(channel_id, msg_id, f"{status_line}\n(no output)")

    except Exception as e:
        _heartbeat_stop.set()
        elapsed_s = time.monotonic() - t0
        inp, out = get_tokens()
        tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
        status_line = f"Arbos: ({fmt_duration(elapsed_s)}{tok})"
        edit_text(channel_id, msg_id, f"{status_line}\nError: {str(e)[:300]}")
    finally:
        _heartbeat_stop.set()
        claude_semaphore.release()

    return current_text
