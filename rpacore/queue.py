"""Queue — SQLite-backed FIFO work queue with atomic claiming."""

from __future__ import annotations

import json
import socket
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable, Protocol, runtime_checkable

from rpacore._json_state import JsonStateError, validate_json_object
from rpacore._sqlite import (
    SCHEMA_VERSION_TABLE,
    QUEUE_SCHEMA_VERSION,
    configure_rollback_journal,
    connect_sqlite,
    ensure_database_compatibility,
    ensure_supported_schema,
    validate_durable_sqlite_path,
)
from rpacore._validation import type_error, value_error
from rpacore.config_validation import optional_config


class QueueStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESSFUL = "successful"
    FAILED = "failed"


class QueueAttemptOutcome(StrEnum):
    """Final disposition for one claimed queue attempt."""

    SUCCESSFUL = "successful"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    LEASE_EXPIRED = "lease_expired"
    ADMIN_OVERRIDE = "admin_override"


class QueueLeaseLostError(RuntimeError):
    """Raised when a worker no longer owns an in-progress queue item lease."""


@dataclass(frozen=True)
class QueueAdminEvent:
    """An audited administrative queue-state override."""

    sequence: int
    item_id: str
    action: str
    reason: str
    previous_status: QueueStatus
    new_status: QueueStatus
    created_at: datetime


@dataclass(frozen=True)
class QueueAttempt:
    """One claimed queue attempt and its eventual durable outcome."""

    sequence: int
    item_id: str
    claim_token: str
    claimed_by: str
    started_at: datetime
    finished_at: datetime | None
    outcome: QueueAttemptOutcome | None


@dataclass(frozen=True)
class QueuePoisonEvent:
    """A malformed pending payload quarantined without claiming it."""

    sequence: int
    item_id: str
    error_type: str
    created_at: datetime


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
    claim_token: str = ""
    transaction_id: str = ""


@runtime_checkable
class QueueProvider(Protocol):
    """Protocol for queue backends."""

    def add(self, item: QueueItem) -> None: ...
    def next_item(self, worker_id: str = "") -> QueueItem | None: ...
    def bind_transaction(
        self, item_id: str, transaction_id: str, *, claimed_by: str, claim_token: str
    ) -> None: ...
    def renew_lease(self, item_id: str, *, claimed_by: str, claim_token: str) -> None: ...
    def complete(self, item_id: str, *, claimed_by: str, claim_token: str) -> None: ...
    def fail(
        self, item_id: str, *, retry: bool = True, claimed_by: str, claim_token: str
    ) -> None: ...


_DEFAULT_DB_PATH = "queue.db"
_DEFAULT_LEASE_TIMEOUT = 30
_DEFAULT_MAX_RETRIES = 3
_SCHEMA_TABLE = SCHEMA_VERSION_TABLE
_QUEUE_SCHEMA_COMPONENT = "queue"
_QUEUE_SCHEMA_VERSION = QUEUE_SCHEMA_VERSION


