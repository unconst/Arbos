"""Goal persistence, lifecycle, and the agent step loop."""

import hashlib
import json
import os
import threading
import time
from datetime import datetime

import requests

from arbos.config import (
    WORKSPACES_DIR, PROVIDER, CLAUDE_MODEL, LLM_API_KEY, LLM_BASE_URL,
    CHUTES_BASE_URL, CHUTES_ROUTING_BOT, CHUTES_API_KEY, CLAUDE_TIMEOUT,
    workspace_dir, goals_json, goal_file, state_file, goal_runs_dir,
)
from arbos.log import log
from arbos.prompt import load_prompt, goal_status_label
from arbos.runner import run_step
from arbos.discord_api import send_new
from arbos.state import GoalState, workspaces, goals_lock, shutdown
import arbos.state as state


def save_goals(workspace: int):
    """Persist goal metadata for a workspace. Caller must hold goals_lock."""
    ws = workspaces.get(workspace, {})
    data = {}
    for tid, gs in ws.items():
        data[str(tid)] = {
            "thread_name": gs.thread_name,
            "summary": gs.summary,
            "delay": gs.delay,
            "started": gs.started,
            "paused": gs.paused,
            "step_count": gs.step_count,
            "goal_hash": gs.goal_hash,
            "last_run": gs.last_run,
            "last_finished": gs.last_finished,
        }
    jf = goals_json(workspace)
    jf.parent.mkdir(parents=True, exist_ok=True)
    jf.write_text(json.dumps(data, indent=2))


def load_all_workspaces():
    """Load goal metadata from all workspace directories."""
    if not WORKSPACES_DIR.exists():
        return
    for ws_dir in WORKSPACES_DIR.iterdir():
        if not ws_dir.is_dir():
            continue
        try:
            workspace = int(ws_dir.name)
        except ValueError:
            continue
        jf = goals_json(workspace)
        if not jf.exists():
            continue
        try:
            data = json.loads(jf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ws = {}
        for tid_str, info in data.items():
            tid = int(tid_str)
            if not goal_file(workspace, tid).exists():
                continue
            ws[tid] = GoalState(
                thread_id=tid,
                workspace=workspace,
                thread_name=info.get("thread_name", ""),
                summary=info.get("summary", ""),
                delay=info.get("delay", 0),
                started=info.get("started", False),
                paused=info.get("paused", False),
                step_count=info.get("step_count", 0),
                goal_hash=info.get("goal_hash", ""),
                last_run=info.get("last_run", ""),
                last_finished=info.get("last_finished", ""),
            )
        if ws:
            workspaces[workspace] = ws


def format_last_time(iso_ts: str) -> str:
    if not iso_ts:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_ts)
        secs = (datetime.now() - dt).total_seconds()
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        return f"{int(secs / 86400)}d ago"
    except (ValueError, TypeError):
        return "unknown"


def summarize_goal(text: str) -> str:
    """Generate a one-line summary of a goal via LLM. Falls back to truncation."""
    try:
        if PROVIDER == "openrouter":
            url = f"{LLM_BASE_URL}/v1/chat/completions"
            headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
            model = CLAUDE_MODEL
        else:
            url = f"{CHUTES_BASE_URL}/chat/completions"
            headers = {
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            }
            model = CHUTES_ROUTING_BOT

        resp = requests.post(url, json={
            "model": model,
            "max_tokens": 50,
            "messages": [
                {"role": "system", "content": "Summarize the user's goal in 8 words or fewer. Reply with ONLY the summary."},
                {"role": "user", "content": text[:500]},
            ],
        }, headers=headers, timeout=30)

        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                summary = choices[0].get("message", {}).get("content", "").strip().strip('"\'.')
                if summary:
                    return summary[:80]
    except Exception as exc:
        log(f"summarize failed: {str(exc)[:100]}")

    first_line = text[:60].split('\n')[0].strip()
    return first_line + ("..." if len(text) > 60 else "")


