"""Command line entry point for RPA Core."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from rpacore import __version__
from rpacore._queue_admin_cli import _administer_queue
from rpacore._queue_cli import _inspect_queue
from rpacore._queue_read import QUERY_DEFAULT_LIMIT
from rpacore._scaffold import _write_project_files
from rpacore._transaction_cli import _inspect_transactions
from rpacore.doctor import collect_doctor_result
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

    list_parser.add_argument("--status", action="append", default=[], help="Status to include; repeat for a union.")
    list_parser.add_argument("--since", help="Inclusive ISO-8601 creation time with offset or Z.")
    list_parser.add_argument("--until", help="Inclusive ISO-8601 creation time with offset or Z.")
    list_parser.add_argument("--reference", help="Exact transaction reference.")
    list_parser.add_argument("--metadata", action="append", default=[], metavar="KEY=JSON", help="Exact top-level metadata match; repeat for different keys.")
    list_parser.add_argument("--run-id", action="append", default=[], help="Exact string metadata match for run_id; use once.")
    list_parser.add_argument("--cursor", help="Continue with the same filters.")

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
    export_parser.add_argument("--transaction-id", help="Export exactly one persisted transaction.")
    export_parser.add_argument(
        "--format",
        choices=("json", "ndjson"),
        required=True,
        help="Export format.",
    )

    queue_parser = subparsers.add_parser(
        "queue",
        help="Inspect the local queue or apply explicit administrative overrides.",
        description="Inspect queue storage or apply confirmed, reasoned administrative overrides.",
    )
    queue_subparsers = queue_parser.add_subparsers(
        dest="queue_command",
        required=True,
    )
    for name, help_text in (
        ("list", "List queue items in creation order."),
        ("show", "Show one queue item."),
        ("attempts", "List claim attempts in durable sequence order."),
        ("poison", "List malformed-payload dispositions in durable sequence order."),
        ("complete", "Administratively mark an item successful (requires --confirm)."),
        ("fail", "Administratively mark an item terminally failed (requires --confirm)."),
    ):
        command_parser = queue_subparsers.add_parser(name, help=help_text)
        command_parser.add_argument("--db", dest="db_path", help="Queue database path.")
        command_parser.add_argument("--config", dest="config_path", help="Configuration file path.")
        command_parser.add_argument("--json", action="store_true", help="Write JSON to stdout.")
        if name in ("complete", "fail"):
            command_parser.add_argument("item_id", help="Exact queue item id to override.")
            command_parser.add_argument("--reason", required=True, help="Nonblank reason recorded in the durable audit.")
            command_parser.add_argument("--confirm", action="store_true", help="Confirm the administrative mutation and writable storage initialization.")
        elif name == "show":
            command_parser.add_argument("item_id", help="Queue item id to inspect.")
        else:
            command_parser.add_argument(
                "--limit", type=_positive_int, default=QUERY_DEFAULT_LIMIT,
                help=f"Maximum records to list. Defaults to {QUERY_DEFAULT_LIMIT}.",
            )
            command_parser.add_argument("--cursor", help="Continue with the same filters.")
            if name == "list":
                command_parser.add_argument(
                    "--status", action="append", default=[],
                    help="Item status to include; repeat for a union (pending, in_progress, successful, failed).",
                )
            else:
                command_parser.add_argument("item_id", nargs="?", help="Optional exact queue item id selector.")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Inspect local runtime and storage without modifying them.",
    )
    doctor_parser.add_argument(
        "--transaction-db", dest="transaction_db_path", help="Transaction database path."
    )
    doctor_parser.add_argument("--queue-db", dest="queue_db_path", help="Queue database path.")
    doctor_parser.add_argument("--config", dest="config_path", help="Configuration file path.")
    doctor_parser.add_argument("--json", action="store_true", help="Write JSON to stdout.")

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
    if args.command == "queue":
        if args.queue_command in ("complete", "fail"):
            return _administer_queue(args)
        return _inspect_queue(args)
    if args.command == "doctor":
        return _run_doctor(args)
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
    if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", project_dir.name) is None:
        _print_error(
            "Invalid project basename: use ASCII letters or digits at both ends, "
            "with only letters, digits, '.', '_', or '-' inside "
            "(for example, 'my-project' instead of 'my project')"
        )
        return USAGE_ERROR

    try:
        project_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            project_dir.mkdir()
        except FileExistsError:
            _print_error(f"Project path already exists: {project_dir}")
            return USAGE_ERROR
    except Exception as exc:
        _print_error(f"Could not create project {project_dir}: {exc}")
        return EXECUTION_ERROR

    try:
        (project_dir / "steps").mkdir()
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


def _run_doctor(args: argparse.Namespace) -> int:
    """Run bounded readonly diagnostics without importing project entrypoints."""
    try:
        result = collect_doctor_result(
            transaction_db_path=args.transaction_db_path,
            queue_db_path=args.queue_db_path,
            config_path=args.config_path,
        )
    except Exception:
        _print_error("Could not complete doctor diagnostics")
        return EXECUTION_ERROR
    if args.json:
        _write_json(result.to_dict())
    else:
        print("RPA Core Doctor (format v1)")
        for check in result.checks:
            print(f"{check.status.upper():<14} {check.id} - {check.summary}")
    return result.exit_code


def _write_json(data: dict[str, object]) -> None:
    print(json.dumps(data, sort_keys=True, separators=(",", ":")))


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1: {value}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1: {value}")
    return parsed


def _print_error(message: str) -> None:
    print(f"rpacore: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
