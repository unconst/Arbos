"""Paths, constants, provider configuration, and workspace path helpers."""

import os
from pathlib import Path
from datetime import datetime

from arbos.env import init_env
init_env()

# ── Paths ────────────────────────────────────────────────────────────────────

WORKING_DIR = Path(__file__).resolve().parent.parent
PROMPT_FILE = WORKING_DIR / "PROMPT.md"
CONTEXT_DIR = WORKING_DIR / "context"
WORKSPACES_DIR = CONTEXT_DIR / "workspace"
RESTART_FLAG = WORKING_DIR / ".restart"
ENV_ENC_FILE = WORKING_DIR / ".env.enc"

# ── Provider configuration ───────────────────────────────────────────────────

MAX_CONCURRENT = int(os.environ.get("CLAUDE_MAX_CONCURRENT", "4"))
PROVIDER = os.environ.get("PROVIDER", "chutes")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8089"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "600"))
CHUTES_API_KEY = os.environ.get("CHUTES_API_KEY", "")
CHUTES_BASE_URL = os.environ.get("CHUTES_BASE_URL", "https://llm.chutes.ai/v1")
CHUTES_POOL = os.environ.get(
    "CHUTES_POOL",
    "moonshotai/Kimi-K2.5-TEE,zai-org/GLM-5-TEE,MiniMaxAI/MiniMax-M2.5-TEE,zai-org/GLM-4.7-TEE",
)

if PROVIDER == "openrouter":
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "anthropic/claude-opus-4.6")
    LLM_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    LLM_BASE_URL = "https://openrouter.ai/api"
    COST_PER_M_INPUT = float(os.environ.get("COST_PER_M_INPUT", "5.00"))
    COST_PER_M_OUTPUT = float(os.environ.get("COST_PER_M_OUTPUT", "25.00"))
    CHUTES_ROUTING_AGENT = CLAUDE_MODEL
    CHUTES_ROUTING_BOT = CLAUDE_MODEL
else:
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "moonshotai/Kimi-K2.5-TEE")
    LLM_API_KEY = CHUTES_API_KEY
    LLM_BASE_URL = CHUTES_BASE_URL
    CHUTES_ROUTING_AGENT = os.environ.get("CHUTES_ROUTING_AGENT", f"{CHUTES_POOL}:throughput")
    CHUTES_ROUTING_BOT = os.environ.get("CHUTES_ROUTING_BOT", f"{CHUTES_POOL}:latency")
    COST_PER_M_INPUT = float(os.environ.get("COST_PER_M_INPUT", "0.14"))
    COST_PER_M_OUTPUT = float(os.environ.get("COST_PER_M_OUTPUT", "0.60"))

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
IS_ROOT = os.getuid() == 0
MAX_RETRIES = int(os.environ.get("CLAUDE_MAX_RETRIES", "5"))
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1200"))

# ── Workspace path helpers (slug when available, else numeric id) ─────────────
# Lazy import state to avoid circular dependency; path resolution uses
# workspace_id_to_slug and GoalState.thread_slug for human-readable dirs.


def _ws_slug(workspace: int) -> str:
    from arbos import state
    return state.workspace_id_to_slug.get(workspace) or str(workspace)


def _thread_slug(workspace: int, thread_id: int) -> str:
    from arbos import state
    gs = state.workspaces.get(workspace, {}).get(thread_id)
    return (gs.thread_slug if gs else None) or str(thread_id)


def workspace_dir(workspace: int) -> Path:
    return WORKSPACES_DIR / _ws_slug(workspace)


def goals_dir(workspace: int) -> Path:
    return workspace_dir(workspace) / "goals"


def goals_json(workspace: int) -> Path:
    return workspace_dir(workspace) / "goals.json"


def chatlog_dir(workspace: int) -> Path:
    return CONTEXT_DIR / "logs" / "chat" / _ws_slug(workspace)


def files_dir(workspace: int) -> Path:
    return workspace_dir(workspace) / "files"


def goal_dir(workspace: int, thread_id: int) -> Path:
    return goals_dir(workspace) / _thread_slug(workspace, thread_id)


def goal_file(workspace: int, thread_id: int) -> Path:
    return goal_dir(workspace, thread_id) / "GOAL.md"


def state_file(workspace: int, thread_id: int) -> Path:
    return goal_dir(workspace, thread_id) / "STATE.md"


def inbox_file(workspace: int, thread_id: int) -> Path:
    return goal_dir(workspace, thread_id) / "INBOX.md"


def goal_runs_dir(workspace: int, thread_id: int) -> Path:
    return CONTEXT_DIR / "logs" / "runs" / _ws_slug(workspace) / _thread_slug(workspace, thread_id)


def step_msg_file(workspace: int, thread_id: int) -> Path:
    return goal_dir(workspace, thread_id) / ".step_msg"


def make_run_dir(workspace: int, thread_id: int) -> Path:
    runs = goal_runs_dir(workspace, thread_id)
    runs.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = runs / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir
