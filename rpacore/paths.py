"""Public helpers for resolving configuration paths safely."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from os import PathLike
from pathlib import Path

from rpacore._validation import type_error
from rpacore.exceptions import SystemException


PathValue = str | PathLike[str]


def resolve_config_path(
    value: PathValue,
    *,
    base_dir: PathValue,
    root: PathValue | None = None,
    key: str = "path",
) -> str:
    """Resolve a path against base_dir and optionally require containment under root."""
    raw_path = _path_value(value, key)
    base_path = _resolved_path(base_dir, key="base_dir")
    resolved = raw_path.resolve() if raw_path.is_absolute() else (base_path / raw_path).resolve()

    if root is not None:
        root_path = _path_value(root, "root")
        resolved_root = root_path.resolve() if root_path.is_absolute() else (base_path / root_path).resolve()
        if not resolved.is_relative_to(resolved_root):
            raise SystemException(
                f"{key} resolves outside root: {resolved} (root: {resolved_root})",
                action=key,
            )

    return str(resolved)


def resolve_config_paths(
    config: dict[str, object],
    keys: Iterable[str],
    *,
    base_dir: PathValue,
    root: PathValue | None = None,
) -> dict[str, object]:
    """Return a copied config with selected dotted path keys resolved."""
    resolved_config = copy.deepcopy(config)
    for key in keys:
        parent, leaf = _dotted_parent(resolved_config, key)
        if leaf not in parent:
            raise KeyError(f"Missing required config key: {key}")
        parent[leaf] = resolve_config_path(
            parent[leaf],  # type: ignore[arg-type]
            base_dir=base_dir,
            root=root,
            key=key,
        )
    return resolved_config


def _path_value(value: object, key: str) -> Path:
    if not isinstance(value, (str, PathLike)):
        raise type_error(key, "str | PathLike[str]", value)
    return Path(value)


def _resolved_path(value: object, *, key: str) -> Path:
    return _path_value(value, key).resolve()


def _dotted_parent(config: dict[str, object], key: str) -> tuple[dict[str, object], str]:
    parts = key.split(".")
    if not key or any(not part for part in parts):
        raise KeyError(f"Invalid config key: {key!r}")

    parent = config
    traversed: list[str] = []
    for part in parts[:-1]:
        traversed.append(part)
        if part not in parent:
            raise KeyError(f"Missing required config key: {key}")
        child = parent[part]
        if not isinstance(child, dict):
            raise type_error(".".join(traversed), "dict", child)
        parent = child
    return parent, parts[-1]
