"""Logging helpers for rpacore."""

from __future__ import annotations

import json
import logging
import math
import sys
from datetime import datetime, timezone
from typing import TextIO

_LOGGER_NAME = "rpacore"
LOG_FORMAT_VERSION = 1
_RESERVED_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_REDACTED_EXTRA_FIELDS = frozenset({"config", "credentials", "resources"})
_CANONICAL_JSON_FIELDS = frozenset(
    {
        "log_format_version",
        "timestamp",
        "event",
        "level",
        "message",
        "exception",
        "stack",
    }
)
_OWNED_HANDLER_ATTRIBUTE = "_rpacore_owned_handler"


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return user-supplied LogRecord fields."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_FIELDS and not key.startswith("_")
    }


def _sanitized_extra_fields(extra: dict[str, object]) -> dict[str, object]:
    """Return safe event attributes without canonical-envelope collisions."""
    return {
        key: _json_log_value(value)
        for key, value in extra.items()
        if key not in _REDACTED_EXTRA_FIELDS
        and key not in _CANONICAL_JSON_FIELDS
    }


class TextFormatter(logging.Formatter):
    """Human-readable formatter for rpacore events."""

    def format(self, record: logging.LogRecord) -> str:
        extra = _extra_fields(record)
        event = str(extra.pop("event", "")) if "event" in extra else ""
        extra = _sanitized_extra_fields(extra)

        parts = [record.levelname]
        if event:
            parts.append(event)
        parts.append(record.getMessage())

        if extra:
            details = " ".join(f"{key}={extra[key]}" for key in sorted(extra))
            parts.append(details)

        if record.exc_info:
            parts.append(self.formatException(record.exc_info))
        if record.stack_info:
            parts.append(self.formatStack(record.stack_info))

        return " | ".join(parts)


class JsonFormatter(logging.Formatter):
    """JSON formatter for rpacore events."""

    def format(self, record: logging.LogRecord) -> str:
        extra = _extra_fields(record)
        event = str(extra.pop("event", "log"))
        payload: dict[str, object] = {
            "log_format_version": LOG_FORMAT_VERSION,
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "event": event,
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        payload.update(_sanitized_extra_fields(extra))
        if record.exc_info:
            exception_type, exception, _ = record.exc_info
            payload["exception"] = {
                "type": (
                    exception_type.__name__
                    if exception_type is not None
                    else "Exception"
                ),
                "message": "" if exception is None else str(exception),
                "traceback": self.formatException(record.exc_info),
            }
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _json_log_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, list):
        return [_json_log_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_log_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_json_log_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, dict):
        return {
            str(key): _json_log_value(item)
            for key, item in value.items()
            if str(key) not in _REDACTED_EXTRA_FIELDS
        }
    return type(value).__name__


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return the rpacore logger without forcing output."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.NullHandler()
        setattr(handler, _OWNED_HANDLER_ATTRIBUTE, True)
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def configure_logger(
    *,
    name: str = _LOGGER_NAME,
    level: str | int = logging.INFO,
    fmt: str = "text",
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure and return an rpacore logger.

    RPA Core-owned handlers are atomically replaced on each successful call so
    repeated configuration does not duplicate them. Handlers installed by the
    embedding application are preserved.
    """
    if fmt == "text":
        formatter: logging.Formatter = TextFormatter()
    elif fmt == "json":
        formatter = JsonFormatter()
    else:
        raise ValueError(f"fmt must be 'text' or 'json', got {fmt!r}")

    level_probe = logging.Logger("rpacore.level-validation")
    level_probe.setLevel(level)
    validated_level = level_probe.level

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    setattr(handler, _OWNED_HANDLER_ATTRIBUTE, True)

    logger = logging.getLogger(name)
    previous_owned = [
        existing
        for existing in logger.handlers
        if getattr(existing, _OWNED_HANDLER_ATTRIBUTE, False)
    ]
    application_handlers = [
        existing
        for existing in logger.handlers
        if not getattr(existing, _OWNED_HANDLER_ATTRIBUTE, False)
    ]
    logger.handlers = [*application_handlers, handler]
    logger.propagate = False
    logger.setLevel(validated_level)
    for previous in previous_owned:
        previous.close()
    return logger
