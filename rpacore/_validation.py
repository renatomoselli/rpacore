"""Internal helpers for validation error messages."""

from __future__ import annotations

import re
from pathlib import Path

PRERELEASE_WHEEL_PATTERN = re.compile(r"(?:a|b|rc)\d", re.IGNORECASE)


class ValidationFailure(ValueError):
    """Base class for validation-tool failures."""


class ValidationError(ValidationFailure):
    """Validation failed before a tool could continue."""


def validate_contained_path(
    path: Path,
    *,
    allowed_roots: tuple[Path, ...],
    label: str,
) -> Path:
    if not isinstance(path, Path):
        raise ValidationError(f"{label} must be a Path, got {type(path).__name__}")
    resolved = path.resolve()
    for root in allowed_roots:
        if not isinstance(root, Path):
            raise ValidationError(f"{label} allowed root must be a Path, got {type(root).__name__}")
        resolved_root = root.resolve()
        if resolved == resolved_root or resolved.is_relative_to(resolved_root):
            return resolved
    roots = ", ".join(str(root.resolve()) for root in allowed_roots if isinstance(root, Path))
    raise ValidationError(f"{label} resolves outside allowed roots: {resolved} (allowed: {roots})")


def assert_relative_path(path: str, *, root: Path, label: str) -> str:
    """Validate a relative path argument and return the original argument."""
    if not isinstance(path, str):
        raise ValidationError(f"{label} must be a string, got {type(path).__name__}")
    candidate = Path(path)
    if candidate.is_absolute():
        raise ValidationError(f"{label} must be relative: {path}")
    validate_contained_path(root / candidate, allowed_roots=(root,), label=f"{label} {path!r}")
    return path


def type_error(key: str, expected: str, value: object) -> TypeError:
    return TypeError(
        f"{key} expected {expected}; got {type(value).__name__} value={value!r}"
    )


def value_error(key: str, expected: str, value: object) -> ValueError:
    return ValueError(
        f"{key} expected {expected}; got {type(value).__name__} value={value!r}"
    )
