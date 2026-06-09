"""Queue — SQLite-backed FIFO work queue with atomic claiming."""

from __future__ import annotations

import json
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable, Protocol, runtime_checkable

from rpacore._json_state import validate_json_object
from rpacore._validation import type_error, value_error


class QueueStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESSFUL = "successful"
    FAILED = "failed"


@dataclass
class QueueItem:
    """A single work item in the queue."""

    reference: str
    payload: dict[str, object]
    id: str = field(default_factory=lambda: __import__("uuid").uuid4().hex)
    status: QueueStatus = QueueStatus.PENDING
    retry_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    claimed_by: str = ""
    claimed_at: datetime | None = None
    transaction_id: str = ""


@runtime_checkable
class QueueProvider(Protocol):
    """Protocol for queue backends."""

    def add(self, item: QueueItem) -> None: ...
    def next_item(self, worker_id: str = "") -> QueueItem | None: ...
    def bind_transaction(self, item_id: str, transaction_id: str, *, claimed_by: str) -> None: ...
    def complete(self, item_id: str, *, claimed_by: str | None = None) -> None: ...
    def fail(self, item_id: str, *, retry: bool = True, claimed_by: str | None = None) -> None: ...


_DEFAULT_DB_PATH = "queue.db"
_DEFAULT_CLAIM_TIMEOUT = 30
_DEFAULT_MAX_RETRIES = 3
_SCHEMA_TABLE = "rpacore_schema_versions"
_QUEUE_SCHEMA_COMPONENT = "queue"
_QUEUE_SCHEMA_VERSION = 2


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=1)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    if column_name not in _table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rpacore_schema_versions (
            component TEXT PRIMARY KEY,
            version   INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue_items (
            id           TEXT PRIMARY KEY,
            reference    TEXT NOT NULL,
            payload      TEXT NOT NULL DEFAULT '{}',
            status       TEXT NOT NULL DEFAULT 'pending',
            retry_count  INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            claimed_by   TEXT NOT NULL DEFAULT '',
            claimed_at   TEXT,
            transaction_id TEXT NOT NULL DEFAULT ''
        )
    """)
    _ensure_column(conn, "queue_items", "transaction_id", "transaction_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_items_created_at_id "
        "ON queue_items (created_at ASC, id ASC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_items_status_created_at_id "
        "ON queue_items (status, created_at ASC, id ASC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_items_reference_status "
        "ON queue_items (reference, status)"
    )
    conn.execute(
        f"INSERT INTO {_SCHEMA_TABLE} (component, version) VALUES (?, ?) "
        "ON CONFLICT(component) DO UPDATE SET version = excluded.version",
        (_QUEUE_SCHEMA_COMPONENT, _QUEUE_SCHEMA_VERSION),
    )


def _load_payload(raw_payload: str) -> dict[str, object]:
    payload = json.loads(raw_payload)
    validate_json_object(payload, path="queue item payload")
    return payload


def _row_to_item(row: sqlite3.Row, *, payload: dict[str, object] | None = None) -> QueueItem:
    claimed_at = None
    if row["claimed_at"]:
        claimed_at = datetime.fromisoformat(row["claimed_at"])
    if payload is None:
        payload = _load_payload(row["payload"])
    return QueueItem(
        id=row["id"],
        reference=row["reference"],
        payload=payload,
        status=QueueStatus(row["status"]),
        retry_count=row["retry_count"],
        created_at=datetime.fromisoformat(row["created_at"]),
        claimed_by=row["claimed_by"],
        claimed_at=claimed_at,
        transaction_id=row["transaction_id"],
    )


def _status_values(statuses: Iterable[QueueStatus]) -> list[str]:
    return [status.value for status in statuses]


def _limit_offset_clause(*, limit: int | None, offset: int) -> tuple[str, list[int]]:
    if isinstance(limit, bool) or (limit is not None and not isinstance(limit, int)):
        raise type_error("queue.list_items.limit", "int | None", limit)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise type_error("queue.list_items.offset", "int", offset)
    if limit is not None and limit < 0:
        raise value_error("queue.list_items.limit", "int >= 0 | None", limit)
    if offset < 0:
        raise value_error("queue.list_items.offset", "int >= 0", offset)

    if limit is None:
        if offset == 0:
            return "", []
        return " LIMIT -1 OFFSET ?", [offset]
    return " LIMIT ? OFFSET ?", [limit, offset]


def _status_filter_clause(statuses: Iterable[QueueStatus] | None) -> tuple[str, list[str]]:
    if statuses is None:
        return "", []
    status_values = _status_values(statuses)
    if not status_values:
        return " AND 1=0", []
    placeholders = ", ".join("?" for _ in status_values)
    return f" AND status IN ({placeholders})", status_values


def _insert_item(conn: sqlite3.Connection, item: QueueItem) -> None:
    validate_json_object(item.payload, path="queue item payload")
    conn.execute(
        "INSERT INTO queue_items "
        "(id, reference, payload, status, retry_count, created_at, claimed_by, claimed_at, transaction_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item.id,
            item.reference,
            json.dumps(item.payload),
            item.status,
            item.retry_count,
            item.created_at.isoformat(),
            item.claimed_by,
            item.claimed_at.isoformat() if item.claimed_at else None,
            item.transaction_id,
        ),
    )


class SqliteQueue:
    """FIFO work queue backed by SQLite.

    Multi-worker safe: uses BEGIN IMMEDIATE to atomically claim items so
    two workers cannot claim the same item simultaneously.

    Stale reclaim: items left IN_PROGRESS longer than claim_timeout seconds
    are treated as abandoned and returned to PENDING on the next next_item() call.

    Retry: fail() increments retry_count. With retry=True it resets to PENDING
    if under max_retries; otherwise it marks FAILED permanently. With
    retry=False it marks FAILED immediately.

    Config keys (all from the [queue] section of config.toml):
        db_path        (str)  Path to the SQLite file. Default: "queue.db"
        claim_timeout  (int)  Seconds before an IN_PROGRESS item is reclaimed. Default: 30
        max_retries    (int)  Max times an item may be retried on fail. Default: 3
    """

    def __init__(self, config: dict[str, object] | None = None) -> None:
        cfg: dict[str, object] = config or {}

        db_path = cfg.get("db_path", _DEFAULT_DB_PATH)
        claim_timeout = cfg.get("claim_timeout", _DEFAULT_CLAIM_TIMEOUT)
        max_retries = cfg.get("max_retries", _DEFAULT_MAX_RETRIES)

        if not isinstance(db_path, str):
            raise type_error("queue.db_path", "str", db_path)
        if isinstance(claim_timeout, bool) or not isinstance(claim_timeout, int):
            raise type_error("queue.claim_timeout", "int", claim_timeout)
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise type_error("queue.max_retries", "int", max_retries)
        if claim_timeout <= 0:
            raise value_error("queue.claim_timeout", "int > 0", claim_timeout)
        if max_retries < 0:
            raise value_error("queue.max_retries", "int >= 0", max_retries)

        self.db_path: str = db_path
        self.claim_timeout: int = claim_timeout
        self.max_retries: int = max_retries

        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
        finally:
            conn.close()

    def get_item(self, item_id: str) -> QueueItem | None:
        """Return a queue item by ID, or None if not found."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM queue_items WHERE id = ?", (item_id,)
            ).fetchone()
            return _row_to_item(row) if row else None
        finally:
            conn.close()

    def add(self, item: QueueItem) -> None:
        """Insert a new item into the queue."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                _insert_item(conn, item)
        finally:
            conn.close()

    def list_items(
        self,
        *,
        statuses: Iterable[QueueStatus] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[QueueItem]:
        """Return queue items, optionally filtered by status, in deterministic order."""
        status_filter, status_values = _status_filter_clause(statuses)
        limit_clause, limit_params = _limit_offset_clause(limit=limit, offset=offset)

        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM queue_items WHERE 1=1"
                f"{status_filter} ORDER BY created_at ASC, id ASC{limit_clause}",
                [*status_values, *limit_params],
            ).fetchall()
            return [_row_to_item(row) for row in rows]
        finally:
            conn.close()

    def has_reference(
        self,
        reference: str,
        *,
        statuses: Iterable[QueueStatus] | None = None,
    ) -> bool:
        """Return True when a queue item exists for reference and optional statuses."""
        status_filter, status_values = _status_filter_clause(statuses)

        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
            row = conn.execute(
                f"SELECT 1 FROM queue_items WHERE reference = ?{status_filter} LIMIT 1",
                [reference, *status_values],
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def add_once(
        self,
        item: QueueItem,
        *,
        active_statuses: Iterable[QueueStatus] | None = (QueueStatus.PENDING, QueueStatus.IN_PROGRESS),
    ) -> bool:
        """Insert item unless another item with the same reference is active."""
        status_filter, status_values = _status_filter_clause(active_statuses)
        conn = _connect(self.db_path)
        transaction_started = False
        try:
            with conn:
                _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            transaction_started = True

            if active_statuses is None or status_values:
                duplicate = conn.execute(
                    f"SELECT 1 FROM queue_items WHERE reference = ?{status_filter} LIMIT 1",
                    [item.reference, *status_values],
                ).fetchone()
                if duplicate is not None:
                    conn.execute("ROLLBACK")
                    transaction_started = False
                    return False

            _insert_item(conn, item)
            conn.execute("COMMIT")
            transaction_started = False
            return True
        except Exception:
            if transaction_started:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def next_item(self, worker_id: str = "") -> QueueItem | None:
        """Atomically claim and return the oldest PENDING item, or None if empty.

        Also reclaims stale IN_PROGRESS items (older than claim_timeout seconds)
        back to PENDING before selecting.
        """
        if not worker_id:
            worker_id = socket.gethostname()

        now = datetime.now(timezone.utc)
        conn = _connect(self.db_path)
        transaction_started = False
        try:
            with conn:
                _ensure_schema(conn)

            # Reclaim stale items in a short write transaction before selecting.
            conn.execute("BEGIN IMMEDIATE")
            transaction_started = True
            conn.execute(
                "UPDATE queue_items SET status = 'pending', claimed_by = '', claimed_at = NULL "
                "WHERE status = 'in_progress' "
                "AND claimed_at IS NOT NULL "
                "AND (CAST(strftime('%s', ?) AS INTEGER) - CAST(strftime('%s', claimed_at) AS INTEGER)) > ?",
                (now.isoformat(), self.claim_timeout),
            )
            conn.execute("COMMIT")
            transaction_started = False

            while True:
                # Validate outside the write lock. A guarded UPDATE below keeps
                # claiming atomic when multiple workers race for the same row.
                row = conn.execute(
                    "SELECT * FROM queue_items WHERE status = 'pending' ORDER BY created_at ASC, id ASC LIMIT 1"
                ).fetchone()

                if row is None:
                    return None

                payload = _load_payload(row["payload"])
                now = datetime.now(timezone.utc)

                conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                result = conn.execute(
                    "UPDATE queue_items SET status = 'in_progress', claimed_by = ?, claimed_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (worker_id, now.isoformat(), row["id"]),
                )
                if result.rowcount != 1:
                    conn.execute("COMMIT")
                    transaction_started = False
                    continue
                conn.execute("COMMIT")
                transaction_started = False

                updated = conn.execute(
                    "SELECT * FROM queue_items WHERE id = ?", (row["id"],)
                ).fetchone()
                return _row_to_item(updated, payload=payload)
        except Exception:
            if transaction_started:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def bind_transaction(self, item_id: str, transaction_id: str, *, claimed_by: str) -> None:
        """Bind a persisted transaction id to the currently claimed queue item."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                result = conn.execute(
                    "UPDATE queue_items SET transaction_id = ? "
                    "WHERE id = ? AND status = 'in_progress' AND claimed_by = ?",
                    (transaction_id, item_id, claimed_by),
                )
                if result.rowcount != 1:
                    raise RuntimeError(
                        f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                    )
        finally:
            conn.close()

    def complete(self, item_id: str, *, claimed_by: str | None = None) -> None:
        """Mark an item as successfully processed."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                if claimed_by is None:
                    conn.execute(
                        "UPDATE queue_items SET status = 'successful', claimed_at = NULL "
                        "WHERE id = ?",
                        (item_id,),
                    )
                else:
                    result = conn.execute(
                        "UPDATE queue_items SET status = 'successful', claimed_at = NULL "
                        "WHERE id = ? AND status = 'in_progress' AND claimed_by = ?",
                        (item_id, claimed_by),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError(
                            f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                        )
        finally:
            conn.close()

    def fail(self, item_id: str, *, retry: bool = True, claimed_by: str | None = None) -> None:
        """Increment retry_count and mark the item retriable or terminally failed."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                if claimed_by is None:
                    row = conn.execute(
                        "SELECT retry_count FROM queue_items WHERE id = ?", (item_id,)
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT retry_count FROM queue_items "
                        "WHERE id = ? AND status = 'in_progress' AND claimed_by = ?",
                        (item_id, claimed_by),
                    ).fetchone()
                if row is None:
                    if claimed_by is not None:
                        raise RuntimeError(
                            f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                        )
                    return
                new_count = row["retry_count"] + 1
                if retry and new_count <= self.max_retries:
                    if claimed_by is None:
                        result = conn.execute(
                            "UPDATE queue_items SET status = 'pending', retry_count = ?, "
                            "claimed_at = NULL WHERE id = ?",
                            (new_count, item_id),
                        )
                    else:
                        result = conn.execute(
                            "UPDATE queue_items SET status = 'pending', retry_count = ?, "
                            "claimed_at = NULL WHERE id = ? AND status = 'in_progress' AND claimed_by = ?",
                            (new_count, item_id, claimed_by),
                        )
                else:
                    if claimed_by is None:
                        result = conn.execute(
                            "UPDATE queue_items SET status = 'failed', retry_count = ?, "
                            "claimed_at = NULL WHERE id = ?",
                            (new_count, item_id),
                        )
                    else:
                        result = conn.execute(
                            "UPDATE queue_items SET status = 'failed', retry_count = ?, "
                            "claimed_at = NULL WHERE id = ? AND status = 'in_progress' AND claimed_by = ?",
                            (new_count, item_id, claimed_by),
                        )
                if claimed_by is not None and result.rowcount != 1:
                    raise RuntimeError(
                        f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                    )
        finally:
            conn.close()
