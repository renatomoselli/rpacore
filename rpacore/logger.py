"""Logging helpers for rpacore."""

from __future__ import annotations

import json
import logging
import math
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterator, TextIO

_LOGGER_NAME = "rpacore"
LOG_FORMAT_VERSION = 3
_RESERVED_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)
_REDACTED_EXTRA_FIELDS = frozenset({"config", "credentials", "resources"})
_STRUCTURED_REDACTED_EXTRA_FIELDS = _REDACTED_EXTRA_FIELDS | frozenset(
    {"state", "metadata", "path", "paths", "url", "urls"}
)
_CANONICAL_JSON_FIELDS = frozenset(
    {
        "log_format_version",
        "timestamp",
        "severity",
        "logger",
        "event",
        "message",
        "attributes",
        "exception",
        "stacktrace",
    }
)
_LOG_CONTEXT_FIELDS = frozenset(
    {
        "transaction_id",
        "transaction_reference",
        "step_name",
        "step_execution_order",
        "queue_item_id",
        "queue_reference",
        "worker_id",
        "retry_number",
        "attempt_number",
    }
)
_LOG_CONTEXT: ContextVar[dict[str, str | int]] = ContextVar(
    "rpacore_log_context",
    default={},
)
_EVENT_NAMES = {
    "queue_run_completed": "rpacore.queue.run_completed",
}
_OWNED_HANDLER_ATTRIBUTE = "_rpacore_owned_handler"


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return user-supplied LogRecord fields."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_FIELDS and not key.startswith("_")
    }


def _sanitized_extra_fields(
    extra: dict[str, object],
    *,
    redacted_fields: frozenset[str] = _REDACTED_EXTRA_FIELDS,
    canonical_fields: frozenset[str] = _CANONICAL_JSON_FIELDS,
) -> dict[str, object]:
    """Return safe event attributes without canonical-envelope collisions."""
    return {
        key: _json_log_value(value, redacted_fields=redacted_fields)
        for key, value in extra.items()
        if key not in redacted_fields and key not in canonical_fields
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
    """JSON log-v3 formatter with a protected structured envelope."""

    def format(self, record: logging.LogRecord) -> str:
        extra = _extra_fields(record)
        event = str(extra.pop("event", "log"))
        attributes = _sanitized_extra_fields(
            extra,
            redacted_fields=_STRUCTURED_REDACTED_EXTRA_FIELDS,
            canonical_fields=_CANONICAL_JSON_FIELDS,
        )
        attributes.update(_LOG_CONTEXT.get())
        payload: dict[str, object] = {
            "log_format_version": LOG_FORMAT_VERSION,
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "severity": record.levelname.lower(),
            "logger": record.name,
            "event": _event_name(event),
            "message": record.getMessage(),
            "attributes": attributes,
        }
        if record.exc_info:
            exception_type, exception, _ = record.exc_info
            payload["exception"] = {
                "type": (
                    exception_type.__name__
                    if exception_type is not None
                    else "Exception"
                ),
                "message": "" if exception is None else str(exception),
                "stacktrace": self.formatException(record.exc_info),
            }
        if record.stack_info:
            payload["stacktrace"] = self.formatStack(record.stack_info)
        return json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _event_name(event: str) -> str:
    """Return the documented v3 vocabulary name for a framework event."""
    if event.startswith("rpacore."):
        return event
    return _EVENT_NAMES.get(event, f"rpacore.{event.replace('_', '.')}")


@contextmanager
def bind_log_context(**attributes: str | int) -> Iterator[None]:
    """Temporarily bind approved scalar correlation attributes to log records.

    Context is local to the current execution context. Callers that create a
    new thread must bind the needed fields again in that thread.
    """
    invalid = set(attributes) - _LOG_CONTEXT_FIELDS
    if invalid:
        raise ValueError(
            "Unsupported log context field(s): "
            + ", ".join(sorted(invalid))
        )
    for key, value in attributes.items():
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise TypeError(
                f"log context {key!r} must be a str or int, got {value!r}"
            )
        if isinstance(value, str) and not value:
            raise ValueError(f"log context {key!r} must not be empty")
        if isinstance(value, int) and value < 0:
            raise ValueError(f"log context {key!r} must be >= 0")
    token = _LOG_CONTEXT.set({**_LOG_CONTEXT.get(), **attributes})
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def _json_log_value(
    value: object,
    *,
    redacted_fields: frozenset[str] = _REDACTED_EXTRA_FIELDS,
) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, list):
        return [_json_log_value(item, redacted_fields=redacted_fields) for item in value]
    if isinstance(value, tuple):
        return [_json_log_value(item, redacted_fields=redacted_fields) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_json_log_value(item, redacted_fields=redacted_fields) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, dict):
        return {
            str(key): _json_log_value(item, redacted_fields=redacted_fields)
            for key, item in value.items()
            if str(key) not in redacted_fields
        }
    return type(value).__name__


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return a rpacore logger without forcing output.

    Names outside the framework hierarchy become ``rpacore.application.*``
    descendants so configuring the ``rpacore`` root captures framework and
    application events consistently.
    """
    resolved_name = _logger_name(name)
    logger = logging.getLogger(resolved_name)
    if resolved_name == _LOGGER_NAME and not logger.handlers:
        handler = logging.NullHandler()
        setattr(handler, _OWNED_HANDLER_ATTRIBUTE, True)
        logger.addHandler(handler)
    logger.propagate = resolved_name != _LOGGER_NAME
    return logger


def _logger_name(name: str) -> str:
    if name == _LOGGER_NAME or name.startswith(f"{_LOGGER_NAME}."):
        return name
    return f"{_LOGGER_NAME}.application.{name}"


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

    logger = logging.getLogger(_logger_name(name))
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
