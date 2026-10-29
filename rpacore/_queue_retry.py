"""Queue delivery timing, independent from Engine's in-claim retry policy."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from rpacore._validation import type_error, value_error


MAX_RETRY_DELAY = 86_400.0


def retry_policy(config: dict[str, object]) -> tuple[float, float]:
    values = []
    for key, default, minimum in (("retry_delay", 1.0, 0), ("retry_backoff", 2.0, 1)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise type_error(f"queue.{key}", "finite number", value)
        try:
            number = float(value)
        except OverflowError:
            raise value_error(f"queue.{key}", "finite number", value) from None
        if not math.isfinite(number) or number < minimum:
            raise value_error(f"queue.{key}", f"finite number >= {minimum}", value)
        if key == "retry_delay" and number > MAX_RETRY_DELAY:
            raise value_error("queue.retry_delay", "number <= 86400", value)
        values.append(number)
    return values[0], values[1]


def available_at_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise type_error("queue.available_at", "aware datetime | None", value)
    if value.utcoffset() is None:
        raise value_error("queue.available_at", "aware datetime", value)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_available_at(raw: str | None) -> datetime | None:
    """Reject stored deadlines that cannot safely use UTC text comparisons."""
    value = datetime.fromisoformat(raw) if raw is not None else None
    if available_at_text(value) != raw:
        raise ValueError("queue.available_at must use canonical UTC microseconds")
    return value


def retry_available_at(
    now: datetime, retry_count: int, delay: float, backoff: float,
) -> str | None:
    if delay == 0:
        return None
    # Bound the exponent first, without losing ordinary values to a log roundtrip.
    log_seconds = math.log(delay) + (retry_count - 1) * math.log(backoff)
    if log_seconds >= math.log(MAX_RETRY_DELAY):
        seconds = MAX_RETRY_DELAY
    else:
        try:
            seconds = delay * backoff ** (retry_count - 1)
        except OverflowError:
            # A tiny base can offset an overflowing intermediate exponent.
            seconds = math.exp(log_seconds)
    wait = timedelta(microseconds=max(1, math.ceil(seconds * 1_000_000)))
    return available_at_text(now + wait)
