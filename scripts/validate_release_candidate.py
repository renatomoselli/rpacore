"""Produce reproducible release-candidate validation results for RPA Core."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import fnmatch
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tarfile
import tomllib
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parents[1]
# These scripts must run directly from scripts/ before rpacore is installed.
if str(REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_FOR_IMPORTS))

from rpacore._validation import (
    PRERELEASE_WHEEL_PATTERN,
    ValidationError,
    artifact_set_sha256 as _artifact_set_sha256,
    assert_relative_path,
    example_pytest_target as _example_pytest_target,
    validate_contained_path as _validate_contained_path,
)


TRUNCATE_OUTPUT_CHARS = 12_000
DEFAULT_OUTPUT_DIR = Path("validation-artifacts/release-candidate-validation")
VALIDATION_FINDINGS = (
    "G2-001",
    "G2-002",
    "G2-004",
    "G2-007",
    "G2-008",
    "G2-013",
)
COPY_IGNORE_NAMES = {
    ".agents",
    ".codex",
    ".git",
    ".internal",
    ".mypy_cache",
    ".pytest_cache",
    ".rpiv",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
}
# Glob-based generated file rules used by source-copy ignores.
COPY_IGNORE_PATTERNS = ("*.egg-info", "*.pyc", "*.pyo")
ARCHIVE_PRIVATE_PATTERNS = ("*.pyc", "*.pyo")
PYTEST_COUNT_PATTERN = re.compile(
    r"\b(?P<count>\d+)\s+(?P<status>passed|failed|skipped|xfailed|xpassed|errors?)\b"
)
FROZEN_EXAMPLE_WHEEL_MATRIX = (
    "acme_work_items",
    "checkpoint_resume",
    "database_reconciliation",
    "excel_reorganization",
    "file_inbox_processor",
    "git_repo_health_monitor",
    "json_event_log_processor",
    "pdf_invoice_extraction",
    "rest_api_batch",
    "rpa_challenge",
    "windows_calculator",
)
PYTEST_COUNT_STATUSES = frozenset(
    {"passed", "failed", "skipped", "xfailed", "xpassed", "errors"}
)


@dataclass
class CommandRecord:
    """One executed command and its bounded output."""

    name: str
    command: list[str]
    cwd: str
    exit_code: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    raw_stdout: str | None = field(default=None, repr=False)
    raw_stderr: str | None = field(default=None, repr=False)


@dataclass
class RepoState:
    """Repository provenance captured before validation starts."""

    name: str
    path: str
    commit: str
    branch: str
    dirty: bool
    status: list[str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _truncate(text: str) -> str:
    if len(text) <= TRUNCATE_OUTPUT_CHARS:
        return text
    return text[:TRUNCATE_OUTPUT_CHARS] + "\n...[truncated]..."


def _run(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    allowed_roots: tuple[Path, ...],
    env: dict[str, str] | None = None,
    check: bool = True,
) -> CommandRecord:
    _validate_contained_path(cwd, allowed_roots=allowed_roots, label=f"{name} cwd")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )
    except subprocess.CalledProcessError as exc:
        command_record = CommandRecord(
            name=name,
            command=command,
            cwd=str(cwd),
            exit_code=exc.returncode,
            duration_seconds=round(time.perf_counter() - started, 3),
            stdout=_truncate(exc.stdout or ""),
            stderr=_truncate(exc.stderr or ""),
            raw_stdout=exc.stdout or "",
            raw_stderr=exc.stderr or "",
        )
        raise ValidationError(
            f"{name} failed with exit code {exc.returncode}\n"
            f"stdout:\n{command_record.stdout}\n"
            f"stderr:\n{command_record.stderr}"
        ) from exc
    command_record = CommandRecord(
        name=name,
        command=command,
        cwd=str(cwd),
        exit_code=completed.returncode,
        duration_seconds=round(time.perf_counter() - started, 3),
        stdout=_truncate(completed.stdout),
        stderr=_truncate(completed.stderr),
        raw_stdout=completed.stdout,
        raw_stderr=completed.stderr,
    )
    return command_record


def _git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _validate_relative_test_path(test_path: str, *, examples_root: Path) -> str:
    return assert_relative_path(test_path, root=examples_root, label="example pytest path")


def _example_project_dir(project_path: str, *, examples_root: Path) -> Path:
    assert_relative_path(project_path, root=examples_root, label="example project path")
    resolved = _validate_contained_path(
        examples_root / Path(project_path),
        allowed_roots=(examples_root,),
        label=f"example project path {project_path!r}",
    )
    examples_dir = (examples_root / "examples").resolve()
    if resolved == examples_dir or not resolved.is_relative_to(examples_dir):
        raise ValidationError(
            "example project path must be inside examples/, "
            f"got {project_path!r}"
        )
    if not resolved.is_dir():
        raise ValidationError(f"example project path does not exist: {resolved}")
    return resolved


def _example_db_path(db_path: str, *, project_dir: Path) -> Path:
    assert_relative_path(db_path, root=project_dir, label="example transaction database path")
    resolved = _validate_contained_path(
        project_dir / Path(db_path),
        allowed_roots=(project_dir,),
        label=f"example transaction database path {db_path!r}",
    )
    return resolved


def _require_git_repo(path: Path, *, name: str) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise ValidationError(f"{name} path is not a git repository: {path}")


def _repo_state(name: str, path: Path) -> RepoState:
    _require_git_repo(path, name=name)
    status = _git_output(path, "status", "--porcelain=v1").splitlines()
    return RepoState(
        name=name,
        path=str(path),
        commit=_git_output(path, "rev-parse", "HEAD"),
        branch=_git_output(path, "branch", "--show-current"),
        dirty=bool(status),
        status=status,
    )


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in COPY_IGNORE_NAMES or name.upper() in WINDOWS_RESERVED_NAMES:
            ignored.add(name)
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in COPY_IGNORE_PATTERNS):
            ignored.add(name)
    return ignored


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        _remove_tree(destination)
    shutil.copytree(source, destination, ignore=_copy_ignore_for_source(), symlinks=True)


def _copy_ignore_for_source() -> Callable[[str, list[str]], set[str]]:
    def _ignore(directory: str, names: list[str]) -> set[str]:
        ignored = _copy_ignore(directory, names)
        for name in names:
            if (Path(directory) / name).is_symlink():
                ignored.add(name)
        return ignored

    return _ignore


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def _onerror(function, failed_path, exc_info) -> None:
        original_error = exc_info[1]
        try:
            os.chmod(failed_path, 0o700)
            function(failed_path)
        except OSError as exc:
            logging.warning(
                "Failed to clean %s after %s: %s",
                failed_path,
                type(original_error).__name__,
                exc,
                exc_info=True,
            )

    shutil.rmtree(path, onerror=_onerror)
    if path.exists():
        raise OSError(f"validation cleanup left directory behind: {path}")


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_script(venv_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_records(wheelhouse: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(wheelhouse.glob("rpacore-*")):
        is_wheel = path.suffix == ".whl"
        is_sdist = path.name.endswith(".tar.gz")
        if not is_wheel and not is_sdist:
            raise ValidationError(f"unsupported release artifact type: {path.name}")
        names = _archive_names(path)
        private_paths = sorted(
            name
            for name in names
            if _private_archive_path_match(name, is_wheel=is_wheel) is not None
        )
        records.append(
            {
                "name": path.name,
                "path": str(path),
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
                "contains_license": any(name.endswith("LICENSE") for name in names),
                "contains_notice": any(name.endswith("NOTICE") for name in names),
                "contains_metadata": _contains_package_metadata(names, is_wheel=is_wheel),
                "contains_record": any(name.endswith(".dist-info/RECORD") for name in names),
                "contains_entry_points": any(name.endswith(".dist-info/entry_points.txt") for name in names),
                "contains_examples": any(
                    name.startswith("examples/") or "/examples/" in name
                    for name in names
                ),
                "contains_private_paths": bool(private_paths),
                "private_paths": private_paths,
            }
        )
    return records


def _is_private_archive_path(name: str) -> bool:
    return _private_archive_path_match(name, is_wheel=True) is not None


def _private_archive_path_match(name: str, *, is_wheel: bool) -> str | None:
    for part in PurePosixPath(name).parts:
        if part in COPY_IGNORE_NAMES:
            return part
        if is_wheel and fnmatch.fnmatch(part, "*.egg-info"):
            return part
        if any(fnmatch.fnmatch(part, pattern) for pattern in ARCHIVE_PRIVATE_PATTERNS):
            return part
    return None


def _contains_package_metadata(names: list[str], *, is_wheel: bool) -> bool:
    if is_wheel:
        return any(name.endswith(".dist-info/METADATA") for name in names)
    return any(name.endswith("PKG-INFO") for name in names)


def _validate_artifact_records(records: list[dict[str, Any]]) -> None:
    if not records:
        raise ValidationError("no release artifacts were recorded")
    for record in records:
        name = str(record["name"])
        if record["contains_private_paths"]:
            private_paths = ", ".join(str(path) for path in record.get("private_paths", []))
            detail = f": {private_paths}" if private_paths else ""
            raise ValidationError(f"release artifact contains private paths: {name}{detail}")
        if not record["contains_license"]:
            raise ValidationError(f"release artifact missing license file: {name}")
        if not record["contains_notice"]:
            raise ValidationError(f"release artifact missing notice file: {name}")
        if not record["contains_metadata"]:
            raise ValidationError(f"release artifact missing package metadata: {name}")
        if record["contains_examples"]:
            raise ValidationError(f"release artifact contains examples directory: {name}")
        if name.endswith(".whl"):
            if not record["contains_record"]:
                raise ValidationError(f"wheel missing RECORD metadata: {name}")
            if not record["contains_entry_points"]:
                raise ValidationError(f"wheel missing console entry point metadata: {name}")


def _verify_published_artifacts(
    artifacts: list[dict[str, Any]],
    artifact_dir: Path,
) -> list[dict[str, Any]]:
    expected_names = {str(artifact["name"]) for artifact in artifacts}
    actual_names = {path.name for path in artifact_dir.iterdir()} if artifact_dir.is_dir() else set()
    if artifact_dir.is_symlink() or actual_names != expected_names:
        raise ValidationError(f"published candidate artifact set is incomplete: {artifact_dir}")
    persisted = []
    for artifact in artifacts:
        path = artifact_dir / str(artifact["name"])
        if not path.is_file() or path.is_symlink():
            raise ValidationError(f"published candidate artifact is not a regular file: {path}")
        if path.stat().st_size != artifact["size_bytes"]:
            raise ValidationError(f"published candidate artifact size mismatch: {path.name}")
        if _sha256(path) != artifact["sha256"]:
            raise ValidationError(f"published candidate artifact hash mismatch: {path.name}")
        persisted.append({**artifact, "path": str(path)})
    return persisted


def _publish_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    wheelhouse: Path,
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Atomically preserve validated artifacts outside the disposable work directory."""
    artifact_root = output_dir / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_set = _artifact_set_sha256(artifacts)
    published_dir = artifact_root / artifact_set
    if published_dir.exists():
        return _verify_published_artifacts(artifacts, published_dir)

    staging_dir = artifact_root / f".{artifact_set}-{uuid.uuid4().hex}.tmp"
    wheelhouse = wheelhouse.resolve()
    try:
        staging_dir.mkdir()
        for artifact in artifacts:
            source = Path(str(artifact["path"])).resolve()
            name = str(artifact["name"])
            if source.parent != wheelhouse or source.name != name or not source.is_file():
                raise ValidationError(f"candidate artifact is not a wheelhouse file: {name}")
            shutil.copyfile(source, staging_dir / name)
        _verify_published_artifacts(artifacts, staging_dir)
        try:
            staging_dir.replace(published_dir)
        except FileExistsError:
            # A concurrent equivalent candidate may have published the same set.
            _remove_tree(staging_dir)
            return _verify_published_artifacts(artifacts, published_dir)
    except BaseException:
        _remove_tree(staging_dir)
        raise
    return _verify_published_artifacts(artifacts, published_dir)