def _goal_loop(workspace: int, thread_id: int):
    """Run the agent loop for a single goal. Exits when stop_event is set."""
    with goals_lock:
        ws = workspaces.get(workspace, {})
        gs = ws.get(thread_id)
    if not gs:
        return

    failures = 0
    gf = goal_file(workspace, thread_id)

    try:
        while not gs.stop_event.is_set():
            if not gf.exists() or not gf.read_text().strip():
                if gs.goal_hash:
                    log(f"goal ws={workspace} t={thread_id} cleared after {gs.step_count} steps")
                    gs.goal_hash = ""
                    gs.step_count = 0
                gs.wake.wait(timeout=5)
                gs.wake.clear()
                continue

            if gs.paused:
                gs.wake.wait(timeout=5)
                gs.wake.clear()
                continue

            current_goal = gf.read_text().strip()
            current_hash = hashlib.sha256(current_goal.encode()).hexdigest()[:16]
            if current_hash != gs.goal_hash:
                if gs.goal_hash:
                    log(f"goal ws={workspace} t={thread_id} changed after {gs.step_count} steps on previous goal")
                gs.goal_hash = current_hash
                gs.step_count = 0
                log(f"goal ws={workspace} t={thread_id} new [{current_hash}]: {current_goal[:100]}")

            state.step_count += 1
            gs.step_count += 1
            gs.last_run = datetime.now().isoformat()
            with goals_lock:
                save_goals(workspace)

            log(f"Goal ws={workspace} t={thread_id} Step {gs.step_count} (global step {state.step_count})", blank=True)

            prompt = load_prompt(workspace=workspace, thread_id=thread_id, consume_inbox=True, goal_step=gs.step_count)
            if not prompt:
                gs.wake.wait(timeout=5)
                gs.wake.clear()
                continue

            log(f"goal ws={workspace} t={thread_id}: prompt={len(prompt)} chars")

            try:
                success = run_step(prompt, state.step_count, workspace=workspace, thread_id=thread_id, goal_step=gs.step_count)
            except Exception as exc:
                success = False
                import traceback
                crash_msg = f"Goal step crashed: {type(exc).__name__}: {str(exc)[:200]}"
                log(f"goal ws={workspace} t={thread_id}: {crash_msg}")
                log(traceback.format_exc()[:800])
                send_new(thread_id, f"⚠ {crash_msg}")

            gs.last_finished = datetime.now().isoformat()
            with goals_lock:
                save_goals(workspace)

            if success:
                failures = 0
            else:
                failures += 1
                log(f"goal ws={workspace} t={thread_id}: failure #{failures}")
                if failures >= 3:
                    send_new(thread_id, f"⚠ {failures} consecutive failures — backing off {min(2 ** failures, 120)}s")

            gs.wake.clear()

            if gs.force_next:
                gs.force_next = False
                log(f"goal ws={workspace} t={thread_id}: forced — skipping delay")
            else:
                step_delay = gs.delay + int(os.environ.get("AGENT_DELAY", "0"))
                if failures:
                    backoff = min(2 ** failures, 120)
                    step_delay += backoff
                    log(f"goal ws={workspace} t={thread_id}: waiting {step_delay}s (failure backoff + delay)")
                    gs.wake.wait(timeout=step_delay)
                elif step_delay > 0:
                    log(f"goal ws={workspace} t={thread_id}: waiting {step_delay}s (delay)")
                    gs.wake.wait(timeout=step_delay)
    except Exception as exc:
        import traceback
        crash_msg = f"Goal loop crashed: {type(exc).__name__}: {str(exc)[:200]}"
        log(f"goal ws={workspace} t={thread_id}: {crash_msg}")
        log(traceback.format_exc()[:800])
        try:
            send_new(thread_id, f"⚠ {crash_msg}\nGoal loop has stopped. Use /resume to restart.")
        except Exception:
            pass

    log(f"goal ws={workspace} t={thread_id} loop exited")


def goal_manager():
    """Monitor all workspaces and spawn/stop goal threads as needed."""
    while not shutdown.is_set():
        with goals_lock:
            for ws_id, ws in list(workspaces.items()):
                for tid, gs in list(ws.items()):
                    if gs.started and not gs.paused and gs.thread is None:
                        gs.stop_event.clear()
                        t = threading.Thread(
                            target=_goal_loop, args=(ws_id, tid),
                            daemon=True, name=f"goal-{ws_id}-{tid}",
                        )
                        gs.thread = t
                        t.start()
                        log(f"goal ws={ws_id} t={tid} thread spawned")
                    elif gs.started and gs.paused and gs.thread is not None:
                        pass
                    elif not gs.started and gs.thread is not None:
                        gs.stop_event.set()
                        gs.wake.set()
                    if gs.thread is not None and not gs.thread.is_alive():
                        if gs.started and not gs.stop_event.is_set():
                            log(f"goal ws={ws_id} t={tid} thread died unexpectedly, will respawn")
                        gs.thread = None
        shutdown.wait(timeout=2)
