"""Validation helpers for durable JSON transaction state."""

from __future__ import annotations

import math


class JsonStateError(TypeError):
    """Raised when durable framework data is not JSON-safe."""


def validate_json_safe(value: object, *, path: str) -> None:
    """Raise TypeError if value cannot be safely represented as JSON."""
    _validate_json_safe(value, path=path, active=set())


def validate_json_object(value: object, *, path: str) -> None:
    """Raise TypeError if value is not a JSON-safe object mapping."""
    if not isinstance(value, dict):
        raise _state_type_error(path, "JSON object", value)
    _validate_json_safe(value, path=path, active=set())


def _validate_json_safe(value: object, *, path: str, active: set[int]) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _state_type_error(path, "finite JSON number", value)
        return
    if isinstance(value, list):
        _enter_container(value, path=path, active=active)
        try:
            for index, item in enumerate(value):
                _validate_json_safe(item, path=f"{path}[{index}]", active=active)
        finally:
            active.remove(id(value))
        return
    if isinstance(value, dict):
        _enter_container(value, path=path, active=active)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _state_type_error(f"{path}[{key!r}]", "string key", key)
                _validate_json_safe(item, path=f"{path}[{key!r}]", active=active)
        finally:
            active.remove(id(value))
        return
    raise _state_type_error(path, "JSON value", value)


def _enter_container(value: object, *, path: str, active: set[int]) -> None:
    value_id = id(value)
    if value_id in active:
        raise _state_type_error(path, "acyclic JSON value", value)
    active.add(value_id)


def _state_type_error(path: str, expected: str, value: object) -> TypeError:
    return JsonStateError(
        f"{path} expected {expected}; got {type(value).__name__} value={value!r}. "
        "Durable RPA Core data must be JSON-safe. Move runtime objects to "
        "ctx.resources or persist an artifact path in ctx.state."
    )