def _dependency_inventory(source_root: Path) -> dict[str, Any]:
    pyproject_path = source_root / "pyproject.toml"
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"cannot read dependency inventory from {pyproject_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"cannot parse dependency inventory from {pyproject_path}: {exc}") from exc
    project = pyproject.get("project", {})
    if not isinstance(project, dict) or not project:
        raise ValidationError("pyproject missing [project] section")
    dependencies = project.get("dependencies", [])
    optional_dependencies = project.get("optional-dependencies", {})
    if not isinstance(dependencies, list):
        raise ValidationError("pyproject project.dependencies must be a list when present")
    if not isinstance(optional_dependencies, dict):
        raise ValidationError("pyproject project.optional-dependencies must be a table")
    for extra, values in optional_dependencies.items():
        if not isinstance(values, list):
            raise ValidationError(f"pyproject optional dependency group must be a list: {extra}")
    return {
        "runtime_dependencies": sorted(str(dependency) for dependency in dependencies),
        "optional_dependencies": {
            str(extra): sorted(str(dependency) for dependency in values)
            for extra, values in sorted(optional_dependencies.items())
        },
    }


def _archive_names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return archive.getnames()
    return []


def _latest_wheel(wheelhouse: Path) -> Path:
    wheels = sorted(
        wheelhouse.glob("rpacore-*.whl"),
        key=lambda path: (
            path.stat().st_mtime,
            not PRERELEASE_WHEEL_PATTERN.search(path.name),
            path.name,
        ),
        reverse=True,
    )
    if not wheels:
        raise FileNotFoundError(f"No rpacore wheel found in {wheelhouse}")
    return wheels[0]


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _pytest_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in PYTEST_COUNT_PATTERN.finditer(output.replace(",", "")):
        status = match.group("status")
        if status == "error":
            status = "errors"
        counts[status] = counts.get(status, 0) + int(match.group("count"))
    return counts


