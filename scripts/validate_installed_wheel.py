"""Validate RPA Core from an installed wheel outside the source checkout."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(command: list[str], *, cwd: Path) -> None:
    print(f"+ {' '.join(command)}")
    subprocess.run(command, cwd=cwd, check=True)


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
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    if not wheels:
        raise FileNotFoundError(f"No rpacore wheel found in {wheelhouse}")
    return wheels[0]


def _smoke_code(repo_root: Path) -> str:
    return f"""
from pathlib import Path

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
"""


def validate_installed_wheel(
    *,
    repo_root: Path,
    work_dir: Path,
    examples_repo: Path | None,
    examples_pytest: list[str],
) -> None:
    wheelhouse = work_dir / "wheelhouse"
    venv_dir = work_dir / "venv"
    outside_dir = work_dir / "outside"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    outside_dir.mkdir(parents=True, exist_ok=True)

    if examples_repo is not None and not examples_pytest:
        raise ValueError("--examples-pytest is required when --examples-repo is provided")
    if examples_pytest and examples_repo is None:
        raise ValueError("--examples-repo is required when --examples-pytest is used")

    _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(wheelhouse)], cwd=repo_root)
    wheel = _latest_wheel(wheelhouse)

    _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=outside_dir)
    python = _venv_python(venv_dir)
    _run([str(python), "-m", "pip", "install", str(wheel)], cwd=outside_dir)
    _run([str(python), "-c", _smoke_code(repo_root)], cwd=outside_dir)
    rpacore_cli = _venv_script(venv_dir, "rpacore")
    _run([str(rpacore_cli), "version"], cwd=outside_dir)
    generated_project = outside_dir / "installed_project"
    if generated_project.exists():
        shutil.rmtree(generated_project, ignore_errors=True)
    _run([str(rpacore_cli), "init", "installed_project"], cwd=outside_dir)
    _run([str(rpacore_cli), "run"], cwd=generated_project)

    if examples_pytest:
        _run([str(python), "-m", "pip", "install", "pytest"], cwd=outside_dir)
        for test_path in examples_pytest:
            _run([str(python), "-m", "pytest", test_path], cwd=examples_repo)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build RPA Core, install the wheel in a clean venv, and run smoke checks."
    )
    default_repo_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--repo-root", type=Path, default=default_repo_root)
    parser.add_argument("--work-dir", type=Path, default=None)
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
            examples_repo=args.examples_repo.resolve() if args.examples_repo else None,
            examples_pytest=list(args.examples_pytest),
        )
    finally:
        if owns_work_dir and not args.keep_work_dir:
            shutil.rmtree(work_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
