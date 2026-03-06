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
AGENTS_META = WORKING_DIR / "agents.json"
CONTEXT_DIR = WORKING_DIR / "context"
CHATLOG_DIR = CONTEXT_DIR / "chat"
RESTART_FLAG = WORKING_DIR / ".restart"
CHAT_ID_FILE = WORKING_DIR / "chat_id.txt"

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


# ── Agent helpers ─────────────────────────────────────────────────────────────

def _agent_dir(aid: str) -> Path:
    d = CONTEXT_DIR / aid
    d.mkdir(parents=True, exist_ok=True)
    return d


def goal_file(aid: str) -> Path:
    return _agent_dir(aid) / "GOAL.md"


def state_file(aid: str) -> Path:
    return _agent_dir(aid) / "STATE.md"


def inbox_file(aid: str) -> Path:
    return _agent_dir(aid) / "INBOX.md"


def load_agents() -> dict:
    if AGENTS_META.exists():
        try:
            return json.loads(AGENTS_META.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_agents(data: dict):
    AGENTS_META.write_text(json.dumps(data, indent=2) + "\n")


# ── File helpers ─────────────────────────────────────────────────────────────

def load_prompt(agent_id: str | None = None, consume_inbox: bool = False) -> str:
    """Build full prompt: PROMPT + GOAL + STATE + INBOX + chatlog. Optionally clears INBOX."""
    parts = []
    if PROMPT_FILE.exists():
        text = PROMPT_FILE.read_text().strip()
        if text:
            parts.append(text)
    if agent_id:
        parts.append(
            f"### Active Agent: `{agent_id}`\n\n"
            f"Edit `context/{agent_id}/STATE.md` to record progress. "
            f"Do not edit GOAL.md."
        )
        gf = goal_file(agent_id)
        if gf.exists():
            goal_text = gf.read_text().strip()
            if goal_text:
                parts.append(f"## Goal\n\n{goal_text}")
        sf = state_file(agent_id)
        if sf.exists():
            state_text = sf.read_text().strip()
            if state_text:
                parts.append(f"## State\n\n{state_text}")
        ibf = inbox_file(agent_id)
        if ibf.exists():
            inbox_text = ibf.read_text().strip()
            if inbox_text:
                parts.append(f"## Inbox\n\n{inbox_text}")
            if consume_inbox:
                ibf.write_text("")
    chatlog = load_chatlog()
    if chatlog:
        parts.append(chatlog)
    return "\n\n".join(parts)


def make_run_dir(agent_id: str) -> Path:
    agent_dir = CONTEXT_DIR / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = agent_dir / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def log_chat(role: str, text: str):
    """Append a message to the current chatlog file. Rolls to a new file when size exceeds limit."""
    CHATLOG_DIR.mkdir(parents=True, exist_ok=True)
    max_file_size = 4000
    max_files = 50

    existing = sorted(CHATLOG_DIR.glob("*.jsonl"))

    current: Path | None = None
    if existing and existing[-1].stat().st_size < max_file_size:
        current = existing[-1]

    if current is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        current = CHATLOG_DIR / f"{ts}.jsonl"

    entry = json.dumps({"role": role, "text": text[:1000], "ts": datetime.now().isoformat()})
    with open(current, "a", encoding="utf-8") as f:
        f.write(entry + "\n")

    # Prune old files
    all_files = sorted(CHATLOG_DIR.glob("*.jsonl"))
    for old in all_files[:-max_files]:
        old.unlink(missing_ok=True)


def load_chatlog(max_chars: int = 8000) -> str:
    """Load recent Telegram chat history for prompt injection."""
    if not CHATLOG_DIR.exists():
        return ""
    files = sorted(CHATLOG_DIR.glob("*.jsonl"))
    if not files:
        return ""

    lines: list[str] = []
    total = 0
    for f in reversed(files):
        for raw in reversed(f.read_text().strip().splitlines()):
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            entry = f"[{msg.get('ts', '?')[:16]}] {msg['role']}: {msg['text']}"
            if total + len(entry) > max_chars:
                lines.reverse()
                return "## Recent Telegram chat\n\n" + "\n".join(lines)
            lines.append(entry)
            total += len(entry) + 1

    lines.reverse()
    if not lines:
        return ""
    return "## Recent Telegram chat\n\n" + "\n".join(lines)


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


def run_step(prompt: str, agent_id: str) -> bool:
    global _log_fh

    run_dir = make_run_dir(agent_id)
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


# ── Agent loop (always-on, runs as daemon thread) ───────────────────────────

def agent_loop():
    step_count = 0

    while True:
        agents = load_agents()
        if not agents:
            time.sleep(5)
            continue

        now = time.time()
        best_id = None
        best_overdue = -1.0
        min_wait = float("inf")

        for aid, meta in agents.items():
            delay = meta.get("delay", 60)
            failures = meta.get("failures", 0)
            effective_delay = delay + min(2 ** failures, 120) * (1 if failures else 0)
            elapsed = now - meta.get("last_run", 0)
            remaining = effective_delay - elapsed
            if remaining <= 0 and elapsed > best_overdue:
                best_id = aid
                best_overdue = elapsed
            elif remaining > 0:
                min_wait = min(min_wait, remaining)

        if best_id is None:
            time.sleep(min(min_wait, 10))
            continue

        step_count += 1
        header(f"Step {step_count}  agent={best_id}")

        prompt = load_prompt(best_id, consume_inbox=True)
        if not prompt:
            time.sleep(5)
            continue

        dim(f"prompt={len(prompt)} chars")

        success = run_step(prompt, best_id)

        agents = load_agents()
        if best_id in agents:
            agents[best_id]["last_run"] = time.time()
            if success:
                agents[best_id]["failures"] = 0
            else:
                agents[best_id]["failures"] = agents[best_id].get("failures", 0) + 1
                err(f"Agent {best_id} failure #{agents[best_id]['failures']}")
            save_agents(agents)


# ── Telegram streaming agent ────────────────────────────────────────────────

def _recent_context(max_chars: int = 6000) -> str:
    if not CONTEXT_DIR.exists():
        return ""
    all_runs: list[tuple[str, Path]] = []
    for agent_dir in CONTEXT_DIR.iterdir():
        if not agent_dir.is_dir() or agent_dir.name == "chat":
            continue
        for run_dir in agent_dir.iterdir():
            if run_dir.is_dir():
                all_runs.append((agent_dir.name, run_dir))
    all_runs.sort(key=lambda x: x[1].name, reverse=True)

    parts: list[str] = []
    total = 0
    for aid, run_dir in all_runs:
        for name in ("plan.md", "rollout.md"):
            f = run_dir / name
            if f.exists():
                content = f.read_text()[:2000]
                hdr = f"\n--- {name} (agent={aid} {run_dir.name}) ---\n"
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
                    current_text = result_text

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

def _build_agent_status_prompt(aid: str) -> str:
    """Build a prompt for the CLI agent to analyze an agent's current state."""
    agents = load_agents()
    meta = agents.get(aid, {})
    agent_dir = CONTEXT_DIR / aid
    gf = goal_file(aid)
    sf = state_file(aid)
    goal_text = gf.read_text().strip() if gf.exists() else "(no GOAL.md)"
    state_text = sf.read_text().strip() if sf.exists() else "(no STATE.md)"

    run_dirs = sorted(
        [d for d in agent_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name, reverse=True,
    ) if agent_dir.exists() else []

    run_summaries: list[str] = []
    for rd in run_dirs[:5]:
        files = {f.name: f for f in rd.iterdir() if f.is_file()}
        entry = f"### Run: {rd.name}\n"
        entry += f"Files: {', '.join(sorted(files.keys()))}\n"
        for name in ("logs.txt", "plan.md", "rollout.md"):
            if name in files:
                content = files[name].read_text()
                tail = content[-1500:] if len(content) > 1500 else content
                entry += f"\n**{name}** (last {len(tail)} of {len(content)} chars):\n```\n{tail}\n```\n"
        run_summaries.append(entry)

    last_run_ts = meta.get("last_run", 0)
    last_run_str = datetime.fromtimestamp(last_run_ts).strftime("%Y-%m-%d %H:%M:%S") if last_run_ts else "never"
    failures = meta.get("failures", 0)
    delay = meta.get("delay", "?")

    return (
        f"Give a concise status report for Arbos agent `{aid}`.\n\n"
        f"## Agent metadata\n"
        f"- Delay: {delay}s\n"
        f"- Last run: {last_run_str}\n"
        f"- Consecutive failures: {failures}\n"
        f"- Total runs on disk: {len(run_dirs)}\n\n"
        f"## GOAL.md\n```\n{goal_text}\n```\n\n"
        f"## STATE.md\n```\n{state_text}\n```\n\n"
        f"## Recent runs (newest first, up to 5)\n\n"
        + ("\n".join(run_summaries) if run_summaries else "(no runs yet)\n")
        + "\n## What to report\n"
        "Synthesize the above into a short status report covering:\n"
        "1. **What the agent is** — one-line summary from GOAL.md\n"
        "2. **Current state** — is it actively running, idle, or stuck in a failure loop?\n"
        "3. **Last run outcome** — did it succeed or fail? What did it do? (cite the plan/rollout)\n"
        "4. **Progress & trajectory** — what has it accomplished across recent runs? Is it making forward progress or looping?\n"
        "5. **Failures** — if any, what's going wrong? Include relevant error snippets.\n"
        "6. **Next expected action** — based on its notes and trajectory, what will it likely do next?\n\n"
        "Keep it under 300 words. Use bullet points. Be direct — the operator wants a fast read, not a novel."
    )


def _handle_agent_status(bot, message, aid: str):
    """Spawn a CLI agent to analyze an agent's run state and stream the result to Telegram."""
    agents = load_agents()
    if aid not in agents and not goal_file(aid).exists():
        bot.reply_to(message, f"Agent `{aid}` not found.")
        return

    def _run():
        prompt = _build_agent_status_prompt(aid)
        log_chat("user", f"/status {aid}")
        response = run_agent_streaming(bot, prompt, message.chat.id, execute=False)
        log_chat("bot", f"Status for {aid}: {response[:500]}")

    threading.Thread(target=_run, daemon=True).start()


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

    # ── /agent — manage agents ───────────────────────────────────────────

    @bot.message_handler(commands=["agent"])
    def handle_agent(message):
        _save_chat_id(message.chat.id)
        log_chat("user", message.text)
        text = message.text.replace("/agent", "", 1).strip()
        agents = load_agents()

        if not text:
            if not agents:
                bot.reply_to(message, "No agents.\n\nUsage: /agent <uuid> <delay_seconds> <description>")
                return
            lines = []
            for aid, meta in agents.items():
                gf = goal_file(aid)
                preview = gf.read_text().strip()[:80] if gf.exists() else "(no goal)"
                lines.append(f"• {aid}  delay={meta.get('delay', '?')}s  {preview}")
            bot.reply_to(message, "\n".join(lines))
            return

        parts = text.split(None, 2)
        aid = parts[0]

        if len(parts) == 1:
            gf = goal_file(aid)
            if aid not in agents and not gf.exists():
                bot.reply_to(message, f"Agent `{aid}` not found.")
                return
            goal = gf.read_text().strip() if gf.exists() else "(no goal)"
            sf = state_file(aid)
            state = sf.read_text().strip() if sf.exists() else "(no state)"
            meta = agents.get(aid, {})
            bot.reply_to(message, f"Agent {aid}  delay={meta.get('delay', '?')}s\n\n📎 Goal:\n{goal[:1500]}\n\n📌 State:\n{state[:1500]}")
            return

        try:
            delay = int(parts[1])
        except ValueError:
            bot.reply_to(message, "Usage: /agent <uuid> <delay_seconds> <description>")
            return

        description = parts[2] if len(parts) > 2 else ""
        agents[aid] = {"delay": delay, "last_run": agents.get(aid, {}).get("last_run", 0), "failures": 0}
        save_agents(agents)
        goal_file(aid).write_text(description + "\n")
        bot.reply_to(message, f"✅ Agent `{aid}` set  delay={delay}s  ({len(description)} chars)")
        log_chat("bot", f"Agent set: {aid} delay={delay}s {description[:200]}")

    # ── /delete — remove an agent ────────────────────────────────────────

    @bot.message_handler(commands=["delete"])
    def handle_delete(message):
        _save_chat_id(message.chat.id)
        log_chat("user", message.text)
        aid = message.text.replace("/delete", "", 1).strip()
        if not aid:
            bot.reply_to(message, "Usage: /delete <uuid>")
            return
        agents = load_agents()
        if aid not in agents:
            bot.reply_to(message, f"Agent `{aid}` not found.")
            return
        agents.pop(aid)
        save_agents(agents)
        for f in (goal_file(aid), state_file(aid), inbox_file(aid)):
            if f.exists():
                f.unlink()
        bot.reply_to(message, f"🗑 Agent `{aid}` deleted.")
        log_chat("bot", f"Agent deleted: {aid}")

    # ── /message — send a message to an agent's inbox ──────────────────

    @bot.message_handler(commands=["message"])
    def handle_message(message):
        _save_chat_id(message.chat.id)
        log_chat("user", message.text)
        text = message.text.replace("/message", "", 1).strip()
        parts = text.split(None, 1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /message <uuid> <text>")
            return
        aid, content = parts
        agents = load_agents()
        if aid not in agents:
            bot.reply_to(message, f"Agent `{aid}` not found.")
            return
        ibf = inbox_file(aid)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(ibf, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {content}\n")
        bot.reply_to(message, f"✅ Message queued for {aid} ({len(content)} chars)")
        log_chat("bot", f"Message to {aid}: {content[:200]}")

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

        parts = rest.strip().split(None, 1)
        value = parts[0] if parts else ""
        description = parts[1] if len(parts) > 1 else None

        env_file = WORKING_DIR / ".env"
        lines = []
        replaced = False
        if env_file.exists():
            prev_was_comment_for_key = False
            for line in env_file.read_text().splitlines():
                if line.startswith("#") and not prev_was_comment_for_key:
                    prev_was_comment_for_key = True
                    lines.append(line)
                    continue
                if line.startswith(f"{key}="):
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
        log_chat("user", message.text)
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
            bot.send_message(message.chat.id, f"🔧 Adapting: {prompt[:200]}")
            response = run_agent_streaming(bot, full_prompt, message.chat.id, execute=True)
            log_chat("bot", f"Adapted: {response[:500]}")

            bot.send_message(message.chat.id, "✅ Code updated. Restarting…")
            RESTART_FLAG.touch()

        threading.Thread(target=_run, daemon=True).start()

    # ── /status — show current state ─────────────────────────────────────

    @bot.message_handler(commands=["status"])
    def handle_status(message):
        _save_chat_id(message.chat.id)
        text = message.text.replace("/status", "", 1).strip()

        if text:
            _handle_agent_status(bot, message, text)
            return

        prompt_ok = "✅" if (PROMPT_FILE.exists() and PROMPT_FILE.read_text().strip()) else "❌"
        agents = load_agents()
        agent_lines = []
        for aid, meta in agents.items():
            agent_lines.append(f"  • {aid}  delay={meta.get('delay', '?')}s")

        bot.reply_to(message, (
            f"Prompt: {prompt_ok}\n"
            f"Agents: {len(agents)}\n"
            + ("\n".join(agent_lines) + "\n" if agent_lines else "")
            + "\nCommands:\n"
            "/prompt <text> — set system prompt\n"
            "/agent <uuid> <delay_s> <desc> — add/view agent\n"
            "/delete <uuid> — remove agent\n"
            "/message <uuid> <text> — send to agent inbox\n"
            "/env KEY=VALUE — set env variable\n"
            "/adapt <desc> — modify code & restart\n"
            "/logs [agent] [N] — tail logs\n"
            "/status [uuid] — overview or deep agent status"
        ))

    # ── /logs — tail recent logs ────────────────────────────────────────

    @bot.message_handler(commands=["logs"])
    def handle_logs(message):
        _save_chat_id(message.chat.id)
        text = message.text.replace("/logs", "", 1).strip()
        parts = text.split()
        agent_filter = None
        num_lines = 50

        for p in parts:
            if p.isdigit():
                num_lines = min(int(p), 200)
            else:
                agent_filter = p

        log_content = ""
        if CONTEXT_DIR.exists():
            if agent_filter:
                search_dirs = [CONTEXT_DIR / agent_filter]
            else:
                search_dirs = sorted(
                    [d for d in CONTEXT_DIR.iterdir() if d.is_dir() and d.name != "chat"],
                    reverse=True,
                )

            for agent_dir in search_dirs:
                if not agent_dir.is_dir():
                    continue
                for run_dir in sorted(agent_dir.iterdir(), reverse=True):
                    if not run_dir.is_dir():
                        continue
                    log_file = run_dir / "logs.txt"
                    if log_file.exists() and log_file.stat().st_size > 0:
                        lines = log_file.read_text().splitlines()
                        tail = lines[-num_lines:]
                        log_content = f"📄 {log_file}\n\n" + "\n".join(tail)
                        break
                if log_content:
                    break

        if not log_content:
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
        log_chat("user", message.text)
        ask_prompt = _build_ask_prompt(message.text)

        def _run():
            response = run_agent_streaming(bot, ask_prompt, message.chat.id, execute=False)
            log_chat("bot", response[:1000])

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
    dim(f"agents   {AGENTS_META}")
    dim(f"workdir  {WORKING_DIR}")
    dim(f"context  {CONTEXT_DIR}")

    threading.Thread(target=agent_loop, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()

    while True:
        if RESTART_FLAG.exists():
            RESTART_FLAG.unlink()
            ok("Restart requested — exiting for pm2")
            sys.exit(0)
        time.sleep(1)


if __name__ == "__main__":
    main()
