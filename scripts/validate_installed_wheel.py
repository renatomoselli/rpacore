"""Validate RPA Core from an installed wheel outside the source checkout."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parents[1]
# These scripts must run directly from scripts/ before rpacore is installed.
if str(REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_FOR_IMPORTS))

from rpacore._validation import (
    PRERELEASE_WHEEL_PATTERN,
    ValidationError,
    example_pytest_target as _example_pytest_target,
    validate_contained_path as _validate_contained_path,
)


def _run(
    command: list[str],
    *,
    cwd: Path,
    allowed_roots: tuple[Path, ...],
) -> None:
    _validate_contained_path(cwd, allowed_roots=allowed_roots, label="command cwd")
    print(f"+ {' '.join(command)}")
    try:
        subprocess.run(command, cwd=cwd, check=True)
    except subprocess.CalledProcessError as exc:
        raise ValidationError(
            f"command failed with exit code {exc.returncode}: {' '.join(command)}"
        ) from exc


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_script(venv_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


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


def _smoke_code(repo_root: Path) -> str:
    return f"""
from pathlib import Path
import platform
import sqlite3

import rpacore
from rpacore import Engine, ProcessContext, Skill, Status, Transaction

repo_root = Path({str(repo_root.resolve())!r})
checkout_package = repo_root / "rpacore"
module_path = Path(rpacore.__file__).resolve()
if module_path.parent == checkout_package:
    raise SystemExit(f"imported rpacore from checkout: {{module_path}}")

class OkSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state["ran"] = True

tx = Transaction(reference="installed-wheel-smoke", skills=[OkSkill("ok", 1)])
ctx = ProcessContext(transaction=tx)
Engine().run(ctx)

assert tx.status is Status.SUCCESSFUL
assert tx.skills[0].status is Status.SUCCESSFUL
assert ctx.state == {{"ran": True}}
print(rpacore.__version__)
print(f"Python {{platform.python_version()}}; SQLite {{sqlite3.sqlite_version}}")
"""


def validate_installed_wheel(
    *,
    repo_root: Path,
    work_dir: Path,
    wheel_dir: Path | None,
    examples_repo: Path | None,
    examples_pytest: list[str],
) -> None:
    generated_wheelhouse = work_dir / "wheelhouse"
    wheelhouse = wheel_dir if wheel_dir is not None else generated_wheelhouse
    venv_dir = work_dir / "venv"
    outside_dir = work_dir / "outside"
    generated_dirs = (venv_dir, outside_dir) if wheel_dir is not None else (generated_wheelhouse, venv_dir, outside_dir)

    if examples_repo is not None and not examples_pytest:
        raise ValidationError("--examples-pytest is required when --examples-repo is provided")
    if examples_pytest and examples_repo is None:
        raise ValidationError("--examples-repo is required when --examples-pytest is used")
    allowed_run_roots = (repo_root, work_dir)

    try:
        wheelhouse.mkdir(parents=True, exist_ok=True)
        outside_dir.mkdir(parents=True, exist_ok=True)

        if wheel_dir is None:
            _run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(wheelhouse)],
                cwd=repo_root,
                allowed_roots=allowed_run_roots,
            )
        wheel = _latest_wheel(wheelhouse)

        _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=outside_dir, allowed_roots=allowed_run_roots)
        python = _venv_python(venv_dir)
        _run([str(python), "-m", "pip", "install", str(wheel)], cwd=outside_dir, allowed_roots=allowed_run_roots)
        _run([str(python), "-c", _smoke_code(repo_root)], cwd=outside_dir, allowed_roots=allowed_run_roots)
        rpacore_cli = _venv_script(venv_dir, "rpacore")
        _run([str(rpacore_cli), "version"], cwd=outside_dir, allowed_roots=allowed_run_roots)
        generated_project = outside_dir / "installed_project"
        if generated_project.exists():
            _remove_tree(generated_project)
        _run([str(rpacore_cli), "init", "installed_project"], cwd=outside_dir, allowed_roots=allowed_run_roots)
        _run([str(rpacore_cli), "run"], cwd=generated_project, allowed_roots=allowed_run_roots)
        _run([str(rpacore_cli), "transaction", "list"], cwd=generated_project, allowed_roots=allowed_run_roots)
        _run([str(rpacore_cli), "transaction", "list", "--json"], cwd=generated_project, allowed_roots=allowed_run_roots)

        if examples_pytest:
            _run([str(python), "-m", "pip", "install", "pytest"], cwd=outside_dir, allowed_roots=allowed_run_roots)
            for test_path in examples_pytest:
                target = _example_pytest_target(test_path, examples_root=examples_repo)
                _run(
                    [str(python), "-m", "pytest", target.pytest_path, "-q"],
                    cwd=target.project_dir,
                    allowed_roots=(examples_repo,),
                )
    except BaseException:
        # Keep generated-directory cleanup deterministic, then re-raise interrupts unchanged.
        cleanup_failures: list[Path] = []
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
                "Installed-wheel cleanup left generated directories behind: %s",
                ", ".join(str(path) for path in cleanup_failures),
            )
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build RPA Core, install the wheel in a clean venv, and run smoke checks."
    )
    default_repo_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--repo-root", type=Path, default=default_repo_root)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument(
        "--wheel-dir",
        type=Path,
        default=None,
        help="Directory containing a prebuilt rpacore wheel. Defaults to building one into --work-dir.",
    )
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--examples-repo", type=Path, default=None)
    parser.add_argument(
        "--examples-pytest",
        action="append",
        default=[],
        help="Optional pytest path to run from --examples-repo using the installed-wheel venv.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    owns_work_dir = args.work_dir is None
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="rpacore-installed-wheel-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        validate_installed_wheel(
            repo_root=args.repo_root.resolve(),
            work_dir=work_dir.resolve(),
            wheel_dir=args.wheel_dir.resolve() if args.wheel_dir else None,
            examples_repo=args.examples_repo.resolve() if args.examples_repo else None,
            examples_pytest=list(args.examples_pytest),
        )
    finally:
        if owns_work_dir and not args.keep_work_dir:
            try:
                _remove_tree(work_dir)
            except OSError:
                logging.warning("Failed to clean owned work directory %s", work_dir, exc_info=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
