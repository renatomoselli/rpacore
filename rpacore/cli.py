"""Command line entry point for RPA Core."""

from __future__ import annotations

import argparse
import shutil
import sys
import textwrap
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from rpacore import __version__
from rpacore.manifest import load_project_manifest, resolve_project_entrypoint


USAGE_ERROR = 2
EXECUTION_ERROR = 1
SUCCESS = 0
_ENTRYPOINT_PATH_LOCK = threading.RLock()


def build_parser() -> argparse.ArgumentParser:
    """Build the RPA Core command line parser."""
    parser = argparse.ArgumentParser(prog="rpacore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a new RPA Core project.")
    init_parser.add_argument("project_name", help="Directory name for the generated project.")

    subparsers.add_parser("run", help="Run the current project's rpacore.toml entrypoint.")
    subparsers.add_parser("version", help="Print the installed RPA Core version.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RPA Core command line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "version":
        print(__version__)
        return SUCCESS
    if args.command == "init":
        return _init_project(Path(args.project_name))
    if args.command == "run":
        return _run_project()
    parser.error(f"unknown command: {args.command}")


def _run_project() -> int:
    try:
        manifest = load_project_manifest()
        entrypoint = resolve_project_entrypoint(manifest)
    except Exception as exc:
        _print_error(f"Project manifest or entrypoint is invalid: {exc}")
        return USAGE_ERROR

    try:
        result = _call_entrypoint(entrypoint, project_dir=manifest.project_dir)
    except Exception as exc:
        _print_error(f"Project execution failed: {exc}")
        return EXECUTION_ERROR

    if result is None:
        return SUCCESS
    if isinstance(result, bool) or not isinstance(result, int):
        _print_error(
            "Project entrypoint must return None or an integer exit code from 0 through 255"
        )
        return USAGE_ERROR
    if result < 0 or result > 255:
        _print_error(f"Project entrypoint returned out-of-range exit code: {result}")
        return USAGE_ERROR
    return result


def _call_entrypoint(entrypoint: Callable[[], object], *, project_dir: Path) -> object:
    project_path = str(project_dir)
    with _ENTRYPOINT_PATH_LOCK:
        inserted = False
        if project_path not in sys.path:
            sys.path.insert(0, project_path)
            inserted = True
        try:
            return entrypoint()
        finally:
            if inserted:
                try:
                    sys.path.remove(project_path)
                except ValueError:
                    pass


def _init_project(project_dir: Path) -> int:
    if project_dir.exists():
        _print_error(f"Project path already exists: {project_dir}")
        return USAGE_ERROR

    try:
        project_dir.mkdir(parents=True)
        (project_dir / "skills").mkdir()
        (project_dir / "tests").mkdir()
        _write_project_files(project_dir)
    except Exception as exc:
        if project_dir.exists():
            try:
                shutil.rmtree(project_dir)
            except Exception as cleanup_exc:
                _print_error(
                    f"Could not remove partial project {project_dir}: {cleanup_exc}"
                )
        _print_error(f"Could not create project {project_dir}: {exc}")
        return EXECUTION_ERROR

    print(f"Created RPA Core project: {project_dir}")
    return SUCCESS


def _write_project_files(project_dir: Path) -> None:
    project_name = project_dir.name
    files = {
        "pyproject.toml": _pyproject_toml(project_name),
        "rpacore.toml": _rpacore_toml(),
        "config.toml": _config_toml(),
        "main.py": _main_py(),
        "skills/__init__.py": "",
        "skills/greeting.py": _greeting_skill_py(),
        "tests/test_greeting_skill.py": _skill_test_py(),
        ".gitignore": _gitignore(),
    }
    for relative_path, content in files.items():
        (project_dir / relative_path).write_text(content, encoding="utf-8")


def _pyproject_toml(project_name: str) -> str:
    return textwrap.dedent(f"""\
        [project]
        name = "{project_name}"
        version = "0.1.0"
        requires-python = ">=3.11"
        dependencies = ["rpacore"]

        [project.optional-dependencies]
        dev = ["pytest>=7.0"]
        """)


def _rpacore_toml() -> str:
    return textwrap.dedent("""\
        [project]
        entrypoint = "main:main"

        [storage]
        transaction_db_path = "rpacore.db"
        """)


def _config_toml() -> str:
    return textwrap.dedent("""\
        max_retries = 0
        retry_delay = 0.0
        retry_backoff = 1.0
        log_level = "INFO"
        transaction_db_path = "rpacore.db"
        screenshot_dir = ""
        credential_provider = "env"
        """)


def _main_py() -> str:
    return textwrap.dedent("""\
        from __future__ import annotations

        from rpacore import Engine, ProcessContext, Status, Transaction, load_config, save_transaction
        from skills.greeting import WriteGreeting


        def main() -> int:
            config = load_config("config.toml")
            transaction = Transaction(
                reference="generated-greeting",
                skills=[
                    WriteGreeting(
                        name="write_greeting",
                        execution_order=1,
                        arguments={"name": "Alice", "output_path": "greeting.txt"},
                    ),
                ],
            )
            ctx = ProcessContext(transaction=transaction, config=config)
            engine = Engine(
                max_retries=int(config["max_retries"]),
                retry_delay=float(config["retry_delay"]),
                retry_backoff=float(config["retry_backoff"]),
                screenshot_dir=str(config["screenshot_dir"]),
            )
            engine.run(ctx)
            save_transaction(transaction, db_path=str(config["transaction_db_path"]))
            return 0 if transaction.status is Status.SUCCESSFUL else 1


        if __name__ == "__main__":
            raise SystemExit(main())
        """)


def _greeting_skill_py() -> str:
    return textwrap.dedent("""\
        from __future__ import annotations

        from pathlib import Path

        from rpacore import BusinessException, ProcessContext, Skill


        class WriteGreeting(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                name = self.arguments.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise BusinessException("'name' must be a non-empty string", action=self.name)

                output_path = Path(str(self.arguments.get("output_path", "greeting.txt")))
                output_path.write_text(f"Hello, {name}\\n", encoding="utf-8")
                ctx.state["greeting_path"] = str(output_path)
        """)


def _skill_test_py() -> str:
    return textwrap.dedent("""\
        from __future__ import annotations

        from pathlib import Path

        from rpacore import ProcessContext, Transaction
        from skills.greeting import WriteGreeting


        def test_write_greeting_skill(tmp_path: Path) -> None:
            output = tmp_path / "greeting.txt"
            skill = WriteGreeting(
                name="write_greeting",
                execution_order=1,
                arguments={"name": "Alice", "output_path": str(output)},
            )
            transaction = Transaction(reference="test", skills=[skill])

            skill.execute(ProcessContext(transaction=transaction))

            assert output.read_text(encoding="utf-8") == "Hello, Alice\\n"
            assert transaction.state["greeting_path"] == str(output)
        """)


def _gitignore() -> str:
    return textwrap.dedent("""\
        .venv/
        __pycache__/
        .pytest_cache/
        *.pyc
        rpacore.db
        greeting.txt
        """)


def _print_error(message: str) -> None:
    print(f"rpacore: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
