"""Queue record decoding/selection and explicit read-only inspection queries.

Public inspection entry points open existing current-schema storage read-only,
rejecting WAL before connecting. They never migrate, claim, or repair storage.
Stateless decoders and SQL/filter rules are also used by writable SqliteQueue;
that caller retains its own connection and schema policy. Record models stay
in queue.py, so decoders import them at call time after module initialization.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Generic, TypeVar

from rpacore._json_state import validate_json_object
from rpacore._queue_retry import parse_available_at
from rpacore._sqlite import (
    QUEUE_SCHEMA_VERSION,
    connect_sqlite,
    database_header_journal_mode,
    ensure_database_compatibility,
    require_current_schema,
)
from rpacore._validation import type_error, value_error

if TYPE_CHECKING:
    from rpacore.queue import (
        QueueAttempt,
        QueueItem,
        QueuePoisonEvent,
        QueueStatus,
    )


QUEUE_COMPONENT = "queue"
QUEUE_DB_FIELD = "queue.db_path"
# Selection and ordering shared with the writable queue methods.
ITEM_SELECT = "SELECT * FROM queue_items"
ITEM_ORDER = "created_at ASC, id ASC"
ATTEMPT_SELECT = "SELECT * FROM queue_attempts"
ATTEMPT_ORDER = "sequence ASC"
POISON_SELECT = "SELECT * FROM queue_poison_events"
POISON_ORDER = "sequence ASC"
QUEUE_PAGE_FORMAT_VERSION = 2
QUEUE_CURSOR_VERSION = 1
QUERY_DEFAULT_LIMIT = 100
QUERY_MAX_LIMIT = 1_000
_KIND_ITEMS = "queue_items"
_KIND_ATTEMPTS = "queue_attempts"
_KIND_POISON_EVENTS = "queue_poison_events"

RecordT = TypeVar("RecordT")


@dataclass(frozen=True)
class QueuePage(Generic[RecordT]):
    """One versioned, point-in-time page of persisted queue records.

    A page is one bounded read of persisted rows, not a queue snapshot: an item
    that leaves a filter behind the cursor can disappear, and an item that
    enters it behind the cursor is not included until a fresh traversal.
    """

    records: tuple[RecordT, ...]
    has_more: bool
    next_cursor: str | None
    format_version: int = QUEUE_PAGE_FORMAT_VERSION


def load_payload(raw_payload: str) -> dict[str, object]:
    """Decode one persisted queue item payload."""
    payload = json.loads(raw_payload)
    validate_json_object(payload, path="queue item payload")
    return payload


def row_to_item(
    row: sqlite3.Row, *, payload: dict[str, object] | None = None
) -> QueueItem:
    from rpacore.queue import QueueItem, QueueStatus

    claimed_at = None
    if row["claimed_at"]:
        claimed_at = datetime.fromisoformat(row["claimed_at"])
    if payload is None:
        payload = load_payload(row["payload"])
    available_at = parse_available_at(row["available_at"])
    return QueueItem(
        id=row["id"],
        reference=row["reference"],
        payload=payload,
        status=QueueStatus(row["status"]),
        retry_count=row["retry_count"],
        created_at=datetime.fromisoformat(row["created_at"]),
        claimed_by=row["claimed_by"],
        claimed_at=claimed_at,
        claim_token=row["claim_token"],
        transaction_id=row["transaction_id"],
        available_at=available_at,
    )


def row_to_attempt(row: sqlite3.Row) -> QueueAttempt:
    from rpacore.queue import QueueAttempt, QueueAttemptOutcome

    return QueueAttempt(
        sequence=row["sequence"],
        item_id=row["item_id"],
        claim_token=row["claim_token"],
        claimed_by=row["claimed_by"],
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=(
            datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
        ),
        outcome=(QueueAttemptOutcome(row["outcome"]) if row["outcome"] else None),
    )


def row_to_poison_event(row: sqlite3.Row) -> QueuePoisonEvent:
    from rpacore.queue import QueuePoisonEvent

    return QueuePoisonEvent(
        sequence=row["sequence"],
        item_id=row["item_id"],
        error_type=row["error_type"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def status_filter_clause(
    statuses: Iterable[QueueStatus] | None,
) -> tuple[str, list[str]]:
    """Return the shared status selection rule: None means all, empty means none."""
    if statuses is None:
        return "", []
    values = [status.value for status in statuses]
    if not values:
        return " AND 1=0", []
    placeholders = ", ".join("?" for _ in values)
    return f" AND status IN ({placeholders})", values


def open_readonly_queue(db_path: str) -> sqlite3.Connection:
    """Open existing current-schema queue storage for reading or fail visibly.

    Missing, pre-current, future, WAL, unreadable, and malformed storage all
    fail here without creating, migrating, or repairing the database or its
    sidecars.
    """
    header_journal_mode = database_header_journal_mode(
        db_path,
        field=QUEUE_DB_FIELD,
    )
    if header_journal_mode == "not_sqlite":
        raise RuntimeError(f"Queue database is not an SQLite database: {db_path}")
    if header_journal_mode == "wal":
        raise RuntimeError(
            f"Queue database uses the WAL journal, which read-only inspection "
            f"does not open: {db_path}"
        )
    connection = connect_sqlite(db_path, field=QUEUE_DB_FIELD, readonly=True)
    try:
        ensure_database_compatibility(connection)
        require_current_schema(
            connection,
            component=QUEUE_COMPONENT,
            supported_version=QUEUE_SCHEMA_VERSION,
            label="queue",
        )
    except BaseException:
        connection.close()
        raise
    return connection


def get_queue_item(item_id: str, db_path: str) -> QueueItem | None:
    """Return one persisted queue item by its opaque ID, or None when absent.

    Item IDs are opaque nonblank strings; no ID format is imposed. Reading never
    creates, migrates, reclaims leases, or repairs storage.
    """
    _validated_item_id(item_id, field="queue item id")
    connection = open_readonly_queue(db_path)
    try:
        row = connection.execute(f"{ITEM_SELECT} WHERE id = ?", (item_id,)).fetchone()
    finally:
        connection.close()
    return None if row is None else row_to_item(row)


def query_queue_items(
    db_path: str,
    *,
    statuses: Iterable[QueueStatus] | None = None,
    cursor: str | None = None,
    limit: int = QUERY_DEFAULT_LIMIT,
) -> QueuePage[QueueItem]:
    """Return one read-only page of items in persisted creation order.

    Items order by the persisted ``(created_at, id)`` text pair ascending, the
    same rule the writable list read uses. The page continues after the last
    returned row through a keyset cursor bound to the normalized status filter;
    it never re-reads an earlier row and never claims queue completeness.
    """
    _validated_limit(limit)
    status_filter, status_params = status_filter_clause(statuses)
    filters = _filter_fingerprint(
        {"kind": _KIND_ITEMS, "statuses": None if statuses is None else sorted(set(status_params))}
    )
    position = (
        None if cursor is None else _decoded_cursor(cursor, kind=_KIND_ITEMS, filters=filters)
    )

    query = f"{ITEM_SELECT} WHERE 1=1{status_filter}"
    params: list[object] = list(status_params)
    if position is not None:
        query += " AND (created_at > ? OR (created_at = ? AND id > ?))"
        position_created_at = _position_text(position, "created_at")
        position_id = _position_text(position, "id")
        params.extend([position_created_at, position_created_at, position_id])
    query += f" ORDER BY {ITEM_ORDER} LIMIT ?"
    params.append(limit + 1)

    connection = open_readonly_queue(db_path)
    try:
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()

    return _item_page(rows, limit=limit, filters=filters)


def query_queue_attempts(
    db_path: str,
    *,
    item_id: str | None = None,
    cursor: str | None = None,
    limit: int = QUERY_DEFAULT_LIMIT,
) -> QueuePage[QueueAttempt]:
    """Return one read-only page of claim attempts in persisted sequence order."""
    return _query_events(
        db_path, select=ATTEMPT_SELECT, order=ATTEMPT_ORDER, kind=_KIND_ATTEMPTS,
        decoder=row_to_attempt, item_id=item_id, cursor=cursor, limit=limit,
    )


def query_queue_poison_events(
    db_path: str,
    *,
    item_id: str | None = None,
    cursor: str | None = None,
    limit: int = QUERY_DEFAULT_LIMIT,
) -> QueuePage[QueuePoisonEvent]:
    """Return one read-only page of poison events in persisted sequence order."""
    return _query_events(
        db_path, select=POISON_SELECT, order=POISON_ORDER, kind=_KIND_POISON_EVENTS,
        decoder=row_to_poison_event, item_id=item_id, cursor=cursor, limit=limit,
    )


def _item_page(
    rows: list[sqlite3.Row],
    *,
    limit: int,
    filters: str,
) -> QueuePage[QueueItem]:
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = None
    if has_more and page_rows:
        last_row = page_rows[-1]
        next_cursor = _encoded_cursor(
            kind=_KIND_ITEMS,
            filters=filters,
            position={
                "created_at": str(last_row["created_at"]),
                "id": str(last_row["id"]),
            },
        )
    return QueuePage(
        records=tuple(row_to_item(row) for row in page_rows),
        has_more=has_more,
        next_cursor=next_cursor,
    )


def _query_events(
    db_path: str,
    *,
    select: str,
    order: str,
    kind: str,
    decoder: Callable[[sqlite3.Row], RecordT],
    item_id: str | None,
    cursor: str | None,
    limit: int,
) -> QueuePage[RecordT]:
    _validated_limit(limit)
    _validated_optional_item_id(item_id)
    filters = _filter_fingerprint({"kind": kind, "item_id": item_id})
    position = None if cursor is None else _decoded_cursor(cursor, kind=kind, filters=filters)
    query = f"{select} WHERE 1=1"
    params: list[object] = []
    if item_id is not None:
        query += " AND item_id = ?"
        params.append(item_id)
    if position is not None:
        query += " AND sequence > ?"
        params.append(_position_sequence(position))
    query += f" ORDER BY {order} LIMIT ?"
    params.append(limit + 1)

    connection = open_readonly_queue(db_path)
    try:
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = None
    if has_more and page_rows:
        next_cursor = _encoded_cursor(
            kind=kind,
            filters=filters,
            position={"sequence": int(page_rows[-1]["sequence"])},
        )
    return QueuePage(
        records=tuple(decoder(row) for row in page_rows),
        has_more=has_more,
        next_cursor=next_cursor,
    )


def _filter_fingerprint(payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _encoded_cursor(*, kind: str, filters: str, position: dict[str, object]) -> str:
    payload = json.dumps(
        {
            "filters": filters,
            "kind": kind,
            "position": position,
            "version": QUEUE_CURSOR_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decoded_cursor(cursor: str, *, kind: str, filters: str) -> dict[str, object]:
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("cursor must be a non-empty queue query cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True))
    except ValueError as exc:
        raise ValueError("cursor is not a valid queue query cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("cursor is not a valid queue query cursor")
    if type(payload.get("version")) is not int or payload.get("version") != QUEUE_CURSOR_VERSION:
        raise ValueError("cursor has an unsupported queue query version")
    if payload.get("kind") != kind or payload.get("filters") != filters:
        raise ValueError("cursor does not match the queue query kind or filters")
    position = payload.get("position")
    if not isinstance(position, dict):
        raise ValueError("cursor is not a valid queue query cursor")
    return position


def _position_text(position: dict[str, object], key: str) -> str:
    value = position.get(key)
    if not isinstance(value, str):
        raise ValueError("cursor is not a valid queue query cursor")
    return value


def _position_sequence(position: dict[str, object]) -> int:
    value = position.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ValueError("cursor is not a valid queue query cursor")
    return value


def _validated_limit(limit: int) -> int:
    message = f"limit must be an integer from 1 through {QUERY_MAX_LIMIT}"
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError(message)
    if limit < 1 or limit > QUERY_MAX_LIMIT:
        raise ValueError(message)
    return limit


def _validated_item_id(item_id: str, *, field: str) -> str:
    if not isinstance(item_id, str):
        raise type_error(field, "str", item_id)
    if not item_id.strip():
        raise value_error(field, "nonblank queue item id", item_id)
    return item_id


def _validated_optional_item_id(item_id: str | None) -> str | None:
    if item_id is None:
        return None
    return _validated_item_id(item_id, field="queue item id selector")
