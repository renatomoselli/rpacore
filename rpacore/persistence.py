"""Persistence — SQLite-backed save/load for transactions and skills."""

import json
import sqlite3
from datetime import datetime

from rpacore._json_state import JsonStateError, validate_json_object
from rpacore.exceptions import BusinessException, SystemException
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import HistoryEntry, HistoryEvent, Transaction


_SCHEMA_TABLE = "rpacore_schema_versions"
_TRANSACTION_SCHEMA_COMPONENT = "transactions"
_TRANSACTION_SCHEMA_VERSION = 4


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=1)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


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
    row = conn.execute(
        f"SELECT version FROM {_SCHEMA_TABLE} WHERE component = ?",
        (component,),
    ).fetchone()
    return 0 if row is None else int(row["version"])


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


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Run explicit transaction schema migrations through the latest version."""
    with conn:
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
        if current_version != _TRANSACTION_SCHEMA_VERSION:
            raise RuntimeError(
                "Unsupported transaction schema version "
                f"{current_version}; expected {_TRANSACTION_SCHEMA_VERSION}"
            )


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


def _timestamp_to_storage(value: datetime | None) -> str:
    return "" if value is None else value.isoformat()


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


def save_transaction(transaction: Transaction, db_path: str = "rpacore.db") -> None:
    """Persist a transaction and all its skills and exceptions.

    Safe to call multiple times. Skills are deleted and reinserted on each save,
    so removed or reordered skills are correctly reflected.
    """
    validate_json_object(transaction.state, path="transaction.state")
    state_json = json.dumps(transaction.state)
    metadata_json = _metadata_to_storage(transaction.metadata)

    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        with conn:
            # INSERT OR IGNORE creates the row when needed; the UPDATE below
            # preserves an existing created_at unless a legacy row needs backfill.
            created_at = _timestamp_to_storage(transaction.created_at)
            started_at = _timestamp_to_storage(transaction.started_at)
            finished_at = _timestamp_to_storage(transaction.finished_at)

            conn.execute(
                "INSERT OR IGNORE INTO transactions "
                "(id, reference, status, retry_count, created_at, started_at, finished_at, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transaction.id,
                    transaction.reference,
                    transaction.status,
                    transaction.retry_count,
                    created_at,
                    started_at or None,
                    finished_at or None,
                    state_json,
                ),
            )
            conn.execute(
                "UPDATE transactions SET reference = ?, status = ?, retry_count = ?, "
                "created_at = CASE WHEN created_at = '' THEN ? ELSE created_at END, "
                "started_at = ?, finished_at = ?, "
                "state = ? "
                "WHERE id = ?",
                (
                    transaction.reference,
                    transaction.status,
                    transaction.retry_count,
                    created_at,
                    started_at or None,
                    finished_at or None,
                    state_json,
                    transaction.id,
                ),
            )
            # Delete all existing skills (cascades to exceptions via ON DELETE CASCADE).
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
                        "datetime_occurred, screenshot_path, stops_execution) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            sid,
                            "business" if isinstance(exc, BusinessException) else "system",
                            str(exc),
                            exc.action,
                            exc.retry_number,
                            exc.datetime_occurred.isoformat(),
                            exc.screenshot_path,
                            1 if exc.stops_execution else 0,
                        ),
                    )
            for entry in transaction.history:
                conn.execute(
                    "INSERT OR IGNORE INTO transaction_history "
                    "(transaction_id, sequence, timestamp, event, status, retry_number, "
                    "skill_name, skill_execution_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
                    "INSERT INTO transaction_metadata (transaction_id, key, value_json) "
                    "VALUES (?, ?, ?)",
                    (transaction.id, key, value_json),
                )
    finally:
        conn.close()


def load_transaction(transaction_id: str, db_path: str = "rpacore.db") -> Transaction:
    """Load a transaction from the database without mutating persisted state."""
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT id, reference, status, retry_count, created_at, started_at, finished_at, state "
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
            skill = Skill(sr["name"], sr["execution_order"], arguments=json.loads(sr["arguments"]))
            skill.status = Status(sr["status"])

            exc_rows = conn.execute(
                "SELECT exception_type, message, action, retry_number, datetime_occurred, "
                "screenshot_path, stops_execution FROM exceptions WHERE skill_id = ? ORDER BY id",
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
                    )
                else:
                    exc = SystemException(
                        er["message"],
                        action=er["action"],
                        retry_number=er["retry_number"],
                        datetime_occurred=dt,
                        screenshot_path=screenshot,
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

        return Transaction(
            reference=row["reference"],
            id=row["id"],
            status=tx_status,
            retry_count=row["retry_count"],
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
            skills=skills,
            history=history,
        )
    finally:
        conn.close()


def list_transactions(
    db_path: str = "rpacore.db",
    *,
    status: Status | None = None,
    since: datetime | None = None,
    metadata_filter: dict[str, object] | None = None,
    limit: int = 100,
) -> list[Transaction]:
    """Return transactions matching optional filters, newest first.

    Args:
        db_path: Path to the SQLite database.
        status:  Only return transactions with this status. Returns all if None.
        since:   Only return transactions created at or after this datetime.
        metadata_filter:
                  Exact-match filter for top-level transaction metadata values.
        limit:   Maximum number of results. Defaults to 100.
    """
    metadata_json = (
        _metadata_to_storage(metadata_filter, path="metadata_filter")
        if metadata_filter is not None
        else {}
    )
    conn = _connect(db_path)
    try:
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

    return [load_transaction(tx_id, db_path) for tx_id in transaction_ids]
