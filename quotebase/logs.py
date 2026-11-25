"""Logging. Every record goes to the console and to logs/YYYY-MM-DD.jsonl."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from . import config

_RUN_ID: int | None = None


def set_run_id(run_id: int | None) -> None:
    global _RUN_ID
    _RUN_ID = run_id


class JsonFileHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        now = datetime.now(timezone.utc)
        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": _RUN_ID,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if extra:
            entry["data"] = extra
        if record.exc_info:
            entry["exc"] = logging.Formatter().formatException(record.exc_info)
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = config.LOG_DIR / f"{now:%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def setup(level: str = "INFO") -> None:
    root = logging.getLogger("quotebase")
    if root.handlers:
        return
    root.setLevel(level)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    root.addHandler(JsonFileHandler())


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"quotebase.{name}")
