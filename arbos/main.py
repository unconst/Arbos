"""Main entry point — startup orchestration, signal handling, main loop."""

import os
import signal
import subprocess
import sys
import time
import threading

from arbos.config import (
    WORKING_DIR, PROVIDER, CLAUDE_MODEL, LLM_API_KEY,
    PROXY_PORT, RESTART_FLAG, CONTEXT_DIR, WORKSPACES_DIR,
    CHUTES_ROUTING_AGENT, CHUTES_ROUTING_BOT, LLM_BASE_URL,
)
from arbos.log import log
from arbos.redact import reload_env_secrets
from arbos.env import process_pending_env
from arbos.state import shutdown, child_procs, child_procs_lock


def kill_child_procs():
    """Kill all tracked claude child processes."""
    with child_procs_lock:
        procs = list(child_procs)
    for proc in procs:
        try:
            if proc.poll() is None:
                log(f"killing child claude pid={proc.pid}")
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
    with child_procs_lock:
        child_procs.clear()


def _kill_stale_claude_procs():
    """Kill any leftover claude processes from a previous arbos instance."""
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            pid = int(line.strip())
            if pid == my_pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                log(f"killed stale claude orphan pid={pid}")
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "send":
        from arbos.cli import send
        send(sys.argv[2:])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "sendfile":
        from arbos.cli import sendfile
        sendfile(sys.argv[2:])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "encrypt":
        from arbos.cli import encrypt
        encrypt()
        return

    if len(sys.argv) > 1 and sys.argv[1] not in ("send", "encrypt", "sendfile"):
        print(f"Unknown subcommand: {sys.argv[1]}", file=sys.stderr)
        print("Usage: arbos [send|sendfile|encrypt]", file=sys.stderr)
        sys.exit(1)

    log(f"arbos starting in {WORKING_DIR} (provider={PROVIDER}, model={CLAUDE_MODEL})")
    _kill_stale_claude_procs()
    reload_env_secrets()
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    (CONTEXT_DIR / "logs").mkdir(parents=True, exist_ok=True)
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

    from arbos.goals import load_all_workspaces, goal_manager
    from arbos.state import workspaces

    load_all_workspaces()
    total_goals = sum(len(ws) for ws in workspaces.values())
    log(f"loaded {total_goals} goal(s) across {len(workspaces)} workspace(s)")

    if not LLM_API_KEY:
        key_name = "OPENROUTER_API_KEY" if PROVIDER == "openrouter" else "CHUTES_API_KEY"
        log(f"WARNING: {key_name} not set -- LLM calls will fail")

    def _handle_sigterm(signum, frame):
        log("SIGTERM received; shutting down gracefully")
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    if PROVIDER != "openrouter":
        from arbos.proxy import start_proxy
        log(f"starting chutes proxy thread (port={PROXY_PORT}, agent={CHUTES_ROUTING_AGENT}, bot={CHUTES_ROUTING_BOT})")
        threading.Thread(target=start_proxy, daemon=True).start()
        time.sleep(1)
    else:
        log(f"openrouter direct mode -- no proxy needed (target={LLM_BASE_URL})")

    from arbos.runner import write_claude_settings
    write_claude_settings()

    from arbos.bot import run_bot

    threading.Thread(target=goal_manager, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()

    while not shutdown.is_set():
        if RESTART_FLAG.exists():
            RESTART_FLAG.unlink()
            log("restart requested; killing children and exiting for pm2")
            kill_child_procs()
            sys.exit(0)
        if process_pending_env():
            reload_env_secrets()
            log("loaded pending env vars from .env.pending")
        shutdown.wait(timeout=1)

    log("shutdown: killing children")
    kill_child_procs()
    log("shutdown complete")
    sys.exit(0)


if __name__ == "__main__":
    main()
