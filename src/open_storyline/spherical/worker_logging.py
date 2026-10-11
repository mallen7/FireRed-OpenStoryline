"""Small structured-log formatter with mandatory secret redaction."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from open_storyline.spherical.worker_security import sanitize_value


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "structured_fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(sanitize_value(payload), separators=(",", ":"), ensure_ascii=False)


def configure_structured_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("open_storyline.spherical.worker")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"structured_fields": fields})
