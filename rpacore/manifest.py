"""Project manifest loading and entrypoint resolution."""

from __future__ import annotations

import importlib
import inspect
import sys
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rpacore._sqlite import validate_durable_sqlite_path
from rpacore._validation import type_error, value_error


MANIFEST_NAME = "rpacore.toml"
_PROJECT_SECTION_KEYS = frozenset({"entrypoint"})
_STORAGE_SECTION_KEYS = frozenset({"transaction_db_path"})
_TOP_LEVEL_KEYS = frozenset({"project", "storage"})
_IMPORT_LOCK = threading.RLock()


@dataclass(frozen=True)
class ProjectManifest:
    """Validated project manifest values."""

    manifest_path: Path
    project_dir: Path
    entrypoint: str
    transaction_db_path: str


def find_project_manifest(start: str | Path = ".") -> Path:
    """Return the nearest rpacore.toml at or above start."""
    current = Path(start)
    search_dir = current if current.is_dir() else current.parent
    resolved_search_dir = search_dir.resolve()
    for directory in [resolved_search_dir, *resolved_search_dir.parents]:
        candidate = directory / MANIFEST_NAME
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"{MANIFEST_NAME} not found from {current}")


def load_project_manifest(path: str | Path | None = None) -> ProjectManifest:
    """Load and validate rpacore.toml.

    When path is None, discovery starts from the current working directory. When
    path names a directory, rpacore.toml in that directory is loaded.
    """
    manifest_path = _manifest_path(path)
    with open(manifest_path, "rb") as file:
        data = tomllib.load(file)
    _validate_manifest_shape(data)

    project = data["project"]
    storage = data["storage"]
    entrypoint = project["entrypoint"]
    transaction_db_path = storage["transaction_db_path"]

    if not isinstance(entrypoint, str):
        raise type_error("project.entrypoint", "str", entrypoint)
    if not entrypoint:
        raise value_error("project.entrypoint", "non-empty str", entrypoint)
    _validate_entrypoint(entrypoint)

    if not isinstance(transaction_db_path, str):
        raise type_error("storage.transaction_db_path", "str", transaction_db_path)
    validate_durable_sqlite_path(
        transaction_db_path,
        field="storage.transaction_db_path",
    )

    project_dir = manifest_path.resolve().parent
    return ProjectManifest(
        manifest_path=manifest_path.resolve(),
        project_dir=project_dir,
        entrypoint=entrypoint,
        transaction_db_path=_resolve_path(project_dir, transaction_db_path),
    )


def resolve_project_entrypoint(manifest: ProjectManifest) -> Callable[[], object]:
    """Import and return the manifest entrypoint callable."""
    module_name, attribute_path = manifest.entrypoint.split(":", 1)
    project_dir = str(manifest.project_dir)
    with _IMPORT_LOCK:
        modules_before = set(sys.modules)
        conflicting_root = _conflicting_module_root(
            module_name,
            manifest.project_dir,
        )
        evicted_modules = _evict_conflicting_modules(
            conflicting_root,
            manifest.project_dir,
        )
        cleanup_root = module_name.split(".", 1)[0]
        try:
            original_path_index: int | None = sys.path.index(project_dir)
        except ValueError:
            original_path_index = None
        if original_path_index is not None:
            sys.path.pop(original_path_index)
        sys.path.insert(0, project_dir)
        try:
            importlib.invalidate_caches()
            module = importlib.import_module(module_name)
            target = _resolve_entrypoint_target(
                module,
                attribute_path=attribute_path,
                entrypoint=manifest.entrypoint,
            )
        except BaseException:
            _restore_modules_after_failure(
                cleanup_root,
                modules_before=modules_before,
                evicted_modules=evicted_modules,
            )
            raise
        finally:
            try:
                sys.path.remove(project_dir)
            except ValueError:
                pass
            if original_path_index is not None:
                restored_index = min(original_path_index, len(sys.path))
                sys.path.insert(restored_index, project_dir)
    return target


def _resolve_entrypoint_target(
    module: object,
    *,
    attribute_path: str,
    entrypoint: str,
) -> Callable[[], object]:
    target: object = module
    for attribute in attribute_path.split("."):
        try:
            target = getattr(target, attribute)
        except AttributeError as exc:
            raise AttributeError(
                f"Project entrypoint attribute not found: {entrypoint}"
            ) from exc

    if not callable(target):
        raise type_error("project.entrypoint", "callable", target)
    if inspect.isclass(target):
        raise TypeError(
            f"project.entrypoint must be a function or callable object instance, not a class: "
            f"{entrypoint}"
        )
    try:
        inspect.signature(target).bind()
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"project.entrypoint must be callable without arguments: {entrypoint}"
        ) from exc
    return target


