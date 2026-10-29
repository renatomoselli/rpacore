"""Private read-only queue CLI inspection for the ``rpacore queue`` commands.

Commands here never create, migrate, reclaim, or repair storage. Storage
selection rejects an implicit working-directory fallback: an explicit ``--db``
bypasses manifest and config loading entirely, an explicit ``--config`` needs no
manifest, and otherwise the nearest ``rpacore.toml`` is discovered upward from
the working directory and its sibling ``config.toml`` supplies the queue path.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from rpacore._doctor_errors import input_error_details
from rpacore._queue_read import (
    QUERY_MAX_LIMIT,
    QueuePage,
    get_queue_item,
    query_queue_attempts,
    query_queue_items,
    query_queue_poison_events,
)
from rpacore.config import load_config
from rpacore.manifest import MANIFEST_NAME, load_project_manifest
from rpacore.queue import QueueAttempt, QueueItem, QueuePoisonEvent, QueueStatus


SUCCESS = 0
EXECUTION_ERROR = 1
USAGE_ERROR = 2
CLI_QUEUE_FORMAT_VERSION = 2
CONFIG_NAME = "config.toml"
STATUS_VOCABULARY = tuple(status.value for status in QueueStatus)


def _inspect_queue(args: argparse.Namespace) -> int:
    """Inspect persisted queue storage without writing it."""
    # Usage validation precedes manifest, config, and storage resolution.
    statuses = None
    if args.queue_command == "list":
        try:
            statuses = [QueueStatus(value) for value in args.status] if args.status else None
        except ValueError:
            _print_error("Invalid --status: use " + ", ".join(STATUS_VOCABULARY))
            return USAGE_ERROR
    if args.queue_command != "show" and not 1 <= args.limit <= QUERY_MAX_LIMIT:
        _print_error(
            f"Invalid --limit: use an integer from 1 through {QUERY_MAX_LIMIT}"
        )
        return USAGE_ERROR
    item_id = None if args.queue_command == "list" else args.item_id
    if item_id is not None and not item_id.strip():
        _print_error("item id must not be blank")
        return USAGE_ERROR

    try:
        db_path = _queue_db_path(args)
    except Exception as exc:
        cause = input_error_details(exc)["cause"]
        _print_error(
            f"Could not select queue storage ({cause}); check --db, --config, "
            f"or the nearest {MANIFEST_NAME} and its sibling {CONFIG_NAME}"
        )
        return EXECUTION_ERROR

    try:
        command = f"queue:{args.queue_command}"
        if args.queue_command == "show":
            item = get_queue_item(args.item_id, db_path)
            if item is None:
                _print_error("Queue item not found")
                return EXECUTION_ERROR
            _write_output(
                as_json=args.json,
                envelope={
                    "command": command,
                    "item": _item_projection(item),
                    "schema_version": CLI_QUEUE_FORMAT_VERSION,
                },
                lines=_item_lines(item),
            )
            return SUCCESS
        page: QueuePage[QueueItem] | QueuePage[QueueAttempt] | QueuePage[QueuePoisonEvent]
        if args.queue_command == "list":
            page = query_queue_items(
                db_path, statuses=statuses, cursor=args.cursor, limit=args.limit,
            )
            records = [_item_projection(item) for item in page.records]
            lines = _item_page_lines(page.records, has_more=page.has_more,
                                     next_cursor=page.next_cursor, limit=args.limit)
        elif args.queue_command == "attempts":
            page = query_queue_attempts(
                db_path, item_id=item_id, cursor=args.cursor, limit=args.limit,
            )
            records = [_attempt_projection(attempt) for attempt in page.records]
            lines = _attempt_page_lines(page.records, has_more=page.has_more,
                                        next_cursor=page.next_cursor)
        else:  # The parser restricts the remaining command to poison.
            page = query_queue_poison_events(
                db_path, item_id=item_id, cursor=args.cursor, limit=args.limit,
            )
            records = [_poison_projection(event) for event in page.records]
            lines = _poison_page_lines(page.records, has_more=page.has_more,
                                       next_cursor=page.next_cursor)
        _write_output(
            as_json=args.json,
            envelope={
                "command": command,
                "has_more": page.has_more,
                "limit": args.limit,
                "next_cursor": page.next_cursor,
                "records": records,
                "schema_version": CLI_QUEUE_FORMAT_VERSION,
            },
            lines=lines,
        )
        return SUCCESS
    except Exception as exc:
        _print_error(
            f"Could not inspect queue storage ({type(exc).__name__}); check file "
            "readability/locks, current schemas, rollback journal, record data, "
            "and cursor/filter compatibility"
        )
        return EXECUTION_ERROR


def _queue_db_path(args: argparse.Namespace) -> str:
    if args.db_path is not None:
        # An explicit database bypasses manifest and config loading entirely.
        return args.db_path
    if args.config_path is not None:
        return _queue_path_from_config(Path(args.config_path))
    manifest = load_project_manifest()
    return _queue_path_from_config(manifest.project_dir / CONFIG_NAME)


def _queue_path_from_config(config_path: Path) -> str:
    """Return the queue path from one selected config, resolved against its directory."""
    config = load_config(config_path, require_file=True)
    queue = config.get("queue")
    db_path = queue.get("db_path") if isinstance(queue, dict) else None
    if not isinstance(db_path, str) or not db_path.strip():
        raise ValueError("Missing queue db_path; pass --db")
    return db_path


def _item_projection(item: QueueItem) -> dict[str, object]:
    return {
        "id": item.id,
        "reference": item.reference,
        "status": item.status.value,
        "retry_count": item.retry_count,
        "created_at": item.created_at.isoformat(),
        "claimed_at": None if item.claimed_at is None else item.claimed_at.isoformat(),
        "claimed_by": item.claimed_by,
        "transaction_id": item.transaction_id,
        "available_at": None if item.available_at is None else item.available_at.isoformat(timespec="microseconds"),
    }


def _attempt_projection(attempt: QueueAttempt) -> dict[str, object]:
    return {
        "sequence": attempt.sequence,
        "item_id": attempt.item_id,
        "claimed_by": attempt.claimed_by,
        "started_at": attempt.started_at.isoformat(),
        "finished_at": None if attempt.finished_at is None else attempt.finished_at.isoformat(),
        "outcome": None if attempt.outcome is None else attempt.outcome.value,
    }


def _poison_projection(event: QueuePoisonEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "item_id": event.item_id,
        "error_type": event.error_type,
        "created_at": event.created_at.isoformat(),
    }


def _item_lines(item: QueueItem) -> list[str]:
    return [
        f"ID:            {item.id}",
        f"Reference:     {item.reference}",
        f"Status:        {item.status.value}",
        f"Retries:       {item.retry_count}",
        f"Created:       {item.created_at.isoformat()}",
        f"Claimed by:    {item.claimed_by or '(unclaimed)'}",
        f"Claimed at:    {_optional_timestamp(item.claimed_at)}",
        f"Transaction:   {item.transaction_id or '(unbound)'}",
        f"Available at:  {_optional_timestamp(item.available_at)}",
    ]


def _item_page_lines(
    items: tuple[QueueItem, ...], *, has_more: bool, next_cursor: str | None, limit: int,
) -> list[str]:
    lines = [f"Queue items, showing up to {limit}."]
    if not items:
        lines.append("No queue items found.")
    for item in items:
        lines.append(
            f"{item.id}  {item.status.value}  retries={item.retry_count}  "
            f"created={item.created_at.isoformat()}  reference={item.reference}"
            f"  available_at={_optional_timestamp(item.available_at)}"
        )
    lines.extend(_continuation_lines(has_more=has_more, next_cursor=next_cursor))
    return lines


def _attempt_page_lines(
    attempts: tuple[QueueAttempt, ...], *, has_more: bool, next_cursor: str | None,
) -> list[str]:
    lines = ["Claim attempts in durable sequence order."]
    if not attempts:
        lines.append("No claim attempts found.")
    for attempt in attempts:
        lines.append(
            f"{attempt.sequence}  {attempt.item_id}  claimed_by={attempt.claimed_by or '(unassigned)'}  "
            f"started={attempt.started_at.isoformat()}  "
            f"finished={_optional_timestamp(attempt.finished_at)}  "
            f"outcome={'(open)' if attempt.outcome is None else attempt.outcome.value}"
        )
    lines.extend(_continuation_lines(has_more=has_more, next_cursor=next_cursor))
    return lines


def _poison_page_lines(
    events: tuple[QueuePoisonEvent, ...], *, has_more: bool, next_cursor: str | None,
) -> list[str]:
    lines = ["Poison events in durable sequence order."]
    if not events:
        lines.append("No poison events found.")
    for event in events:
        lines.append(
            f"{event.sequence}  {event.item_id}  error_type={event.error_type}  "
            f"created={event.created_at.isoformat()}"
        )
    lines.extend(_continuation_lines(has_more=has_more, next_cursor=next_cursor))
    return lines


def _continuation_lines(*, has_more: bool, next_cursor: str | None) -> list[str]:
    if not has_more:
        return []
    return [f"More records are available; continue with the same filters and --cursor {next_cursor}"]


def _optional_timestamp(value: datetime | None) -> str:
    return "(none)" if value is None else value.isoformat()


def _write_output(*, as_json: bool, envelope: dict[str, object], lines: list[str]) -> None:
    """Write one command's complete output only after every record is ready."""
    if as_json:
        print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
    else:
        print("\n".join(lines))


def _print_error(message: str) -> None:
    print(f"rpacore: {message}", file=sys.stderr)
