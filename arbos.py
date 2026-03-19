import asyncio
import base64
import json
import os
import selectors
import signal
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any

import hashlib
import re

from dotenv import load_dotenv
import httpx
import requests
import uvicorn
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

WORKING_DIR = Path(__file__).parent
PROMPT_FILE = WORKING_DIR / "PROMPT.md"
CONTEXT_DIR = WORKING_DIR / "context"
WORKSPACES_DIR = CONTEXT_DIR / "workspaces"
RESTART_FLAG = WORKING_DIR / ".restart"
ENV_ENC_FILE = WORKING_DIR / ".env.enc"

# ── Encrypted .env ───────────────────────────────────────────────────────────

def _derive_fernet_key(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=b"arbos-env-v1", iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def _encrypt_env_file(bot_token: str):
    """Encrypt .env -> .env.enc and delete the plaintext file."""
    env_path = WORKING_DIR / ".env"
    plaintext = env_path.read_bytes()
    f = Fernet(_derive_fernet_key(bot_token))
    ENV_ENC_FILE.write_bytes(f.encrypt(plaintext))
    os.chmod(str(ENV_ENC_FILE), 0o600)
    env_path.unlink()


def _decrypt_env_content(bot_token: str) -> str:
    """Decrypt .env.enc and return plaintext (never written to disk)."""
    f = Fernet(_derive_fernet_key(bot_token))
    return f.decrypt(ENV_ENC_FILE.read_bytes()).decode()


def _load_encrypted_env(bot_token: str) -> bool:
    """Decrypt .env.enc, load into os.environ. Returns True on success."""
    if not ENV_ENC_FILE.exists():
        return False
    try:
        content = _decrypt_env_content(bot_token)
    except InvalidToken:
        return False
    for line in content.splitlines():
        line = line.split("#")[0].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    return True


def _save_to_encrypted_env(key: str, value: str):
    """Add/update a single key in the encrypted env file."""
    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token or not ENV_ENC_FILE.exists():
        return
    try:
        content = _decrypt_env_content(bot_token)
    except InvalidToken:
        return
    lines = content.splitlines()
    updated = False
    for i, line in enumerate(lines):
        stripped = line.split("#")[0].strip()
        if stripped.startswith(f"{key}="):
            lines[i] = f"{key}='{value}'"
            updated = True
            break
    if not updated:
        lines.append(f"{key}='{value}'")
    f = Fernet(_derive_fernet_key(bot_token))
    ENV_ENC_FILE.write_bytes(f.encrypt("\n".join(lines).encode()))
    os.environ[key] = value


ENV_PENDING_FILE = CONTEXT_DIR / ".env.pending"


def _init_env():
    """Load environment from .env (plaintext) or .env.enc (encrypted)."""
    env_path = WORKING_DIR / ".env"

    if env_path.exists():
        load_dotenv(env_path)
        return

    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if ENV_ENC_FILE.exists() and bot_token:
        if _load_encrypted_env(bot_token):
            return
        print("ERROR: failed to decrypt .env.enc — wrong DISCORD_BOT_TOKEN?", file=sys.stderr)
        sys.exit(1)

    if ENV_ENC_FILE.exists() and not bot_token:
        print("ERROR: .env.enc exists but DISCORD_BOT_TOKEN not set.", file=sys.stderr)
        print("Pass it as an env var: DISCORD_BOT_TOKEN=xxx python arbos.py", file=sys.stderr)
        sys.exit(1)


def _process_pending_env():
    """Pick up env vars the operator agent wrote to .env.pending and persist them."""
    with _pending_env_lock:
        if not ENV_PENDING_FILE.exists():
            return
        content = ENV_PENDING_FILE.read_text().strip()
        ENV_PENDING_FILE.unlink(missing_ok=True)
        if not content:
            return

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            os.environ[k] = v

        env_path = WORKING_DIR / ".env"
        if env_path.exists():
            with open(env_path, "a") as f:
                f.write("\n" + content + "\n")
        elif ENV_ENC_FILE.exists():
            bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
            if bot_token:
                try:
                    existing = _decrypt_env_content(bot_token)
                except InvalidToken:
                    existing = ""
                new_content = existing.rstrip() + "\n" + content + "\n"
                enc = Fernet(_derive_fernet_key(bot_token))
                ENV_ENC_FILE.write_bytes(enc.encrypt(new_content.encode()))

        _reload_env_secrets()
        _log(f"loaded pending env vars from .env.pending")


_init_env()

# ── Redaction ────────────────────────────────────────────────────────────────

_SECRET_KEY_WORDS = {"KEY", "SECRET", "TOKEN", "PASSWORD", "SEED", "CREDENTIAL"}

_SECRET_PATTERNS = [
    re.compile(r'sk-[a-zA-Z0-9_\-]{20,}'),
    re.compile(r'sk_[a-zA-Z0-9_\-]{20,}'),
    re.compile(r'sk-proj-[a-zA-Z0-9_\-]{20,}'),
    re.compile(r'sk-or-v1-[a-fA-F0-9]{20,}'),
    re.compile(r'ghp_[a-zA-Z0-9]{20,}'),
    re.compile(r'gho_[a-zA-Z0-9]{20,}'),
    re.compile(r'hf_[a-zA-Z0-9]{20,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'cpk_[a-zA-Z0-9._\-]{20,}'),
    re.compile(r'crsr_[a-zA-Z0-9]{20,}'),
    re.compile(r'dckr_pat_[a-zA-Z0-9_\-]{10,}'),
    re.compile(r'sn\d+_[a-zA-Z0-9_]{10,}'),
    re.compile(r'tpn-[a-zA-Z0-9_\-]{10,}'),
    re.compile(r'wandb_v\d+_[a-zA-Z0-9]{10,}'),
    re.compile(r'basilica_[a-zA-Z0-9]{20,}'),
    re.compile(r'MT[A-Za-z0-9]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{20,}'),
]


def _load_env_secrets() -> set[str]:
    """Build redaction blocklist from env vars whose names suggest secrets."""
    secrets = set()
    for key, val in os.environ.items():
        if len(val) < 16:
            continue
        key_upper = key.upper()
        if any(w in key_upper for w in _SECRET_KEY_WORDS):
            secrets.add(val)
    return secrets


_env_secrets: set[str] = _load_env_secrets()


def _reload_env_secrets():
    global _env_secrets
    _env_secrets = _load_env_secrets()


def _redact_secrets(text: str) -> str:
    """Strip known secrets and common key patterns from outgoing text."""
    for secret in _env_secrets:
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text
MAX_CONCURRENT = int(os.environ.get("CLAUDE_MAX_CONCURRENT", "4"))
PROVIDER = os.environ.get("PROVIDER", "chutes")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8089"))
PROXY_TIMEOUT = int(os.environ.get("PROXY_TIMEOUT", "600"))
CHUTES_API_KEY = os.environ.get("CHUTES_API_KEY", "")

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
    CHUTES_BASE_URL = os.environ.get("CHUTES_BASE_URL", "https://llm.chutes.ai/v1")
    LLM_API_KEY = CHUTES_API_KEY
    LLM_BASE_URL = CHUTES_BASE_URL
    CHUTES_POOL = os.environ.get(
        "CHUTES_POOL",
        "moonshotai/Kimi-K2.5-TEE,zai-org/GLM-5-TEE,MiniMaxAI/MiniMax-M2.5-TEE,zai-org/GLM-4.7-TEE",
    )
    CHUTES_ROUTING_AGENT = os.environ.get("CHUTES_ROUTING_AGENT", f"{CHUTES_POOL}:throughput")
    CHUTES_ROUTING_BOT = os.environ.get("CHUTES_ROUTING_BOT", f"{CHUTES_POOL}:latency")
    COST_PER_M_INPUT = float(os.environ.get("COST_PER_M_INPUT", "0.14"))
    COST_PER_M_OUTPUT = float(os.environ.get("COST_PER_M_OUTPUT", "0.60"))

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))

