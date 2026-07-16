"""Public helpers for validating plain configuration dictionaries.

These helpers are the stable API for user projects and examples. Core modules
may keep using the private `_validation` formatting helpers until a focused
migration commit replaces duplicated validation logic deliberately.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from typing import TypeAlias

from rpacore._validation import type_error, value_error


ExpectedType: TypeAlias = type | tuple[type, ...]
_MISSING_DEFAULT = object()


@dataclass(frozen=True)
class ConfigField:
    """Describe one required or optional value for :func:`validate_config`."""

    key: str
    expected_type: ExpectedType
    required: bool = True
    default: object = _MISSING_DEFAULT
    choices: Iterable[object] | None = None
    min_value: object | None = None
    max_value: object | None = None
    allow_empty: bool = True
    _default_snapshot: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, str)
            or not self.key
            or any(not part.strip() for part in self.key.split("."))
        ):
            raise ValueError(f"Config field key must be a non-empty dotted key, got {self.key!r}")
        expected_types = _field_expected_types(self.expected_type)
        if not isinstance(self.required, bool):
            raise TypeError(f"{self.key} required must be bool")
        if not isinstance(self.allow_empty, bool):
            raise TypeError(f"{self.key} allow_empty must be bool")
        if self.required and self.default is not _MISSING_DEFAULT:
            raise ValueError("Required config fields cannot define a default")
        choice_values = _freeze_choices(self.key, self.choices)
        object.__setattr__(self, "choices", choice_values)
        _validate_bounds(
            self.key,
            self.expected_type,
            expected_types,
            min_value=self.min_value,
            max_value=self.max_value,
        )
        for choice in choice_values or ():
            _validate_value(
                self.key,
                choice,
                self.expected_type,
                choices=None,
                min_value=self.min_value,
                max_value=self.max_value,
                allow_empty=self.allow_empty,
            )
        if self.default is _MISSING_DEFAULT:
            object.__setattr__(self, "_default_snapshot", _MISSING_DEFAULT)
            return
        _validate_value(
            self.key,
            self.default,
            self.expected_type,
            choices=choice_values,
            min_value=self.min_value,
            max_value=self.max_value,
            allow_empty=self.allow_empty,
        )
        default = _copy_json_default(self.key, self.default)
        object.__setattr__(self, "default", default)
        object.__setattr__(self, "_default_snapshot", _copy_json_default(self.key, default))


def validate_config(
    config: dict[str, object],
    fields: Iterable[ConfigField],
) -> dict[str, object]:
    """Validate a flat field specification and return values by dotted key."""
    validated: dict[str, object] = {}
    seen: set[str] = set()
    for field in fields:
        if field.key in seen:
            raise ValueError(f"Duplicate config field: {field.key}")
        seen.add(field.key)
        found, value = _lookup_dotted_value(config, field.key)
        if not found:
            if field.required:
                raise KeyError(f"Missing required config key: {field.key}")
            if field._default_snapshot is _MISSING_DEFAULT:
                continue
            value = _copy_json_default(field.key, field._default_snapshot)
        validated[field.key] = _validate_value(
            field.key,
            value,
            field.expected_type,
            choices=field.choices,
            min_value=field.min_value,
            max_value=field.max_value,
            allow_empty=field.allow_empty,
        )
    return validated


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
    """Return a validated optional config value or default without mutating config."""
    if key not in config:
        return _validate_value(
            key,
            default,
            expected_type,
            choices=choices,
            min_value=min_value,
            max_value=max_value,
            allow_empty=allow_empty,
        )
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


def _lookup_dotted_value(config: dict[str, object], key: str) -> tuple[bool, object]:
    current: object = config
    parts = key.split(".")
    for index, part in enumerate(parts):
        if not isinstance(current, dict):
            section = ".".join(parts[:index])
            raise type_error(section, "dict", current)
        if part not in current:
            return False, None
        current = current[part]
    return True, current


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

    choice_values = _choice_values(key, choices)
    if choice_values is not None and value not in choice_values:
        raise value_error(key, _choices_text(choice_values), value)

    if (
        (min_value is not None or max_value is not None)
        and isinstance(value, float)
        and not math.isfinite(value)
    ):
        raise value_error(key, f"finite {expected_text}", value)

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


def _choice_values(
    key: str,
    choices: Iterable[object] | None,
) -> list[object] | None:
    if choices is None:
        return None
    if isinstance(choices, (str, bytes)):
        raise TypeError(f"{key} choices must be a non-string iterable")
    values = list(choices)
    if not values:
        raise ValueError(f"{key} choices must not be empty")
    return values


def _field_expected_types(expected_type: object) -> tuple[type, ...]:
    if isinstance(expected_type, type):
        return (expected_type,)
    if (
        not isinstance(expected_type, tuple)
        or not expected_type
        or any(not isinstance(value, type) for value in expected_type)
    ):
        raise TypeError("Config field expected_type must be a type or non-empty tuple of types")
    return expected_type


def _freeze_choices(key: str, choices: Iterable[object] | None) -> tuple[object, ...] | None:
    if choices is None:
        return None
    if isinstance(choices, (str, bytes)):
        raise TypeError(f"{key} choices must be a non-string iterable")
    try:
        values = tuple(choices)
    except TypeError as exc:
        raise TypeError(f"{key} choices must be a non-string iterable") from exc
    if not values:
        raise ValueError(f"{key} choices must not be empty")
    return values


def _validate_bounds(
    key: str,
    expected_type: ExpectedType,
    expected_types: tuple[type, ...],
    *,
    min_value: object | None,
    max_value: object | None,
) -> None:
    for name, value in (("min_value", min_value), ("max_value", max_value)):
        if value is None:
            continue
        _validate_value(
            f"{key} {name}",
            value,
            expected_type,
            choices=None,
            min_value=None,
            max_value=None,
            allow_empty=True,
        )
    if min_value is None or max_value is None:
        return
    try:
        if min_value > max_value:  # type: ignore[operator]
            raise ValueError(f"{key} min_value must be <= max_value")
    except TypeError as exc:
        expected_text = _expected_text(expected_types)
        raise value_error(key, f"{expected_text} comparable bounds", min_value) from exc


def _copy_json_default(key: str, value: object) -> object:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{key} default must be JSON-safe") from exc
    return deepcopy(value)


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
