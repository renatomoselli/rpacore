"""Private time source for deterministic queue timing tests."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class _Clock(Protocol):
    """Internal source for the queue's durable time and retry waits."""

    def now_utc(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    """The immutable production clock implementation."""

    __slots__ = ()

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_SYSTEM_CLOCK: _Clock = _SystemClock()
