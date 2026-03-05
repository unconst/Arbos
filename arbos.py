import json
import os
import subprocess
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

WORKING_DIR = Path(__file__).parent
load_dotenv(WORKING_DIR / ".env")

PROMPT_FILE = WORKING_DIR / "PROMPT.md"
GOAL_FILE = WORKING_DIR / "GOAL.md"
HISTORY_DIR = WORKING_DIR / "history"
RESTART_FLAG = WORKING_DIR / ".restart"
CHAT_ID_FILE = WORKING_DIR / "chat_id.txt"
PAUSE_FILE = WORKING_DIR / ".pause_until"

_agent_lock = threading.Lock()
_loop_stop = threading.Event()
_loop_thread: threading.Thread | None = None

# ── Colors ───────────────────────────────────────────────────────────────────

if sys.stdout.isatty():
    GREEN = '\033[0;32m'
    RED = '\033[0;31m'
    CYAN = '\033[0;36m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    NC = '\033[0m'
else:
    GREEN = RED = CYAN = BOLD = DIM = NC = ''

_log_fh = None
_log_lock = threading.Lock()


def _file_log(msg: str):
    with _log_lock:
        if _log_fh:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _log_fh.write(f"{ts}  {msg}\n")
            _log_fh.flush()


def ok(msg: str):
    print(f"  {GREEN}+{NC} {msg}", flush=True)
    _file_log(f"+  {msg}")


def err(msg: str):
    print(f"  {RED}x{NC} {msg}", flush=True)
    _file_log(f"x  {msg}")


def header(msg: str):
    print(f"\n  {BOLD}{msg}{NC}\n", flush=True)
    _file_log(f"── {msg}")


def dim(msg: str):
    print(f"  {DIM}{msg}{NC}", flush=True)
    _file_log(f"   {msg}")


def info(msg: str):
    print(f"  {CYAN}·{NC} {msg}", flush=True)
    _file_log(f"·  {msg}")


def banner():
    print(f"\n{CYAN}{BOLD}", end="")
    print("      _         _               ")
    print("     / \\   _ __| |__   ___  ___ ")
    print("    / _ \\ | '__| '_ \\ / _ \\/ __|")
    print("   / ___ \\| |  | |_) | (_) \\__ \\")
    print("  /_/   \\_\\_|  |_.__/ \\___/|___/")
    print(f"{NC}")


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


# ── File helpers ─────────────────────────────────────────────────────────────

def load_prompt() -> str:
    """Build full prompt from PROMPT.md + GOAL.md."""
    parts = []
    if PROMPT_FILE.exists():
        text = PROMPT_FILE.read_text().strip()
        if text:
            parts.append(text)
    if GOAL_FILE.exists():
        goal = GOAL_FILE.read_text().strip()
        if goal:
            parts.append(f"### Goal\n\n{goal}")
    return "\n\n".join(parts)


def make_run_dir() -> Path:
    HISTORY_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = HISTORY_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _describe_tool_call(tc: dict) -> str:
    for key, val in tc.items():
        if not isinstance(val, dict):
            continue
        args = val.get("args", {})
        if "path" in args:
            return f"{key}({args['path']})"
        if "command" in args:
            cmd = args["command"]
            return f"{key}({cmd[:80]}{'…' if len(cmd) > 80 else ''})"
        if "pattern" in args:
            return f"{key}(pattern={args['pattern']!r})"
        arg_summary = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:2])
        return f"{key}({arg_summary})"
    return str(list(tc.keys()))


# ── Agent runner (console, used by the main loop) ───────────────────────────

