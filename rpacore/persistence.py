"""Persistence — SQLite-backed save/load for transactions and skills."""

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rpacore._json_state import JsonStateError, validate_json_object
from rpacore._sqlite import (
    QUEUE_SCHEMA_VERSION,
    SCHEMA_VERSION_TABLE,
    TRANSACTION_SCHEMA_VERSION,
    component_schema_version,
    configure_rollback_journal,
    connect_sqlite,
    ensure_database_compatibility,
    ensure_supported_schema,
    require_current_schema,
)
from rpacore.exceptions import BusinessException, SystemException
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Artifact, HistoryEntry, HistoryEvent, Transaction


_SCHEMA_TABLE = SCHEMA_VERSION_TABLE
_TRANSACTION_SCHEMA_COMPONENT = "transactions"
_TRANSACTION_SCHEMA_VERSION = TRANSACTION_SCHEMA_VERSION
_QUERY_PAGE_FORMAT_VERSION = 1
_QUERY_MAX_LIMIT = 1_000
_MIGRATION_BATCH_SIZE = 500


@dataclass(frozen=True)
class TransactionSummary:
    """Small immutable transaction record returned by read-only queries."""

    id: str
    reference: str
    status: Status
    retry_count: int
    created_at: datetime | None


@dataclass(frozen=True)
class TransactionPage:
    """One versioned page of transaction summaries."""

    transactions: tuple[TransactionSummary, ...]
    has_more: bool
    next_cursor: str | None
    format_version: int = _QUERY_PAGE_FORMAT_VERSION


class TransactionFenceError(RuntimeError):
    """Raised when a queue checkpoint no longer owns its claim or revision."""


def _connect(db_path: str, *, readonly: bool = False) -> sqlite3.Connection:
    return connect_sqlite(
        db_path,
        field="transaction_db_path",
        readonly=readonly,
    )


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    if column_name not in _table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rpacore_schema_versions (
            component TEXT PRIMARY KEY,
            version   INTEGER NOT NULL
        )
    """)


def _component_schema_version(conn: sqlite3.Connection, component: str) -> int:
    return component_schema_version(conn, component)


def _record_component_schema_version(conn: sqlite3.Connection, component: str, version: int) -> None:
    conn.execute(
        f"INSERT INTO {_SCHEMA_TABLE} (component, version) VALUES (?, ?) "
        "ON CONFLICT(component) DO UPDATE SET version = excluded.version",
        (component, version),
    )


def _migrate_transactions_to_v1(conn: sqlite3.Connection) -> None:
    """Create or migrate the v1 transaction persistence schema."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          TEXT PRIMARY KEY,
            reference   TEXT NOT NULL,
            status      TEXT NOT NULL,
            retry_count INTEGER NOT NULL,
            created_at  TEXT NOT NULL DEFAULT ''
        )
    """)
    _ensure_column(conn, "transactions", "created_at", "created_at TEXT NOT NULL DEFAULT ''")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id              TEXT PRIMARY KEY,
            transaction_id  TEXT NOT NULL,
            name            TEXT NOT NULL,
            execution_order INTEGER NOT NULL,
            status          TEXT NOT NULL,
            arguments       TEXT NOT NULL DEFAULT '{}',
            UNIQUE (transaction_id, name, execution_order),
            FOREIGN KEY (transaction_id) REFERENCES transactions(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS exceptions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id          TEXT NOT NULL,
            exception_type    TEXT NOT NULL,
            message           TEXT NOT NULL,
            action            TEXT NOT NULL,
            retry_number      INTEGER NOT NULL,
            datetime_occurred TEXT NOT NULL,
            screenshot_path   TEXT NOT NULL DEFAULT '',
            stops_execution   INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
        )
    """)
    _ensure_column(conn, "exceptions", "stops_execution", "stops_execution INTEGER NOT NULL DEFAULT 0")
    _record_component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT, 1)


def _migrate_transactions_to_v2(conn: sqlite3.Connection) -> None:
    """Add durable transaction state.

    This migration is additive and forward-only. Older v1 code can coexist with
    the extra column, but the component schema version remains v2.
    """
    _ensure_column(conn, "transactions", "state", "state TEXT NOT NULL DEFAULT '{}'")
    _record_component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT, 2)


