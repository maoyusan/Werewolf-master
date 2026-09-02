from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


_REDACT_KEYS = ("app_secret", "secret", "token", "authorization", "password")


def _redact(value: str) -> str:
    lowered = value.casefold()
    if any(key in lowered for key in _REDACT_KEYS):
        return "[redacted]"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        for key in ("event_id", "room_id", "delivery_id", "session_id", "reason"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = str(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
