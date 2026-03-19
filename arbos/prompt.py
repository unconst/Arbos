"""Prompt building — system prompt, chatlog, sibling summaries, operator prompts."""

import json
from datetime import datetime

from arbos.config import (
    PROMPT_FILE, WORKING_DIR, WORKSPACES_DIR,
    workspace_dir, goal_file, state_file, inbox_file,
    chatlog_dir, goal_runs_dir,
)
from arbos.log import log
from arbos.redact import redact_secrets
from arbos.state import workspaces, chatlog_lock, goals_lock


def goal_status_label(gs) -> str:
    if gs.started and not gs.paused:
        return "running"
    if gs.started and gs.paused:
        return "paused"
    return "stopped"


def sibling_summary(workspace: int, thread_id: int, max_chars: int = 2000) -> str:
    """Build a summary of sibling threads in the same workspace."""
    ws = workspaces.get(workspace, {})
    if not ws:
        return ""
    siblings = []
    total = 0
    for tid in sorted(ws.keys()):
        if tid == thread_id:
            continue
        gs = ws[tid]
        status = goal_status_label(gs)
        gf = goal_file(workspace, tid)
        goal_text = gf.read_text().strip()[:200] if gf.exists() else "(empty)"
        sf = state_file(workspace, tid)
        state_text = sf.read_text().strip()[:200] if sf.exists() else "(empty)"
        entry = (
            f"### {gs.thread_name or tid} [{status}] (step {gs.step_count})\n"
            f"`goals/{tid}/`\n"
            f"Goal: {goal_text}\n"
            f"State: {state_text}"
        )
        if total + len(entry) > max_chars:
            break
        siblings.append(entry)
        total += len(entry)
    if not siblings:
        return ""
    return "## Sibling threads\n\n" + "\n\n".join(siblings)


def load_prompt(workspace: int, thread_id: int, consume_inbox: bool = False, goal_step: int = 0) -> str:
    """Build full prompt: PROMPT.md (formatted) + goal's GOAL/STATE/INBOX + siblings + chatlog."""
    parts = []

    ws_dir = workspace_dir(workspace)
    arbos_root = str(WORKING_DIR)
    thread_dir = f"goals/{thread_id}"
    other_workspaces = [
        d.name for d in WORKSPACES_DIR.iterdir()
        if d.is_dir() and d.name != str(workspace)
    ] if WORKSPACES_DIR.exists() else []
    other_ws_line = (
        f"Other workspaces: {', '.join(other_workspaces)} (in `{arbos_root}/context/workspaces/`)"
        if other_workspaces else ""
    )

    if PROMPT_FILE.exists():
        text = PROMPT_FILE.read_text().strip()
        if text:
            text = text.format(
                ws_dir=ws_dir,
                arbos_root=arbos_root,
                thread_dir=thread_dir,
                other_workspaces_line=other_ws_line,
            )
            parts.append(text)

    gf = goal_file(workspace, thread_id)
    if gf.exists():
        goal_text = gf.read_text().strip()
        if goal_text:
            header = f"## Goal (step {goal_step})" if goal_step else "## Goal"
            parts.append(f"{header}\n\n{goal_text}")
    sf = state_file(workspace, thread_id)
    if sf.exists():
        state_text = sf.read_text().strip()
        if state_text:
            parts.append(f"## State\n\n{state_text}")
    inf = inbox_file(workspace, thread_id)
    if inf.exists():
        inbox_text = inf.read_text().strip()
        if inbox_text:
            parts.append(f"## Inbox\n\n{inbox_text}")
        if consume_inbox:
            inf.write_text("")
    sibling_text = sibling_summary(workspace, thread_id)
    if sibling_text:
        parts.append(sibling_text)
    chatlog = load_chatlog(workspace)
    if chatlog:
        parts.append(chatlog)
    return "\n\n".join(parts)