def _connect(db_path: str) -> sqlite3.Connection:
    return connect_sqlite(db_path, field="queue.db_path")


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    if column_name not in _table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rpacore_schema_versions (
            component TEXT PRIMARY KEY,
            version   INTEGER NOT NULL
        )
    """)


def _record_queue_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        f"INSERT INTO {_SCHEMA_TABLE} (component, version) VALUES (?, ?) "
        "ON CONFLICT(component) DO UPDATE SET version = excluded.version",
        (_QUEUE_SCHEMA_COMPONENT, version),
    )


def _migrate_queue_to_v1(conn: sqlite3.Connection) -> None:
    """Create the original queue item schema."""
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
    _record_queue_schema_version(conn, 1)


def _migrate_queue_to_v2(conn: sqlite3.Connection) -> None:
    """Add durable transaction binding and queue inspection indexes."""
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
    _record_queue_schema_version(conn, 2)


def _migrate_queue_to_v3(conn: sqlite3.Connection) -> None:
    """Add per-attempt claim credentials and audited operator overrides.

    A pre-v3 in-progress row has no credential that can distinguish its current
    worker from a later worker using the same label. Invalidate those leases so
    they must be reacquired with a token before any further mutation.
    """
    _ensure_column(conn, "queue_items", "claim_token", "claim_token TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "UPDATE queue_items SET status = 'pending', claimed_by = '', claimed_at = NULL "
        "WHERE status = 'in_progress'"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue_admin_events (
            sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id         TEXT NOT NULL,
            action          TEXT NOT NULL,
            reason          TEXT NOT NULL,
            previous_status TEXT NOT NULL,
            new_status      TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES queue_items(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_admin_events_item_sequence "
        "ON queue_admin_events (item_id, sequence)"
    )
    _record_queue_schema_version(conn, 3)


def _migrate_queue_to_v4(conn: sqlite3.Connection) -> None:
    """Add append-only claim attempts and corrupt-payload dispositions.

    Existing rows predate attempt tracking. Their history remains intact in
    ``queue_items``; only claims created after this migration receive attempt
    rows. The upgrade is offline under the same queue migration policy as v3.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue_attempts (
            sequence    INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     TEXT NOT NULL,
            claim_token TEXT NOT NULL,
            claimed_by  TEXT NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            outcome     TEXT NOT NULL DEFAULT '',
            UNIQUE (item_id, claim_token),
            FOREIGN KEY (item_id) REFERENCES queue_items(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_attempts_item_sequence "
        "ON queue_attempts (item_id, sequence)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queue_poison_events (
            sequence   INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    TEXT NOT NULL,
            error_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES queue_items(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_poison_events_item_sequence "
        "ON queue_poison_events (item_id, sequence)"
    )
    _record_queue_schema_version(conn, 4)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Run sequential queue migrations after rejecting future schemas."""
    with conn:
        ensure_database_compatibility(conn)
        current_version = ensure_supported_schema(
            conn,
            component=_QUEUE_SCHEMA_COMPONENT,
            supported_version=_QUEUE_SCHEMA_VERSION,
        )
        _ensure_schema_version_table(conn)
        if current_version < 1:
            _migrate_queue_to_v1(conn)
            current_version = 1
        if current_version < 2:
            _migrate_queue_to_v2(conn)
            current_version = 2
        if current_version < 3:
            _migrate_queue_to_v3(conn)
            current_version = 3
        if current_version < 4:
            _migrate_queue_to_v4(conn)
            current_version = 4
        if current_version != _QUEUE_SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported queue schema version {current_version}; "
                f"expected {_QUEUE_SCHEMA_VERSION}"
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
        claim_token=row["claim_token"],
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
        "(id, reference, payload, status, retry_count, created_at, claimed_by, claimed_at, "
        "claim_token, transaction_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item.id,
            item.reference,
            json.dumps(item.payload),
            item.status,
            item.retry_count,
            item.created_at.isoformat(),
            item.claimed_by,
            item.claimed_at.isoformat() if item.claimed_at else None,
            item.claim_token,
            item.transaction_id,
        ),
    )


def _open_attempt(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    claim_token: str,
    claimed_by: str,
    started_at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO queue_attempts "
        "(item_id, claim_token, claimed_by, started_at) VALUES (?, ?, ?, ?)",
        (item_id, claim_token, claimed_by, started_at.isoformat()),
    )


def _close_attempt(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    claim_token: str,
    outcome: QueueAttemptOutcome,
    finished_at: datetime,
) -> None:
    """Record one final outcome when the claim was created under schema v4."""
    conn.execute(
        "UPDATE queue_attempts SET outcome = ?, finished_at = ? "
        "WHERE item_id = ? AND claim_token = ? AND outcome = ''",
        (outcome, finished_at.isoformat(), item_id, claim_token),
    )


def _record_poison_event(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    error_type: str,
    created_at: datetime,
) -> None:
    conn.execute(
        "INSERT INTO queue_poison_events (item_id, error_type, created_at) VALUES (?, ?, ?)",
        (item_id, error_type, created_at.isoformat()),
    )


class SqliteQueue:
    """FIFO work queue backed by SQLite.

    Multi-worker safe: uses BEGIN IMMEDIATE to atomically claim items so
    two workers cannot claim the same item simultaneously.

    Stale reclaim: items left IN_PROGRESS longer than lease_timeout seconds
    are treated as abandoned and returned to PENDING on the next next_item() call.

    Retry: fail() increments retry_count. With retry=True it resets to PENDING
    if under max_retries; otherwise it marks FAILED permanently. With
    retry=False it marks FAILED immediately.

    Config keys (all from the [queue] section of config.toml):
        db_path        (str)  Path to the SQLite file. Default: "queue.db"
        lease_timeout  (int)  Seconds before an IN_PROGRESS item is reclaimed. Default: 30
        max_retries    (int)  Max times an item may be retried on fail. Default: 3
    """

    def __init__(self, config: dict[str, object] | None = None) -> None:
        cfg: dict[str, object] = config or {}

        validation_config = {f"queue.{key}": value for key, value in cfg.items()}
        db_path = optional_config(
            validation_config,
            "queue.db_path",
            str,
            _DEFAULT_DB_PATH,
        )
        lease_timeout = optional_config(
            validation_config,
            "queue.lease_timeout",
            int,
            _DEFAULT_LEASE_TIMEOUT,
        )
        max_retries = optional_config(
            validation_config,
            "queue.max_retries",
            int,
            _DEFAULT_MAX_RETRIES,
        )
        if lease_timeout <= 0:
            raise value_error("queue.lease_timeout", "int > 0", lease_timeout)
        if max_retries < 0:
            raise value_error("queue.max_retries", "int >= 0", max_retries)
        validate_durable_sqlite_path(db_path, field="queue.db_path")

        self.db_path: str = db_path
        self.lease_timeout: int = lease_timeout
        self.max_retries: int = max_retries

        conn = _connect(self.db_path)
        try:
            # These checks must precede the persistent journal policy. The
            # checks inside _ensure_schema protect every later connection.
            ensure_database_compatibility(conn)
            ensure_supported_schema(
                conn,
                component=_QUEUE_SCHEMA_COMPONENT,
                supported_version=_QUEUE_SCHEMA_VERSION,
            )
            configure_rollback_journal(conn)
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

        Also reclaims stale IN_PROGRESS items (older than lease_timeout seconds)
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
            stale_rows = conn.execute(
                "SELECT id, retry_count, claimed_by, claim_token FROM queue_items "
                "WHERE status = 'in_progress' AND claimed_at IS NOT NULL "
                "AND (CAST(strftime('%s', ?) AS INTEGER) - CAST(strftime('%s', claimed_at) AS INTEGER)) > ?",
                (now.isoformat(), self.lease_timeout),
            ).fetchall()
            for stale in stale_rows:
                retry_count = int(stale["retry_count"]) + 1
                new_status = "pending" if retry_count <= self.max_retries else "failed"
                claimed_by = "" if new_status == "pending" else stale["claimed_by"]
                result = conn.execute(
                    "UPDATE queue_items SET status = ?, retry_count = ?, claimed_by = ?, "
                    "claimed_at = NULL, claim_token = '' "
                    "WHERE id = ? AND status = 'in_progress' AND claim_token = ?",
                    (new_status, retry_count, claimed_by, stale["id"], stale["claim_token"]),
                )
                if result.rowcount == 1 and stale["claim_token"]:
                    _close_attempt(
                        conn,
                        item_id=stale["id"],
                        claim_token=stale["claim_token"],
                        outcome=QueueAttemptOutcome.LEASE_EXPIRED,
                        finished_at=now,
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

                try:
                    payload = _load_payload(row["payload"])
                except (json.JSONDecodeError, JsonStateError) as exc:
                    conn.execute("BEGIN IMMEDIATE")
                    transaction_started = True
                    result = conn.execute(
                        "UPDATE queue_items SET status = 'failed', claimed_by = '', "
                        "claimed_at = NULL, claim_token = '' "
                        "WHERE id = ? AND status = 'pending'",
                        (row["id"],),
                    )
                    if result.rowcount == 1:
                        _record_poison_event(
                            conn,
                            item_id=row["id"],
                            error_type=type(exc).__name__,
                            created_at=datetime.now(timezone.utc),
                        )
                    conn.execute("COMMIT")
                    transaction_started = False
                    continue
                now = datetime.now(timezone.utc)
                claim_token = uuid.uuid4().hex

                conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                result = conn.execute(
                    "UPDATE queue_items SET status = 'in_progress', claimed_by = ?, claimed_at = ?, "
                    "claim_token = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (worker_id, now.isoformat(), claim_token, row["id"]),
                )
                if result.rowcount != 1:
                    conn.execute("COMMIT")
                    transaction_started = False
                    continue
                _open_attempt(
                    conn,
                    item_id=row["id"],
                    claim_token=claim_token,
                    claimed_by=worker_id,
                    started_at=now,
                )
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

    def bind_transaction(
        self,
        item_id: str,
        transaction_id: str,
        *,
        claimed_by: str,
        claim_token: str,
    ) -> None:
        """Bind a persisted transaction id to the currently claimed queue item."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                result = conn.execute(
                    "UPDATE queue_items SET transaction_id = ? "
                    "WHERE id = ? AND status = 'in_progress' AND claimed_by = ? "
                    "AND claim_token = ? AND claim_token != ''",
                    (transaction_id, item_id, claimed_by, claim_token),
                )
                if result.rowcount != 1:
                    raise QueueLeaseLostError(
                        f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                    )
        finally:
            conn.close()

    def renew_lease(self, item_id: str, *, claimed_by: str, claim_token: str) -> None:
        """Extend the currently claimed queue item lease for its owner."""
        now = datetime.now(timezone.utc)
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                result = conn.execute(
                    "UPDATE queue_items SET claimed_at = ? "
                    "WHERE id = ? AND status = 'in_progress' AND claimed_by = ? "
                    "AND claim_token = ? AND claim_token != ''",
                    (now.isoformat(), item_id, claimed_by, claim_token),
                )
                if result.rowcount != 1:
                    raise QueueLeaseLostError(
                        f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                    )
        finally:
            conn.close()

    def complete(self, item_id: str, *, claimed_by: str, claim_token: str) -> None:
        """Mark an item successful when the supplied claim is still current."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                result = conn.execute(
                    "UPDATE queue_items SET status = 'successful', "
                    "claimed_at = NULL, claim_token = '' "
                    "WHERE id = ? AND status = 'in_progress' AND claimed_by = ? "
                    "AND claim_token = ? AND claim_token != ''",
                    (item_id, claimed_by, claim_token),
                )
                if result.rowcount != 1:
                    raise QueueLeaseLostError(
                        f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                    )
                _close_attempt(
                    conn,
                    item_id=item_id,
                    claim_token=claim_token,
                    outcome=QueueAttemptOutcome.SUCCESSFUL,
                    finished_at=datetime.now(timezone.utc),
                )
        finally:
            conn.close()

    def fail(
        self,
        item_id: str,
        *,
        retry: bool = True,
        claimed_by: str,
        claim_token: str,
    ) -> None:
        """Increment retry_count and mark the item retriable or terminally failed."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                row = conn.execute(
                    "SELECT retry_count FROM queue_items "
                    "WHERE id = ? AND status = 'in_progress' AND claimed_by = ? "
                    "AND claim_token = ? AND claim_token != ''",
                    (item_id, claimed_by, claim_token),
                ).fetchone()
                if row is None:
                    raise QueueLeaseLostError(
                        f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                    )
                new_count = row["retry_count"] + 1
                new_status = "pending" if retry and new_count <= self.max_retries else "failed"
                next_claimed_by = "" if new_status == "pending" else claimed_by
                result = conn.execute(
                    "UPDATE queue_items SET status = ?, retry_count = ?, claimed_by = ?, "
                    "claimed_at = NULL, claim_token = '' "
                    "WHERE id = ? AND status = 'in_progress' AND claimed_by = ? "
                    "AND claim_token = ? AND claim_token != ''",
                    (new_status, new_count, next_claimed_by, item_id, claimed_by, claim_token),
                )
                if result.rowcount != 1:
                    raise QueueLeaseLostError(
                        f"Queue item {item_id!r} is no longer claimed by {claimed_by!r}"
                    )
                _close_attempt(
                    conn,
                    item_id=item_id,
                    claim_token=claim_token,
                    outcome=(
                        QueueAttemptOutcome.RETRY_SCHEDULED
                        if new_status == "pending"
                        else QueueAttemptOutcome.FAILED
                    ),
                    finished_at=datetime.now(timezone.utc),
                )
        finally:
            conn.close()

    def force_complete(self, item_id: str, *, reason: str) -> None:
        """Administratively mark an item successful and record the reason."""
        self._force_transition(item_id, QueueStatus.SUCCESSFUL, reason=reason, retry=False)

    def force_fail(self, item_id: str, *, reason: str, retry: bool = False) -> None:
        """Administratively fail or requeue an item and record the reason."""
        self._force_transition(item_id, QueueStatus.FAILED, reason=reason, retry=retry)

    def _force_transition(
        self,
        item_id: str,
        requested_status: QueueStatus,
        *,
        reason: str,
        retry: bool,
    ) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise value_error("queue.admin.reason", "non-empty str", reason)
        now = datetime.now(timezone.utc)
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
                row = conn.execute(
                    "SELECT status, retry_count, claim_token FROM queue_items WHERE id = ?", (item_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Queue item not found: {item_id!r}")
                retry_count = int(row["retry_count"])
                new_status = requested_status
                if requested_status == QueueStatus.FAILED:
                    retry_count += 1
                    if retry and retry_count <= self.max_retries:
                        new_status = QueueStatus.PENDING
                conn.execute(
                    "UPDATE queue_items SET status = ?, retry_count = ?, claimed_by = '', "
                    "claimed_at = NULL, claim_token = '' WHERE id = ?",
                    (new_status, retry_count, item_id),
                )
                if row["claim_token"]:
                    _close_attempt(
                        conn,
                        item_id=item_id,
                        claim_token=row["claim_token"],
                        outcome=QueueAttemptOutcome.ADMIN_OVERRIDE,
                        finished_at=now,
                    )
                conn.execute(
                    "INSERT INTO queue_admin_events "
                    "(item_id, action, reason, previous_status, new_status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item_id,
                        "force_complete" if requested_status == QueueStatus.SUCCESSFUL else "force_fail",
                        reason.strip(),
                        row["status"],
                        new_status,
                        now.isoformat(),
                    ),
                )
        finally:
            conn.close()

    def list_admin_events(self, item_id: str | None = None) -> list[QueueAdminEvent]:
        """Return administrative overrides in durable sequence order."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
            if item_id is None:
                rows = conn.execute(
                    "SELECT * FROM queue_admin_events ORDER BY sequence"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM queue_admin_events WHERE item_id = ? ORDER BY sequence",
                    (item_id,),
                ).fetchall()
            return [
                QueueAdminEvent(
                    sequence=row["sequence"],
                    item_id=row["item_id"],
                    action=row["action"],
                    reason=row["reason"],
                    previous_status=QueueStatus(row["previous_status"]),
                    new_status=QueueStatus(row["new_status"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def list_attempts(self, item_id: str | None = None) -> list[QueueAttempt]:
        """Return claimed attempts and final outcomes in durable sequence order."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
            if item_id is None:
                rows = conn.execute("SELECT * FROM queue_attempts ORDER BY sequence").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM queue_attempts WHERE item_id = ? ORDER BY sequence",
                    (item_id,),
                ).fetchall()
            return [
                QueueAttempt(
                    sequence=row["sequence"],
                    item_id=row["item_id"],
                    claim_token=row["claim_token"],
                    claimed_by=row["claimed_by"],
                    started_at=datetime.fromisoformat(row["started_at"]),
                    finished_at=(
                        datetime.fromisoformat(row["finished_at"])
                        if row["finished_at"]
                        else None
                    ),
                    outcome=(QueueAttemptOutcome(row["outcome"]) if row["outcome"] else None),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def list_poison_events(self, item_id: str | None = None) -> list[QueuePoisonEvent]:
        """Return malformed-payload quarantines in durable sequence order."""
        conn = _connect(self.db_path)
        try:
            with conn:
                _ensure_schema(conn)
            if item_id is None:
                rows = conn.execute("SELECT * FROM queue_poison_events ORDER BY sequence").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM queue_poison_events WHERE item_id = ? ORDER BY sequence",
                    (item_id,),
                ).fetchall()
            return [
                QueuePoisonEvent(
                    sequence=row["sequence"],
                    item_id=row["item_id"],
                    error_type=row["error_type"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows
            ]
        finally:
            conn.close()
