"""Internal helpers for validation error messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PRERELEASE_WHEEL_PATTERN = re.compile(r"(?:a|b|rc)\d", re.IGNORECASE)


class ValidationFailure(ValueError):
    """Base class for validation-tool failures."""


class ValidationError(ValidationFailure):
    """Validation failed before a tool could continue."""


@dataclass(frozen=True)
class ExamplePytestTarget:
    """Validated pytest target inside one standalone example project."""

    manifest_path: str
    project_dir: Path
    pytest_path: str


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


def example_pytest_target(test_path: str, *, examples_root: Path) -> ExamplePytestTarget:
    """Resolve an examples/<project>/... pytest target for isolated execution."""
    assert_relative_path(test_path, root=examples_root, label="example pytest path")
    resolved = validate_contained_path(
        examples_root / Path(test_path),
        allowed_roots=(examples_root,),
        label=f"example pytest path {test_path!r}",
    )
    examples_dir = (examples_root / "examples").resolve()
    if not resolved.is_relative_to(examples_dir):
        raise ValidationError(
            "example pytest path must be inside one example project, "
            f"got {test_path!r}"
        )

    relative_parts = resolved.relative_to(examples_dir).parts
    if len(relative_parts) < 2:
        raise ValidationError(
            "example pytest path must include a target inside one example project, "
            f"got {test_path!r}"
        )

    project_dir = examples_dir / relative_parts[0]
    pytest_path = Path(*relative_parts[1:])
    if not project_dir.is_dir():
        raise ValidationError(f"example project path does not exist: {project_dir}")
    if not (project_dir / pytest_path).exists():
        raise ValidationError(f"example pytest path does not exist: {project_dir / pytest_path}")

    return ExamplePytestTarget(
        manifest_path=(Path("examples") / relative_parts[0] / pytest_path).as_posix(),
        project_dir=project_dir,
        pytest_path=pytest_path.as_posix(),
    )


def type_error(key: str, expected: str, value: object) -> TypeError:
    return TypeError(
        f"{key} expected {expected}; got {type(value).__name__} value={value!r}"
    )


def value_error(key: str, expected: str, value: object) -> ValueError:
    return ValueError(
        f"{key} expected {expected}; got {type(value).__name__} value={value!r}"
    )