def _manifest_path(path: str | Path | None) -> Path:
    if path is None:
        return find_project_manifest()
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / MANIFEST_NAME
    if not candidate.exists():
        raise FileNotFoundError(f"Project manifest not found: {candidate}")
    return candidate


def _validate_manifest_shape(data: dict[str, object]) -> None:
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "manifest")
    project = _required_section(data, "project")
    storage = _required_section(data, "storage")
    _reject_unknown_keys(project, _PROJECT_SECTION_KEYS, "project")
    _reject_unknown_keys(storage, _STORAGE_SECTION_KEYS, "storage")
    if "entrypoint" not in project:
        raise KeyError("Missing required manifest key: project.entrypoint")
    if "transaction_db_path" not in storage:
        raise KeyError("Missing required manifest key: storage.transaction_db_path")


def _required_section(data: dict[str, object], key: str) -> dict[str, object]:
    if key not in data:
        raise KeyError(f"Missing required manifest section: {key}")
    section = data[key]
    if not isinstance(section, dict):
        raise type_error(key, "dict", section)
    return section


def _reject_unknown_keys(data: dict[str, object], allowed: frozenset[str], path: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise KeyError(f"Unknown manifest key: {path}.{unknown[0]}")


def _validate_entrypoint(entrypoint: str) -> None:
    if entrypoint.count(":") != 1:
        raise value_error("project.entrypoint", "module:callable", entrypoint)
    module_name, attribute_path = entrypoint.split(":", 1)
    if not _is_dotted_identifier(module_name) or not _is_dotted_identifier(attribute_path):
        raise value_error("project.entrypoint", "module:callable", entrypoint)


def _is_dotted_identifier(value: str) -> bool:
    return bool(value) and all(part.isidentifier() for part in value.split("."))


def _resolve_path(base_dir: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(base_dir / path)


def _module_belongs_to_project(module: object, project_dir: Path) -> bool:
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        return True
    try:
        Path(module_file).resolve().relative_to(project_dir.resolve())
        return True
    except ValueError:
        return False


def _conflicting_module_root(
    module_name: str,
    project_dir: Path,
) -> str | None:
    parts = module_name.split(".")
    for length in range(1, len(parts) + 1):
        prefix = ".".join(parts[:length])
        if prefix not in sys.modules:
            continue
        module = sys.modules[prefix]
        if module is None or not _module_belongs_to_project(module, project_dir):
            return prefix
    return None


def _evict_conflicting_modules(
    conflicting_root: str | None,
    project_dir: Path,
) -> dict[str, object]:
    if conflicting_root is None:
        return {}
    evicted: dict[str, object] = {}
    for name, module in list(sys.modules.items()):
        if not _module_is_at_or_below(name, conflicting_root):
            continue
        if module is not None and _module_belongs_to_project(module, project_dir):
            continue
        evicted[name] = sys.modules.pop(name)
    return evicted


def _restore_modules_after_failure(
    cleanup_root: str,
    *,
    modules_before: set[str],
    evicted_modules: dict[str, object],
) -> None:
    removed_modules: dict[str, object] = {}
    for name in list(sys.modules):
        if not _module_is_at_or_below(name, cleanup_root):
            continue
        if name not in modules_before or name in evicted_modules:
            removed_modules[name] = sys.modules.pop(name)
    sys.modules.update(evicted_modules)

    missing = object()
    for name, removed_module in sorted(
        removed_modules.items(),
        key=lambda item: item[0].count("."),
    ):
        parent_name, separator, attribute = name.rpartition(".")
        if not separator:
            continue
        parent = sys.modules.get(parent_name)
        if parent is None:
            continue
        parent_namespace = getattr(parent, "__dict__", None)
        if not isinstance(parent_namespace, dict):
            continue
        restored_module = sys.modules.get(name, missing)
        if restored_module is not missing:
            parent_namespace[attribute] = restored_module
        elif parent_namespace.get(attribute, missing) is removed_module:
            parent_namespace.pop(attribute, None)


def _module_is_at_or_below(module_name: str, root_name: str) -> bool:
    return module_name == root_name or module_name.startswith(f"{root_name}.")
