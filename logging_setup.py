"""Structured JSON logging with a per-request correlation id."""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id",
    default="-",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": (
                record.msg
                if isinstance(record.msg, str)
                and not record.args
                and re.fullmatch(r"[a-z]+(?:_[a-z0-9]+)+", record.msg)
                else "application_event"
            ),
            "request_id": request_id_var.get(),
        }
        # Operational counters only: no IDs, addresses, tokens, destinations,
        # arbitrary extras or exception messages/tracebacks on disk/stdout.
        for key in ("status", "status_code", "attempt", "retry_in", "count"):
            value = record.__dict__.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                payload[key] = value
        if record.exc_info and record.exc_info[0]:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", log_dir: str | None = "logs") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    formatter = JsonFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir:
        path = Path(log_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path / "bot.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError:
            root.warning("log_dir_not_writable", extra={"log_dir": log_dir})

    for noisy in ("aiohttp.access", "httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
