"""Logging and formatting utilities."""

from datetime import datetime

from arbos.config import COST_PER_M_INPUT, COST_PER_M_OUTPUT
from arbos.state import tls, log_lock, token_lock, token_usage
from arbos.redact import redact_secrets


def file_log(msg: str):
    fh = getattr(tls, "log_fh", None)
    if fh:
        with log_lock:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"{ts}  {redact_secrets(msg)}\n")
            fh.flush()


def log(msg: str, *, blank: bool = False):
    safe = redact_secrets(msg)
    if blank:
        print(flush=True)
    print(safe, flush=True)
    file_log(safe)


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def reset_tokens():
    with token_lock:
        token_usage["input"] = 0
        token_usage["output"] = 0


def get_tokens() -> tuple[int, int]:
    with token_lock:
        return token_usage["input"], token_usage["output"]


def fmt_tokens(inp: int, out: int, elapsed: float = 0) -> str:
    def _k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
    tps = ""
    if elapsed > 0 and out > 0:
        tps = f" | {out / elapsed:.0f} t/s"
    cost = (inp * COST_PER_M_INPUT + out * COST_PER_M_OUTPUT) / 1_000_000
    cost_str = f" | ${cost:.4f}" if cost >= 0.0001 else ""
    return f"{_k(inp)} in / {_k(out)} out{tps}{cost_str}"