def _aggregate_pytest_counts(commands: list[CommandRecord]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for command in commands:
        for status, count in command.parsed.items():
            if not isinstance(count, int):
                continue
            if status in PYTEST_COUNT_STATUSES:
                totals[status] = totals.get(status, 0) + count
    return dict(sorted(totals.items()))


def _manifest_result(
    *,
    commands: list[CommandRecord],
    repos: list[RepoState],
) -> dict[str, int | str]:
    failed_command_count = sum(1 for command in commands if command.exit_code != 0)
    dirty_repository_count = sum(1 for repo in repos if repo.dirty)
    return {
        "status": "fail" if failed_command_count or dirty_repository_count else "pass",
        "command_count": len(commands),
        "failed_command_count": failed_command_count,
        "dirty_repository_count": dirty_repository_count,
    }


def _validation_index(commands: list[CommandRecord]) -> dict[str, list[str]]:
    command_names = {command.name for command in commands}
    index = {
        "G2-001": ["framework_tests"],
        "G2-002": ["installed_import_smoke", "example_cli_run"],
        "G2-004": [
            "example_cli_transaction_list_json",
            "example_cli_transaction_show_json",
            "example_cli_transaction_export_json",
            "example_cli_transaction_export_ndjson",
        ],
        "G2-007": ["installed_import_smoke"],
        "G2-008": ["installed_import_smoke"],
        "G2-013": sorted(
            name for name in command_names if name.startswith("example_pytest:")
        ),
    }
    return {
        finding_id: [name for name in names if name in command_names]
        for finding_id, names in index.items()
    }


def _parse_json_output(command_record: CommandRecord) -> dict[str, Any]:
    stdout = command_record.raw_stdout if command_record.raw_stdout is not None else command_record.stdout
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{command_record.name} did not produce valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"{command_record.name} did not produce a JSON object")
    return parsed