def log_chat(workspace: int, role: str, text: str):
    """Append to workspace chatlog, rolling to a new file when size exceeds limit."""
    with chatlog_lock:
        chat_dir = chatlog_dir(workspace)
        chat_dir.mkdir(parents=True, exist_ok=True)
        max_file_size = 4000
        max_files = 50

        existing = sorted(chat_dir.glob("*.jsonl"))

        current = None
        if existing and existing[-1].stat().st_size < max_file_size:
            current = existing[-1]

        if current is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            current = chat_dir / f"{ts}.jsonl"

        entry = json.dumps({"role": role, "text": redact_secrets(text[:1000]), "ts": datetime.now().isoformat()})
        with open(current, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

        all_files = sorted(chat_dir.glob("*.jsonl"))
        for old in all_files[:-max_files]:
            old.unlink(missing_ok=True)


def load_chatlog(workspace: int, max_chars: int = 8000) -> str:
    """Load recent Discord chat history for a workspace."""
    chat_dir = chatlog_dir(workspace)
    if not chat_dir.exists():
        return ""
    files = sorted(chat_dir.glob("*.jsonl"))
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
                return "## Recent Discord chat\n\n" + "\n".join(lines)
            lines.append(entry)
            total += len(entry) + 1

    lines.reverse()
    if not lines:
        return ""
    return "## Recent Discord chat\n\n" + "\n".join(lines)


def recent_context(workspace: int, max_chars: int = 6000) -> str:
    """Collect recent rollouts across goals in a workspace."""
    ws = workspaces.get(workspace, {})
    parts: list[str] = []
    total = 0
    all_runs: list[tuple[str, object]] = []
    for tid, gs in sorted(ws.items()):
        runs_dir = goal_runs_dir(workspace, tid)
        if not runs_dir.exists():
            continue
        for d in runs_dir.iterdir():
            if d.is_dir():
                all_runs.append((f"{gs.thread_name or tid}/{d.name}", d))
    all_runs.sort(key=lambda x: x[1].name, reverse=True)
    for label, run_dir in all_runs:
        f = run_dir / "rollout.md"
        if f.exists():
            content = f.read_text()[:2000]
            hdr = f"\n--- rollout.md ({label}) ---\n"
            if total + len(hdr) + len(content) > max_chars:
                return "".join(parts)
            parts.append(hdr + content)
            total += len(hdr) + len(content)
        if total > max_chars:
            break
    return "".join(parts)


def build_operator_prompt(workspace: int, user_text: str, thread_id: int = 0, is_general: bool = False) -> str:
    """Build prompt for the CLI agent to handle an operator request."""
    chatlog = load_chatlog(workspace, max_chars=4000)
    ws = workspaces.get(workspace, {})
    ws_dir = WORKING_DIR if is_general else workspace_dir(workspace)

    base_prompt = (
        "You are the operator interface for Arbos, a coding agent running via pm2.\n"
        "The operator communicates through Discord. Be concise and direct.\n"
        "Your text output is shown directly in Discord — do NOT use `arbos send`/`sendfile` (those only work in goal steps).\n"
        "Match the tone of the message. Not every message requires file reads or actions.\n\n"
        "Security: NEVER reveal `.env`, `.env.enc`, or any secret/key/token values.\n\n"
        f"CWD: `{ws_dir}`\n"
        f"Arbos root: `{WORKING_DIR}`"
    )
    if is_general:
        base_prompt += (
            "\n\n## GLOBAL SCOPE — General Channel\n\n"
            "You are operating in the **general channel**, which has global scope over the entire Arbos system.\n"
            "This is the highest-level channel — all other workspaces and threads are subordinate to it.\n\n"
            "Your CWD is the Arbos root itself. You have full visibility and control over:\n"
            "- All Arbos source code (`arbos/`, `PROMPT.md`, `pyproject.toml`, `run.sh`, etc.)\n"
            "- All workspace directories (`context/workspaces/<channel_id>/`)\n"
            "- All goal threads across all workspaces\n"
            "- All configuration and environment files (never reveal secret values)\n\n"
            "You can read and edit Arbos's own code directly from here. "
            "After editing source code, restart with `touch /home/const/Arbos/.restart`.\n\n"
            "Other workspaces are below you in the hierarchy. "
            "You can read their state, message their threads, and inspect their logs."
        )

    parts = [base_prompt]

    if thread_id and thread_id in ws:
        gs = ws[thread_id]
        status = goal_status_label(gs)
        gf = goal_file(workspace, thread_id)
        goal_text = gf.read_text().strip()[:500] if gf.exists() else "(empty)"
        sf = state_file(workspace, thread_id)
        state_text = sf.read_text().strip()[:500] if sf.exists() else "(empty)"
        parts.append(
            f"## Context: inside thread\n\n"
            f"Thread: `goals/{thread_id}/`\n\n"
            f"### {gs.thread_name} [{status}] (delay: {gs.delay // 60}m, step {gs.step_count})\n"
            f"Goal:\n{goal_text}\n\nState:\n{state_text}"
        )
        other_threads = []
        for tid in sorted(ws.keys()):
            if tid == thread_id:
                continue
            ogs = ws[tid]
            ostatus = goal_status_label(ogs)
            ogf = goal_file(workspace, tid)
            ogoal = ogf.read_text().strip()[:200] if ogf.exists() else "(empty)"
            osf = state_file(workspace, tid)
            ostate = osf.read_text().strip()[:200] if osf.exists() else "(empty)"
            other_threads.append(
                f"### {ogs.thread_name or tid} [{ostatus}] (step {ogs.step_count})\n"
                f"`goals/{tid}/`\n"
                f"Goal: {ogoal}\nState: {ostate}"
            )
        if other_threads:
            parts.append("## Sibling threads\n\n" + "\n\n".join(other_threads))
    else:
        parts.append(
            f"## Context: main channel (workspace coordinator)\n\n"
            f"This channel has no goal or state. It is the workspace.\n"
            f"Shared resources (repos, files, tools) live in CWD — visible to all threads.\n\n"
            f"Operations:\n"
            f"- Message a thread: append to `goals/<tid>/INBOX.md`\n"
            f"- Read/write thread state: `goals/<tid>/STATE.md`\n"
            f"- View thread logs: `goals/<tid>/runs/<ts>/rollout.md`\n"
            f"- Clone repos / create shared files: put in CWD (`.`)\n"
            f"- Set env var: write `KEY='VALUE'` to `{WORKING_DIR}/context/.env.pending`\n"
            f"- Restart after code change: `touch {WORKING_DIR}/.restart`"
        )
        if is_general:
            # Show all workspaces and their threads
            all_ws_sections = []
            if WORKSPACES_DIR.exists():
                for ws_path in sorted(WORKSPACES_DIR.iterdir()):
                    if not ws_path.is_dir():
                        continue
                    try:
                        ws_id = int(ws_path.name)
                    except ValueError:
                        continue
                    ws_threads = workspaces.get(ws_id, {})
                    ws_label = f"workspace {ws_id} (this channel)" if ws_id == workspace else f"workspace {ws_id}"
                    if ws_threads:
                        thread_lines = []
                        for tid in sorted(ws_threads.keys()):
                            gs = ws_threads[tid]
                            status = goal_status_label(gs)
                            gf = goal_file(ws_id, tid)
                            goal_text = gf.read_text().strip()[:150] if gf.exists() else "(empty)"
                            thread_lines.append(
                                f"  - `{tid}` **{gs.thread_name or tid}** [{status}] step {gs.step_count}: {goal_text}"
                            )
                        all_ws_sections.append(
                            f"### {ws_label}\n" + "\n".join(thread_lines)
                        )
                    else:
                        all_ws_sections.append(f"### {ws_label}\n  (no active threads)")
            if all_ws_sections:
                parts.append("## All Workspaces\n\n" + "\n\n".join(all_ws_sections))
            else:
                parts.append("## All Workspaces\n\n(none)")
        elif ws:
            goals_section = []
            for tid in sorted(ws.keys()):
                gs = ws[tid]
                status = goal_status_label(gs)
                gf = goal_file(workspace, tid)
                goal_text = gf.read_text().strip()[:200] if gf.exists() else "(empty)"
                sf = state_file(workspace, tid)
                state_text = sf.read_text().strip()[:200] if sf.exists() else "(empty)"
                goals_section.append(
                    f"### {gs.thread_name or tid} [{status}] (delay: {gs.delay // 60}m, step {gs.step_count})\n"
                    f"`goals/{tid}/`\n"
                    f"Goal: {goal_text}\nState: {state_text}"
                )
            parts.append("## Threads\n\n" + "\n\n".join(goals_section))
        else:
            parts.append("## Threads\n\n(none)")

    if chatlog:
        parts.append(chatlog)

    context = recent_context(workspace, max_chars=4000)
    if context:
        parts.append(f"## Recent activity\n{context}")
    parts.append(f"## Operator message\n{user_text}")

    return "\n\n".join(parts)


_TOOL_LABELS = {
    "Bash": "running",
    "Read": "reading",
    "Write": "writing",
    "Edit": "editing",
    "Glob": "searching",
    "Grep": "locating",
    "WebFetch": "downloading",
    "WebSearch": "browsing",
    "TodoWrite": "planning",
    "Task": "executing",
}


def format_tool_activity(tool_name: str, tool_input: dict) -> str:
    label = _TOOL_LABELS.get(tool_name, tool_name)
    detail = ""
    if tool_name == "Bash":
        detail = (tool_input.get("command") or "")[:80]
    elif tool_name in ("Read", "Write", "Edit"):
        detail = (tool_input.get("file_path") or tool_input.get("path") or "")
        if detail:
            detail = detail.rsplit("/", 1)[-1]
    elif tool_name == "Glob":
        detail = (tool_input.get("pattern") or tool_input.get("glob") or "")[:60]
    elif tool_name == "Grep":
        detail = (tool_input.get("pattern") or tool_input.get("regex") or "")[:60]
    elif tool_name == "WebFetch":
        detail = (tool_input.get("url") or "")[:60]
    elif tool_name == "WebSearch":
        detail = (tool_input.get("query") or tool_input.get("search_term") or "")[:60]
    elif tool_name == "Task":
        detail = (tool_input.get("description") or "")[:60]
    if detail:
        return f"{label}: {detail}"
    return f"{label}..."