def _migrate_transactions_to_v3(conn: sqlite3.Connection) -> None:
    """Add truthful transaction timestamps and append-only history storage."""
    _ensure_column(conn, "transactions", "started_at", "started_at TEXT")
    _ensure_column(conn, "transactions", "finished_at", "finished_at TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transaction_history (
            transaction_id        TEXT NOT NULL,
            sequence              INTEGER NOT NULL,
            timestamp             TEXT NOT NULL,
            event                 TEXT NOT NULL CHECK (
                event IN (
                    'transaction_started',
                    'skill_started',
                    'skill_succeeded',
                    'skill_failed',
                    'skill_skipped',
                    'skill_interrupted',
                    'retry_scheduled',
                    'transaction_resumed',
                    'transaction_completed'
                )
            ),
            status                TEXT NOT NULL CHECK (
                status IN ('pending', 'in_progress', 'successful', 'failed', 'skipped')
            ),
            retry_number          INTEGER NOT NULL,
            skill_name            TEXT NOT NULL DEFAULT '',
            skill_execution_order INTEGER,
            PRIMARY KEY (transaction_id, sequence),
            FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
        )
    """)
    _record_component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT, 3)


def _migrate_transactions_to_v4(conn: sqlite3.Connection) -> None:
    """Add exact-match top-level transaction metadata storage."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transaction_metadata (
            transaction_id TEXT NOT NULL,
            key            TEXT NOT NULL,
            value_json     TEXT NOT NULL,
            PRIMARY KEY (transaction_id, key),
            FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
        )
    """)
    _record_component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT, 4)


def _migrate_transactions_to_v5(conn: sqlite3.Connection) -> None:
    """Add dedicated artifact audit storage."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transaction_artifacts (
            transaction_id TEXT NOT NULL,
            sequence       INTEGER NOT NULL,
            id             TEXT NOT NULL,
            name           TEXT NOT NULL,
            path           TEXT NOT NULL,
            kind           TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL,
            metadata       TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (transaction_id, id),
            UNIQUE (transaction_id, sequence),
            FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
        )
    """)
    _record_component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT, 5)


def _migrate_transactions_to_v6(conn: sqlite3.Connection) -> None:
    """Add queue-attempt identity and an optimistic persistence revision."""
    _ensure_column(conn, "transactions", "revision", "revision INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "transactions", "queue_item_id", "queue_item_id TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "transactions", "claim_token", "claim_token TEXT NOT NULL DEFAULT ''")
    _record_component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT, 6)


def _migrate_transactions_to_v7(conn: sqlite3.Connection) -> None:
    """Add UTC query keys and indexes for deterministic transaction inspection."""
    _ensure_column(
        conn,
        "transactions",
        "created_at_utc",
        "created_at_utc TEXT NOT NULL DEFAULT ''",
    )
    last_id: str | None = None
    while True:
        if last_id is None:
            rows = conn.execute(
                "SELECT id, created_at, created_at_utc FROM transactions ORDER BY id LIMIT ?",
                (_MIGRATION_BATCH_SIZE,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, created_at, created_at_utc FROM transactions "
                "WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, _MIGRATION_BATCH_SIZE),
            ).fetchall()
        if not rows:
            break
        updates = [
            (
                _legacy_utc_timestamp(
                    row["created_at"],
                    path="transactions.created_at",
                    transaction_id=row["id"],
                ),
                row["id"],
            )
            for row in rows
            if not row["created_at_utc"]
        ]
        if updates:
            conn.executemany(
                "UPDATE transactions SET created_at_utc = ? WHERE id = ?",
                updates,
            )
        last_id = str(rows[-1]["id"])
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_created_at_utc_id "
        "ON transactions (created_at_utc DESC, id ASC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_status_created_at_utc_id "
        "ON transactions (status, created_at_utc DESC, id ASC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transaction_metadata_key_value_transaction "
        "ON transaction_metadata (key, value_json, transaction_id)"
    )
    _record_component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT, 7)


def _migrate_transactions_to_v8(conn: sqlite3.Connection) -> None:
    """Add durable terminal outcome truth and optional failure codes."""
    _ensure_column(
        conn,
        "transactions",
        "outcome_category",
        "outcome_category TEXT NOT NULL DEFAULT 'unknown'",
    )
    _ensure_column(
        conn,
        "transactions",
        "retry_disposition",
        "retry_disposition TEXT NOT NULL DEFAULT 'unknown'",
    )
    _ensure_column(
        conn,
        "transactions",
        "failure_code",
        "failure_code TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(conn, "exceptions", "code", "code TEXT NOT NULL DEFAULT ''")
    _record_component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT, 8)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Run explicit transaction schema migrations through one write transaction."""
    # sqlite3's connection context manager does not begin a transaction for
    # DDL. Start one explicitly so a failed migration cannot leave its columns
    # or indexes ahead of the component schema marker.
    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_database_compatibility(conn)
        ensure_supported_schema(
            conn,
            component=_TRANSACTION_SCHEMA_COMPONENT,
            supported_version=_TRANSACTION_SCHEMA_VERSION,
            label="transaction",
        )
        _ensure_schema_version_table(conn)
        current_version = _component_schema_version(conn, _TRANSACTION_SCHEMA_COMPONENT)
        if current_version == 0 and _table_exists(conn, "transactions"):
            # Pre-version-table private-development databases are migrated by
            # the v1 migration, which adds missing columns without rewriting
            # existing execution truth.
            current_version = 0
        if current_version < 1:
            _migrate_transactions_to_v1(conn)
            current_version = 1
        if current_version < 2:
            _migrate_transactions_to_v2(conn)
            current_version = 2
        if current_version < 3:
            _migrate_transactions_to_v3(conn)
            current_version = 3
        if current_version < 4:
            _migrate_transactions_to_v4(conn)
            current_version = 4
        if current_version < 5:
            _migrate_transactions_to_v5(conn)
            current_version = 5
        if current_version < 6:
            _migrate_transactions_to_v6(conn)
            current_version = 6
        if current_version < 7:
            _migrate_transactions_to_v7(conn)
            current_version = 7
        if current_version < 8:
            _migrate_transactions_to_v8(conn)
            current_version = 8
        if current_version != _TRANSACTION_SCHEMA_VERSION:
            raise RuntimeError(
                "Unsupported transaction schema version "
                f"{current_version}; expected {_TRANSACTION_SCHEMA_VERSION}"
            )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _skill_id(transaction_id: str, skill: Skill) -> str:
    return f"{transaction_id}:{skill.name}:{skill.execution_order}"