def _parse_ndjson_output(command_record: CommandRecord) -> list[dict[str, Any]]:
    records = []
    stdout = command_record.raw_stdout if command_record.raw_stdout is not None else command_record.stdout
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"{command_record.name} line {line_number} did not produce valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValidationError(f"{command_record.name} produced a non-object NDJSON line")
        records.append(parsed)
    return records


def _transactions_from_list_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "transactions" not in payload:
        raise ValidationError("transaction list JSON did not include 'transactions'")
    transactions = payload["transactions"]
    if not isinstance(transactions, list):
        raise ValidationError(
            f"transaction list JSON 'transactions' must be a list, got {type(transactions).__name__}"
        )
    for index, transaction in enumerate(transactions):
        if not isinstance(transaction, dict):
            raise ValidationError(
                f"transaction list JSON item {index} must be an object, got {type(transaction).__name__}"
            )
    return transactions


def _transaction_ids(transactions: list[dict[str, Any]]) -> list[str]:
    if not transactions:
        raise ValidationError("generated project created no transactions for CLI inspection")
    tx_ids = [tx["id"] for tx in transactions if isinstance(tx.get("id"), str) and tx["id"]]
    if not tx_ids:
        raise ValidationError("transaction list JSON entries did not include string 'id' values")
    return tx_ids


def _installed_smoke_code(
    *,
    repo_root: Path,
    source_copy: Path,
    allowed_install_root: Path,
) -> str:
    return f"""
import json
from pathlib import Path

import rpacore

forbidden_roots = [
    Path({str(repo_root.resolve())!r}),
    Path({str(source_copy.resolve())!r}),
]
allowed_install_root = Path({str(allowed_install_root.resolve())!r})
module_file = Path(rpacore.__file__).resolve()
if any(
    module_file.is_relative_to(root.resolve())
    and not module_file.is_relative_to(allowed_install_root.resolve())
    for root in forbidden_roots
):
    raise SystemExit(f"imported rpacore from checkout: {{module_file}}")

missing = [name for name in rpacore.__all__ if not hasattr(rpacore, name)]
if missing:
    raise SystemExit(f"missing public exports: {{missing}}")

print(json.dumps({{
    "version": rpacore.__version__,
    "module_file": str(module_file),
    "exports": sorted(rpacore.__all__),
}}, sort_keys=True))
"""


def _python_version_info(python: Path) -> dict[str, str]:
    version_result = subprocess.run(
        [str(python), "-c", "import sys; print(sys.version)"],
        capture_output=True,
        text=True,
        check=True,
    )
    result = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        "executable": str(python),
        "version": version_result.stdout.strip(),
        "pip": result.stdout.strip(),
    }


def _tool_versions() -> dict[str, str]:
    return {
        package: importlib.metadata.version(package)
        for package in ("pip", "build", "twine")
    }


def _installed_environment_probe_code(probe_root: Path) -> str:
    return f"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from rpacore import SqliteQueue, Transaction, save_transaction

probe_root = Path({str(probe_root)!r})
probe_root.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(dir=probe_root) as temporary:
    root = Path(temporary)
    transaction_db = root / "transaction.db"
    queue_db = root / "queue.db"
    save_transaction(Transaction("release-candidate-journal-probe"), str(transaction_db))
    SqliteQueue({{"db_path": str(queue_db)}})

    def journal_mode(path):
        connection = sqlite3.connect(path)
        try:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            connection.close()

    transaction_mode = journal_mode(transaction_db)
    queue_mode = journal_mode(queue_db)
    if transaction_mode != "delete" or queue_mode != "delete":
        raise RuntimeError(
            "release-candidate journal probe requires delete mode, got "
            f"transaction={{transaction_mode!r}}, queue={{queue_mode!r}}"
        )
    print(json.dumps({{
        "python": sys.version,
        "sqlite": {{"library_version": sqlite3.sqlite_version}},
        "journal": {{
            "policy": "rollback_delete",
            "transaction": {{"effective_mode": transaction_mode}},
            "queue": {{"effective_mode": queue_mode}},
        }},
    }}, sort_keys=True))
