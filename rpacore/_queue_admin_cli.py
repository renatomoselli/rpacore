"""Explicit, reasoned queue overrides through the existing administrative API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rpacore._doctor_errors import input_error_details
from rpacore._queue_cli import (
    CLI_QUEUE_FORMAT_VERSION,
    EXECUTION_ERROR,
    SUCCESS,
    USAGE_ERROR,
    _print_error,
    _queue_db_path,
)
from rpacore.queue import SqliteQueue


def _administer_queue(args: argparse.Namespace) -> int:
    """Apply one confirmed override without decoding items or querying after it."""
    if not args.item_id.strip() or not args.reason.strip():
        _print_error("Queue item id and --reason must not be blank")
        return USAGE_ERROR
    if not args.confirm:
        _print_error("Administrative override requires --confirm; no storage was accessed")
        return USAGE_ERROR

    # Prepare the complete acknowledgement before any writable construction.
    try:
        output = json.dumps({
            "schema_version": CLI_QUEUE_FORMAT_VERSION,
            "command": f"queue:{args.queue_command}",
            "item_id": args.item_id,
            "applied": True,
        }, sort_keys=True, separators=(",", ":")) if args.json else (
            f"Administrative {args.queue_command} applied for queue item {args.item_id!r}"
        )
    except Exception:
        _print_error("Could not prepare queue override acknowledgement; no storage was accessed")
        return EXECUTION_ERROR

    try:
        db_path = _queue_db_path(args)
        if not Path(db_path).is_file():
            raise FileNotFoundError("Queue storage must be an existing file")
    except Exception as exc:
        cause = input_error_details(exc)["cause"]
        _print_error(
            f"Could not select queue storage ({cause}); check --db, --config, "
            "or the nearest rpacore.toml and its sibling config.toml; "
            "an existing database file is required"
        )
        return EXECUTION_ERROR

    try:
        queue = SqliteQueue({"db_path": db_path})
        if args.queue_command == "complete":
            queue.force_complete(args.item_id, reason=args.reason)
        else:  # The parser restricts administration to complete/fail.
            queue.force_fail(args.item_id, reason=args.reason, retry=False)
    except KeyError:
        _print_error("Queue item not found; storage initialization may already have changed the database")
        return EXECUTION_ERROR
    except Exception as exc:
        _print_error(
            f"Could not apply queue override ({type(exc).__name__}); check storage "
            "access/locks, compatible schemas and journal mode; storage initialization "
            "may already have changed the database"
        )
        return EXECUTION_ERROR

    try:
        print(output, flush=True)
    except Exception:
        _print_error(
            "Queue override applied but acknowledgement could not be written; "
            "inspect state and list_admin_events() through Python before retrying"
        )
        return EXECUTION_ERROR
    return SUCCESS