def run_agent(cmd: list[str], phase: str, output_file: Path) -> subprocess.CompletedProcess:
    stream_cmd = []
    for arg in cmd:
        if arg == "--output-format":
            stream_cmd.append(arg)
            continue
        if stream_cmd and stream_cmd[-1] == "--output-format":
            stream_cmd.append("stream-json")
            continue
        stream_cmd.append(arg)
    if "--stream-partial-output" not in stream_cmd:
        stream_cmd.insert(-1, "--stream-partial-output")

    api_key = os.environ.get("CURSOR_API_KEY")
    if api_key and "--api-key" not in stream_cmd:
        stream_cmd.insert(1, "--api-key")
        stream_cmd.insert(2, api_key)

    dim(f"running: {' '.join(stream_cmd[:6])}{'…' if len(stream_cmd) > 6 else ''}")
    t0 = time.monotonic()

    proc = subprocess.Popen(
        stream_cmd, cwd=WORKING_DIR,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    result_text = ""
    raw_lines: list[str] = []
    for line in iter(proc.stdout.readline, ""):
        raw_lines.append(line)
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = evt.get("type")
        subtype = evt.get("subtype")

        if etype == "tool_call" and subtype == "started":
            desc = _describe_tool_call(evt.get("tool_call", {}))
            info(f"{phase} tool call  {desc}")
        elif etype == "tool_call" and subtype == "completed":
            desc = _describe_tool_call(evt.get("tool_call", {}))
            ok(f"{phase} tool done  {desc}")
        elif etype == "assistant":
            text = ""
            for block in evt.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text", "")
            if text.strip():
                for tline in text.strip().splitlines():
                    dim(f"[{phase}] {tline}")
        elif etype == "result":
            result_text = evt.get("result", "")
            dur = evt.get("duration_ms", 0)
            usage = evt.get("usage", {})
            ok(
                f"{phase} done  {fmt_duration(dur / 1000)}"
                f"  in={usage.get('inputTokens', '?')}"
                f"  out={usage.get('outputTokens', '?')}"
            )

    stderr_output = proc.stderr.read() if proc.stderr else ""
    returncode = proc.wait()
    elapsed = time.monotonic() - t0
    output_file.write_text("".join(raw_lines))

    if returncode == 0:
        ok(f"{phase} finished  rc={returncode}  {fmt_duration(elapsed)}")
    else:
        err(f"{phase} finished  rc={returncode}  {fmt_duration(elapsed)}")
        if stderr_output.strip():
            for sline in stderr_output.strip().splitlines()[:20]:
                err(f"  stderr: {sline}")

    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode,
        stdout=result_text, stderr=stderr_output,
    )


def extract_text(result: subprocess.CompletedProcess) -> str:
    output = result.stdout or ""
    if not output.strip():
        output = result.stderr or "(no output)"
    return output


def run_step(prompt: str) -> bool:
    global _log_fh

    run_dir = make_run_dir()
    t0 = time.monotonic()

    log_file = run_dir / "logs.txt"
    with _log_lock:
        _log_fh = open(log_file, "a", encoding="utf-8")

    dim(f"run dir  {run_dir}")
    dim(f"log file {log_file}")

    header("Planning")

    preview = prompt[:200] + ("…" if len(prompt) > 200 else "")
    dim(f"prompt preview: {preview}")

    plan_result = run_agent(
        ["agent", "-p", "--force", "--mode", "plan", "--output-format", "text", prompt],
        phase="plan",
        output_file=run_dir / "plan_output.txt",
    )

    plan_text = extract_text(plan_result)
    (run_dir / "plan.md").write_text(plan_text)
    ok(f"Plan saved → {run_dir / 'plan.md'} ({len(plan_text)} chars)")

    if plan_result.returncode != 0:
        err(f"Plan phase exited with code {plan_result.returncode} — skipping execution")
        with _log_lock:
            _log_fh.close()
            _log_fh = None
        return False

    header("Execution")

    execute_prompt = (
        f"Here is the plan that was previously generated:\n\n"
        f"---\n{plan_text}\n---\n\n"
        f"Now implement this plan. The original request was:\n\n{prompt}"
    )
    dim(f"prompt size: {len(execute_prompt)} chars (plan={len(plan_text)} + original={len(prompt)})")

    exec_result = run_agent(
        ["agent", "-p", "--force", "--output-format", "text", execute_prompt],
        phase="exec",
        output_file=run_dir / "exec_output.txt",
    )

    exec_text = extract_text(exec_result)
    (run_dir / "rollout.md").write_text(exec_text)
    ok(f"Rollout saved → {run_dir / 'rollout.md'} ({len(exec_text)} chars)")

    elapsed = time.monotonic() - t0
    success = exec_result.returncode == 0
    if not success:
        err(f"Execution phase exited with code {exec_result.returncode}")
    else:
        ok("Run completed successfully")

    dim(f"total duration: {fmt_duration(elapsed)}")

    with _log_lock:
        _log_fh.close()
        _log_fh = None
    return success