IS_ROOT = os.getuid() == 0
MAX_RETRIES = int(os.environ.get("CLAUDE_MAX_RETRIES", "5"))
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "600"))
_tls = threading.local()
_log_lock = threading.Lock()
_chatlog_lock = threading.Lock()
_pending_env_lock = threading.Lock()
_shutdown = threading.Event()
_claude_semaphore = threading.Semaphore(MAX_CONCURRENT)
_step_count = 0
_token_usage = {"input": 0, "output": 0}
_token_lock = threading.Lock()
_child_procs: set[subprocess.Popen] = set()
_child_procs_lock = threading.Lock()

_discord_client: Any = None
_discord_loop: asyncio.AbstractEventLoop | None = None


# ── Multi-goal state (workspace-scoped) ──────────────────────────────────────


@dataclass
class GoalState:
    thread_id: int
    workspace: int
    thread_name: str = ""
    summary: str = ""
    delay: int = 0
    started: bool = False
    paused: bool = False
    step_count: int = 0
    goal_hash: str = ""
    last_run: str = ""
    last_finished: str = ""
    thread: threading.Thread | None = field(default=None, repr=False)
    wake: threading.Event = field(default_factory=threading.Event, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)


_workspaces: dict[int, dict[int, GoalState]] = {}
_goals_lock = threading.Lock()


# ── Workspace-scoped path helpers ────────────────────────────────────────────


def _workspace_dir(workspace: int) -> Path:
    return WORKSPACES_DIR / str(workspace)


def _goals_dir(workspace: int) -> Path:
    return _workspace_dir(workspace) / "goals"


def _goals_json(workspace: int) -> Path:
    return _workspace_dir(workspace) / "goals.json"


def _chatlog_dir(workspace: int) -> Path:
    return _workspace_dir(workspace) / "chat"


def _files_dir(workspace: int) -> Path:
    return _workspace_dir(workspace) / "files"


def _goal_dir(workspace: int, thread_id: int) -> Path:
    return _goals_dir(workspace) / str(thread_id)


def _goal_file(workspace: int, thread_id: int) -> Path:
    return _goal_dir(workspace, thread_id) / "GOAL.md"


def _state_file(workspace: int, thread_id: int) -> Path:
    return _goal_dir(workspace, thread_id) / "STATE.md"


def _inbox_file(workspace: int, thread_id: int) -> Path:
    return _goal_dir(workspace, thread_id) / "INBOX.md"


def _goal_runs_dir(workspace: int, thread_id: int) -> Path:
    return _goal_dir(workspace, thread_id) / "runs"


def _step_msg_file(workspace: int, thread_id: int) -> Path:
    return _goal_dir(workspace, thread_id) / ".step_msg"


# ── Save/load goals (per workspace) ─────────────────────────────────────────


def _save_goals(workspace: int):
    """Persist goal metadata for a workspace. Caller must hold _goals_lock."""
    ws = _workspaces.get(workspace, {})
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
    jf = _goals_json(workspace)
    jf.parent.mkdir(parents=True, exist_ok=True)
    jf.write_text(json.dumps(data, indent=2))


def _load_all_workspaces():
    """Load goal metadata from all workspace directories."""
    global _workspaces
    if not WORKSPACES_DIR.exists():
        return
    for ws_dir in WORKSPACES_DIR.iterdir():
        if not ws_dir.is_dir():
            continue
        try:
            workspace = int(ws_dir.name)
        except ValueError:
            continue
        jf = _goals_json(workspace)
        if not jf.exists():
            continue
        try:
            data = json.loads(jf.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ws = {}
        for tid_str, info in data.items():
            tid = int(tid_str)
            if not _goal_file(workspace, tid).exists():
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
            _workspaces[workspace] = ws


def _format_last_time(iso_ts: str) -> str:
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


def _goal_status_label(gs: GoalState) -> str:
    if gs.started and not gs.paused:
        return "running"
    if gs.started and gs.paused:
        return "paused"
    return "stopped"


def _file_log(msg: str):
    fh = getattr(_tls, "log_fh", None)
    if fh:
        with _log_lock:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"{ts}  {_redact_secrets(msg)}\n")
            fh.flush()


def _log(msg: str, *, blank: bool = False):
    safe = _redact_secrets(msg)
    if blank:
        print(flush=True)
    print(safe, flush=True)
    _file_log(safe)


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def _reset_tokens():
    with _token_lock:
        _token_usage["input"] = 0
        _token_usage["output"] = 0


def _get_tokens() -> tuple[int, int]:
    with _token_lock:
        return _token_usage["input"], _token_usage["output"]


def fmt_tokens(inp: int, out: int, elapsed: float = 0) -> str:
    def _k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
    tps = ""
    if elapsed > 0 and out > 0:
        tps = f" | {out / elapsed:.0f} t/s"
    cost = (inp * COST_PER_M_INPUT + out * COST_PER_M_OUTPUT) / 1_000_000
    cost_str = f" | ${cost:.4f}" if cost >= 0.0001 else ""
    return f"{_k(inp)} in / {_k(out)} out{tps}{cost_str}"


# ── Prompt helpers (workspace-scoped) ────────────────────────────────────────

def load_prompt(workspace: int, thread_id: int, consume_inbox: bool = False, goal_step: int = 0) -> str:
    """Build full prompt: PROMPT.md + goal's GOAL/STATE/INBOX + chatlog."""
    parts = []
    if PROMPT_FILE.exists():
        text = PROMPT_FILE.read_text().strip()
        if text:
            parts.append(text)
    gf = _goal_file(workspace, thread_id)
    if gf.exists():
        goal_text = gf.read_text().strip()
        if goal_text:
            header = f"## Goal (step {goal_step})" if goal_step else "## Goal"
            ctx_path = f"context/workspaces/{workspace}/goals/{thread_id}/"
            parts.append(f"{header}\n\n{goal_text}\n\nYour context files are in {ctx_path} (STATE.md, INBOX.md, runs/).")
    sf = _state_file(workspace, thread_id)
    if sf.exists():
        state_text = sf.read_text().strip()
        if state_text:
            parts.append(f"## State\n\n{state_text}")
    inf = _inbox_file(workspace, thread_id)
    if inf.exists():
        inbox_text = inf.read_text().strip()
        if inbox_text:
            parts.append(f"## Inbox\n\n{inbox_text}")
        if consume_inbox:
            inf.write_text("")
    chatlog = load_chatlog(workspace)
    if chatlog:
        parts.append(chatlog)
    return "\n\n".join(parts)


