"""Queue — SQLite-backed FIFO work queue with atomic claiming."""

from __future__ import annotations

import json
import socket
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class QueueProvider(Protocol):
    """Protocol for queue backends."""

    def add(self, item: QueueItem) -> None: ...
    def next_item(self, worker_id: str = "") -> QueueItem | None: ...
    def complete(self, item_id: str) -> None: ...
    def fail(self, item_id: str, *, retry: bool = True) -> None: ...


_DEFAULT_DB_PATH = "queue.db"
_DEFAULT_CLAIM_TIMEOUT = 30
_DEFAULT_MAX_RETRIES = 3


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=1)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue_items (
            id           TEXT PRIMARY KEY,
            reference    TEXT NOT NULL,
            payload      TEXT NOT NULL DEFAULT '{}',
            status       TEXT NOT NULL DEFAULT 'pending',
            retry_count  INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            claimed_by   TEXT NOT NULL DEFAULT '',
            claimed_at   TEXT
        )
    """)


def _row_to_item(row: sqlite3.Row) -> QueueItem:
    claimed_at = None
    if row["claimed_at"]:
        claimed_at = datetime.fromisoformat(row["claimed_at"])
    return QueueItem(
        id=row["id"],
        reference=row["reference"],
        payload=json.loads(row["payload"]),
        status=QueueStatus(row["status"]),
        retry_count=row["retry_count"],
        created_at=datetime.fromisoformat(row["created_at"]),
        claimed_by=row["claimed_by"],
        claimed_at=claimed_at,
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
                conn.execute(
                    "INSERT INTO queue_items "
                    "(id, reference, payload, status, retry_count, created_at, claimed_by, claimed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.id,
                        item.reference,
                        json.dumps(item.payload),
                        item.status,
                        item.retry_count,
                        item.created_at.isoformat(),
                        item.claimed_by,
                        item.claimed_at.isoformat() if item.claimed_at else None,
                    ),
                )
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
            # BEGIN IMMEDIATE prevents two workers from claiming the same item.
            conn.execute("BEGIN IMMEDIATE")
            transaction_started = True

            # Reclaim stale items first.
            conn.execute(
                "UPDATE queue_items SET status = 'pending', claimed_by = '', claimed_at = NULL "
                "WHERE status = 'in_progress' "
                "AND claimed_at IS NOT NULL "
                "AND (CAST(strftime('%s', ?) AS INTEGER) - CAST(strftime('%s', claimed_at) AS INTEGER)) > ?",
                (now.isoformat(), self.claim_timeout),
            )

            # Claim the oldest pending item (tie-break on id for determinism).
            row = conn.execute(
                "SELECT * FROM queue_items WHERE status = 'pending' ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()

            if row is None:
                conn.execute("COMMIT")
                return None

            conn.execute(
                "UPDATE queue_items SET status = 'in_progress', claimed_by = ?, claimed_at = ? WHERE id = ?",
                (worker_id, now.isoformat(), row["id"]),
            )
            conn.execute("COMMIT")

            # Re-fetch to get the updated row.
            updated = conn.execute(
                "SELECT * FROM queue_items WHERE id = ?", (row["id"],)
            ).fetchone()
            return _row_to_item(updated)
        except Exception:
            if transaction_started:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def complete(self, item_id: str) -> None:
        """Mark an item as successfully processed."""
        conn = _connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE queue_items SET status = 'successful', claimed_at = NULL "
                    "WHERE id = ?",
                    (item_id,),
                )
        finally:
            conn.close()

    def fail(self, item_id: str, *, retry: bool = True) -> None:
        """Increment retry_count and mark the item retriable or terminally failed."""
        conn = _connect(self.db_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT retry_count FROM queue_items WHERE id = ?", (item_id,)
                ).fetchone()
                if row is None:
                    return
                new_count = row["retry_count"] + 1
                if retry and new_count <= self.max_retries:
                    conn.execute(
                        "UPDATE queue_items SET status = 'pending', retry_count = ?, "
                        "claimed_at = NULL WHERE id = ?",
                        (new_count, item_id),
                    )
                else:
                    conn.execute(
                        "UPDATE queue_items SET status = 'failed', retry_count = ?, "
                        "claimed_at = NULL WHERE id = ?",
                        (new_count, item_id),
                    )
        finally:
            conn.close()
