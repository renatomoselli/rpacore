"""Logging helpers for OREF."""

from __future__ import annotations

import json
import logging
import sys
from typing import TextIO

_LOGGER_NAME = "oref"
_RESERVED_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return user-supplied LogRecord fields."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_FIELDS and not key.startswith("_")
    }


class TextFormatter(logging.Formatter):
    """Human-readable formatter for OREF events."""

    def format(self, record: logging.LogRecord) -> str:
        extra = _extra_fields(record)
        event = str(extra.pop("event", "")) if "event" in extra else ""

        parts = [record.levelname]
        if event:
            parts.append(event)
        parts.append(record.getMessage())

        if extra:
            details = " ".join(f"{key}={extra[key]}" for key in sorted(extra))
            parts.append(details)

        return " | ".join(parts)


class JsonFormatter(logging.Formatter):
    """JSON formatter for OREF events."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        return json.dumps(payload, default=str)


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return the OREF logger without forcing output."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def configure_logger(
    *,
    name: str = _LOGGER_NAME,
    level: str | int = logging.INFO,
    fmt: str = "text",
    stream: TextIO | None = None,
) -> logging.Logger:
    """Configure and return an OREF logger.

    The logger is reset on each call so repeated configuration does not
    duplicate handlers.
    """
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(level)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    if fmt == "text":
        handler.setFormatter(TextFormatter())
    elif fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        raise ValueError(f"fmt must be 'text' or 'json', got {fmt!r}")

    logger.addHandler(handler)
    return logger