# ── Agent loop (started/stopped via Telegram) ───────────────────────────────

def agent_loop(bot, chat_id: int):
    loop_count = 0
    consecutive_failures = 0

    while not _loop_stop.is_set():
        # Check for pause file
        if PAUSE_FILE.exists():
            try:
                resume_at = float(PAUSE_FILE.read_text().strip())
                remaining = resume_at - time.time()
                if remaining > 0:
                    mins = remaining / 60
                    if mins >= 60:
                        h, m = divmod(int(mins), 60)
                        human = f"{h}h {m}m"
                    else:
                        human = f"{mins:.0f}m"
                    info(f"Paused — resuming in {human}")
                    _loop_stop.wait(min(remaining, 30))
                    continue
                else:
                    PAUSE_FILE.unlink(missing_ok=True)
                    ok("Pause expired — resuming")
            except (ValueError, OSError):
                PAUSE_FILE.unlink(missing_ok=True)

        prompt = load_prompt()
        if not prompt:
            _loop_stop.wait(5)
            continue

        loop_count += 1
        header(f"Iteration {loop_count}")
        dim(f"prompt={len(prompt)} chars")

        with _agent_lock:
            success = run_step(prompt)

        if RESTART_FLAG.exists():
            RESTART_FLAG.unlink()
            ok("Restart requested — exiting for pm2")
            sys.exit(0)

        if success:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            delay = min(2 ** consecutive_failures, 120)
            err(f"Backing off for {delay}s after {consecutive_failures} consecutive failure(s)")
            _loop_stop.wait(delay)

    ok("Agent loop stopped")
    try:
        bot.send_message(chat_id, "⏹ Agent loop stopped.")
    except Exception:
        pass


# ── Telegram streaming agent ────────────────────────────────────────────────

