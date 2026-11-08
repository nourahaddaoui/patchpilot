"""Append-only JSONL log of every tool call: input, output, time.

One file per run: logs/<run_id>.jsonl. One line per call.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
MAX_LOGGED_OUTPUT = 20_000


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


class CallLogger:
    def __init__(self, run_id: str | None = None, log_dir: Path | str = DEFAULT_LOG_DIR):
        self.run_id = run_id or new_run_id()
        self.path = Path(log_dir) / f"{self.run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, record: dict) -> None:
        line = {"run_id": self.run_id, **record}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