def _load_transaction_state(row: sqlite3.Row) -> dict[str, object]:
    try:
        state = json.loads(row["state"])
        validate_json_object(state, path="transaction.state")
    except (json.JSONDecodeError, JsonStateError) as exc:
        raise SystemException(
            f"Persisted transaction state is invalid for transaction {row['id']!r}: {exc}",
            action="repair transaction state in the persistence database",
        ) from exc
    return state


def _load_skill_arguments(
    transaction_id: str,
    row: sqlite3.Row,
) -> dict[str, object]:
    """Load one Skill argument mapping or raise actionable repair guidance."""
    try:
        arguments = json.loads(row["arguments"])
        validate_json_object(
            arguments,
            path=f"transaction.skills[{row['name']!r}].arguments",
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise SystemException(
            f"Persisted skill arguments are invalid for transaction "
            f"{transaction_id!r} skill {row['name']!r}: {exc}",
            action="repair skill arguments in the persistence database",
        ) from exc
    return arguments


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _metadata_to_storage(
    metadata: dict[str, object],
    *,
    path: str = "transaction.metadata",
) -> dict[str, str]:
    validate_json_object(metadata, path=path)
    return {key: _canonical_json(value) for key, value in metadata.items()}


def _load_metadata(transaction_id: str, rows: list[sqlite3.Row]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for row in rows:
        key = row["key"]
        try:
            value = json.loads(row["value_json"])
            validate_json_object({key: value}, path="transaction.metadata")
        except (json.JSONDecodeError, JsonStateError) as exc:
            raise SystemException(
                f"Persisted transaction metadata is invalid for transaction {transaction_id!r} "
                f"at key {key!r}: {exc}",
                action="repair transaction metadata in the persistence database",
            ) from exc
        metadata[key] = value
    return metadata


def _artifact_metadata_to_storage(artifact: Artifact, index: int) -> str:
    path = f"transaction.artifacts[{index}].metadata"
    validate_json_object(artifact.metadata, path=path)
    return _canonical_json(artifact.metadata)


def _load_artifact_metadata(
    transaction_id: str,
    artifact_id: str,
    raw_metadata: str,
) -> dict[str, object]:
    try:
        metadata = json.loads(raw_metadata)
        validate_json_object(metadata, path="artifact.metadata")
    except (json.JSONDecodeError, JsonStateError) as exc:
        raise SystemException(
            f"Persisted transaction artifact metadata is invalid for transaction "
            f"{transaction_id!r} artifact {artifact_id!r}: {exc}",
            action="repair transaction artifact metadata in the persistence database",
        ) from exc
    return metadata


def _load_artifacts(transaction_id: str, rows: list[sqlite3.Row]) -> list[Artifact]:
    artifacts: list[Artifact] = []
    for row in rows:
        artifact_id = row["id"]
        try:
            created_at = datetime.fromisoformat(row["created_at"])
        except ValueError as exc:
            raise SystemException(
                f"Persisted transaction artifact timestamp is invalid for transaction "
                f"{transaction_id!r} artifact {artifact_id!r}: {row['created_at']!r}",
                action="repair transaction artifacts in the persistence database",
            ) from exc
        artifacts.append(
            Artifact(
                id=artifact_id,
                name=row["name"],
                path=row["path"],
                kind=row["kind"],
                created_at=created_at,
                metadata=_load_artifact_metadata(
                    transaction_id,
                    artifact_id,
                    row["metadata"],
                ),
            )
        )
    return artifacts


def _is_aware_timestamp(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _timestamp_to_storage(value: datetime | None, *, legacy_value: str | None = None) -> str:
    """Return a UTC timestamp, preserving an unchanged legacy-naive value."""
    if value is None:
        return ""
    if not _is_aware_timestamp(value):
        if legacy_value:
            try:
                legacy_timestamp = datetime.fromisoformat(legacy_value)
            except ValueError:
                legacy_timestamp = None
            if legacy_timestamp is not None and not _is_aware_timestamp(legacy_timestamp):
                if value == legacy_timestamp:
                    return legacy_value
        raise ValueError("transaction timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _legacy_utc_timestamp(
    value: str | None,
    *,
    path: str,
    transaction_id: str,
) -> str:
    """Normalize a legacy timestamp for query ordering without inventing UTC."""
    if not value:
        return ""
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemException(
            f"Persisted transaction timestamp is invalid for transaction {transaction_id!r} "
            f"at {path}: {value!r}",
            action="repair transaction timestamps in the persistence database",
        ) from exc
    if not _is_aware_timestamp(timestamp):
        return ""
    return timestamp.astimezone(timezone.utc).isoformat()


def _query_timestamp(value: datetime | None, *, field: str) -> str | None:
    """Normalize one public UTC query boundary or reject an ambiguous value."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime or None")
    if not _is_aware_timestamp(value):
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _timestamp_from_storage(value: str | None, *, path: str, transaction_id: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemException(
            f"Persisted transaction timestamp is invalid for transaction {transaction_id!r} "
            f"at {path}: {value!r}",
            action="repair transaction timestamps in the persistence database",
        ) from exc


def _load_history(transaction_id: str, rows: list[sqlite3.Row]) -> list[HistoryEntry]:
    history: list[HistoryEntry] = []
    for row in rows:
        try:
            history.append(
                HistoryEntry(
                    sequence=row["sequence"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    event=HistoryEvent(row["event"]),
                    status=Status(row["status"]),
                    retry_number=row["retry_number"],
                    skill_name=row["skill_name"],
                    skill_execution_order=row["skill_execution_order"],
                )
            )
        except (TypeError, ValueError) as exc:
            raise SystemException(
                f"Persisted transaction history is invalid for transaction {transaction_id!r} "
                f"at sequence {row['sequence']!r}: {exc}",
                action="repair transaction history in the persistence database",
            ) from exc
    return history


def _serialized_transaction(
    transaction: Transaction,
) -> tuple[str, dict[str, str], list[tuple[str, int, str, str, str, str, str]]]:
    """Validate and serialize durable values before opening SQLite."""
    transaction.validate_for_execution()
    state_json = json.dumps(transaction.state)
    metadata_json = _metadata_to_storage(transaction.metadata)
    artifact_rows = [
        (
            artifact.id,
            index + 1,
            artifact.name,
            artifact.path,
            artifact.kind,
            artifact.created_at.isoformat(),
            _artifact_metadata_to_storage(artifact, index),
        )
        for index, artifact in enumerate(transaction.artifacts)
    ]
    return state_json, metadata_json, artifact_rows


def _write_transaction_rows(
    conn: sqlite3.Connection,
    transaction: Transaction,
    *,
    state_json: str,
    metadata_json: dict[str, str],
    artifact_rows: list[tuple[str, int, str, str, str, str, str]],
    expected_revision: int | None,
    queue_item_id: str = "",
    claim_token: str = "",
) -> int:
    """Write one full transaction snapshot and return its new revision."""
    legacy_values: sqlite3.Row | None = None
    timestamps = (transaction.created_at, transaction.started_at, transaction.finished_at)
    if any(value is not None and not _is_aware_timestamp(value) for value in timestamps):
        legacy_values = conn.execute(
            "SELECT created_at, started_at, finished_at FROM transactions WHERE id = ?",
            (transaction.id,),
        ).fetchone()
    created_at = _timestamp_to_storage(
        transaction.created_at,
        legacy_value=None if legacy_values is None else legacy_values["created_at"],
    )
    created_at_utc = _legacy_utc_timestamp(
        created_at,
        path="transactions.created_at",
        transaction_id=transaction.id,
    )
    started_at = _timestamp_to_storage(
        transaction.started_at,
        legacy_value=None if legacy_values is None else legacy_values["started_at"],
    )
    finished_at = _timestamp_to_storage(
        transaction.finished_at,
        legacy_value=None if legacy_values is None else legacy_values["finished_at"],
    )

    conn.execute(
        "INSERT OR IGNORE INTO transactions "
        "(id, reference, status, retry_count, outcome_category, retry_disposition, failure_code, "
        "created_at, created_at_utc, started_at, finished_at, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            transaction.id,
            transaction.reference,
            transaction.status,
            transaction.retry_count,
            transaction.outcome_category,
            transaction.retry_disposition,
            transaction.failure_code,
            created_at,
            created_at_utc,
            started_at or None,
            finished_at or None,
            state_json,
        ),
    )
    params: tuple[object, ...] = (
        transaction.reference,
        transaction.status,
        transaction.retry_count,
        transaction.outcome_category,
        transaction.retry_disposition,
        transaction.failure_code,
        created_at,
        created_at_utc,
        started_at or None,
        finished_at or None,
        state_json,
    )
    if expected_revision is None:
        result = conn.execute(
            "UPDATE transactions SET reference = ?, status = ?, retry_count = ?, "
            "outcome_category = ?, retry_disposition = ?, failure_code = ?, "
            "created_at = CASE WHEN created_at = '' THEN ? ELSE created_at END, "
            "created_at_utc = CASE WHEN created_at_utc = '' THEN ? ELSE created_at_utc END, "
            "started_at = ?, finished_at = ?, state = ?, revision = revision + 1 "
            "WHERE id = ?",
            (*params, transaction.id),
        )
    else:
        result = conn.execute(
            "UPDATE transactions SET reference = ?, status = ?, retry_count = ?, "
            "outcome_category = ?, retry_disposition = ?, failure_code = ?, "
            "created_at = CASE WHEN created_at = '' THEN ? ELSE created_at END, "
            "created_at_utc = CASE WHEN created_at_utc = '' THEN ? ELSE created_at_utc END, "
            "started_at = ?, finished_at = ?, state = ?, revision = revision + 1, "
            "queue_item_id = ?, claim_token = ? "
            "WHERE id = ? AND revision = ?",
            (*params, queue_item_id, claim_token, transaction.id, expected_revision),
        )
        if result.rowcount != 1:
            raise TransactionFenceError(
                f"Transaction {transaction.id!r} revision {expected_revision} is stale"
            )

    conn.execute("DELETE FROM skills WHERE transaction_id = ?", (transaction.id,))
    for skill in transaction.skills:
        sid = _skill_id(transaction.id, skill)
        conn.execute(
            "INSERT INTO skills "
            "(id, transaction_id, name, execution_order, status, arguments) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                sid,
                transaction.id,
                skill.name,
                skill.execution_order,
                skill.status,
                json.dumps(skill.arguments),
            ),
        )
        for exc in skill.exceptions:
            conn.execute(
                "INSERT INTO exceptions "
                "(skill_id, exception_type, message, action, retry_number, "
                "datetime_occurred, screenshot_path, stops_execution, code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    "business" if isinstance(exc, BusinessException) else "system",
                    str(exc),
                    exc.action,
                    exc.retry_number,
                    exc.datetime_occurred.isoformat(),
                    exc.screenshot_path,
                    1 if exc.stops_execution else 0,
                    exc.code,
                ),
            )
    for entry in transaction.history:
        conn.execute(
            "INSERT OR IGNORE INTO transaction_history "
            "(transaction_id, sequence, timestamp, event, status, retry_number, "
            "skill_name, skill_execution_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transaction.id,
                entry.sequence,
                entry.timestamp.isoformat(),
                entry.event,
                entry.status,
                entry.retry_number,
                entry.skill_name,
                entry.skill_execution_order,
            ),
        )
    conn.execute("DELETE FROM transaction_metadata WHERE transaction_id = ?", (transaction.id,))
    for key, value_json in metadata_json.items():
        conn.execute(
            "INSERT INTO transaction_metadata (transaction_id, key, value_json) VALUES (?, ?, ?)",
            (transaction.id, key, value_json),
        )
    conn.execute("DELETE FROM transaction_artifacts WHERE transaction_id = ?", (transaction.id,))
    for artifact_id, sequence, name, path, kind, created_at, artifact_metadata in artifact_rows:
        conn.execute(
            "INSERT INTO transaction_artifacts "
            "(transaction_id, sequence, id, name, path, kind, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transaction.id,
                sequence,
                artifact_id,
                name,
                path,
                kind,
                created_at,
                artifact_metadata,
            ),
        )
    row = conn.execute(
        "SELECT revision FROM transactions WHERE id = ?", (transaction.id,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Transaction disappeared while saving: {transaction.id!r}")
    return int(row["revision"])


def save_transaction(transaction: Transaction, db_path: str = "rpacore.db") -> None:
    """Persist a non-queue transaction snapshot unconditionally.

    Queue runners use an internal fenced save instead. Calling this function for
    a queue-bound transaction deliberately advances its revision, causing any
    concurrent queue runner with an older revision to fail closed.
    """
    state_json, metadata_json, artifact_rows = _serialized_transaction(transaction)

    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        with conn:
            _write_transaction_rows(
                conn,
                transaction,
                state_json=state_json,
                metadata_json=metadata_json,
                artifact_rows=artifact_rows,
                expected_revision=None,
            )
    finally:
        conn.close()


def _prepare_transaction_database(db_path: str) -> None:
    """Create or migrate transaction storage before a queue item is claimed."""
    conn = _connect(db_path)
    try:
        ensure_database_compatibility(conn)
        ensure_supported_schema(
            conn,
            component=_TRANSACTION_SCHEMA_COMPONENT,
            supported_version=_TRANSACTION_SCHEMA_VERSION,
            label="transaction",
        )
        configure_rollback_journal(conn)
        _ensure_schema(conn)
    finally:
        conn.close()


def _load_transaction_revision(transaction_id: str, db_path: str) -> int:
    """Return the current durable revision for a bound queue transaction."""
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT revision FROM transactions WHERE id = ?", (transaction_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Transaction not found: {transaction_id!r}")
        return int(row["revision"])
    finally:
        conn.close()


def _qualified_queue_schema_version(conn: sqlite3.Connection, schema: str) -> int:
    table = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master "
        "WHERE type = 'table' AND name = ?",
        (SCHEMA_VERSION_TABLE,),
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute(
        f"SELECT version FROM {schema}.{SCHEMA_VERSION_TABLE} WHERE component = 'queue'"
    ).fetchone()
    return 0 if row is None else int(row["version"])


def _save_queue_transaction_fenced(
    transaction: Transaction,
    *,
    db_path: str,
    queue_db_path: str,
    queue_item_id: str,
    claimed_by: str,
    claim_token: str,
    expected_revision: int,
) -> int:
    """Atomically validate a queue claim and persist one transaction snapshot."""
    if not claim_token:
        raise TransactionFenceError(
            f"Queue item {queue_item_id!r} has no claim credential"
        )
    if expected_revision < 0:
        raise ValueError(f"expected_revision must be >= 0, got {expected_revision}")
    state_json, metadata_json, artifact_rows = _serialized_transaction(transaction)

    transaction_path = Path(db_path).resolve()
    queue_path = Path(queue_db_path).resolve()
    conn = _connect(str(transaction_path))
    transaction_started = False
    try:
        _ensure_schema(conn)
        transaction_journal_mode = str(
            conn.execute("PRAGMA main.journal_mode").fetchone()[0]
        ).lower()
        if transaction_journal_mode != "delete":
            raise RuntimeError(
                "Queue claim fencing requires transaction rollback journal mode; "
                f"got {transaction_journal_mode!r}"
            )
        queue_schema = "main"
        if queue_path != transaction_path:
            conn.execute("ATTACH DATABASE ? AS queue_guard", (str(queue_path),))
            queue_schema = "queue_guard"

        queue_version = _qualified_queue_schema_version(conn, queue_schema)
        if queue_version != QUEUE_SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported queue schema version {queue_version}; "
                f"expected {QUEUE_SCHEMA_VERSION}; migrate queue and transaction databases offline"
            )
        journal_mode = str(
            conn.execute(f"PRAGMA {queue_schema}.journal_mode").fetchone()[0]
        ).lower()
        if journal_mode != "delete":
            raise RuntimeError(
                f"Queue claim fencing requires rollback journal mode; got {journal_mode!r}"
            )

        conn.execute("BEGIN IMMEDIATE")
        transaction_started = True
        claim = conn.execute(
            f"SELECT 1 FROM {queue_schema}.queue_items "
            "WHERE id = ? AND status = 'in_progress' AND claimed_by = ? "
            "AND claim_token = ? AND claim_token != '' "
            "AND (transaction_id = '' OR transaction_id = ?)",
            (queue_item_id, claimed_by, claim_token, transaction.id),
        ).fetchone()
        if claim is None:
            raise TransactionFenceError(
                f"Queue item {queue_item_id!r} claim is stale or bound to another transaction"
            )
        new_revision = _write_transaction_rows(
            conn,
            transaction,
            state_json=state_json,
            metadata_json=metadata_json,
            artifact_rows=artifact_rows,
            expected_revision=expected_revision,
            queue_item_id=queue_item_id,
            claim_token=claim_token,
        )
        conn.execute("COMMIT")
        transaction_started = False
        return new_revision
    except Exception:
        if transaction_started:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _delete_unbound_pending_transaction(
    transaction_id: str,
    *,
    db_path: str,
) -> None:
    """Delete one unstarted pending transaction created before queue binding."""
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        with conn:
            row = conn.execute(
                "SELECT status FROM transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                return
            has_history = conn.execute(
                "SELECT 1 FROM transaction_history WHERE transaction_id = ? LIMIT 1",
                (transaction_id,),
            ).fetchone()
            if row["status"] != Status.PENDING or has_history is not None:
                raise RuntimeError(
                    "Refusing to delete transaction "
                    f"{transaction_id!r}; expected pending status with no history"
                )
            conn.execute(
                "DELETE FROM exceptions WHERE skill_id IN "
                "(SELECT id FROM skills WHERE transaction_id = ?)",
                (transaction_id,),
            )
            conn.execute(
                "DELETE FROM skills WHERE transaction_id = ?",
                (transaction_id,),
            )
            conn.execute(
                "DELETE FROM transaction_history WHERE transaction_id = ?",
                (transaction_id,),
            )
            conn.execute(
                "DELETE FROM transaction_metadata WHERE transaction_id = ?",
                (transaction_id,),
            )
            conn.execute(
                "DELETE FROM transaction_artifacts WHERE transaction_id = ?",
                (transaction_id,),
            )
            conn.execute(
                "DELETE FROM transactions WHERE id = ?",
                (transaction_id,),
            )
    finally:
        conn.close()


def load_transaction(
    transaction_id: str,
    db_path: str = "rpacore.db",
    *,
    readonly: bool = False,
) -> Transaction:
    """Load a transaction from the database without mutating persisted state."""
    conn = _connect(db_path, readonly=readonly)
    try:
        if readonly:
            ensure_database_compatibility(conn)
            require_current_schema(
                conn,
                component=_TRANSACTION_SCHEMA_COMPONENT,
                supported_version=_TRANSACTION_SCHEMA_VERSION,
                label="transaction",
            )
        else:
            _ensure_schema(conn)
        row = conn.execute(
            "SELECT id, reference, status, retry_count, outcome_category, retry_disposition, "
            "failure_code, created_at, started_at, finished_at, state "
            "FROM transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Transaction not found: {transaction_id!r}")
        transaction_state = _load_transaction_state(row)

        skill_rows = conn.execute(
                "SELECT id, name, execution_order, status, arguments FROM skills "
            "WHERE transaction_id = ? ORDER BY execution_order",
            (transaction_id,),
        ).fetchall()

        skills: list[Skill] = []
        for sr in skill_rows:
            skill = Skill(
                sr["name"],
                sr["execution_order"],
                arguments=_load_skill_arguments(transaction_id, sr),
            )
            skill.status = Status(sr["status"])

            exc_rows = conn.execute(
                "SELECT exception_type, message, action, retry_number, datetime_occurred, "
                "screenshot_path, stops_execution, code FROM exceptions WHERE skill_id = ? ORDER BY id",
                (sr["id"],),
            ).fetchall()
            for er in exc_rows:
                dt = datetime.fromisoformat(er["datetime_occurred"])
                screenshot = er["screenshot_path"]
                if er["exception_type"] == "business":
                    exc: BusinessException | SystemException = BusinessException(
                        er["message"],
                        action=er["action"],
                        retry_number=er["retry_number"],
                        datetime_occurred=dt,
                        screenshot_path=screenshot,
                        stop=bool(er["stops_execution"]),
                        code=er["code"],
                    )
                else:
                    exc = SystemException(
                        er["message"],
                        action=er["action"],
                        retry_number=er["retry_number"],
                        datetime_occurred=dt,
                        screenshot_path=screenshot,
                        code=er["code"],
                    )
                skill.exceptions.append(exc)
            skills.append(skill)

        tx_status = Status(row["status"])

        history_rows = conn.execute(
            "SELECT sequence, timestamp, event, status, retry_number, "
            "skill_name, skill_execution_order FROM transaction_history "
            "WHERE transaction_id = ? ORDER BY sequence",
            (transaction_id,),
        ).fetchall()
        history = _load_history(transaction_id, history_rows)
        metadata_rows = conn.execute(
            "SELECT key, value_json FROM transaction_metadata "
            "WHERE transaction_id = ? ORDER BY key",
            (transaction_id,),
        ).fetchall()
        metadata = _load_metadata(transaction_id, metadata_rows)
        artifact_rows = conn.execute(
            "SELECT id, name, path, kind, created_at, metadata FROM transaction_artifacts "
            "WHERE transaction_id = ? ORDER BY sequence",
            (transaction_id,),
        ).fetchall()
        artifacts = _load_artifacts(transaction_id, artifact_rows)

        return Transaction(
            reference=row["reference"],
            id=row["id"],
            status=tx_status,
            retry_count=row["retry_count"],
            outcome_category=OutcomeCategory(row["outcome_category"]),
            retry_disposition=RetryDisposition(row["retry_disposition"]),
            failure_code=row["failure_code"],
            created_at=_timestamp_from_storage(
                row["created_at"], path="transactions.created_at", transaction_id=transaction_id
            ),
            started_at=_timestamp_from_storage(
                row["started_at"], path="transactions.started_at", transaction_id=transaction_id
            ),
            finished_at=_timestamp_from_storage(
                row["finished_at"], path="transactions.finished_at", transaction_id=transaction_id
            ),
            state=transaction_state,
            metadata=metadata,
            artifacts=artifacts,
            skills=skills,
            history=history,
        )
    finally:
        conn.close()


def _query_statuses(statuses: Iterable[Status] | None) -> tuple[str, ...]:
    if statuses is None:
        return ()
    if isinstance(statuses, (str, Status)):
        raise TypeError("statuses must be an iterable of Status values")
    values: set[str] = set()
    for status in statuses:
        if not isinstance(status, Status):
            raise TypeError("statuses must contain only Status values")
        values.add(str(status))
    if not values:
        raise ValueError("statuses must not be empty; pass None for every status")
    return tuple(sorted(values))


def _query_filter_fingerprint(
    *,
    statuses: tuple[str, ...],
    since: str | None,
    until: str | None,
    reference: str | None,
    metadata: dict[str, str],
) -> str:
    serialized = json.dumps(
        {
            "metadata": metadata,
            "reference": reference,
            "since": since,
            "statuses": statuses,
            "until": until,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _encode_query_cursor(*, created_at_utc: str, transaction_id: str, filters: str) -> str:
    payload = json.dumps(
        {
            "created_at_utc": created_at_utc,
            "filters": filters,
            "id": transaction_id,
            "version": _QUERY_PAGE_FORMAT_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_query_cursor(cursor: str, *, filters: str) -> tuple[str, str]:
    if not isinstance(cursor, str) or not cursor:
        raise ValueError("cursor must be a non-empty query cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (binascii.Error, UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is not a valid transaction query cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("cursor is not a valid transaction query cursor")
    if payload.get("version") != _QUERY_PAGE_FORMAT_VERSION:
        raise ValueError("cursor has an unsupported transaction query version")
    if payload.get("filters") != filters:
        raise ValueError("cursor does not match the transaction query filters")
    created_at_utc = payload.get("created_at_utc")
    transaction_id = payload.get("id")
    if not isinstance(created_at_utc, str) or not isinstance(transaction_id, str):
        raise ValueError("cursor is not a valid transaction query cursor")
    return created_at_utc, transaction_id


def query_transactions(
    db_path: str = "rpacore.db",
    *,
    statuses: Iterable[Status] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    reference: str | None = None,
    metadata_filter: dict[str, object] | None = None,
    cursor: str | None = None,
    limit: int = 100,
    readonly: bool = True,
) -> TransactionPage:
    """Return one deterministic, read-only page of lightweight transactions.

    Results sort by normalized UTC creation time descending, then transaction id
    ascending. A cursor fixes its filter set and continues after the last item
    in that ordering. The query snapshots one page only: inserts before the
    cursor are not included in later pages, while inserts after it may appear.
    Timezone-naive legacy timestamps remain loadable but have no UTC query key;
    they sort after timestamped rows and do not match time-window filters.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer from 1 through 1000")
    if limit < 1 or limit > _QUERY_MAX_LIMIT:
        raise ValueError("limit must be an integer from 1 through 1000")
    if reference is not None and not isinstance(reference, str):
        raise TypeError("reference must be str or None")

    normalized_statuses = _query_statuses(statuses)
    normalized_since = _query_timestamp(since, field="since")
    normalized_until = _query_timestamp(until, field="until")
    if normalized_since is not None and normalized_until is not None:
        if normalized_since > normalized_until:
            raise ValueError("since must be earlier than or equal to until")
    metadata_json = (
        _metadata_to_storage(metadata_filter, path="metadata_filter")
        if metadata_filter is not None
        else {}
    )
    filters = _query_filter_fingerprint(
        statuses=normalized_statuses,
        since=normalized_since,
        until=normalized_until,
        reference=reference,
        metadata=metadata_json,
    )
    cursor_position = None if cursor is None else _decode_query_cursor(cursor, filters=filters)

    conn = _connect(db_path, readonly=readonly)
    try:
        if readonly:
            ensure_database_compatibility(conn)
            require_current_schema(
                conn,
                component=_TRANSACTION_SCHEMA_COMPONENT,
                supported_version=_TRANSACTION_SCHEMA_VERSION,
                label="transaction",
            )
        else:
            _ensure_schema(conn)

        query = "SELECT id, reference, status, retry_count, created_at_utc FROM transactions"
        params: list[object] = []
        for index, (key, value_json) in enumerate(sorted(metadata_json.items())):
            alias = f"tm_{index}"
            query += (
                f" JOIN transaction_metadata {alias} ON {alias}.transaction_id = transactions.id "
                f"AND {alias}.key = ? AND {alias}.value_json = ?"
            )
            params.extend([key, value_json])
        query += " WHERE 1=1"
        if normalized_statuses:
            placeholders = ", ".join("?" for _ in normalized_statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(normalized_statuses)
        if normalized_since is not None or normalized_until is not None:
            query += " AND created_at_utc != ''"
        if normalized_since is not None:
            query += " AND created_at_utc >= ?"
            params.append(normalized_since)
        if normalized_until is not None:
            query += " AND created_at_utc <= ?"
            params.append(normalized_until)
        if reference is not None:
            query += " AND reference = ?"
            params.append(reference)
        if cursor_position is not None:
            cursor_created_at, cursor_id = cursor_position
            query += (
                " AND (created_at_utc < ? OR (created_at_utc = ? AND id > ?))"
            )
            params.extend([cursor_created_at, cursor_created_at, cursor_id])
        query += " ORDER BY created_at_utc DESC, id ASC LIMIT ?"
        params.append(limit + 1)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    transactions = tuple(
        TransactionSummary(
            id=row["id"],
            reference=row["reference"],
            status=Status(row["status"]),
            retry_count=row["retry_count"],
            created_at=_timestamp_from_storage(
                row["created_at_utc"],
                path="transactions.created_at_utc",
                transaction_id=row["id"],
            ),
        )
        for row in page_rows
    )
    next_cursor = None
    if has_more and page_rows:
        last_row = page_rows[-1]
        next_cursor = _encode_query_cursor(
            created_at_utc=last_row["created_at_utc"],
            transaction_id=last_row["id"],
            filters=filters,
        )
    return TransactionPage(
        transactions=transactions,
        has_more=has_more,
        next_cursor=next_cursor,
    )


def list_transactions(
    db_path: str = "rpacore.db",
    *,
    status: Status | None = None,
    since: datetime | None = None,
    metadata_filter: dict[str, object] | None = None,
    limit: int = 100,
    readonly: bool = False,
) -> list[Transaction]:
    """Return transactions matching optional filters, newest first.

    Args:
        db_path: Path to the SQLite database.
        status:  Only return transactions with this status. Returns all if None.
        since:   Only return transactions created at or after this datetime.
        metadata_filter:
                  Exact-match filter for top-level transaction metadata values.
        limit:   Maximum number of results. Defaults to 100.

    A selected transaction removed by concurrent cleanup before its deferred
    load is omitted. Every other load failure propagates.
    """
    metadata_json = (
        _metadata_to_storage(metadata_filter, path="metadata_filter")
        if metadata_filter is not None
        else {}
    )
    conn = _connect(db_path, readonly=readonly)
    try:
        if readonly:
            ensure_database_compatibility(conn)
            require_current_schema(
                conn,
                component=_TRANSACTION_SCHEMA_COMPONENT,
                supported_version=_TRANSACTION_SCHEMA_VERSION,
                label="transaction",
            )
        else:
            _ensure_schema(conn)
        query = "SELECT id FROM transactions WHERE 1=1"
        params: list[object] = []
        if status is not None:
            query += " AND status = ?"
            params.append(str(status))
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since.isoformat())
        for key, value_json in sorted(metadata_json.items()):
            query += (
                " AND EXISTS ("
                "SELECT 1 FROM transaction_metadata tm "
                "WHERE tm.transaction_id = transactions.id "
                "AND tm.key = ? AND tm.value_json = ?)"
            )
            params.extend([key, value_json])
        query += " ORDER BY created_at DESC, id ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        transaction_ids = [row["id"] for row in rows]
    finally:
        conn.close()

    transactions: list[Transaction] = []
    for transaction_id in transaction_ids:
        try:
            transaction = load_transaction(
                transaction_id,
                db_path,
                readonly=readonly,
            )
        except KeyError:
            continue
        transactions.append(transaction)
    return transactions


def iter_transactions(
    db_path: str = "rpacore.db",
    *,
    status: Status | None = None,
    since: datetime | None = None,
    metadata_filter: dict[str, object] | None = None,
    readonly: bool = False,
) -> Iterator[Transaction]:
    """Yield a fixed matching transaction set, newest first.

    Matching identifiers are snapshotted and the inspection connection is
    closed before the first transaction is yielded. Inserts after iteration
    starts are excluded, while updates to a snapshotted transaction are visible
    when that transaction is loaded. A transaction deleted before its deferred
    load is omitted. Every other load failure propagates.
    """
    metadata_json = (
        _metadata_to_storage(metadata_filter, path="metadata_filter")
        if metadata_filter is not None
        else {}
    )
    conn = _connect(db_path, readonly=readonly)
    try:
        if readonly:
            ensure_database_compatibility(conn)
            require_current_schema(
                conn,
                component=_TRANSACTION_SCHEMA_COMPONENT,
                supported_version=_TRANSACTION_SCHEMA_VERSION,
                label="transaction",
            )
        else:
            _ensure_schema(conn)
        query = "SELECT id FROM transactions WHERE 1=1"
        params: list[object] = []
        if status is not None:
            query += " AND status = ?"
            params.append(str(status))
        if since is not None:
            query += " AND created_at >= ?"
            params.append(since.isoformat())
        for key, value_json in sorted(metadata_json.items()):
            query += (
                " AND EXISTS ("
                "SELECT 1 FROM transaction_metadata tm "
                "WHERE tm.transaction_id = transactions.id "
                "AND tm.key = ? AND tm.value_json = ?)"
            )
            params.extend([key, value_json])
        query += " ORDER BY created_at DESC, id ASC"
        transaction_ids = [row["id"] for row in conn.execute(query, params)]
    finally:
        conn.close()

    for transaction_id in transaction_ids:
        try:
            transaction = load_transaction(
                transaction_id,
                db_path,
                readonly=readonly,
            )
        except KeyError:
            continue
        yield transaction
