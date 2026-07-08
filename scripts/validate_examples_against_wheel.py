"""Validate every external example against a freshly built RPA Core wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rpacore._validation import ValidationError
from rpacore._validation import validate_contained_path

logger = logging.getLogger(__name__)

TIMEOUT_EXIT_CODE = 124
BROKEN_VENV_SENTINEL = ".rpacore-validation-broken"


class BuildValidationError(ValidationError):
    """Validation failure that preserves the build command record."""

    def __init__(self, message: str, command_record: CommandRecord) -> None:
        super().__init__(message)
        self.command_record = command_record

EXAMPLE_REGISTRY: dict[str, dict[str, object]] = {
    "acme_work_items": {
        "category": "manual",
        "main_skip_reason": "requires prepared browser/session state",
    },
    "checkpoint_resume": {"category": "deterministic"},
    "database_reconciliation": {
        "category": "deterministic",
        "main_expected_exit_codes": {0, 1},
    },
    "excel_reorganization": {"category": "deterministic"},
    "file_inbox_processor": {"category": "deterministic"},
    "git_repo_health_monitor": {"category": "deterministic"},
    "json_event_log_processor": {"category": "deterministic"},
    "pdf_invoice_extraction": {
        "category": "manual",
        "main_skip_reason": "requires optional document-processing setup",
    },
    "rest_api_batch": {
        "category": "manual",
        "main_skip_reason": "requires network/API availability",
    },
    "rpa_challenge": {
        "category": "manual",
        "main_skip_reason": "requires browser automation setup",
    },
    "windows_calculator": {
        "category": "manual",
        "main_skip_reason": "requires Windows desktop UI automation",
    },
}

GENERATED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}

MAX_CAPTURE_CHARS = 12_000
RUN_MAIN_CHOICES = ("deterministic", "all", "none")


@dataclass
class CommandRecord:
    """Serializable record for a command run."""

    name: str
    command: list[str]
    cwd: str
    exit_code: int
    duration_seconds: float
    stdout: str
    stderr: str
    skipped: bool = False
    skip_reason: str | None = None
    timed_out: bool = False
    expected_exit_codes: list[int] = field(default_factory=lambda: [0])

    @property
    def passed(self) -> bool:
        return self.skipped or self.exit_code in self.expected_exit_codes


@dataclass
class ExampleResult:
    """Validation result for one example project."""

    name: str
    path: str
    venv_path: str
    category: str
    commands: list[CommandRecord] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(not command.passed for command in self.commands):
            return "fail"
        if self.commands and all(command.skipped for command in self.commands):
            return "skip"
        return "pass"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _truncate(text: str) -> str:
    if len(text) <= MAX_CAPTURE_CHARS:
        return text
    return f"{text[:MAX_CAPTURE_CHARS]}\n...<truncated {len(text) - MAX_CAPTURE_CHARS} chars>"


def _timeout_stderr(stderr: str | bytes | None, *, timeout_seconds: int) -> str:
    stderr_text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else (stderr or "")
    return f"{_truncate(stderr_text)}\ncommand timed out after {timeout_seconds} seconds"


def _run(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: int,
    allowed_roots: tuple[Path, ...] = (),
    expected_exit_codes: set[int] | None = None,
) -> CommandRecord:
    expected = sorted(expected_exit_codes or {0})
    if allowed_roots:
        cwd = validate_contained_path(cwd, allowed_roots=allowed_roots, label=f"{name} cwd")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandRecord(
            name=name,
            command=[str(part) for part in command],
            cwd=str(cwd),
            exit_code=TIMEOUT_EXIT_CODE,
            duration_seconds=round(time.perf_counter() - started, 3),
            stdout=_truncate(exc.stdout or ""),
            stderr=_timeout_stderr(exc.stderr, timeout_seconds=timeout_seconds),
            timed_out=True,
            expected_exit_codes=expected,
        )
    return CommandRecord(
        name=name,
        command=[str(part) for part in command],
        cwd=str(cwd),
        exit_code=completed.returncode,
        duration_seconds=round(time.perf_counter() - started, 3),
        stdout=_truncate(completed.stdout),
        stderr=_truncate(completed.stderr),
        expected_exit_codes=expected,
    )


def _skipped(name: str, *, cwd: Path, reason: str) -> CommandRecord:
    return CommandRecord(
        name=name,
        command=[],
        cwd=str(cwd),
        exit_code=0,
        duration_seconds=0.0,
        stdout="",
        stderr="",
        skipped=True,
        skip_reason=reason,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def _onerror(function: Any, failed_path: str, exc_info: object) -> None:
        logger.warning("Retrying cleanup after removal failure: %s", failed_path, exc_info=exc_info)
        try:
            os.chmod(failed_path, 0o700)
            function(failed_path)
        except OSError:
            logger.warning("Cleanup retry failed: %s", failed_path, exc_info=True)
            raise

    shutil.rmtree(path, onerror=_onerror)
    if path.exists():
        raise OSError(f"cleanup left directory behind: {path}")


def _copy_example_workspace(source: Path, destination: Path) -> Path:
    try:
        _remove_tree(destination)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(*GENERATED_DIR_NAMES),
        )
    except (OSError, shutil.Error) as exc:
        try:
            _remove_tree(destination)
        except OSError as cleanup_exc:
            cleanup_exc.add_note(f"original copy failure: {exc}")
            raise OSError(f"copy failed and partial workspace cleanup failed: {destination}") from cleanup_exc
        raise
    return destination


def _venv_python(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def _example_venv_path(example_dir: Path, *, venv_mode: str, venv_root: Path) -> Path:
    if venv_mode == "in-place":
        return example_dir / ".venv"
    return venv_root / example_dir.name


def _build_wheel(repo_root: Path, wheelhouse: Path, *, timeout_seconds: int) -> tuple[Path, CommandRecord]:
    _remove_tree(wheelhouse)
    wheelhouse.mkdir(parents=True)
    command = [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(wheelhouse)]
    try:
        command_record = _run(
            "build_wheel",
            command,
            cwd=repo_root,
            timeout_seconds=timeout_seconds,
            allowed_roots=(repo_root,),
        )
        wheels = sorted(wheelhouse.glob("rpacore-*.whl"))
        if command_record.exit_code != 0:
            raise BuildValidationError(
                f"wheel build failed with exit code {command_record.exit_code}",
                command_record,
            )
        if not wheels:
            raise BuildValidationError(f"build completed but no wheel was created in {wheelhouse}", command_record)
        if len(wheels) != 1:
            raise BuildValidationError(f"build created multiple wheels in {wheelhouse}: {wheels}", command_record)
    except BuildValidationError:
        try:
            _remove_tree(wheelhouse)
        except OSError as cleanup_error:
            logger.warning("Failed to clean partial wheelhouse %s: %s", wheelhouse, cleanup_error)
        raise
    return wheels[0], command_record


def _example_dirs(examples_repo: Path) -> list[Path]:
    examples_dir = examples_repo / "examples"
    if not examples_dir.is_dir():
        raise ValidationError(f"examples directory does not exist: {examples_dir}")
    example_dirs = []
    for path in sorted(path for path in examples_dir.iterdir() if path.is_dir()):
        if path.name in GENERATED_DIR_NAMES:
            logger.warning("Skipping generated examples directory candidate: %s", path)
            continue
        example_dirs.append(path)
    return example_dirs


def _filter_example_dirs(
    example_dirs: list[Path],
    *,
    include_examples: set[str],
    exclude_examples: set[str],
) -> list[Path]:
    names = {path.name for path in example_dirs}
    missing_includes = sorted(include_examples - names)
    if missing_includes:
        raise ValidationError(f"requested examples do not exist: {missing_includes}")
    if include_examples:
        example_dirs = [path for path in example_dirs if path.name in include_examples]
    return [path for path in example_dirs if path.name not in exclude_examples]


def _validated_examples_repo(examples_repo: Path) -> Path:
    resolved = examples_repo.resolve()
    examples_dir = validate_contained_path(
        resolved / "examples",
        allowed_roots=(resolved,),
        label="examples directory",
    )
    if not examples_dir.is_dir():
        raise ValidationError(f"examples directory does not exist: {examples_dir}")
    return resolved


def _requirements_files(example_dir: Path, examples_repo: Path) -> list[Path]:
    candidates = [
        examples_repo / "requirements.txt",
        example_dir / "requirements.txt",
        example_dir / "requirements-test.txt",
    ]
    return [path for path in candidates if path.exists()]


def _requirement_name(line: str) -> str:
    requirement = line.strip()
    if not requirement or requirement.startswith(("#", "-")):
        return ""
    for separator in ("[", "==", ">=", "<=", "~=", "!=", ">", "<", ";"):
        requirement = requirement.split(separator, 1)[0]
    return requirement.strip().lower().replace("_", "-")


def _requires_playwright(example_dir: Path, examples_repo: Path) -> bool:
    for requirements in _requirements_files(example_dir, examples_repo):
        for line in requirements.read_text(encoding="utf-8").splitlines():
            if _requirement_name(line) == "playwright":
                return True
    return False


def _requirements_command_name(requirements: Path, *, example_dir: Path, examples_repo: Path) -> str:
    if requirements.parent == examples_repo:
        scope = "repo"
    elif requirements.parent == example_dir:
        scope = "example"
    else:
        scope = "external"
    return f"install_requirements:{scope}:{requirements.name}"


def _test_targets(example_dir: Path) -> list[str]:
    targets: list[str] = []
    for name in ("tests", "test"):
        target = example_dir / name
        if target.is_dir() and any(target.rglob("test_*.py")):
            targets.append(name)
    return targets


def _example_settings(example_name: str) -> dict[str, object]:
    return EXAMPLE_REGISTRY.get(
        example_name,
        {
            "category": "manual",
            "main_skip_reason": "not in deterministic main allowlist",
        },
    )


def _classify_example(example_name: str, include_manual_main: bool) -> tuple[str, str | None]:
    settings = _example_settings(example_name)
    category = str(settings["category"])
    if category == "deterministic":
        return category, None
    if include_manual_main:
        return "manual-included", None
    return category, str(settings["main_skip_reason"])


def _main_expected_exit_codes(example_name: str) -> set[int] | None:
    settings = _example_settings(example_name)
    expected_exit_codes = settings.get("main_expected_exit_codes")
    if expected_exit_codes is None:
        return None
    return set(expected_exit_codes)


def _pip_install_requirements(
    result: ExampleResult,
    python: Path,
    example_dir: Path,
    examples_repo: Path,
    *,
    timeout_seconds: int,
) -> None:
    requirements_files = _requirements_files(example_dir, examples_repo)
    if not requirements_files:
        result.commands.append(
            _skipped(
                "install_requirements:skip:no_files",
                cwd=example_dir,
                reason="no requirements files",
            )
        )
        return
    for index, requirements in enumerate(requirements_files):
        command_name = _requirements_command_name(
            requirements,
            example_dir=example_dir,
            examples_repo=examples_repo,
        )
        result.commands.append(
            _run(
                command_name,
                [str(python), "-m", "pip", "install", "-r", str(requirements)],
                cwd=example_dir,
                timeout_seconds=timeout_seconds,
                allowed_roots=(example_dir, examples_repo),
            )
        )
        if not result.commands[-1].passed:
            for remaining in requirements_files[index + 1 :]:
                result.commands.append(
                    _skipped(
                        _requirements_command_name(
                            remaining,
                            example_dir=example_dir,
                            examples_repo=examples_repo,
                        ),
                        cwd=example_dir,
                        reason=f"dependency setup failed at {command_name}",
                    )
                )
            break


def _last_failed_command(commands: list[CommandRecord]) -> CommandRecord | None:
    for command in reversed(commands):
        if not command.passed:
            return command
    return None


def _cleanup_failed_setup_venv(venv_path: Path) -> None:
    try:
        _remove_tree(venv_path)
    except OSError:
        logger.warning("Failed to clean invalid example venv: %s", venv_path, exc_info=True)
        try:
            venv_path.mkdir(parents=True, exist_ok=True)
            (venv_path / BROKEN_VENV_SENTINEL).write_text("cleanup failed\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to mark invalid example venv: %s", venv_path, exc_info=True)
            raise OSError(f"failed to mark invalid example venv: {venv_path}") from exc


def _record_broken_venv(result: ExampleResult, example_dir: Path, venv_path: Path) -> None:
    result.commands.append(
        CommandRecord(
            name="venv_cleanup_required",
            command=[],
            cwd=str(example_dir),
            exit_code=1,
            duration_seconds=0.0,
            stdout="",
            stderr=f"example venv is marked unusable: {venv_path}",
        )
    )
    result.commands.append(
        _skipped("installed_import", cwd=example_dir, reason="venv cleanup required")
    )
    result.commands.append(_skipped("pytest", cwd=example_dir, reason="venv cleanup required"))
    result.commands.append(_skipped("run_main", cwd=example_dir, reason="venv cleanup required"))


def _validate_example(
    example_dir: Path,
    *,
    examples_repo: Path,
    wheel: Path,
    venv_path: Path,
    recreate_venv: bool,
    run_main: str,
    install_playwright_browsers: bool,
    timeout_seconds: int,
) -> ExampleResult:
    include_manual_main = run_main == "all"
    category, main_skip_reason = _classify_example(example_dir.name, include_manual_main)
    result = ExampleResult(
        name=example_dir.name,
        path=str(example_dir),
        venv_path=str(venv_path),
        category=category,
    )

    broken_sentinel = venv_path / BROKEN_VENV_SENTINEL
    if broken_sentinel.exists():
        _cleanup_failed_setup_venv(venv_path)
        if broken_sentinel.exists():
            _record_broken_venv(result, example_dir, venv_path)
            return result

    if recreate_venv:
        _remove_tree(venv_path)
    if not venv_path.exists():
        venv_path.parent.mkdir(parents=True, exist_ok=True)
        result.commands.append(
            _run(
                "create_venv",
                [sys.executable, "-m", "venv", str(venv_path)],
                cwd=example_dir,
                timeout_seconds=timeout_seconds,
                allowed_roots=(example_dir,),
            )
        )
        if not result.commands[-1].passed:
            _cleanup_failed_setup_venv(venv_path)
            result.commands.append(
                _skipped(
                    "installed_import",
                    cwd=example_dir,
                    reason="venv setup failed at create_venv",
                )
            )
            result.commands.append(
                _skipped(
                    "pytest",
                    cwd=example_dir,
                    reason="venv setup failed at create_venv",
                )
            )
            result.commands.append(
                _skipped(
                    "run_main",
                    cwd=example_dir,
                    reason="venv setup failed at create_venv",
                )
            )
            return result

    python = _venv_python(venv_path)
    if not python.exists():
        logger.error("venv Python not found: %s", python)
        _cleanup_failed_setup_venv(venv_path)
        result.commands.append(
            CommandRecord(
                name="venv_python_missing",
                command=[str(python)],
                cwd=str(example_dir),
                exit_code=1,
                duration_seconds=0.0,
                stdout="",
                stderr=f"venv Python was not found: {python}",
            )
        )
        return result

    result.commands.append(
        _run(
            "upgrade_pip",
            [str(python), "-m", "pip", "install", "--upgrade", "pip"],
            cwd=example_dir,
            timeout_seconds=timeout_seconds,
            allowed_roots=(example_dir,),
        )
    )
    if not result.commands[-1].passed:
        _cleanup_failed_setup_venv(venv_path)
        result.commands.append(_skipped("uninstall_rpacore", cwd=example_dir, reason="dependency setup failed at upgrade_pip"))
        result.commands.append(_skipped("install_rpacore_wheel", cwd=example_dir, reason="dependency setup failed at upgrade_pip"))
        result.commands.append(_skipped("installed_import", cwd=example_dir, reason="dependency setup failed at upgrade_pip"))
        result.commands.append(_skipped("pytest", cwd=example_dir, reason="dependency setup failed at upgrade_pip"))
        result.commands.append(_skipped("run_main", cwd=example_dir, reason="dependency setup failed at upgrade_pip"))
        return result
    result.commands.append(
        _run(
            "uninstall_rpacore",
            [str(python), "-m", "pip", "uninstall", "-y", "rpacore"],
            cwd=example_dir,
            timeout_seconds=timeout_seconds,
            allowed_roots=(example_dir,),
        )
    )
    if not result.commands[-1].passed:
        _cleanup_failed_setup_venv(venv_path)
        result.commands.append(_skipped("install_rpacore_wheel", cwd=example_dir, reason="dependency setup failed at uninstall_rpacore"))
        result.commands.append(_skipped("installed_import", cwd=example_dir, reason="dependency setup failed at uninstall_rpacore"))
        result.commands.append(_skipped("pytest", cwd=example_dir, reason="dependency setup failed at uninstall_rpacore"))
        result.commands.append(_skipped("run_main", cwd=example_dir, reason="dependency setup failed at uninstall_rpacore"))
        return result
    result.commands.append(
        _run(
            "install_rpacore_wheel",
            [str(python), "-m", "pip", "install", str(wheel)],
            cwd=example_dir,
            timeout_seconds=timeout_seconds,
            allowed_roots=(example_dir, wheel.parent),
        )
    )
    wheel_install_command = result.commands[-1]
    if not wheel_install_command.passed:
        _cleanup_failed_setup_venv(venv_path)
        result.commands.append(
            _skipped(
                "installed_import",
                cwd=example_dir,
                reason="dependency setup failed at install_rpacore_wheel",
            )
        )
        result.commands.append(
            _skipped(
                "pytest",
                cwd=example_dir,
                reason="dependency setup failed at install_rpacore_wheel",
            )
        )
        result.commands.append(
            _skipped(
                "run_main",
                cwd=example_dir,
                reason="dependency setup failed at install_rpacore_wheel",
            )
        )
        return result
    result.commands.append(
        _run(
            "installed_import",
            [
                str(python),
                "-c",
                "import json, rpacore; print(json.dumps({'version': rpacore.__version__, 'file': rpacore.__file__}, sort_keys=True))",
            ],
            cwd=example_dir,
            timeout_seconds=timeout_seconds,
            allowed_roots=(example_dir,),
        )
    )
    if not result.commands[-1].passed:
        _cleanup_failed_setup_venv(venv_path)
        result.commands.append(
            _skipped("pytest", cwd=example_dir, reason="installed import failed")
        )
        result.commands.append(
            _skipped("run_main", cwd=example_dir, reason="installed import failed")
        )
        return result

    _pip_install_requirements(
        result,
        python,
        example_dir,
        examples_repo,
        timeout_seconds=timeout_seconds,
    )
    dependency_failure = _last_failed_command(result.commands)
    if dependency_failure is not None:
        _cleanup_failed_setup_venv(venv_path)
        result.commands.append(
            _skipped(
                "pytest",
                cwd=example_dir,
                reason=f"dependency setup failed at {dependency_failure.name}",
            )
        )
        result.commands.append(
            _skipped(
                "run_main",
                cwd=example_dir,
                reason=f"dependency setup failed at {dependency_failure.name}",
            )
        )
        return result

    if install_playwright_browsers and _requires_playwright(example_dir, examples_repo):
        result.commands.append(
            _run(
                "install_playwright_browsers",
                [str(python), "-m", "playwright", "install", "chromium"],
                cwd=example_dir,
                timeout_seconds=timeout_seconds,
                allowed_roots=(example_dir,),
            )
        )
        playwright_command = result.commands[-1]
        if not playwright_command.passed:
            _cleanup_failed_setup_venv(venv_path)
            result.commands.append(
                _skipped(
                    "pytest",
                    cwd=example_dir,
                    reason=f"browser setup failed at {playwright_command.name}",
                )
            )
            result.commands.append(
                _skipped(
                    "run_main",
                    cwd=example_dir,
                    reason=f"browser setup failed at {playwright_command.name}",
                )
            )
            return result

    test_targets = _test_targets(example_dir)
    pytest_ready = True
    if test_targets:
        result.commands.append(
            _run(
                "install_pytest",
                [str(python), "-m", "pip", "install", "pytest"],
                cwd=example_dir,
                timeout_seconds=timeout_seconds,
                allowed_roots=(example_dir,),
            )
        )
        pytest_ready = result.commands[-1].passed

    if test_targets and not pytest_ready:
        _cleanup_failed_setup_venv(venv_path)
        for test_target in test_targets:
            result.commands.append(
                _skipped(
                    f"pytest:{test_target}",
                    cwd=example_dir,
                    reason="pytest setup failed at install_pytest",
                )
            )
        result.commands.append(_skipped("run_main", cwd=example_dir, reason="pytest setup failed at install_pytest"))
        return result
    else:
        for test_target in test_targets:
            result.commands.append(
                _run(
                    f"pytest:{test_target}",
                    [str(python), "-m", "pytest", test_target, "-q"],
                    cwd=example_dir,
                    timeout_seconds=timeout_seconds,
                    allowed_roots=(example_dir,),
                )
            )
    if not test_targets:
        result.commands.append(_skipped("pytest", cwd=example_dir, reason="no tests or test directory"))

    if run_main == "none":
        result.commands.append(_skipped("run_main", cwd=example_dir, reason="main execution disabled"))
    elif main_skip_reason is not None:
        result.commands.append(_skipped("run_main", cwd=example_dir, reason=main_skip_reason))
    elif (example_dir / "main.py").exists():
        result.commands.append(
            _run(
                "run_main",
                [str(python), "main.py"],
                cwd=example_dir,
                timeout_seconds=timeout_seconds,
                allowed_roots=(example_dir,),
                expected_exit_codes=_main_expected_exit_codes(example_dir.name),
            )
        )
    else:
        result.commands.append(_skipped("run_main", cwd=example_dir, reason="main.py not found"))

    return result


def _command_to_dict(command: CommandRecord) -> dict[str, Any]:
    return {
        "name": command.name,
        "command": command.command,
        "cwd": command.cwd,
        "exit_code": command.exit_code,
        "duration_seconds": command.duration_seconds,
        "stdout": command.stdout,
        "stderr": command.stderr,
        "skipped": command.skipped,
        "skip_reason": command.skip_reason,
        "timed_out": command.timed_out,
        "expected_exit_codes": command.expected_exit_codes,
    }


def _result_to_dict(result: ExampleResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "path": result.path,
        "venv_path": result.venv_path,
        "category": result.category,
        "status": result.status,
        "commands": [_command_to_dict(command) for command in result.commands],
    }


def _write_outputs(manifest: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "examples-wheel-validation.json"
    summary_path = output_dir / "examples-wheel-validation.md"
    manifest_tmp = manifest_path.with_suffix(".json.tmp")
    summary_tmp = summary_path.with_suffix(".md.tmp")
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True)

    lines = [
        "# Examples Wheel Validation",
        "",
        f"- Generated at: `{manifest['generated_at']}`",
        f"- Result: `{manifest['result']['status']}`",
        f"- Examples: `{manifest['result']['example_count']}` total, `{manifest['result']['failed_example_count']}` failed, `{manifest['result']['skipped_example_count']}` skipped",
        f"- Commands: `{manifest['result']['command_count']}` total, `{manifest['result']['failed_command_count']}` failed, `{manifest['result']['skipped_command_count']}` skipped",
        f"- Wheel: `{manifest['wheel']['name']}` sha256 `{manifest['wheel']['sha256']}`",
        "",
        "## Examples",
        "",
        "| Example | Category | Status | Tests | Main |",
        "| --- | --- | --- | --- | --- |",
    ]
    for example in manifest["examples"]:
        tests = ", ".join(
            command["name"]
            for command in example["commands"]
            if command["name"].startswith("pytest")
        )
        main = next((command for command in example["commands"] if command["name"] == "run_main"), None)
        main_status = "not recorded"
        if main is not None:
            main_status = "skip" if main["skipped"] else ("pass" if main["exit_code"] in main["expected_exit_codes"] else "fail")
        lines.append(
            f"| `{example['name']}` | {example['category']} | {example['status']} | {tests or 'none'} | {main_status} |"
        )

    failed_examples = [example for example in manifest["examples"] if example["status"] == "fail"]
    if failed_examples:
        lines.extend(["", "## Failures", ""])
        for example in failed_examples:
            lines.append(f"### `{example['name']}`")
            for command in example["commands"]:
                if command["exit_code"] not in command["expected_exit_codes"] and not command["skipped"]:
                    lines.append(f"- `{command['name']}` exited `{command['exit_code']}`")
            lines.append("")

    try:
        summary_tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        manifest_tmp.write_text(manifest_text, encoding="utf-8")
        manifest_tmp.replace(manifest_path)
        summary_tmp.replace(summary_path)
    except Exception:
        manifest_path.unlink(missing_ok=True)
        summary_path.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)
        summary_tmp.unlink(missing_ok=True)
        raise


def validate_examples_against_wheel(
    *,
    repo_root: Path,
    examples_repo: Path,
    output_dir: Path,
    work_dir: Path,
    venv_mode: str,
    recreate_venvs: bool,
    run_main: str,
    install_playwright_browsers: bool,
    include_examples: set[str] | None = None,
    exclude_examples: set[str] | None = None,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Build the current wheel and validate every example against it."""

    repo_root = repo_root.resolve()
    examples_repo = _validated_examples_repo(examples_repo)
    work_dir = work_dir.resolve()
    output_dir = output_dir.resolve()
    wheelhouse = work_dir / "wheelhouse"
    venv_root = work_dir / "venvs"
    build_started = _utc_now()
    try:
        wheel, build_command = _build_wheel(repo_root, wheelhouse, timeout_seconds=timeout_seconds)
    except BuildValidationError as exc:
        build_command = exc.command_record
        wheel = wheelhouse / ".rpacore-build-failed"
    build_failed = not build_command.passed

    results: list[ExampleResult] = []
    if not build_failed:
        workspace_root = work_dir / "examples"
        source_example_dirs = _filter_example_dirs(
            _example_dirs(examples_repo),
            include_examples=include_examples or set(),
            exclude_examples=exclude_examples or set(),
        )
        for source_example_dir in source_example_dirs:
            example_dir = _copy_example_workspace(
                source_example_dir,
                workspace_root / source_example_dir.name,
            )
            venv_path = _example_venv_path(source_example_dir, venv_mode=venv_mode, venv_root=venv_root)
            results.append(
                _validate_example(
                    example_dir,
                    examples_repo=examples_repo,
                    wheel=wheel,
                    venv_path=venv_path,
                    recreate_venv=recreate_venvs,
                    run_main=run_main,
                    install_playwright_browsers=install_playwright_browsers,
                    timeout_seconds=timeout_seconds,
                )
            )

    commands = [build_command, *(command for result in results for command in result.commands)]
    failed_commands = [command for command in commands if not command.passed]
    skipped_commands = [command for command in commands if command.skipped]
    failed_examples = [result for result in results if result.status == "fail"]
    skipped_examples = [result for result in results if result.status == "skip"]
    manifest = {
        "schema_version": 1,
        "generated_at": build_started.isoformat(),
        "repositories": {
            "rpacore": str(repo_root),
            "rpacore_examples": str(examples_repo),
        },
        "wheel": {
            "build_failed": build_failed,
            "name": None if build_failed else wheel.name,
            "path": None if build_failed else str(wheel),
            "sha256": _sha256(wheel) if wheel.exists() else None,
        },
        "settings": {
            "venv_mode": venv_mode,
            "recreate_venvs": recreate_venvs,
            "run_main": run_main,
            "install_playwright_browsers": install_playwright_browsers,
            "include_examples": sorted(include_examples or []),
            "exclude_examples": sorted(exclude_examples or []),
            "timeout_seconds": timeout_seconds,
        },
        "result": {
            "status": "fail" if failed_commands else "pass",
            "example_count": len(results),
            "failed_example_count": len(failed_examples),
            "skipped_example_count": len(skipped_examples),
            "command_count": len(commands),
            "failed_command_count": len(failed_commands),
            "skipped_command_count": len(skipped_commands),
        },
        "build": _command_to_dict(build_command),
        "examples": [_result_to_dict(result) for result in results],
    }
    _write_outputs(manifest, output_dir)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the current RPA Core wheel and validate every external example against it."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--examples-repo", type=Path, default=Path.cwd().parent / "rpacore-examples")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("validation-artifacts/examples-wheel-validation"),
        help="Directory for the JSON and Markdown validation results.",
    )
    parser.add_argument("--work-dir", type=Path, default=Path(tempfile.gettempdir()) / "rpacore-examples-wheel-validation")
    parser.add_argument(
        "--venv-mode",
        choices=("in-place", "work-dir"),
        default="in-place",
        help="Use each example's .venv or isolated venvs under --work-dir.",
    )
    parser.add_argument(
        "--reuse-venvs",
        action="store_true",
        help="Reuse existing venvs instead of recreating them before installing the wheel.",
    )
    parser.add_argument(
        "--run-main",
        choices=RUN_MAIN_CHOICES,
        default="deterministic",
        help="Choose which examples should execute python main.py after tests.",
    )
    parser.add_argument(
        "--install-playwright-browsers",
        action="store_true",
        help="Install Chromium for examples that declare Playwright.",
    )
    parser.add_argument(
        "--example",
        action="append",
        default=[],
        dest="examples",
        metavar="NAME",
        help="Validate only the named example. Repeat to include multiple examples.",
    )
    parser.add_argument(
        "--exclude-example",
        action="append",
        default=[],
        dest="excluded_examples",
        metavar="NAME",
        help="Skip the named example. Repeat to exclude multiple examples.",
    )
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Always exit 0 after writing the matrix, even when required commands fail.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="Maximum seconds allowed for each build, install, test, or main command.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manifest = validate_examples_against_wheel(
        repo_root=args.repo_root,
        examples_repo=args.examples_repo,
        output_dir=args.output_dir,
        work_dir=args.work_dir,
        venv_mode=args.venv_mode,
        recreate_venvs=not args.reuse_venvs,
        run_main=args.run_main,
        install_playwright_browsers=args.install_playwright_browsers,
        include_examples=set(args.examples),
        exclude_examples=set(args.excluded_examples),
        timeout_seconds=args.timeout_seconds,
    )
    print(f"Wrote examples wheel validation to {args.output_dir.resolve()}")
    if manifest["result"]["status"] == "fail" and not args.allow_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
