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

# Project loggers use fixed snake_case events; a library logger's format string
# (only when it came with separate arguments, so it is code, not data) is kept
# without the arguments, up to this many characters.
TEMPLATE_MAX = 160
IDENTIFIER_RE = re.compile(r"[a-z0-9_]{1,60}")
# Only the characters a code template uses; anything else and it is left out.
TEMPLATE_RE = re.compile(
    r"[A-Za-z0-9 _%()\[\]:;,.'\"=<>/!?+-]{1," + str(TEMPLATE_MAX) + "}"
)

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
        # Fixed YooKassa codes and exception class names only, never free text.
        for key in ("provider_code", "failure", "reason"):
            value = record.__dict__.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z_]{0,60}", value):
                payload[key] = value
        # Scheduler job names are fixed identifiers from the code.
        job = record.__dict__.get("job")
        if isinstance(job, str) and IDENTIFIER_RE.fullmatch(job):
            payload["job"] = job
        if (
            payload["event"] == "application_event"
            and record.args
            and isinstance(record.msg, str)
        ):
            # A library's own message template (aiogram, apscheduler): the text
            # written in its code, never the arguments that fill it in.
            first_line = record.msg.split("\n", 1)[0].strip()
            if TEMPLATE_RE.fullmatch(first_line):
                payload["template"] = first_line
        if record.exc_info and record.exc_info[0]:
            payload["error_type"] = record.exc_info[0].__name__
            exc = record.exc_info[1]
            cause = None if exc is None else (exc.__cause__ or exc.__context__)
            if cause is not None:
                # The class only, e.g. why the database gateway was unreachable.
                payload["cause_type"] = type(cause).__name__
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
