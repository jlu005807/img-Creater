"""Structured logging support for the image-generation backend.

Provides a JSON formatter and a context-aware logger adapter that injects
correlation fields (task_id, history_id, api_id, api_name, attempt, etc.)
into every log record without changing call sites.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects.

    Fields: timestamp, level, logger, message, plus any extra fields attached
    to the record (e.g. task_id, history_id from the adapter).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Pull any extra attributes added by the adapter or logextra.
        for key, value in record.__dict__.items():
            if key in payload or key.startswith("_"):
                continue
            if key in ("args", "msg", "levelname", "name", "created", "msecs",
                        "relativeCreated", "exc_info", "exc_text", "stack_info",
                        "filename", "module", "funcName", "lineno", "thread",
                        "threadName", "process", "processName", "pathname",
                        "levelno", "message"):
                continue
            if isinstance(value, (str, int, float, bool, type(None))):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TaskLogAdapter(logging.LoggerAdapter):
    """Logger adapter that injects task correlation fields into every record.

    Usage:
        adapter = TaskLogAdapter(logger, {"task_id": "abc123"})
        adapter.info("task queued", extra={"providers": 2})
    """

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        # Merge adapter extra into the record's extra.
        extra = kwargs.get("extra", {})
        merged = {**self.extra, **extra}
        kwargs["extra"] = merged
        return msg, kwargs


def configure_structured_logging(level: int = logging.INFO) -> None:
    """Replace the root logger's handlers with a JSON formatter.

    Safe to call from create_app() in place of basicConfig.
    """
    root = logging.getLogger()
    root.setLevel(level)
    # Remove existing handlers to avoid duplicate output.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


__all__ = ["JsonFormatter", "TaskLogAdapter", "configure_structured_logging"]
