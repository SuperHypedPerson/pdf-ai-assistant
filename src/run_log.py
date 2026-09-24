"""
Stage 6: per-run summary logging. Appends one JSON line per run to a log
file (so it's easy to grep/parse later) and prints a human-readable
summary to the terminal at the end of the run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LOG_PATH = Path("run_log.jsonl")


def log_run(entry: dict, path: str | Path = DEFAULT_LOG_PATH) -> dict:
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
    path = Path(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def print_summary(entry: dict) -> None:
    print("\nRun summary:")
    for key, value in entry.items():
        if key == "timestamp":
            continue
        print(f"  {key}: {value}")
