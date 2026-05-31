"""Internal helpers for validation error messages."""

from __future__ import annotations


def type_error(key: str, expected: str, value: object) -> TypeError:
    return TypeError(
        f"{key} expected {expected}; got {type(value).__name__} value={value!r}"
    )


def value_error(key: str, expected: str, value: object) -> ValueError:
    return ValueError(
        f"{key} expected {expected}; got {type(value).__name__} value={value!r}"
    )