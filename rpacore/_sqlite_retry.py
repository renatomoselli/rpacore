"""Shared SQLite retry helpers for short-lived lock handling."""

from __future__ import annotations

import sqlite3


def is_transient_sqlite_lock(error: sqlite3.OperationalError) -> bool:
    """Return True for SQLite lock/busy errors worth a short retry."""
    message = str(error).lower()
    return "locked" in message or "busy" in message


def sqlite_retry_delay(attempt: int, *, base_delay_seconds: float) -> float:
    """Return the exponential retry delay for a zero-based attempt index."""
    return base_delay_seconds * (2**attempt)