def _recent_context(max_chars: int = 6000) -> str:
    if not HISTORY_DIR.exists():
        return ""
    parts: list[str] = []
    total = 0
    for run_dir in sorted(HISTORY_DIR.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        for name in ("plan.md", "rollout.md"):
            f = run_dir / name
            if f.exists():
                content = f.read_text()[:2000]
                hdr = f"\n--- {name} ({run_dir.name}) ---\n"
                if total + len(hdr) + len(content) > max_chars:
                    return "".join(parts)
                parts.append(hdr + content)
                total += len(hdr) + len(content)
        if total > max_chars:
            break
    return "".join(parts)


def _build_ask_prompt(question: str) -> str:
    prompt_md = load_prompt()[:2000]
    context = _recent_context()
    return (
        "You are answering a question about the Arbos agent.\n\n"
        f"System prompt:\n{prompt_md}\n\n"
        f"Recent activity:\n{context}\n\n"
        f"User question: {question}\n\n"
        "Answer concisely based on available information. "
        "If you need to check specific files (like scratch/ or history/), do so."
    )


def run_agent_streaming(bot, prompt: str, chat_id: int, *, execute: bool = False) -> str:
    """Run the Cursor agent CLI and stream output into a Telegram message."""
    cmd = [
        "agent", "-p", "--force",
        "--output-format", "stream-json",
        "--stream-partial-output",
    ]
    if not execute:
        cmd.extend(["--mode", "plan"])

    api_key = os.environ.get("CURSOR_API_KEY")
    if api_key:
        cmd.insert(1, "--api-key")
        cmd.insert(2, api_key)

    cmd.append(prompt)

    msg = bot.send_message(chat_id, "🤔")
    current_text = ""
    last_edit = 0.0

    def _edit(text: str, force: bool = False):
        nonlocal last_edit
        now = time.time()
        if not force and now - last_edit < 1.5:
            return
        display = text[-3800:] if len(text) > 3800 else text
        if not display.strip():
            return
        try:
            bot.edit_message_text(display, chat_id, msg.message_id)
            last_edit = now
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            cmd, cwd=WORKING_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

        for line in iter(proc.stdout.readline, ""):
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = evt.get("type")

            if etype == "assistant":
                for block in evt.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text", "")
                        if t:
                            current_text += t

            elif etype == "tool_call" and evt.get("subtype") == "started":
                tc = evt.get("tool_call", {})
                for key, val in tc.items():
                    if isinstance(val, dict):
                        args = val.get("args", {})
                        if "command" in args:
                            current_text += f"\n🔧 {args['command'][:80]}\n"
                        elif "path" in args:
                            current_text += f"\n🔧 {key}({args['path']})\n"
                        break

            elif etype == "result":
                result_text = evt.get("result", "")
                if result_text.strip():
                    current_text += f"\n{result_text}"

            _edit(current_text)

        proc.wait()
        _edit(current_text, force=True)

        if not current_text.strip():
            try:
                bot.edit_message_text("(no output)", chat_id, msg.message_id)
            except Exception:
                pass

    except Exception as e:
        try:
            bot.edit_message_text(f"Error: {str(e)[:300]}", chat_id, msg.message_id)
        except Exception:
            pass

    return current_text


# ── Telegram bot ─────────────────────────────────────────────────────────────

def run_bot():
    """Run the Telegram bot. Blocks forever (with auto-reconnect)."""
    token = os.getenv("TAU_BOT_TOKEN")
    if not token:
        err("TAU_BOT_TOKEN not set — add it to .env and restart")
        err("  Get a token from @BotFather on Telegram")
        sys.exit(1)

    import telebot
    bot = telebot.TeleBot(token)

    def _save_chat_id(chat_id: int):
        CHAT_ID_FILE.write_text(str(chat_id))

    def _is_loop_running() -> bool:
        return _loop_thread is not None and _loop_thread.is_alive()

    # ── /start — start the agent loop ────────────────────────────────────

    @bot.message_handler(commands=["start"])
    def handle_start(message):
        global _loop_thread
        _save_chat_id(message.chat.id)

        if _is_loop_running():
            bot.reply_to(message, "🟢 Already running. Use /stop first.")
            return

        prompt = load_prompt()
        if not prompt:
            missing = []
            if not (PROMPT_FILE.exists() and PROMPT_FILE.read_text().strip()):
                missing.append("/prompt <text>")
            if not (GOAL_FILE.exists() and GOAL_FILE.read_text().strip()):
                missing.append("/goal <text>")
            bot.reply_to(message, "Set up first:\n" + "\n".join(f"  • {m}" for m in missing))
            return

        _loop_stop.clear()
        _loop_thread = threading.Thread(
            target=agent_loop, args=(bot, message.chat.id), daemon=True,
        )
        _loop_thread.start()
        bot.reply_to(message, "▶️ Agent loop started.")

    # ── /stop — stop the agent loop ──────────────────────────────────────

    @bot.message_handler(commands=["stop"])
    def handle_stop(message):
        _save_chat_id(message.chat.id)
        if not _is_loop_running():
            bot.reply_to(message, "Not running.")
            return
        _loop_stop.set()
        bot.reply_to(message, "⏹ Stopping after current step finishes…")

    # ── /prompt — replace PROMPT.md ──────────────────────────────────────

    @bot.message_handler(commands=["prompt"])
    def handle_prompt(message):
        _save_chat_id(message.chat.id)
        text = message.text.replace("/prompt", "", 1).strip()
        if not text:
            current = PROMPT_FILE.read_text().strip() if PROMPT_FILE.exists() else "(empty)"
            bot.reply_to(message, f"Current prompt:\n\n{current[:3500]}")
            return
        PROMPT_FILE.write_text(text)
        bot.reply_to(message, f"✅ Prompt set ({len(text)} chars)")

    # ── /goal — replace GOAL.md ──────────────────────────────────────────

    @bot.message_handler(commands=["goal"])
    def handle_goal(message):
        _save_chat_id(message.chat.id)
        text = message.text.replace("/goal", "", 1).strip()
        if not text:
            current = GOAL_FILE.read_text().strip() if GOAL_FILE.exists() else "(empty)"
            bot.reply_to(message, f"Current goal:\n\n{current[:3500]}")
            return
        GOAL_FILE.write_text(text)
        bot.reply_to(message, f"✅ Goal set ({len(text)} chars)")

    # ── /env — add/update .env variable ──────────────────────────────────

    @bot.message_handler(commands=["env"])
    def handle_env(message):
        _save_chat_id(message.chat.id)
        text = message.text.replace("/env", "", 1).strip()
        if not text or "=" not in text:
            bot.reply_to(message, "Usage: /env KEY=VALUE [description]")
            return

        key, _, rest = text.partition("=")
        key = key.strip()

        # Split VALUE from optional description at first whitespace after value
        # Handles: KEY=VALUE description of this key
        parts = rest.strip().split(None, 1)
        value = parts[0] if parts else ""
        description = parts[1] if len(parts) > 1 else None

        env_file = WORKING_DIR / ".env"
        lines = []
        replaced = False
        if env_file.exists():
            # When replacing, also remove the old comment line above it
            prev_was_comment_for_key = False
            for line in env_file.read_text().splitlines():
                if line.startswith("#") and not prev_was_comment_for_key:
                    # Check if next line is our key (peek ahead handled below)
                    prev_was_comment_for_key = True
                    lines.append(line)
                    continue
                if line.startswith(f"{key}="):
                    # Remove previous comment for this key if present
                    if prev_was_comment_for_key and lines:
                        lines.pop()
                    if description:
                        lines.append(f"# {description}")
                    lines.append(f"{key}={value}")
                    replaced = True
                else:
                    lines.append(line)
                prev_was_comment_for_key = False
        if not replaced:
            if description:
                lines.append(f"# {description}")
            lines.append(f"{key}={value}")
        env_file.write_text("\n".join(lines) + "\n")
        os.environ[key] = value
        reply = f"✅ {key} set"
        if description:
            reply += f" ({description})"
        bot.reply_to(message, reply)

    # ── /adapt — modify code and restart ─────────────────────────────────

    @bot.message_handler(commands=["adapt"])
    def handle_adapt(message):
        _save_chat_id(message.chat.id)
        prompt = message.text.replace("/adapt", "", 1).strip()
        if not prompt:
            bot.reply_to(message, "Usage: /adapt <description of changes>")
            return

        full_prompt = (
            "You are modifying the Arbos agent codebase.\n"
            f"The user wants you to: {prompt}\n\n"
            "Make the changes directly to the code files in the project."
        )

        def _run():
            if _agent_lock.locked():
                bot.send_message(message.chat.id, "⏳ Waiting for current agent step to finish…")

            bot.send_message(message.chat.id, f"🔧 Adapting: {prompt[:200]}")
            with _agent_lock:
                run_agent_streaming(bot, full_prompt, message.chat.id, execute=True)

            bot.send_message(message.chat.id, "✅ Code updated. Restarting…")
            RESTART_FLAG.touch()

        threading.Thread(target=_run, daemon=True).start()

    # ── /status — show current state ─────────────────────────────────────

    @bot.message_handler(commands=["status"])
    def handle_status(message):
        _save_chat_id(message.chat.id)
        running = "🟢 Running" if _is_loop_running() else "⏹ Stopped"
        prompt_ok = "✅" if (PROMPT_FILE.exists() and PROMPT_FILE.read_text().strip()) else "❌"
        goal_ok = "✅" if (GOAL_FILE.exists() and GOAL_FILE.read_text().strip()) else "❌"

        bot.reply_to(message, (
            f"Loop: {running}\n"
            f"Prompt: {prompt_ok}\n"
            f"Goal: {goal_ok}\n\n"
            "Commands:\n"
            "/prompt <text> — set system prompt\n"
            "/goal <text> — set goal\n"
            "/env KEY=VALUE — set env variable\n"
            "/start — start agent loop\n"
            "/stop — stop agent loop\n"
            "/adapt <desc> — modify code & restart\n"
            "/logs [N] — tail last N log lines (default 50)\n"
            "/status — this message"
        ))

    # ── /logs — tail recent logs ────────────────────────────────────────

    @bot.message_handler(commands=["logs"])
    def handle_logs(message):
        _save_chat_id(message.chat.id)
        text = message.text.replace("/logs", "", 1).strip()
        num_lines = 50
        if text.isdigit():
            num_lines = min(int(text), 200)

        # Find the most recent history dir with a logs.txt
        log_content = ""
        if HISTORY_DIR.exists():
            for run_dir in sorted(HISTORY_DIR.iterdir(), reverse=True):
                log_file = run_dir / "logs.txt"
                if log_file.exists() and log_file.stat().st_size > 0:
                    lines = log_file.read_text().splitlines()
                    tail = lines[-num_lines:]
                    log_content = f"📄 {log_file}\n\n" + "\n".join(tail)
                    break

        if not log_content:
            # Fall back to pm2 log file
            pm2_log = WORKING_DIR / "logs" / "arbos.log"
            if pm2_log.exists():
                lines = pm2_log.read_text().splitlines()
                tail = lines[-num_lines:]
                log_content = f"📄 {pm2_log}\n\n" + "\n".join(tail)

        if not log_content:
            bot.reply_to(message, "No logs found.")
            return

        if len(log_content) > 4000:
            log_content = log_content[-4000:]
        bot.reply_to(message, log_content)

    # ── free text — ask the agent (runs concurrently, no lock) ──────────

    @bot.message_handler(func=lambda m: True)
    def handle_question(message):
        _save_chat_id(message.chat.id)
        ask_prompt = _build_ask_prompt(message.text)

        def _run():
            run_agent_streaming(bot, ask_prompt, message.chat.id, execute=False)

        threading.Thread(target=_run, daemon=True).start()

    # ── start polling with auto-reconnect ────────────────────────────────

    ok("Telegram bot started — waiting for commands")
    while True:
        try:
            bot.infinity_polling()
        except Exception as e:
            err(f"Bot polling error: {str(e)[:80]}, reconnecting in 5s…")
            time.sleep(5)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    banner()
    header("Arbos")

    dim(f"prompt   {PROMPT_FILE}")
    dim(f"goal     {GOAL_FILE}")
    dim(f"workdir  {WORKING_DIR}")
    dim(f"history  {HISTORY_DIR}")

    # Start bot in a background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Main thread: watch for .restart flag (touched by /adapt)
    while True:
        if RESTART_FLAG.exists():
            RESTART_FLAG.unlink()
            ok("Restart requested — exiting for pm2 to restart with updated code")
            sys.exit(0)
        time.sleep(1)


if __name__ == "__main__":
    main()
