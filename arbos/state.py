"""All mutable global state — locks, events, shared data structures."""

import asyncio
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

from arbos.config import MAX_CONCURRENT


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
    force_next: bool = False
    thread: threading.Thread | None = field(default=None, repr=False)
    wake: threading.Event = field(default_factory=threading.Event, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)


# Thread-local storage (used by log module for per-thread log file handles)
tls = threading.local()

# Locks
log_lock = threading.Lock()
chatlog_lock = threading.Lock()
goals_lock = threading.Lock()
token_lock = threading.Lock()
child_procs_lock = threading.Lock()

# Events
shutdown = threading.Event()

# Semaphores
claude_semaphore = threading.Semaphore(MAX_CONCURRENT)

# Counters
step_count = 0
token_usage = {"input": 0, "output": 0}

# Child processes tracked for cleanup
child_procs: set[subprocess.Popen] = set()

# Discord client references (set by bot.py at startup)
discord_client: Any = None
discord_loop: asyncio.AbstractEventLoop | None = None
discord_async_failures: int = 0
DISCORD_ASYNC_MAX_FAILURES: int = 3

# Workspaces: {channel_id: {thread_id: GoalState}}
workspaces: dict[int, dict[int, GoalState]] = {}
