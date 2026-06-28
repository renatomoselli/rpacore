"""Produce reproducible release-candidate evidence for RPA Core."""

from __future__ import annotations

import argparse
from collections.abc import Callable
import fnmatch
import hashlib
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
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parents[1]
# These scripts must run directly from scripts/ before rpacore is installed.
if str(REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_FOR_IMPORTS))

from rpacore._validation import (
    PRERELEASE_WHEEL_PATTERN,
    ValidationError,
    assert_relative_path,
    example_pytest_target as _example_pytest_target,
    validate_contained_path as _validate_contained_path,
)


TRUNCATE_OUTPUT_CHARS = 12_000
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
    "AUX",
    "CON",
    "NUL",
    "PRN",
}
COPY_IGNORE_PATTERNS = ("*.egg-info", "*.pyc", "*.pyo")
PYTEST_COUNT_PATTERN = re.compile(
    r"\b(?P<count>\d+)\s+(?P<status>passed|failed|skipped|xfailed|xpassed|errors?)\b"
)


@dataclass
class CommandEvidence:
    """One executed command and its bounded output."""

    name: str
    command: list[str]
    cwd: str
    exit_code: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepoEvidence:
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
) -> CommandEvidence:
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
        evidence = CommandEvidence(
            name=name,
            command=command,
            cwd=str(cwd),
            exit_code=exc.returncode,
            duration_seconds=round(time.perf_counter() - started, 3),
            stdout=_truncate(exc.stdout or ""),
            stderr=_truncate(exc.stderr or ""),
        )
        raise ValidationError(
            f"{name} failed with exit code {exc.returncode}\n"
            f"stdout:\n{evidence.stdout}\n"
            f"stderr:\n{evidence.stderr}"
        ) from exc
    evidence = CommandEvidence(
        name=name,
        command=command,
        cwd=str(cwd),
        exit_code=completed.returncode,
        duration_seconds=round(time.perf_counter() - started, 3),
        stdout=_truncate(completed.stdout),
        stderr=_truncate(completed.stderr),
    )
    if check and completed.returncode != 0:
        raise ValidationError(
            f"{name} failed with exit code {completed.returncode}\n"
            f"stdout:\n{evidence.stdout}\n"
            f"stderr:\n{evidence.stderr}"
        )
    return evidence


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


