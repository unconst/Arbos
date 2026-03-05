#!/usr/bin/env python3
"""Pause the agent loop for a specified duration.

The next plan+execute cycle will not start until the pause expires.

Usage:
    python tools/pause.py 30m          # pause for 30 minutes
    python tools/pause.py 2h           # pause for 2 hours
    python tools/pause.py 90s          # pause for 90 seconds
    python tools/pause.py 1h30m        # pause for 1 hour 30 minutes
    python tools/pause.py clear        # cancel any active pause
"""
import re
import sys
import time
from pathlib import Path

PAUSE_FILE = Path(__file__).resolve().parent.parent / ".pause_until"


def parse_duration(s: str) -> int:
    """Parse a human duration string like '2h30m' into seconds."""
    s = s.strip().lower()

    # Try pure number (treat as minutes)
    if s.isdigit():
        return int(s) * 60

    total = 0
    for match in re.finditer(r"(\d+)\s*(h|m|s)", s):
        val = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            total += val * 3600
        elif unit == "m":
            total += val * 60
        elif unit == "s":
            total += val

    if total == 0:
        print(f"ERROR: Could not parse duration '{s}'. Use e.g. 30m, 2h, 90s, 1h30m", file=sys.stderr)
        sys.exit(1)

    return total


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]

    if arg == "clear":
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
            print("Pause cleared — agent will resume on next cycle")
        else:
            print("No active pause")
        return

    seconds = parse_duration(arg)
    resume_at = time.time() + seconds
    PAUSE_FILE.write_text(str(resume_at))

    mins = seconds / 60
    if mins >= 60:
        h, m = divmod(int(mins), 60)
        human = f"{h}h {m}m"
    else:
        human = f"{mins:.0f}m"

    print(f"Paused for {human} (resumes at {time.strftime('%H:%M:%S', time.localtime(resume_at))})")


if __name__ == "__main__":
    main()
