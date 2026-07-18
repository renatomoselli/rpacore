"""Stable outcome and retry vocabulary for durable operator inspection."""

from __future__ import annotations

import re
from enum import StrEnum


class OutcomeCategory(StrEnum):
    """Why a transaction or queue attempt ended, independent of lifecycle status."""

    SUCCESSFUL = "successful"
    BUSINESS_FAILED = "business_failed"
    SYSTEM_FAILED = "system_failed"
    VALIDATION_FAILED = "validation_failed"
    INTERRUPTED = "interrupted"
    LEASE_LOST = "lease_lost"
    UNKNOWN = "unknown"


class RetryDisposition(StrEnum):
    """The final retry action actually decided at the owning execution boundary."""

    NOT_APPLICABLE = "not_applicable"
    NOT_REQUESTED = "not_requested"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRY_EXHAUSTED = "retry_exhausted"
    UNKNOWN = "unknown"


_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")


def validate_failure_code(code: str) -> str:
    """Return a valid optional, namespaced failure code.

    ``rpacore.*`` is reserved for framework-owned causes. Applications should
    use their own namespace, for example ``acme.invoice.missing_number``.
    """
    if not isinstance(code, str):
        raise TypeError(f"code must be a str, got {type(code).__name__}")
    if code and _FAILURE_CODE.fullmatch(code) is None:
        raise ValueError(
            "code must be empty or lowercase dot-separated ASCII segments, "
            "for example 'acme.invoice.missing_number'"
        )
    return code
