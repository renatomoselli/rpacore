"""Public helpers for validating plain configuration dictionaries.

These helpers are the stable API for user projects and examples. Core modules
may keep using the private `_validation` formatting helpers until a focused
migration commit replaces duplicated validation logic deliberately.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeAlias

from rpacore._validation import type_error, value_error


ExpectedType: TypeAlias = type | tuple[type, ...]


def require_config(
    config: dict[str, object],
    key: str,
    expected_type: ExpectedType,
    *,
    choices: Iterable[object] | None = None,
    min_value: object | None = None,
    max_value: object | None = None,
    allow_empty: bool = True,
) -> object:
    """Return a required config value after explicit type and domain checks."""
    if key not in config:
        raise KeyError(f"Missing required config key: {key}")
    return _validate_value(
        key,
        config[key],
        expected_type,
        choices=choices,
        min_value=min_value,
        max_value=max_value,
        allow_empty=allow_empty,
    )


def optional_config(
    config: dict[str, object],
    key: str,
    expected_type: ExpectedType,
    default: object,
    *,
    choices: Iterable[object] | None = None,
    min_value: object | None = None,
    max_value: object | None = None,
    allow_empty: bool = True,
) -> object:
    """Return an optional config value or default without mutating config."""
    if key not in config:
        return default
    return _validate_value(
        key,
        config[key],
        expected_type,
        choices=choices,
        min_value=min_value,
        max_value=max_value,
        allow_empty=allow_empty,
    )


def require_section(config: dict[str, object], key: str) -> dict[str, object]:
    """Return a required nested config section."""
    value = require_config(config, key, dict)
    return value  # type: ignore[return-value]


def _validate_value(
    key: str,
    value: object,
    expected_type: ExpectedType,
    *,
    choices: Iterable[object] | None,
    min_value: object | None,
    max_value: object | None,
    allow_empty: bool,
) -> object:
    expected_types = _expected_types(expected_type)
    expected_text = _expected_text(expected_types)
    if _is_rejected_bool(value, expected_types) or not isinstance(value, expected_types):
        raise type_error(key, expected_text, value)

    if not allow_empty and _is_empty(value):
        raise value_error(key, f"non-empty {expected_text}", value)

    choice_values = list(choices) if choices is not None else None
    if choice_values is not None and value not in choice_values:
        raise value_error(key, _choices_text(choice_values), value)

    if min_value is not None and _is_below_min(key, expected_text, value, min_value):
        raise value_error(key, f"{expected_text} >= {min_value!r}", value)
    if max_value is not None and _is_above_max(key, expected_text, value, max_value):
        raise value_error(key, f"{expected_text} <= {max_value!r}", value)

    return value


def _expected_types(expected_type: ExpectedType) -> tuple[type, ...]:
    if isinstance(expected_type, tuple):
        return expected_type
    return (expected_type,)


def _expected_text(expected_types: tuple[type, ...]) -> str:
    return " | ".join(t.__name__ for t in expected_types)


def _choices_text(choices: list[object]) -> str:
    return "one of " + ", ".join(repr(choice) for choice in choices)


def _is_below_min(key: str, expected_text: str, value: object, min_value: object) -> bool:
    try:
        return value < min_value  # type: ignore[operator]
    except TypeError as exc:
        raise value_error(key, f"{expected_text} comparable to {min_value!r}", value) from exc


def _is_above_max(key: str, expected_text: str, value: object, max_value: object) -> bool:
    try:
        return value > max_value  # type: ignore[operator]
    except TypeError as exc:
        raise value_error(key, f"{expected_text} comparable to {max_value!r}", value) from exc


def _is_empty(value: object) -> bool:
    return isinstance(value, (str, list, tuple, dict, set, frozenset)) and len(value) == 0


def _is_rejected_bool(value: object, expected_types: tuple[type, ...]) -> bool:
    if not isinstance(value, bool):
        return False
    if bool in expected_types:
        return False
    return int in expected_types or float in expected_types