def _repo_evidence(name: str, path: Path) -> RepoEvidence:
    _require_git_repo(path, name=name)
    status = _git_output(path, "status", "--porcelain=v1").splitlines()
    return RepoEvidence(
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
        if name in COPY_IGNORE_NAMES:
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
        if path.suffix not in {".whl", ".gz"}:
            continue
        names = _archive_names(path)
        records.append(
            {
                "name": path.name,
                "path": str(path),
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
                "contains_license": any(name.endswith("LICENSE") for name in names),
                "contains_examples": any(
                    name.startswith("examples/") or "/examples/" in name
                    for name in names
                ),
            }
        )
    return records


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


def _parse_json_output(evidence: CommandEvidence) -> dict[str, Any]:
    try:
        parsed = json.loads(evidence.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{evidence.name} did not produce valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"{evidence.name} did not produce a JSON object")
    return parsed


def _parse_ndjson_output(evidence: CommandEvidence) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(evidence.stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"{evidence.name} line {line_number} did not produce valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValidationError(f"{evidence.name} produced a non-object NDJSON line")
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


def _installed_smoke_code(repo_root: Path) -> str:
    return f"""
import json
from pathlib import Path

import rpacore

repo_root = Path({str(repo_root.resolve())!r})
module_file = Path(rpacore.__file__).resolve()
if module_file.is_relative_to(repo_root.resolve()):
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
    result = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        "executable": str(python),
        "version": sys.version,
        "pip": result.stdout.strip(),
    }


def validate_release_candidate(
    *,
    repo_root: Path,
    examples_repo: Path | None,
    work_dir: Path,
    output_dir: Path,
    examples_pytest: list[str],
) -> dict[str, Any]:
    """Run release-candidate validation and return the evidence manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir.resolve()
    source_dir = work_dir / "source"
    framework_copy = source_dir / "rpacore"
    examples_copy = source_dir / "rpacore-examples"
    wheelhouse = work_dir / "wheelhouse"
    outside_dir = work_dir / "outside"
    venv_dir = work_dir / "venv"
    generated_dirs = (source_dir, wheelhouse, outside_dir, venv_dir)

    for generated_dir in generated_dirs:
        if generated_dir.exists():
            _remove_tree(generated_dir)
    if examples_repo is not None and not examples_pytest:
        raise ValidationError("--examples-pytest is required when --examples-repo is provided")
    cleanup_failures: list[Path] = []
    try:
        source_dir.mkdir(parents=True)
        wheelhouse.mkdir(parents=True)
        outside_dir.mkdir(parents=True)

        repos = [_repo_evidence("rpacore", repo_root)]
        if examples_repo is not None:
            repos.append(_repo_evidence("rpacore-examples", examples_repo))

        _copy_tree(repo_root, framework_copy)
        if examples_repo is not None:
            _copy_tree(examples_repo, examples_copy)

        commands: list[CommandEvidence] = []
        env = _python_env()
        allowed_run_roots = (work_dir,)
        commands.append(_run("framework_tests", [sys.executable, "-m", "pytest", "-q"], cwd=framework_copy, allowed_roots=allowed_run_roots, env=env))
        commands[-1].parsed = _pytest_counts(commands[-1].stdout + "\n" + commands[-1].stderr)

        commands.append(
            _run(
                "build_artifacts",
                [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(wheelhouse)],
                cwd=framework_copy,
                allowed_roots=allowed_run_roots,
                env=env,
            )
        )
        artifacts = _artifact_records(wheelhouse)
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
        commands.append(_run("create_venv", [sys.executable, "-m", "venv", str(venv_dir)], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        python = _venv_python(venv_dir)
        commands.append(_run("install_wheel", [str(python), "-m", "pip", "install", str(wheel)], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        commands.append(_run("installed_import_smoke", [str(python), "-c", _installed_smoke_code(repo_root)], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        commands[-1].parsed = _parse_json_output(commands[-1])

        rpacore_cli = _venv_script(venv_dir, "rpacore")
        commands.append(_run("cli_version", [str(rpacore_cli), "version"], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        generated_project = outside_dir / "installed_project"
        if generated_project.exists():
            _remove_tree(generated_project)
        commands.append(_run("cli_init", [str(rpacore_cli), "init", "installed_project"], cwd=outside_dir, allowed_roots=allowed_run_roots, env=env))
        commands.append(_run("cli_run_generated_project", [str(rpacore_cli), "run"], cwd=generated_project, allowed_roots=allowed_run_roots, env=env))
        commands.append(
            _run(
                "cli_transaction_list",
                [str(rpacore_cli), "transaction", "list"],
                cwd=generated_project,
                allowed_roots=allowed_run_roots,
                env=env,
            )
        )

        list_json = _run("cli_transaction_list_json", [str(rpacore_cli), "transaction", "list", "--json"], cwd=generated_project, allowed_roots=allowed_run_roots, env=env)
        list_payload = _parse_json_output(list_json)
        transactions = _transactions_from_list_payload(list_payload)
        list_json.parsed = {"transaction_count": len(transactions)}
        commands.append(list_json)
        tx_ids = _transaction_ids(transactions)

        commands.append(
            _run(
                "cli_transaction_show",
                [str(rpacore_cli), "transaction", "show", tx_ids[0]],
                cwd=generated_project,
                allowed_roots=allowed_run_roots,
                env=env,
            )
        )
        show_json = _run(
            "cli_transaction_show_json",
            [str(rpacore_cli), "transaction", "show", tx_ids[0], "--json"],
            cwd=generated_project,
            allowed_roots=allowed_run_roots,
            env=env,
        )
        show_json.parsed = {"transaction_id": _parse_json_output(show_json)["transaction"]["id"]}
        commands.append(show_json)

        export_json = _run(
            "cli_transaction_export_json",
            [str(rpacore_cli), "transaction", "export", "--format", "json"],
            cwd=generated_project,
            allowed_roots=allowed_run_roots,
            env=env,
        )
        export_payload = _parse_json_output(export_json)
        export_json.parsed = {
            "transaction_count": len(_transactions_from_list_payload(export_payload))
        }
        commands.append(export_json)

        export_ndjson = _run(
            "cli_transaction_export_ndjson",
            [str(rpacore_cli), "transaction", "export", "--format", "ndjson"],
            cwd=generated_project,
            allowed_roots=allowed_run_roots,
            env=env,
        )
        export_ndjson.parsed = {"record_count": len(_parse_ndjson_output(export_ndjson))}
        commands.append(export_ndjson)

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
                evidence = _run(
                    f"example_pytest:{target.manifest_path}",
                    command,
                    cwd=target.project_dir,
                    allowed_roots=allowed_run_roots,
                    env=env,
                )
                evidence.parsed = _pytest_counts(evidence.stdout + "\n" + evidence.stderr)
                commands.append(evidence)

        manifest = {
            "schema_version": 1,
            "generated_at": _utc_now().isoformat(),
            "finding_ids": list(VALIDATION_FINDINGS),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "architecture": platform.architecture()[0],
            },
            "python": _python_version_info(Path(sys.executable)),
            "repositories": [asdict(repo) for repo in repos],
            "artifacts": artifacts,
            "commands": [asdict(command) for command in commands],
            "work_dir": str(work_dir),
        }
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
    manifest_path = output_dir / "release-candidate-evidence.json"
    summary_path = output_dir / "release-candidate-summary.md"
    manifest_tmp = output_dir / f".{manifest_path.name}.tmp"
    summary_tmp = output_dir / f".{summary_path.name}.tmp"
    lines = [
        "# Release Candidate Evidence Summary",
        "",
        f"- Generated at: `{manifest['generated_at']}`",
        f"- Platform: `{manifest['platform']['system']} {manifest['platform']['release']} {manifest['platform']['machine']}`",
        f"- Finding IDs: `{', '.join(manifest['finding_ids'])}`",
        "",
        "## Repositories",
        "",
    ]
    for repo in manifest["repositories"]:
        state = "dirty" if repo["dirty"] else "clean"
        lines.append(f"- `{repo['name']}` `{repo['commit'][:7]}` on `{repo['branch']}`: {state}")
    lines.extend(["", "## Commands", ""])
    for command in manifest["commands"]:
        status = "pass" if command["exit_code"] == 0 else "fail"
        lines.append(
            f"- `{command['name']}`: {status} in {command['duration_seconds']}s"
        )
    lines.extend(["", "## Artifacts", ""])
    for artifact in manifest["artifacts"]:
        lines.append(f"- `{artifact['name']}` sha256 `{artifact['sha256']}`")
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
        description="Run release-candidate validation and write evidence files."
    )
    parser.add_argument("--repo-root", type=Path, default=default_repo_root)
    parser.add_argument("--examples-repo", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help=(
            "Keep an owned work directory after validation. When --output-dir is omitted, "
            "evidence is written inside the work directory and the directory is kept."
        ),
    )
    parser.add_argument("--examples-pytest", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    owns_work_dir = args.work_dir is None
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="rpacore-rc-"))
    output_dir = args.output_dir or (work_dir / "evidence")
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        validate_release_candidate(
            repo_root=args.repo_root.resolve(),
            examples_repo=args.examples_repo.resolve() if args.examples_repo else None,
            work_dir=work_dir.resolve(),
            output_dir=output_dir.resolve(),
            examples_pytest=list(args.examples_pytest),
        )
        print(f"Wrote release-candidate evidence to {output_dir.resolve()}")
    finally:
        if owns_work_dir and args.output_dir is not None and not args.keep_work_dir:
            _remove_tree(work_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