def make_run_dir(workspace: int, thread_id: int) -> Path:
    runs_dir = _goal_runs_dir(workspace, thread_id)
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = runs_dir / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def log_chat(workspace: int, role: str, text: str):
    """Append to workspace chatlog, rolling to a new file when size exceeds limit."""
    with _chatlog_lock:
        chat_dir = _chatlog_dir(workspace)
        chat_dir.mkdir(parents=True, exist_ok=True)
        max_file_size = 4000
        max_files = 50

        existing = sorted(chat_dir.glob("*.jsonl"))

        current: Path | None = None
        if existing and existing[-1].stat().st_size < max_file_size:
            current = existing[-1]

        if current is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            current = chat_dir / f"{ts}.jsonl"

        entry = json.dumps({"role": role, "text": _redact_secrets(text[:1000]), "ts": datetime.now().isoformat()})
        with open(current, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

        all_files = sorted(chat_dir.glob("*.jsonl"))
        for old in all_files[:-max_files]:
            old.unlink(missing_ok=True)


def load_chatlog(workspace: int, max_chars: int = 8000) -> str:
    """Load recent Discord chat history for a workspace."""
    chat_dir = _chatlog_dir(workspace)
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


# ── Discord messaging ────────────────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v10"


def _discord_rest_send(channel_id: int, text: str) -> int | None:
    """Send a message via Discord REST API. Returns message ID or None."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        return None
    text = _redact_secrets(text)[:2000]
    try:
        resp = requests.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            json={"content": text},
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return int(resp.json()["id"])
    except Exception as exc:
        _log(f"discord REST send failed: {str(exc)[:120]}")
    return None


def _discord_rest_edit(channel_id: int, message_id: int, text: str) -> bool:
    """Edit a message via Discord REST API."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        return False
    text = _redact_secrets(text)[:2000]
    try:
        resp = requests.patch(
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}",
            json={"content": text},
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _discord_rest_send_file(channel_id: int, file_path: str, caption: str = "") -> bool:
    """Send a file via Discord REST API."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        return False
    caption = _redact_secrets(caption)[:2000]
    try:
        data = {}
        if caption:
            data["content"] = caption
        with open(file_path, "rb") as f:
            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                data=data,
                files={"file": (Path(file_path).name, f)},
                headers={"Authorization": f"Bot {token}"},
                timeout=60,
            )
        return resp.status_code in (200, 201)
    except Exception as exc:
        _log(f"discord REST file send failed: {str(exc)[:120]}")
        return False


def _send_discord_new(channel_id: int, text: str) -> int | None:
    """Send a new Discord message. Works from any thread via async bridge or REST fallback."""
    text = _redact_secrets(text)[:2000]
    if _discord_client and _discord_loop and _discord_loop.is_running():
        try:
            async def _do():
                ch = _discord_client.get_channel(channel_id)
                if not ch:
                    ch = await _discord_client.fetch_channel(channel_id)
                msg = await ch.send(text)
                return msg.id
            future = asyncio.run_coroutine_threadsafe(_do(), _discord_loop)
            return future.result(timeout=15)
        except Exception as exc:
            _log(f"discord send failed (async): {str(exc)[:120]}")
    return _discord_rest_send(channel_id, text)


def _edit_discord_text(channel_id: int, message_id: int, text: str) -> bool:
    """Edit a Discord message. Works from any thread via async bridge or REST fallback."""
    text = _redact_secrets(text)[:2000]
    if _discord_client and _discord_loop and _discord_loop.is_running():
        try:
            async def _do():
                ch = _discord_client.get_channel(channel_id)
                if not ch:
                    ch = await _discord_client.fetch_channel(channel_id)
                msg = await ch.fetch_message(message_id)
                await msg.edit(content=text)
            future = asyncio.run_coroutine_threadsafe(_do(), _discord_loop)
            future.result(timeout=15)
            return True
        except Exception as exc:
            _log(f"discord edit failed (async): {str(exc)[:120]}")
    return _discord_rest_edit(channel_id, message_id, text)


def _send_discord_file(channel_id: int, file_path: str, caption: str = "") -> bool:
    """Send a file to a Discord channel."""
    caption = _redact_secrets(caption)[:2000]
    if _discord_client and _discord_loop and _discord_loop.is_running():
        try:
            import discord as _dc
            async def _do():
                ch = _discord_client.get_channel(channel_id)
                if not ch:
                    ch = await _discord_client.fetch_channel(channel_id)
                await ch.send(content=caption or None, file=_dc.File(file_path))
            future = asyncio.run_coroutine_threadsafe(_do(), _discord_loop)
            future.result(timeout=60)
            return True
        except Exception as exc:
            _log(f"discord file send failed (async): {str(exc)[:120]}")
    return _discord_rest_send_file(channel_id, file_path, caption)


def _download_discord_attachment(url: str, filename: str, workspace: int) -> Path:
    """Download a Discord attachment and save it to the workspace files directory."""
    fdir = _files_dir(workspace)
    fdir.mkdir(parents=True, exist_ok=True)
    save_path = fdir / filename
    if save_path.exists():
        stem, suffix = save_path.stem, save_path.suffix
        ts = datetime.now().strftime("%H%M%S")
        save_path = fdir / f"{stem}_{ts}{suffix}"
    resp = requests.get(url, timeout=60)
    save_path.write_bytes(resp.content)
    _log(f"saved discord file: {save_path.name} ({len(resp.content)} bytes)")
    return save_path


# ── Chutes proxy (Anthropic Messages API -> OpenAI Chat Completions) ──────────

_proxy_app = FastAPI(title="Chutes Proxy")


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
            _log(f"proxy: stream model={chunk['model']}")
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

    with _token_lock:
        _token_usage["input"] += usage["input_tokens"]
        _token_usage["output"] += usage["output_tokens"]

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


@_proxy_app.get("/health")
async def _proxy_health():
    return {"status": "ok"}


@_proxy_app.get("/")
async def _proxy_root():
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
    """Extract (context_limit, requested_output, input_tokens) from a context-length 400."""
    m = _CONTEXT_LENGTH_RE.search(error_msg)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def _maybe_reduce_max_tokens(oai_request: dict, error_msg: str) -> bool:
    """If the error is a context-length overflow, reduce max_tokens to fit. Returns True if adjusted."""
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
    _log(f"proxy: reduced max_tokens {old_max} -> {new_max} (ctx_limit={ctx_limit}, input={input_tokens})")
    return True


@_proxy_app.post("/v1/messages")
async def _proxy_messages(request: Request):
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
                    _log(f"proxy: chutes returned {resp.status_code} (attempt {attempt}/{PROXY_MAX_RETRIES}): {last_error_msg[:300]}")

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
                        _log(f"proxy: streaming [{routing}] via {routing_label}")
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
                _log(f"proxy: {last_error_msg} (attempt {attempt}/{PROXY_MAX_RETRIES})")
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
                _log(f"proxy: error (attempt {attempt}/{PROXY_MAX_RETRIES}): {last_error_msg}")
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
                    _log(f"proxy: chutes returned {resp.status_code} (attempt {attempt}/{PROXY_MAX_RETRIES}): {last_error_msg[:300]}")

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
                    with _token_lock:
                        _token_usage["input"] += u.get("prompt_tokens", 0)
                        _token_usage["output"] += u.get("completion_tokens", 0)
                _log(f"proxy: response [{routing}] via {routing_label} model={actual_model}")
                return JSONResponse(content=_openai_response_to_anthropic(oai_data, model))
            except httpx.TimeoutException:
                last_error_msg = f"timed out after {PROXY_TIMEOUT}s"
                _log(f"proxy: {last_error_msg} (attempt {attempt}/{PROXY_MAX_RETRIES})")
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
                _log(f"proxy: error (attempt {attempt}/{PROXY_MAX_RETRIES}): {last_error_msg}")
                if attempt < PROXY_MAX_RETRIES:
                    continue
                return JSONResponse(status_code=502, content={
                    "type": "error", "error": {
                        "type": "api_error",
                        "message": f"Chutes routing error: {last_error_msg}",
                    },
                })


@_proxy_app.post("/v1/messages/count_tokens")
async def _proxy_count_tokens(request: Request):
    body = await request.json()
    rough = sum(len(json.dumps(m)) for m in body.get("messages", [])) // 4
    rough += len(json.dumps(body.get("tools", []))) // 4
    rough += len(str(body.get("system", ""))) // 4
    return JSONResponse(content={"input_tokens": max(rough, 1)})


def _start_proxy():
    """Run the Chutes translation proxy in-process on a background thread."""
    config = uvicorn.Config(
        _proxy_app, host="127.0.0.1", port=PROXY_PORT, log_level="warning",
    )
    server = uvicorn.Server(config)
    server.run()


# ── Agent runner ─────────────────────────────────────────────────────────────

def _claude_cmd(prompt: str, extra_flags: list[str] | None = None) -> list[str]:
    cmd = ["claude", "-p", prompt]
    if not IS_ROOT:
        cmd.append("--dangerously-skip-permissions")
    cmd.extend(["--output-format", "stream-json", "--verbose"])
    if extra_flags:
        cmd.extend(extra_flags)
    return cmd


def _write_claude_settings():
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
    _log(f"wrote .claude/settings.local.json (provider={PROVIDER}, model={CLAUDE_MODEL}, target={target_label})")


def _claude_env(workspace: int = 0, thread_id: int = 0) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DISCORD_BOT_TOKEN", None)
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


def _run_claude_once(cmd, env, on_text=None, on_activity=None):
    """Run a single claude subprocess, return (returncode, result_text, raw_lines, stderr).

    on_text: optional callback(accumulated_text) fired as assistant text streams in.
    on_activity: optional callback(status_str) fired on tool use and other activity.
    Kills the process if no output is received for CLAUDE_TIMEOUT seconds.
    """
    proc = subprocess.Popen(
        cmd, cwd=WORKING_DIR, env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    with _child_procs_lock:
        _child_procs.add(proc)

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
                    _log(f"claude timeout: no output for {CLAUDE_TIMEOUT}s, killing pid={proc.pid}")
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
                        on_activity(_format_tool_activity(tool_name, tool_input))
                if PROVIDER == "openrouter":
                    u = msg.get("usage", {})
                    if u:
                        with _token_lock:
                            _token_usage["input"] += u.get("input_tokens", 0)
                            _token_usage["output"] += u.get("output_tokens", 0)
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
                        with _token_lock:
                            _token_usage["input"] += u.get("input_tokens", 0)
                            _token_usage["output"] += u.get("output_tokens", 0)
    finally:
        sel.unregister(proc.stdout)
        sel.close()

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
    with _child_procs_lock:
        _child_procs.discard(proc)
    return returncode, result_text, raw_lines, stderr_output


def run_agent(cmd: list[str], phase: str, output_file: Path,
              on_text=None, on_activity=None, workspace: int = 0, thread_id: int = 0) -> subprocess.CompletedProcess:
    _claude_semaphore.acquire()
    try:
        env = _claude_env(workspace=workspace, thread_id=thread_id)
        flags = " ".join(a for a in cmd if a.startswith("-"))

        returncode, result_text, raw_lines, stderr_output = 1, "", [], "no attempts made"

        for attempt in range(1, MAX_RETRIES + 1):
            _log(f"{phase}: starting (attempt={attempt}) flags=[{flags}]")
            t0 = time.monotonic()

            returncode, result_text, raw_lines, stderr_output = _run_claude_once(
                cmd, env, on_text=on_text, on_activity=on_activity,
            )
            elapsed = time.monotonic() - t0

            output_file.write_text(_redact_secrets("".join(raw_lines)))
            _log(f"{phase}: finished rc={returncode} {fmt_duration(elapsed)}")

            if returncode != 0 and stderr_output.strip():
                _log(f"{phase}: stderr {stderr_output.strip()[:300]}")
                if attempt < MAX_RETRIES:
                    delay = min(2 ** attempt, 30)
                    _log(f"{phase}: retrying in {delay}s (attempt {attempt}/{MAX_RETRIES})")
                    time.sleep(delay)
                    continue

            return subprocess.CompletedProcess(
                args=cmd, returncode=returncode,
                stdout=result_text, stderr=stderr_output,
            )

        _log(f"{phase}: all {MAX_RETRIES} retries exhausted")
        output_file.write_text(_redact_secrets("".join(raw_lines)))
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode,
            stdout=result_text, stderr=stderr_output,
        )
    finally:
        _claude_semaphore.release()


def extract_text(result: subprocess.CompletedProcess) -> str:
    output = result.stdout or ""
    if not output.strip():
        output = result.stderr or "(no output)"
    return output


def run_step(prompt: str, step_number: int, workspace: int = 0, thread_id: int = 0, goal_step: int = 0) -> bool:
    run_dir = make_run_dir(workspace=workspace, thread_id=thread_id)
    t0 = time.monotonic()

    log_file = run_dir / "logs.txt"
    _tls.log_fh = open(log_file, "a", encoding="utf-8")

    smf = _step_msg_file(workspace, thread_id)
    smf.parent.mkdir(parents=True, exist_ok=True)

    step_label = f"Goal Step {goal_step}" if goal_step else f"Step {step_number}"
    step_msg_id: int | None = None
    step_msg_text = ""
    last_edit = 0.0

    step_msg_id = _send_discord_new(thread_id, f"{step_label}: starting...")
    if step_msg_id:
        smf.write_text(json.dumps({
            "msg_id": step_msg_id, "channel_id": thread_id, "text": f"{step_label}: starting...",
        }))
    else:
        smf.unlink(missing_ok=True)

    def _edit_step_msg(text: str, *, force: bool = False):
        nonlocal last_edit, step_msg_text
        if not step_msg_id:
            return
        now = time.time()
        if not force and now - last_edit < 3.0:
            return
        step_msg_text = text
        _edit_discord_text(thread_id, step_msg_id, text)
        smf.write_text(json.dumps({"msg_id": step_msg_id, "channel_id": thread_id, "text": text}))
        last_edit = now

    _reset_tokens()

    _last_activity = [""]
    _heartbeat_stop = threading.Event()

    def _on_activity(status: str):
        _last_activity[0] = status
        elapsed_s = time.monotonic() - t0
        inp, out = _get_tokens()
        tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
        _edit_step_msg(f"{step_label} ({fmt_duration(elapsed_s)}{tok})\n{status}")

    def _heartbeat():
        while not _heartbeat_stop.wait(timeout=10):
            elapsed_s = time.monotonic() - t0
            inp, out = _get_tokens()
            tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
            status = _last_activity[0] or "working..."
            _edit_step_msg(f"{step_label} ({fmt_duration(elapsed_s)}{tok})\n{status}", force=True)

    success = False
    try:
        _log(f"run dir {run_dir}")

        preview = prompt[:200] + ("..." if len(prompt) > 200 else "")
        _log(f"prompt preview: {preview}")

        _log(f"workspace={workspace} thread={thread_id} step {goal_step}: executing")

        threading.Thread(target=_heartbeat, daemon=True).start()

        result = run_agent(
            _claude_cmd(prompt),
            phase=f"ws{workspace}/t{thread_id}",
            output_file=run_dir / "output.txt",
            on_activity=_on_activity,
            workspace=workspace,
            thread_id=thread_id,
        )

        rollout_text = _redact_secrets(extract_text(result))
        (run_dir / "rollout.md").write_text(rollout_text)
        _log(f"rollout saved ({len(rollout_text)} chars)")

        elapsed = time.monotonic() - t0
        success = result.returncode == 0
        _log(f"step {'succeeded' if success else 'failed'} in {fmt_duration(elapsed)}")
        return success
    finally:
        _heartbeat_stop.set()
        fh = getattr(_tls, "log_fh", None)
        if fh:
            fh.close()
            _tls.log_fh = None
        try:
            elapsed = fmt_duration(time.monotonic() - t0)
            rollout = (run_dir / "rollout.md").read_text() if (run_dir / "rollout.md").exists() else ""
            status = "done" if success else "failed"

            agent_text = ""
            if smf.exists():
                try:
                    state = json.loads(smf.read_text())
                    saved = state.get("text", "")
                    prefix = f"{step_label}: starting..."
                    if saved != prefix and not saved.startswith(f"{step_label} ("):
                        agent_text = saved
                except (json.JSONDecodeError, KeyError):
                    pass

            elapsed_s = time.monotonic() - t0
            inp, out = _get_tokens()
            tok = f" | {fmt_tokens(inp, out, elapsed_s)}" if (inp or out) else ""
            parts = [f"{step_label} ({elapsed}, {status}{tok})"]
            if agent_text:
                parts.append(agent_text)
            if rollout.strip():
                parts.append(rollout.strip()[:1800])
            final = "\n\n".join(parts)

            _edit_step_msg(final, force=True)
            log_chat(workspace, "bot", final[:1000])
            smf.unlink(missing_ok=True)
        except Exception as exc:
            _log(f"step message finalize failed: {str(exc)[:120]}")


# ── Agent loop (workspace-scoped) ────────────────────────────────────────────


def _goal_loop(workspace: int, thread_id: int):
    """Run the agent loop for a single goal. Exits when stop_event is set."""
    global _step_count

    with _goals_lock:
        ws = _workspaces.get(workspace, {})
        gs = ws.get(thread_id)
    if not gs:
        return

    failures = 0
    gf = _goal_file(workspace, thread_id)

    while not gs.stop_event.is_set():
        if not gf.exists() or not gf.read_text().strip():
            if gs.goal_hash:
                _log(f"goal ws={workspace} t={thread_id} cleared after {gs.step_count} steps")
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
                _log(f"goal ws={workspace} t={thread_id} changed after {gs.step_count} steps on previous goal")
            gs.goal_hash = current_hash
            gs.step_count = 0
            _log(f"goal ws={workspace} t={thread_id} new [{current_hash}]: {current_goal[:100]}")

        _step_count += 1
        gs.step_count += 1
        gs.last_run = datetime.now().isoformat()
        with _goals_lock:
            _save_goals(workspace)

        _log(f"Goal ws={workspace} t={thread_id} Step {gs.step_count} (global step {_step_count})", blank=True)

        prompt = load_prompt(workspace=workspace, thread_id=thread_id, consume_inbox=True, goal_step=gs.step_count)
        if not prompt:
            gs.wake.wait(timeout=5)
            gs.wake.clear()
            continue

        _log(f"goal ws={workspace} t={thread_id}: prompt={len(prompt)} chars")

        success = run_step(prompt, _step_count, workspace=workspace, thread_id=thread_id, goal_step=gs.step_count)

        gs.last_finished = datetime.now().isoformat()
        with _goals_lock:
            _save_goals(workspace)

        if success:
            failures = 0
        else:
            failures += 1
            _log(f"goal ws={workspace} t={thread_id}: failure #{failures}")

        gs.wake.clear()

        step_delay = gs.delay + int(os.environ.get("AGENT_DELAY", "0"))
        if failures:
            backoff = min(2 ** failures, 120)
            step_delay += backoff
            _log(f"goal ws={workspace} t={thread_id}: waiting {step_delay}s (failure backoff + delay)")
            gs.wake.wait(timeout=step_delay)
        elif step_delay > 0:
            _log(f"goal ws={workspace} t={thread_id}: waiting {step_delay}s (delay)")
            gs.wake.wait(timeout=step_delay)

    _log(f"goal ws={workspace} t={thread_id} loop exited")


def _goal_manager():
    """Monitor all workspaces and spawn/stop goal threads as needed."""
    while not _shutdown.is_set():
        with _goals_lock:
            for ws_id, ws in list(_workspaces.items()):
                for tid, gs in list(ws.items()):
                    if gs.started and not gs.paused and gs.thread is None:
                        gs.stop_event.clear()
                        t = threading.Thread(
                            target=_goal_loop, args=(ws_id, tid),
                            daemon=True, name=f"goal-{ws_id}-{tid}",
                        )
                        gs.thread = t
                        t.start()
                        _log(f"goal ws={ws_id} t={tid} thread spawned")
                    elif gs.started and gs.paused and gs.thread is not None:
                        pass
                    elif not gs.started and gs.thread is not None:
                        gs.stop_event.set()
                        gs.wake.set()
                    if gs.thread is not None and not gs.thread.is_alive():
                        gs.thread = None
        _shutdown.wait(timeout=2)


def _summarize_goal(text: str) -> str:
    """Generate a one-line summary of a goal via LLM. Falls back to truncation."""
    try:
        if PROVIDER == "openrouter":
            url = f"{LLM_BASE_URL}/v1/chat/completions"
            headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
            model = CLAUDE_MODEL
        else:
            url = f"{CHUTES_BASE_URL}/chat/completions"
            headers = _chutes_headers()
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
        _log(f"summarize failed: {str(exc)[:100]}")

    first_line = text[:60].split('\n')[0].strip()
    return first_line + ("..." if len(text) > 60 else "")


def transcribe_voice(file_path: str, fmt: str = "ogg") -> str:
    """Transcribe audio via Chutes Whisper Large V3 STT endpoint."""
    try:
        with open(file_path, "rb") as f:
            b64_audio = base64.b64encode(f.read()).decode("utf-8")

        resp = requests.post(
            "https://chutes-whisper-large-v3.chutes.ai/transcribe",
            headers={
                "Authorization": f"Bearer {CHUTES_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"language": None, "audio_b64": b64_audio},
            timeout=90,
        )
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("text", "") if isinstance(data, dict) else str(data)
            if text.strip():
                _log(f"whisper transcription ok ({len(text)} chars)")
                return text.strip()
            return "(voice transcription returned empty -- send text instead)"
        _log(f"whisper STT failed: status={resp.status_code} body={resp.text[:200]}")
        return "(voice transcription unavailable -- send text instead)"
    except Exception as exc:
        _log(f"transcription failed: {str(exc)[:200]}")
        return "(voice transcription unavailable -- send text instead)"


# ── Operator prompt (workspace-aware) ────────────────────────────────────────

def _recent_context(workspace: int, max_chars: int = 6000) -> str:
    """Collect recent rollouts across goals in a workspace."""
    ws = _workspaces.get(workspace, {})
    parts: list[str] = []
    total = 0
    all_runs: list[tuple[str, Path]] = []
    for tid, gs in sorted(ws.items()):
        runs_dir = _goal_runs_dir(workspace, tid)
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


def _build_operator_prompt(workspace: int, user_text: str, thread_id: int = 0) -> str:
    """Build prompt for the CLI agent to handle an operator request.

    If thread_id is set, focuses on that specific goal's context.
    Otherwise includes all goals in the workspace.
    """
    chatlog = load_chatlog(workspace, max_chars=4000)
    ws = _workspaces.get(workspace, {})
    ctx_base = f"context/workspaces/{workspace}/"

    parts = [
        "You are the operator interface for Arbos, a coding agent running in a loop via pm2.\n"
        "The operator communicates with you through Discord. Be concise and direct.\n"
        "When the operator asks you to do something, do it by modifying the relevant files.\n"
        "When the operator asks a question, answer from the available context.\n\n"
        "## Security\n\n"
        "NEVER read, output, or reveal the contents of `.env`, `.env.enc`, or any secret/key/token values.\n"
        "Do not include API keys, passwords, seed phrases, or credentials in any response.\n"
        "If asked to show secrets, refuse. The .env file is encrypted; do not attempt to decrypt it.\n\n"
        "## Multi-goal system\n\n"
        f"Goals are stored in `{ctx_base}goals/<thread_id>/`. Each goal has its own GOAL.md, STATE.md, INBOX.md, and runs/.\n"
        "Goal management is handled via Discord slash commands (/thread, /start, /stop, /pause, /delete, /delay, /ls, /status).\n"
        f"To modify a specific goal's context, write to `{ctx_base}goals/<thread_id>/STATE.md` or `{ctx_base}goals/<thread_id>/INBOX.md`.\n\n"
        "## Available operations\n\n"
        f"- **Message a goal's agent**: append a timestamped line to `{ctx_base}goals/<thread_id>/INBOX.md`.\n"
        f"- **Update a goal's state**: write to `{ctx_base}goals/<thread_id>/STATE.md`.\n"
        "- **Set system prompt**: write to `PROMPT.md`.\n"
        "- **Set env variable**: write `KEY='VALUE'` lines (one per line) to `context/.env.pending`. They are picked up automatically and persisted.\n"
        f"- **View logs**: read files in `{ctx_base}goals/<thread_id>/runs/<timestamp>/` (rollout.md, logs.txt).\n"
        "- **Modify code & restart**: edit code files, then run `touch .restart`.\n"
        "- **Send follow-up**: run `python arbos.py send \"your text here\"`.\n"
        "- **Send file to operator**: run `python arbos.py sendfile path/to/file [--caption 'text']`.\n"
        f"- **Received files**: operator-sent files are saved in `{ctx_base}files/` and their path is shown in the message.",
    ]

    if thread_id and thread_id in ws:
        gs = ws[thread_id]
        status = _goal_status_label(gs)
        gf = _goal_file(workspace, thread_id)
        goal_text = gf.read_text().strip()[:500] if gf.exists() else "(empty)"
        sf = _state_file(workspace, thread_id)
        state_text = sf.read_text().strip()[:500] if sf.exists() else "(empty)"
        parts.append(
            f"## Current Goal: {gs.thread_name} [{status}] (delay: {gs.delay}s, step {gs.step_count})\n"
            f"Thread ID: {thread_id}\n\n"
            f"{goal_text}\n\nState: {state_text}"
        )
    elif ws:
        goals_section = []
        for tid in sorted(ws.keys()):
            gs = ws[tid]
            status = _goal_status_label(gs)
            gf = _goal_file(workspace, tid)
            goal_text = gf.read_text().strip()[:200] if gf.exists() else "(empty)"
            sf = _state_file(workspace, tid)
            state_text = sf.read_text().strip()[:200] if sf.exists() else "(empty)"
            goals_section.append(
                f"### {gs.thread_name} [{status}] (delay: {gs.delay}s, step {gs.step_count})\n"
                f"Thread ID: {tid}\n"
                f"{goal_text}\nState: {state_text}"
            )
        parts.append("## Goals\n" + "\n\n".join(goals_section))
    else:
        parts.append("## Goals\n(no goals set)")

    if chatlog:
        parts.append(chatlog)

    context = _recent_context(workspace, max_chars=4000)
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


def _format_tool_activity(tool_name: str, tool_input: dict) -> str:
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


def run_agent_streaming(channel_id: int, msg_id: int, prompt: str) -> str:
    """Run Claude Code CLI and stream output by editing a Discord message."""
    if PROVIDER == "openrouter":
        cmd = _claude_cmd(prompt)
    else:
        cmd = _claude_cmd(prompt, extra_flags=["--model", "bot"])

    current_text = ""
    activity_status = ""
    last_edit = 0.0

    def _edit(text: str, force: bool = False):
        nonlocal last_edit
        now = time.time()
        if not force and now - last_edit < 1.5:
            return
        display = text[-1900:] if len(text) > 1900 else text
        display = _redact_secrets(display)
        if not display.strip():
            return
        _edit_discord_text(channel_id, msg_id, display)
        last_edit = now

    def _on_text(text: str):
        nonlocal current_text
        current_text = text
        _edit(text)

    def _on_activity(status: str):
        nonlocal activity_status
        activity_status = status
        if not current_text:
            _edit(status)

    _claude_semaphore.acquire()
    try:
        env = _claude_env()

        for attempt in range(1, MAX_RETRIES + 1):
            current_text = ""
            activity_status = ""
            last_edit = 0.0

            returncode, result_text, raw_lines, stderr_output = _run_claude_once(
                cmd, env, on_text=_on_text, on_activity=_on_activity,
            )

            if result_text.strip():
                current_text = result_text
                break

            if returncode != 0 and attempt < MAX_RETRIES:
                delay = min(2 ** attempt, 30)
                _edit(f"Error, retrying in {delay}s... (attempt {attempt}/{MAX_RETRIES})", force=True)
                time.sleep(delay)
                continue
            break

        _edit(current_text, force=True)

        if not current_text.strip():
            _edit_discord_text(channel_id, msg_id, "(no output)")

    except Exception as e:
        _edit_discord_text(channel_id, msg_id, f"Error: {str(e)[:300]}")
    finally:
        _claude_semaphore.release()

    return current_text


# ── Discord bot ──────────────────────────────────────────────────────────────


def run_bot():
    """Run the Discord bot with slash commands and message handlers."""
    global _discord_client, _discord_loop
    import discord
    from discord import app_commands

    if not DISCORD_BOT_TOKEN:
        _log("DISCORD_BOT_TOKEN not set; add it to .env and restart")
        sys.exit(1)
    if not DISCORD_GUILD_ID:
        _log("DISCORD_GUILD_ID not set; add it to .env and restart")
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.members = True

    guild_obj = discord.Object(id=DISCORD_GUILD_ID)

    class ArbosBot(discord.Client):
        def __init__(self):
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)

        async def setup_hook(self):
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            _log("discord slash commands synced")

        async def on_ready(self):
            _log(f"discord bot ready as {self.user} (guild={DISCORD_GUILD_ID})")
            guild = self.get_guild(DISCORD_GUILD_ID)
            if guild:
                for ch in guild.text_channels:
                    if ch.name == "general":
                        await ch.send("Restarted.")
                        break

        async def on_message(self, message: discord.Message):
            if message.author == self.user or message.author.bot:
                return
            if not message.guild or message.guild.id != DISCORD_GUILD_ID:
                return
            if message.content.startswith("/"):
                return

            is_thread = isinstance(message.channel, discord.Thread)
            if is_thread:
                workspace = message.channel.parent_id
                thread_id = message.channel.id
            else:
                workspace = message.channel.id
                thread_id = 0

            user_text = message.content or ""

            if message.attachments:
                for att in message.attachments:
                    saved = _download_discord_attachment(att.url, att.filename, workspace)
                    size_kb = att.size / 1024 if att.size else saved.stat().st_size / 1024
                    att_info = f"\n[Sent file: {saved.name}] saved to {saved} ({size_kb:.1f} KB)"
                    is_text_file = False
                    try:
                        content = saved.read_text(errors="strict")
                        if len(content) <= 8000:
                            att_info += f"\n[File contents]:\n{content}"
                            is_text_file = True
                    except (UnicodeDecodeError, ValueError):
                        pass
                    if not is_text_file:
                        att_info += "\n(Binary file -- not included inline. Read it from the saved path if needed.)"
                    user_text += att_info

            if not user_text.strip():
                return

            log_chat(workspace, "user", user_text[:1000])
            prompt = _build_operator_prompt(workspace, user_text, thread_id=thread_id)

            thinking_msg = await message.channel.send("thinking...")

            async def _run():
                response = run_agent_streaming(message.channel.id, thinking_msg.id, prompt)
                log_chat(workspace, "bot", response[:1000])
                _process_pending_env()

            await asyncio.to_thread(lambda: _run_sync(_run))

    def _run_sync(coro_fn):
        """Run an async function synchronously using the bot's event loop."""
        future = asyncio.run_coroutine_threadsafe(coro_fn(), _discord_loop)
        future.result()

    bot = ArbosBot()

    # ── Slash commands ───────────────────────────────────────────────────────

    @bot.tree.command(name="thread", description="Create a new goal thread", guild=guild_obj)
    @app_commands.describe(name="Thread name", message="Goal description")
    async def cmd_thread(interaction: discord.Interaction, name: str, message: str):
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Use /thread in a text channel, not inside a thread.", ephemeral=True)
            return

        await interaction.response.defer()
        workspace = interaction.channel.id

        thread = await interaction.channel.create_thread(
            name=name, type=discord.ChannelType.public_thread,
        )
        thread_id = thread.id

        summary = await asyncio.to_thread(_summarize_goal, message)

        with _goals_lock:
            ws = _workspaces.setdefault(workspace, {})
            gs = GoalState(
                thread_id=thread_id, workspace=workspace,
                thread_name=name, summary=summary, started=True,
            )
            ws[thread_id] = gs
            gdir = _goal_dir(workspace, thread_id)
            gdir.mkdir(parents=True, exist_ok=True)
            _goal_file(workspace, thread_id).write_text(message)
            _state_file(workspace, thread_id).write_text("")
            _inbox_file(workspace, thread_id).write_text("")
            _goal_runs_dir(workspace, thread_id).mkdir(parents=True, exist_ok=True)
            _save_goals(workspace)

        await thread.send(f"**Goal created**: {summary}\n\n{message}")
        await interaction.followup.send(f"Thread **{name}** created and started: {summary}")
        _log(f"goal created ws={workspace} t={thread_id}: {summary}")

    @bot.tree.command(name="start", description="Start/resume this thread's goal", guild=guild_obj)
    async def cmd_start(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /start inside a goal thread.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        with _goals_lock:
            ws = _workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            gs.started = True
            gs.paused = False
            gs.wake.set()
            _save_goals(workspace)
        await interaction.response.send_message(f"Goal started: {gs.summary}")
        _log(f"goal started ws={workspace} t={thread_id}")

    @bot.tree.command(name="pause", description="Pause this thread's goal", guild=guild_obj)
    async def cmd_pause(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /pause inside a goal thread.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        with _goals_lock:
            ws = _workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            if gs.paused:
                await interaction.response.send_message("Already paused.", ephemeral=True)
                return
            gs.paused = True
            _save_goals(workspace)
        await interaction.response.send_message(f"Goal paused. Use /start to resume.")
        _log(f"goal paused ws={workspace} t={thread_id}")

    @bot.tree.command(name="delay", description="Set step delay for this thread's goal", guild=guild_obj)
    @app_commands.describe(seconds="Delay between steps in seconds")
    async def cmd_delay(interaction: discord.Interaction, seconds: int):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /delay inside a goal thread.", ephemeral=True)
            return
        if seconds < 0:
            await interaction.response.send_message("Delay must be >= 0.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        with _goals_lock:
            ws = _workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            gs.delay = seconds
            _save_goals(workspace)
        await interaction.response.send_message(f"Delay set to {seconds}s.")
        _log(f"goal delay set ws={workspace} t={thread_id} delay={seconds}s")

    @bot.tree.command(name="ls", description="List all goals in this channel", guild=guild_obj)
    async def cmd_ls(interaction: discord.Interaction):
        if isinstance(interaction.channel, discord.Thread):
            workspace = interaction.channel.parent_id
        else:
            workspace = interaction.channel.id
        ws = _workspaces.get(workspace, {})
        if not ws:
            await interaction.response.send_message("No goals. Use /thread to create one.")
            return
        lines = []
        for tid in sorted(ws.keys()):
            gs = ws[tid]
            status = _goal_status_label(gs)
            last = _format_last_time(gs.last_finished)
            delay_str = f" delay:{gs.delay}s" if gs.delay else ""
            lines.append(f"**{gs.thread_name}** [{status}]{delay_str} last:{last} - {gs.summary}")
        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="status", description="Show workspace or goal status", guild=guild_obj)
    async def cmd_status(interaction: discord.Interaction):
        is_thread = isinstance(interaction.channel, discord.Thread)
        if is_thread:
            workspace = interaction.channel.parent_id
            thread_id = interaction.channel.id
            ws = _workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            status = _goal_status_label(gs)
            gf = _goal_file(workspace, thread_id)
            goal_text = gf.read_text().strip()[:500] if gf.exists() else "(empty)"
            sf = _state_file(workspace, thread_id)
            state_text = sf.read_text().strip()[:500] if sf.exists() else "(empty)"
            lines = [
                f"**{gs.thread_name}** [{status}] (delay: {gs.delay}s, step {gs.step_count})",
                f"Last run: {gs.last_run or 'never'}",
                f"Last finished: {gs.last_finished or 'never'}",
                "",
                f"**Goal:** {goal_text}",
                "",
                f"**State:** {state_text}",
            ]
            await interaction.response.send_message("\n".join(lines))
        else:
            workspace = interaction.channel.id
            ws = _workspaces.get(workspace, {})
            if not ws:
                await interaction.response.send_message(f"No goals. Total steps: {_step_count}")
                return
            lines = [f"Total steps: {_step_count}"]
            for tid in sorted(ws.keys()):
                gs = ws[tid]
                status = _goal_status_label(gs)
                last = _format_last_time(gs.last_finished)
                delay_str = f" delay:{gs.delay}s" if gs.delay else ""
                lines.append(f"**{gs.thread_name}** [{status}]{delay_str} last:{last} - {gs.summary}")
            await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(name="stop", description="Stop all goals in this channel", guild=guild_obj)
    async def cmd_stop(interaction: discord.Interaction):
        if isinstance(interaction.channel, discord.Thread):
            workspace = interaction.channel.parent_id
        else:
            workspace = interaction.channel.id
        with _goals_lock:
            ws = _workspaces.get(workspace, {})
            count = 0
            for gs in ws.values():
                if gs.started:
                    gs.started = False
                    gs.stop_event.set()
                    gs.wake.set()
                    count += 1
            _save_goals(workspace)
        await interaction.response.send_message(f"Stopped {count} goal(s).")
        _log(f"all goals stopped in ws={workspace} ({count})")

    @bot.tree.command(name="delete", description="Delete this thread's goal", guild=guild_obj)
    async def cmd_delete(interaction: discord.Interaction):
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /delete inside a goal thread.", ephemeral=True)
            return
        thread_id = interaction.channel.id
        workspace = interaction.channel.parent_id
        with _goals_lock:
            ws = _workspaces.get(workspace, {})
            gs = ws.get(thread_id)
            if not gs:
                await interaction.response.send_message("No goal found for this thread.", ephemeral=True)
                return
            gs.stop_event.set()
            gs.wake.set()
            gs.started = False
            bg_thread = gs.thread
            del ws[thread_id]
            _save_goals(workspace)
        if bg_thread and bg_thread.is_alive():
            bg_thread.join(timeout=5)
        import shutil
        gdir = _goal_dir(workspace, thread_id)
        if gdir.exists():
            shutil.rmtree(gdir, ignore_errors=True)
        await interaction.response.send_message(f"Goal deleted.")
        try:
            await interaction.channel.edit(archived=True)
        except Exception:
            pass
        _log(f"goal deleted ws={workspace} t={thread_id}")

    @bot.tree.command(name="clear", description="Clear all goals in this channel", guild=guild_obj)
    async def cmd_clear(interaction: discord.Interaction):
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Use /clear in a channel, not a thread.", ephemeral=True)
            return
        workspace = interaction.channel.id
        import shutil
        with _goals_lock:
            ws = _workspaces.get(workspace, {})
            for gs in ws.values():
                gs.stop_event.set()
                gs.wake.set()
            ws.clear()
        ws_dir = _workspace_dir(workspace)
        removed = []
        if ws_dir.exists():
            shutil.rmtree(ws_dir)
            removed.append(f"workspace {workspace}")
        ws_dir.mkdir(parents=True, exist_ok=True)
        summary = ", ".join(removed) if removed else "nothing to clear"
        await interaction.response.send_message(f"Cleared: {summary}\nReady for a fresh /thread.")
        _log(f"cleared workspace ws={workspace}: {summary}")

    @bot.tree.command(name="restart", description="Restart arbos (pm2)", guild=guild_obj)
    async def cmd_restart(interaction: discord.Interaction):
        await interaction.response.send_message("Restarting -- killing agent and exiting for pm2...")
        _log("restart requested via /restart command")
        _kill_child_procs()
        RESTART_FLAG.touch()

    @bot.tree.command(name="update", description="Git pull and restart", guild=guild_obj)
    async def cmd_update(interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            r = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=WORKING_DIR, capture_output=True, text=True, timeout=30,
            )
            output = (r.stdout.strip() + "\n" + r.stderr.strip()).strip()
            if r.returncode != 0:
                await interaction.followup.send(f"Git pull failed:\n{output[:1900]}")
                _log(f"update failed: {output[:200]}")
                return
            await interaction.followup.send(f"Pulled:\n{output[:1900]}\n\nRestarting...")
            _log(f"update pulled: {output[:200]}")
        except Exception as exc:
            await interaction.followup.send(f"Git pull error: {str(exc)[:1900]}")
            _log(f"update error: {str(exc)[:200]}")
            return
        _kill_child_procs()
        RESTART_FLAG.touch()

    @bot.tree.command(name="help", description="Show available commands", guild=guild_obj)
    async def cmd_help(interaction: discord.Interaction):
        help_text = """**Available Commands:**

• `/thread` - Create a new goal thread
• `/start` - Start/resume this thread's goal
• `/pause` - Pause this thread's goal
• `/stop` - Stop all goals in this channel
• `/status` - Show workspace or goal status
• `/ls` - List all goals in this channel
• `/delay` - Set step delay for this thread's goal
• `/delete` - Delete this thread's goal
• `/clear` - Clear all goals in this channel
• `/restart` - Restart arbos (pm2)
• `/update` - Git pull and restart
• `/help` - Show this help message"""
        await interaction.response.send_message(help_text)

    # ── Start bot ────────────────────────────────────────────────────────────

    _discord_client = bot
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _discord_loop = loop

    async def runner():
        async with bot:
            await bot.start(DISCORD_BOT_TOKEN)

    try:
        loop.run_until_complete(runner())
    except Exception as exc:
        _log(f"discord bot error: {str(exc)[:200]}")
    finally:
        loop.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def _kill_child_procs():
    """Kill all tracked claude child processes."""
    with _child_procs_lock:
        procs = list(_child_procs)
    for proc in procs:
        try:
            if proc.poll() is None:
                _log(f"killing child claude pid={proc.pid}")
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
    with _child_procs_lock:
        _child_procs.clear()


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
                _log(f"killed stale claude orphan pid={pid}")
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
    except Exception:
        pass


def _send_cli(args: list[str]):
    """CLI entry point: python arbos.py send 'message' [--file path]

    Within a step, all sends are consolidated into a single Discord message.
    The first send creates it; subsequent sends edit it by appending.
    Uses ARBOS_WORKSPACE/ARBOS_THREAD_ID env vars to find the per-goal step message file.
    """
    import argparse
    parser = argparse.ArgumentParser(description="Send a Discord message to the operator")
    parser.add_argument("message", nargs="?", help="Message text to send")
    parser.add_argument("--file", help="Send contents of a file instead")
    parsed = parser.parse_args(args)

    if not parsed.message and not parsed.file:
        parser.error("Provide a message or --file")

    if parsed.file:
        text = Path(parsed.file).read_text()
    else:
        text = parsed.message

    workspace = int(os.environ.get("ARBOS_WORKSPACE", "0"))
    thread_id = int(os.environ.get("ARBOS_THREAD_ID", "0"))

    if not workspace or not thread_id:
        print("ARBOS_WORKSPACE and ARBOS_THREAD_ID must be set", file=sys.stderr)
        sys.exit(1)

    smf = _step_msg_file(workspace, thread_id)
    smf.parent.mkdir(parents=True, exist_ok=True)

    if smf.exists():
        try:
            state = json.loads(smf.read_text())
            msg_id = int(state["msg_id"])
            channel_id = int(state.get("channel_id", thread_id))
            prev_text = state.get("text", "")
        except (json.JSONDecodeError, KeyError):
            msg_id = None
            channel_id = thread_id
            prev_text = ""
    else:
        msg_id = None
        channel_id = thread_id
        prev_text = ""

    if msg_id:
        combined = (prev_text + "\n\n" + text).strip()
        if _discord_rest_edit(channel_id, msg_id, combined):
            smf.write_text(json.dumps({"msg_id": msg_id, "channel_id": channel_id, "text": combined}))
            log_chat(workspace, "bot", combined[:1000])
            print(f"Edited step message ({len(combined)} chars)")
        else:
            new_id = _discord_rest_send(channel_id, text)
            if new_id:
                smf.write_text(json.dumps({"msg_id": new_id, "channel_id": channel_id, "text": text}))
                log_chat(workspace, "bot", text[:1000])
                print(f"Sent new message ({len(text)} chars)")
            else:
                print("Failed to send", file=sys.stderr)
                sys.exit(1)
    else:
        new_id = _discord_rest_send(channel_id, text)
        if new_id:
            smf.write_text(json.dumps({"msg_id": new_id, "channel_id": channel_id, "text": text}))
            log_chat(workspace, "bot", text[:1000])
            print(f"Sent ({len(text)} chars)")
        else:
            print("Failed to send (check DISCORD_BOT_TOKEN)", file=sys.stderr)
            sys.exit(1)


def _sendfile_cli(args: list[str]):
    """CLI entry point: python arbos.py sendfile path/to/file [--caption 'text']"""
    import argparse
    parser = argparse.ArgumentParser(description="Send a file to the operator via Discord")
    parser.add_argument("path", help="Path to the file to send")
    parser.add_argument("--caption", default="", help="Caption for the file")
    parsed = parser.parse_args(args)

    file_path = Path(parsed.path)
    if not file_path.exists():
        print(f"File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    thread_id = int(os.environ.get("ARBOS_THREAD_ID", "0"))
    if not thread_id:
        print("ARBOS_THREAD_ID must be set", file=sys.stderr)
        sys.exit(1)

    ok = _discord_rest_send_file(thread_id, str(file_path), caption=parsed.caption)
    if ok:
        print(f"Sent file: {file_path.name}")
    else:
        print("Failed to send (check DISCORD_BOT_TOKEN)", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "send":
        _send_cli(sys.argv[2:])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "sendfile":
        _sendfile_cli(sys.argv[2:])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "encrypt":
        env_path = WORKING_DIR / ".env"
        if not env_path.exists():
            if ENV_ENC_FILE.exists():
                print(".env.enc already exists (already encrypted)")
            else:
                print(".env not found, nothing to encrypt")
            return
        load_dotenv(env_path)
        bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not bot_token:
            print("DISCORD_BOT_TOKEN must be set in .env", file=sys.stderr)
            sys.exit(1)
        _encrypt_env_file(bot_token)
        print("Encrypted .env -> .env.enc, deleted plaintext.")
        print(f"On future starts: DISCORD_BOT_TOKEN='{bot_token}' python arbos.py")
        return

    if len(sys.argv) > 1 and sys.argv[1] not in ("send", "encrypt", "sendfile"):
        print(f"Unknown subcommand: {sys.argv[1]}", file=sys.stderr)
        print("Usage: arbos.py [send|sendfile|encrypt]", file=sys.stderr)
        sys.exit(1)

    _log(f"arbos starting in {WORKING_DIR} (provider={PROVIDER}, model={CLAUDE_MODEL})")
    _kill_stale_claude_procs()
    _reload_env_secrets()
    CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

    _load_all_workspaces()
    total_goals = sum(len(ws) for ws in _workspaces.values())
    _log(f"loaded {total_goals} goal(s) across {len(_workspaces)} workspace(s)")

    if not LLM_API_KEY:
        key_name = "OPENROUTER_API_KEY" if PROVIDER == "openrouter" else "CHUTES_API_KEY"
        _log(f"WARNING: {key_name} not set -- LLM calls will fail")

    def _handle_sigterm(signum, frame):
        _log("SIGTERM received; shutting down gracefully")
        _shutdown.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    if PROVIDER != "openrouter":
        _log(f"starting chutes proxy thread (port={PROXY_PORT}, agent={CHUTES_ROUTING_AGENT}, bot={CHUTES_ROUTING_BOT})")
        threading.Thread(target=_start_proxy, daemon=True).start()
        time.sleep(1)
    else:
        _log(f"openrouter direct mode -- no proxy needed (target={LLM_BASE_URL})")

    _write_claude_settings()

    threading.Thread(target=_goal_manager, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()

    while not _shutdown.is_set():
        if RESTART_FLAG.exists():
            RESTART_FLAG.unlink()
            _log("restart requested; killing children and exiting for pm2")
            _kill_child_procs()
            sys.exit(0)
        _process_pending_env()
        _shutdown.wait(timeout=1)

    _log("shutdown: killing children")
    _kill_child_procs()
    _log("shutdown complete")
    sys.exit(0)


if __name__ == "__main__":
    main()