"""


def _command_record(command_record: CommandRecord) -> dict[str, Any]:
    record = asdict(command_record)
    record.pop("raw_stdout", None)
    record.pop("raw_stderr", None)
    return record


def _transaction_command(
    rpacore_cli: Path,
    *args: str,
    db_path: Path | None,
) -> list[str]:
    command = [str(rpacore_cli), "transaction", *args]
    if db_path is not None:
        command.extend(["--db", str(db_path)])
    return command


def _append_cli_transaction_inspection(
    commands: list[CommandRecord],
    *,
    name_prefix: str,
    rpacore_cli: Path,
    cwd: Path,
    db_path: Path | None,
    allowed_roots: tuple[Path, ...],
    env: dict[str, str],
) -> None:
    inspection_commands: list[CommandRecord] = []
    inspection_commands.append(
        _run(
            f"{name_prefix}_transaction_list",
            _transaction_command(rpacore_cli, "list", db_path=db_path),
            cwd=cwd,
            allowed_roots=allowed_roots,
            env=env,
        )
    )

    list_json = _run(
        f"{name_prefix}_transaction_list_json",
        _transaction_command(rpacore_cli, "list", "--json", db_path=db_path),
        cwd=cwd,
        allowed_roots=allowed_roots,
        env=env,
    )
    list_payload = _parse_json_output(list_json)
    transactions = _transactions_from_list_payload(list_payload)
    list_json.parsed = {"transaction_count": len(transactions)}
    tx_ids = _transaction_ids(transactions)
    inspection_commands.append(list_json)

    inspection_commands.append(
        _run(
            f"{name_prefix}_transaction_show",
            _transaction_command(rpacore_cli, "show", tx_ids[0], db_path=db_path),
            cwd=cwd,
            allowed_roots=allowed_roots,
            env=env,
        )
    )
    show_json = _run(
        f"{name_prefix}_transaction_show_json",
        _transaction_command(rpacore_cli, "show", tx_ids[0], "--json", db_path=db_path),
        cwd=cwd,
        allowed_roots=allowed_roots,
        env=env,
    )
    show_json.parsed = {"transaction_id": _parse_json_output(show_json)["transaction"]["id"]}
    inspection_commands.append(show_json)

    export_json = _run(
        f"{name_prefix}_transaction_export_json",
        _transaction_command(rpacore_cli, "export", "--format", "json", db_path=db_path),
        cwd=cwd,
        allowed_roots=allowed_roots,
        env=env,
    )
    export_payload = _parse_json_output(export_json)
    export_json.parsed = {
        "transaction_count": len(_transactions_from_list_payload(export_payload))
    }
    inspection_commands.append(export_json)

    export_ndjson = _run(
        f"{name_prefix}_transaction_export_ndjson",
        _transaction_command(rpacore_cli, "export", "--format", "ndjson", db_path=db_path),
        cwd=cwd,
        allowed_roots=allowed_roots,
        env=env,
    )
    export_ndjson.parsed = {"record_count": len(_parse_ndjson_output(export_ndjson))}
    inspection_commands.append(export_ndjson)
    commands.extend(inspection_commands)


def _run_examples_wheel_matrix(
    *,
    framework_copy: Path,
    examples_copy: Path,
    wheel: Path,
    expected_wheel_sha256: str,
    work_dir: Path,
    output_dir: Path,
    allowed_roots: tuple[Path, ...],
    env: dict[str, str],
) -> tuple[CommandRecord, dict[str, Any]]:
    if _sha256(wheel) != expected_wheel_sha256:
        raise ValidationError("candidate wheel changed before the examples wheel matrix started")
    evidence_dir = output_dir / "examples-wheel-validation"
    command = [
        sys.executable,
        str(framework_copy / "scripts" / "validate_examples_against_wheel.py"),
        "--repo-root",
        str(framework_copy),
        "--examples-repo",
        str(examples_copy),
        "--prebuilt-wheel",
        str(wheel),
        "--output-dir",
        str(evidence_dir),
        "--work-dir",
        str(work_dir),
        "--venv-mode",
        "work-dir",
        "--run-main",
        "deterministic",
    ]
    for example_name in FROZEN_EXAMPLE_WHEEL_MATRIX:
        command.extend(("--example", example_name))
    command_record = _run(
        "examples_wheel_matrix",
        command,
        cwd=framework_copy,
        allowed_roots=allowed_roots,
        env=env,
    )
    manifest_path = evidence_dir / "examples-wheel-validation.json"
    try:
        evidence = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(f"examples wheel matrix did not write evidence: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"examples wheel matrix wrote invalid JSON: {manifest_path}") from exc
    if not isinstance(evidence, dict):
        raise ValidationError("examples wheel matrix did not write a JSON object")
    wheel_evidence = evidence.get("wheel")
    if not isinstance(wheel_evidence, dict) or wheel_evidence.get("sha256") != expected_wheel_sha256:
        raise ValidationError("examples wheel matrix did not validate the candidate wheel hash")
    result = evidence.get("result")
    if not isinstance(result, dict) or result.get("status") != "pass":
        raise ValidationError("examples wheel matrix did not pass")
    examples_evidence = evidence.get("examples")
    actual_examples = (
        [item.get("name") for item in examples_evidence if isinstance(item, dict)]
        if isinstance(examples_evidence, list)
        else []
    )
    expected_examples = list(FROZEN_EXAMPLE_WHEEL_MATRIX)
    if (
        actual_examples != expected_examples
        or result.get("example_count") != len(expected_examples)
        or any(item.get("status") != "pass" for item in examples_evidence)
    ):
        raise ValidationError("examples wheel matrix did not cover the frozen examples exactly")
    return command_record, {
        "status": result["status"],
        "evidence_path": str(manifest_path),
        "wheel_sha256": wheel_evidence["sha256"],
        "examples": list(FROZEN_EXAMPLE_WHEEL_MATRIX),
    }


def validate_release_candidate(
    *,
    repo_root: Path,
    examples_repo: Path | None,
    work_dir: Path,
    output_dir: Path,
    examples_pytest: list[str],
    example_cli_project: str | None,
    example_cli_db: str,
    examples_wheel_matrix: bool = False,
    prebuilt_artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    """Run release-candidate validation and return the validation manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir.resolve()
    source_dir = work_dir / "source"
    framework_copy = source_dir / "rpacore"
    examples_copy = source_dir / "rpacore-examples"
    wheelhouse = (
        prebuilt_artifacts_dir.resolve()
        if prebuilt_artifacts_dir is not None
        else work_dir / "wheelhouse"
    )
    outside_dir = work_dir / "outside"
    venv_dir = work_dir / "venv"
    examples_matrix_work_dir = work_dir / "examples-wheel-validation"
    generated_dirs = (source_dir, outside_dir, venv_dir, examples_matrix_work_dir)
    if prebuilt_artifacts_dir is None:
        generated_dirs = (source_dir, wheelhouse, outside_dir, venv_dir, examples_matrix_work_dir)

    for generated_dir in generated_dirs:
        if generated_dir.exists():
            _remove_tree(generated_dir)
    if examples_repo is not None and not examples_pytest and example_cli_project is None and not examples_wheel_matrix:
        raise ValidationError("--examples-pytest, --example-cli-project, or --examples-wheel-matrix is required when --examples-repo is provided")
    if example_cli_project is not None and examples_repo is None:
        raise ValidationError("--examples-repo is required when --example-cli-project is used")
    if examples_wheel_matrix and examples_repo is None:
        raise ValidationError("--examples-repo is required when --examples-wheel-matrix is used")
    cleanup_failures: list[Path] = []
    try:
        source_dir.mkdir(parents=True)
        if prebuilt_artifacts_dir is None:
            wheelhouse.mkdir(parents=True)
        elif not wheelhouse.is_dir():
            raise ValidationError(f"prebuilt artifact directory does not exist: {wheelhouse}")
        outside_dir.mkdir(parents=True)

        repos = [_repo_state("rpacore", repo_root)]
        if examples_repo is not None:
            repos.append(_repo_state("rpacore-examples", examples_repo))

        _copy_tree(repo_root, framework_copy)
        if examples_repo is not None:
            _copy_tree(examples_repo, examples_copy)

        commands: list[CommandRecord] = []
        env = _python_env()
        allowed_run_roots = (work_dir,)
        commands.append(_run("framework_tests", [sys.executable, "-m", "pytest", "-q"], cwd=framework_copy, allowed_roots=allowed_run_roots, env=env))
        commands[-1].parsed = _pytest_counts(commands[-1].stdout + "\n" + commands[-1].stderr)

        if prebuilt_artifacts_dir is None:
            commands.append(
                _run(
                    "build_artifacts",
                    [sys.executable, "-m", "build", "--outdir", str(wheelhouse)],
                    cwd=framework_copy,
                    allowed_roots=allowed_run_roots,
                    env=env,
                )
            )
        artifacts = _artifact_records(wheelhouse)
        _validate_artifact_records(artifacts)
        # Read dependency metadata from the isolated source copy used for builds.
        dependency_inventory = _dependency_inventory(framework_copy)
        twine_inputs = [str(path) for path in sorted(wheelhouse.glob("rpacore-*"))]
        commands.append(
            _run(
                "twine_check",
                [sys.executable, "-m", "twine", "check", *twine_inputs],
                cwd=outside_dir,
                allowed_roots=allowed_run_roots,
                env=env,
            )
        )

        wheel = _latest_wheel(wheelhouse)
        wheel_record = next(
            (record for record in artifacts if record["path"] == str(wheel)),
            None,
        )
        if wheel_record is None:
            raise ValidationError(f"candidate wheel was not recorded as an artifact: {wheel}")
        wheel_sha256 = str(wheel_record["sha256"])
        commands.append(_run("create_venv", [sys.executable, "-m", "venv", str(venv_dir)], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        python = _venv_python(venv_dir)
        commands.append(_run("install_wheel", [str(python), "-m", "pip", "install", str(wheel)], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        commands.append(
            _run(
                "installed_environment_probe",
                [str(python), "-c", _installed_environment_probe_code(outside_dir / "environment-probe")],
                cwd=outside_dir,
                allowed_roots=allowed_run_roots,
                env=env,
            )
        )
        environment_evidence = _parse_json_output(commands[-1])
        commands[-1].parsed = environment_evidence
        commands.append(
            _run(
                "installed_import_smoke",
                [
                    str(python),
                    "-c",
                    _installed_smoke_code(
                        repo_root=repo_root,
                        source_copy=framework_copy,
                        allowed_install_root=venv_dir,
                    ),
                ],
                cwd=outside_dir,
                allowed_roots=allowed_run_roots,
                env=env,
            )
        )
        commands[-1].parsed = _parse_json_output(commands[-1])

        rpacore_cli = _venv_script(venv_dir, "rpacore")
        commands.append(_run("cli_version", [str(rpacore_cli), "version"], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        generated_project = outside_dir / "installed_project"
        if generated_project.exists():
            _remove_tree(generated_project)
        commands.append(_run("cli_init", [str(rpacore_cli), "init", "installed_project"], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        commands.append(_run("cli_run_generated_project", [str(rpacore_cli), "run"], cwd=generated_project, allowed_roots=allowed_run_roots, env=env))
        _append_cli_transaction_inspection(
            commands,
            name_prefix="cli",
            rpacore_cli=rpacore_cli,
            cwd=generated_project,
            db_path=None,
            allowed_roots=allowed_run_roots,
            env=env,
        )

        if example_cli_project is not None:
            example_project_dir = _example_project_dir(
                example_cli_project,
                examples_root=examples_copy,
            )
            example_db_path = _example_db_path(example_cli_db, project_dir=example_project_dir)
            commands.append(
                _run(
                    "example_cli_run",
                    [str(python), "main.py"],
                    cwd=example_project_dir,
                    allowed_roots=allowed_run_roots,
                    env=env,
                )
            )
            if not example_db_path.exists():
                raise ValidationError(f"example transaction database was not created: {example_db_path}")
            _append_cli_transaction_inspection(
                commands,
                name_prefix="example_cli",
                rpacore_cli=rpacore_cli,
                cwd=example_project_dir,
                db_path=example_db_path,
                allowed_roots=allowed_run_roots,
                env=env,
            )

        if examples_pytest:
            if examples_repo is None:
                raise ValidationError("--examples-repo is required when --examples-pytest is used")
            commands.append(
                _run(
                    "install_pytest_for_examples",
                    [str(python), "-m", "pip", "install", "pytest"],
                    cwd=outside_dir,
                    allowed_roots=allowed_run_roots,
                    env=env,
                )
            )
            for test_path in examples_pytest:
                target = _example_pytest_target(test_path, examples_root=examples_copy)
                command = [str(python), "-m", "pytest", target.pytest_path, "-q"]
                command_record = _run(
                    f"example_pytest:{target.manifest_path}",
                    command,
                    cwd=target.project_dir,
                    allowed_roots=allowed_run_roots,
                    env=env,
                )
                command_record.parsed = _pytest_counts(command_record.stdout + "\n" + command_record.stderr)
                commands.append(command_record)

        examples_wheel_evidence = None
        if examples_wheel_matrix:
            matrix_command, examples_wheel_evidence = _run_examples_wheel_matrix(
                framework_copy=framework_copy,
                examples_copy=examples_copy,
                wheel=wheel,
                expected_wheel_sha256=wheel_sha256,
                work_dir=examples_matrix_work_dir,
                output_dir=output_dir,
                allowed_roots=allowed_run_roots,
                env=env,
            )
            commands.append(matrix_command)

        artifacts = _publish_artifacts(
            artifacts,
            wheelhouse=wheelhouse,
            output_dir=output_dir,
        )

        manifest = {
            "schema_version": 3,
            "generated_at": _utc_now().isoformat(),
            "finding_ids": list(VALIDATION_FINDINGS),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "architecture": platform.architecture()[0],
            },
            "python": _python_version_info(Path(sys.executable)),
            "environment": environment_evidence,
            "tools": _tool_versions(),
            "repositories": [asdict(repo) for repo in repos],
            "artifacts": artifacts,
            "artifact_source": "prebuilt" if prebuilt_artifacts_dir is not None else "built",
            "dependency_inventory": dependency_inventory,
            "result": _manifest_result(commands=commands, repos=repos),
            "pytest_totals": _aggregate_pytest_counts(commands),
            "validation_index": _validation_index(commands),
            "commands": [_command_record(command) for command in commands],
            "work_dir": str(work_dir),
        }
        if examples_wheel_evidence is not None:
            manifest["examples_wheel_validation"] = examples_wheel_evidence
        _write_manifest(manifest, output_dir)
        return manifest
    except BaseException:
        # Keep generated-directory cleanup deterministic, then re-raise interrupts unchanged.
        for generated_dir in generated_dirs:
            try:
                _remove_tree(generated_dir)
            except OSError:
                logging.warning("Failed to clean generated directory %s", generated_dir, exc_info=True)
                cleanup_failures.append(generated_dir)
        for generated_dir in generated_dirs:
            if generated_dir.exists() and generated_dir not in cleanup_failures:
                cleanup_failures.append(generated_dir)
        if cleanup_failures:
            logging.warning(
                "Validation cleanup left generated directories behind: %s",
                ", ".join(str(path) for path in cleanup_failures),
            )
        raise


def _write_manifest(manifest: dict[str, Any], output_dir: Path) -> None:
    manifest_path = output_dir / "release-candidate-validation-results.json"
    summary_path = output_dir / "release-candidate-summary.md"
    manifest_tmp = output_dir / f".{manifest_path.name}.tmp"
    summary_tmp = output_dir / f".{summary_path.name}.tmp"
    lines = [
        "# Release Candidate Validation Summary",
        "",
        f"- Generated at: `{manifest['generated_at']}`",
        f"- Result: `{manifest['result']['status']}`",
        f"- Commands: `{manifest['result']['command_count']}` total, `{manifest['result']['failed_command_count']}` failed",
        f"- Dirty repositories: `{manifest['result']['dirty_repository_count']}`",
        f"- Platform: `{manifest['platform']['system']} {manifest['platform']['release']} {manifest['platform']['machine']}`",
        f"- Finding IDs: `{', '.join(manifest['finding_ids'])}`",
        "",
        "## Repositories",
        "",
    ]
    for repo in manifest["repositories"]:
        state = "dirty" if repo["dirty"] else "clean"
        lines.append(f"- `{repo['name']}` `{repo['commit'][:7]}` on `{repo['branch']}`: {state}")
    if "pytest_totals" in manifest:
        totals = ", ".join(
            f"{status}={count}"
            for status, count in manifest["pytest_totals"].items()
        ) or "(none)"
        lines.extend(["", "## Pytest Totals", "", f"- {totals}"])
    lines.extend(["", "## Validation Index", ""])
    for finding_id in manifest.get("finding_ids", []):
        command_names = manifest.get("validation_index", {}).get(finding_id, [])
        validation_commands = ", ".join(f"`{name}`" for name in command_names) or "(none)"
        lines.append(f"- `{finding_id}`: {validation_commands}")
    lines.extend(["", "## Commands", ""])
    for command in manifest["commands"]:
        status = "pass" if command["exit_code"] == 0 else "fail"
        lines.append(
            f"- `{command['name']}`: {status} in {command['duration_seconds']}s"
        )
    lines.extend(["", "## Artifacts", ""])
    for artifact in manifest["artifacts"]:
        lines.append(f"- `{artifact['name']}` sha256 `{artifact['sha256']}`")
    inventory = manifest.get("dependency_inventory")
    if inventory is not None:
        runtime_dependencies = inventory.get("runtime_dependencies", [])
        runtime = ", ".join(f"`{dependency}`" for dependency in runtime_dependencies) or "(none)"
        lines.extend(["", "## Dependency Inventory", "", f"- Runtime dependencies: {runtime}"])
        for extra, dependencies in inventory.get("optional_dependencies", {}).items():
            listed = ", ".join(f"`{dependency}`" for dependency in dependencies) or "(none)"
            lines.append(f"- `{extra}` extra: {listed}")
    examples_wheel_evidence = manifest.get("examples_wheel_validation")
    if examples_wheel_evidence is not None:
        lines.extend([
            "",
            "## Examples Wheel Matrix",
            "",
            f"- Status: `{examples_wheel_evidence['status']}`",
            f"- Wheel SHA-256: `{examples_wheel_evidence['wheel_sha256']}`",
            f"- Evidence: `{examples_wheel_evidence['evidence_path']}`",
        ])
    manifest_replaced = False
    summary_replaced = False
    try:
        manifest_tmp.write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        summary_tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest_tmp.replace(manifest_path)
        manifest_replaced = True
        summary_tmp.replace(summary_path)
        summary_replaced = True
    except OSError:
        if manifest_replaced and not summary_replaced:
            manifest_path.unlink(missing_ok=True)
        raise
    finally:
        manifest_tmp.unlink(missing_ok=True)
        summary_tmp.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    default_repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Run release-candidate validation and write result files."
    )
    parser.add_argument("--repo-root", type=Path, default=default_repo_root)
    parser.add_argument("--examples-repo", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for release-candidate validation result files.",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help=(
            "Keep an owned temporary work directory after validation. By default, "
            "owned temporary work directories are removed after writing validation results."
        ),
    )
    parser.add_argument(
        "--prebuilt-artifacts-dir",
        type=Path,
        default=None,
        help="Validate the exact wheel and source distribution already present in this directory.",
    )
    parser.add_argument("--examples-pytest", action="append", default=[])
    parser.add_argument(
        "--examples-wheel-matrix",
        action="store_true",
        help="Run the frozen deterministic external examples matrix against the candidate wheel.",
    )
    parser.add_argument(
        "--example-cli-project",
        default=None,
        help="Optional relative example project path to run with the installed wheel.",
    )
    parser.add_argument(
        "--example-cli-db",
        default="rpacore.db",
        help="Transaction database path, relative to --example-cli-project.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    owns_work_dir = args.work_dir is None
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="rpacore-rc-"))
    output_dir = args.output_dir or (work_dir / "validation-results")
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest = validate_release_candidate(
            repo_root=args.repo_root.resolve(),
            examples_repo=args.examples_repo.resolve() if args.examples_repo else None,
            work_dir=work_dir.resolve(),
            output_dir=output_dir.resolve(),
            examples_pytest=list(args.examples_pytest),
            example_cli_project=args.example_cli_project,
            example_cli_db=args.example_cli_db,
            examples_wheel_matrix=args.examples_wheel_matrix,
            prebuilt_artifacts_dir=(
                args.prebuilt_artifacts_dir.resolve()
                if args.prebuilt_artifacts_dir is not None
                else None
            ),
        )
        print(f"Wrote release-candidate validation results to {output_dir.resolve()}")
    finally:
        if owns_work_dir and args.output_dir is not None and not args.keep_work_dir:
            _remove_tree(work_dir)
    return 0 if manifest["result"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
