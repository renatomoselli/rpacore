"""Command line entry point for RPA Core."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import textwrap
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from rpacore import __version__
from rpacore.exceptions import BusinessException
from rpacore.manifest import load_project_manifest, resolve_project_entrypoint
from rpacore.persistence import iter_transactions, list_transactions, load_transaction
from rpacore.serialization import serialize_transaction
from rpacore.transaction import Transaction


USAGE_ERROR = 2
EXECUTION_ERROR = 1
SUCCESS = 0
EXPORT_FORMAT_VERSION = 1
_NDJSON_EXPORT_KEYS = frozenset(
    {"export_format_version", "framework_version", "exported_at"}
)
_ENTRYPOINT_PATH_LOCK = threading.RLock()


def build_parser() -> argparse.ArgumentParser:
    """Build the RPA Core command line parser."""
    parser = argparse.ArgumentParser(prog="rpacore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a new RPA Core project.")
    init_parser.add_argument("project_name", help="Directory name for the generated project.")

    subparsers.add_parser("run", help="Run the current project's rpacore.toml entrypoint.")

    transaction_parser = subparsers.add_parser(
        "transaction",
        help="Inspect persisted transactions.",
    )
    transaction_subparsers = transaction_parser.add_subparsers(
        dest="transaction_command",
        required=True,
    )
    list_parser = transaction_subparsers.add_parser(
        "list",
        help="List persisted transactions.",
    )
    list_parser.add_argument("--db", dest="db_path", help="Transaction database path.")
    list_parser.add_argument("--json", action="store_true", help="Write JSON to stdout.")
    list_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=100,
        help="Maximum transactions to list. Defaults to 100.",
    )

    show_parser = transaction_subparsers.add_parser(
        "show",
        help="Show one persisted transaction.",
    )
    show_parser.add_argument("transaction_id", help="Transaction id to inspect.")
    show_parser.add_argument("--db", dest="db_path", help="Transaction database path.")
    show_parser.add_argument("--json", action="store_true", help="Write JSON to stdout.")

    export_parser = transaction_subparsers.add_parser(
        "export",
        help="Export persisted transactions as JSON or NDJSON.",
    )
    export_parser.add_argument("--db", dest="db_path", help="Transaction database path.")
    export_parser.add_argument(
        "--format",
        choices=("json", "ndjson"),
        required=True,
        help="Export format.",
    )

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
    if args.command == "transaction":
        return _inspect_transactions(args)
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


def _inspect_transactions(args: argparse.Namespace) -> int:
    db_path = ""
    try:
        db_path = _transaction_db_path(args.db_path)
        if args.transaction_command == "list":
            transactions = list_transactions(db_path, limit=args.limit)
            if args.json:
                _write_json(
                    {
                        "schema_version": 1,
                        "command": "transaction:list",
                        "limit": args.limit,
                        "transactions": [
                            serialize_transaction(tx)
                            for tx in transactions
                        ],
                    }
                )
            else:
                _write_transaction_list(transactions, limit=args.limit)
            return SUCCESS
        if args.transaction_command == "show":
            transaction = load_transaction(args.transaction_id, db_path)
            if args.json:
                _write_json(
                    {
                        "schema_version": 1,
                        "command": "transaction:show",
                        "transaction": serialize_transaction(transaction),
                    }
                )
            else:
                _write_transaction_detail(transaction)
            return SUCCESS
        if args.transaction_command == "export":
            transactions = iter_transactions(db_path)
            _write_transaction_export(transactions, export_format=args.format)
            return SUCCESS
    except KeyError as exc:
        _print_error(str(exc.args[0] if exc.args else exc))
        return EXECUTION_ERROR
    except TypeError as exc:
        _print_error(str(exc))
        return EXECUTION_ERROR
    except Exception as exc:
        _print_error(f"Could not inspect transactions in {db_path}: {exc}")
        return EXECUTION_ERROR


def _transaction_db_path(db_path: str | None) -> str:
    if db_path is not None:
        return db_path
    try:
        return load_project_manifest().transaction_db_path
    except Exception as exc:
        raise RuntimeError(
            f"could not resolve transaction database from rpacore.toml; pass --db: {exc}"
        ) from exc


def _write_transaction_list(transactions: list[Transaction], *, limit: int) -> None:
    print(f"Showing up to {limit} transactions.")
    if not transactions:
        print("No transactions found.")
        return
    print("ID                                   STATUS       REFERENCE")
    for transaction in transactions:
        print(
            f"{transaction.id:<36} {str(transaction.status):<12} {transaction.reference}"
        )


def _write_transaction_detail(transaction: Transaction) -> None:
    print(f"ID:          {transaction.id}")
    print(f"Reference:   {transaction.reference}")
    print(f"Status:      {transaction.status}")
    print(f"Retries:     {transaction.retry_count}")
    print(f"Created:     {_format_optional_datetime(transaction.created_at)}")
    print(f"Started:     {_format_optional_datetime(transaction.started_at)}")
    print(f"Finished:    {_format_optional_datetime(transaction.finished_at)}")
    print(f"Skills:      {len(transaction.skills)}")
    for skill in transaction.ordered_skills():
        print(f"  {skill.execution_order}. {skill.name}: {skill.status}")
        for exc in skill.exceptions:
            print(f"     - {_exception_kind(exc)}: {exc}")
    print(f"History:     {len(transaction.history)}")
    for entry in transaction.history:
        skill = "" if not entry.skill_name else f" skill={entry.skill_name}"
        print(
            f"  {entry.sequence}. {entry.event} status={entry.status} "
            f"retry={entry.retry_number}{skill}"
        )
    print(f"Artifacts:   {len(transaction.artifacts)}")
    for artifact in transaction.artifacts:
        kind = "" if not artifact.kind else f" ({artifact.kind})"
        print(f"  {artifact.name}{kind}: {artifact.path}")
    print(f"Metadata:    {len(transaction.metadata)} keys")


def _write_json(data: dict[str, object]) -> None:
    print(json.dumps(data, sort_keys=True, separators=(",", ":")))


def _write_transaction_export(
    transactions: Iterable[Transaction],
    *,
    export_format: str,
) -> None:
    exported_at = _utc_now().isoformat()
    with tempfile.TemporaryFile("w+", encoding="utf-8", newline="\n") as output:
        if export_format == "json":
            output.write("{")
            output.write(
                _json_field("export_format_version", EXPORT_FORMAT_VERSION)
            )
            output.write(",")
            output.write(_json_field("exported_at", exported_at))
            output.write(",")
            output.write(_json_field("framework_version", __version__))
            output.write(',"transactions":[')
            first = True
            for transaction in transactions:
                if not first:
                    output.write(",")
                first = False
                output.write(
                    json.dumps(
                        serialize_transaction(transaction),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            output.write("]}")
        else:
            for transaction in transactions:
                record = serialize_transaction(transaction)
                _raise_for_ndjson_export_key_collision(record)
                export_record = {
                    **record,
                    "export_format_version": EXPORT_FORMAT_VERSION,
                    "framework_version": __version__,
                    "exported_at": exported_at,
                }
                output.write(
                    json.dumps(export_record, sort_keys=True, separators=(",", ":"))
                )
                output.write("\n")
        output.seek(0)
        shutil.copyfileobj(output, sys.stdout)


def _raise_for_ndjson_export_key_collision(record: dict[str, object]) -> None:
    collisions = sorted(_NDJSON_EXPORT_KEYS.intersection(record))
    if collisions:
        keys = ", ".join(collisions)
        raise RuntimeError(f"transaction export record key collision: {keys}")


def _json_field(key: str, value: object) -> str:
    return (
        json.dumps(key, sort_keys=True, separators=(",", ":"))
        + ":"
        + json.dumps(value, sort_keys=True, separators=(",", ":"))
    )


def _exception_kind(exc: BaseException) -> str:
    return "business" if isinstance(exc, BusinessException) else "system"


def _format_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1: {value}")
    return parsed


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
        log_format = "text"
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
