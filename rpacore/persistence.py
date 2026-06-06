"""Persistence — SQLite-backed save/load for transactions and skills."""

import json
import sqlite3
from datetime import datetime, timezone

from rpacore._json_state import JsonStateError, validate_json_object
from rpacore.exceptions import BusinessException, SystemException
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction


_SCHEMA_TABLE = "rpacore_schema_versions"
_TRANSACTION_SCHEMA_COMPONENT = "transactions"
_TRANSACTION_SCHEMA_VERSION = 2


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
    conn.execute(
        "UPDATE transactions SET created_at = ? WHERE created_at = ''",
        (datetime.now(timezone.utc).isoformat(),),
    )
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


def save_transaction(transaction: Transaction, db_path: str = "rpacore.db") -> None:
    """Persist a transaction and all its skills and exceptions.

    Safe to call multiple times. Skills are deleted and reinserted on each save,
    so removed or reordered skills are correctly reflected.
    """
    validate_json_object(transaction.state, path="transaction.state")
    state_json = json.dumps(transaction.state)

    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        now_iso = datetime.now(timezone.utc).isoformat()
        with conn:
            # INSERT OR IGNORE creates the row when needed; the UPDATE below
            # preserves an existing created_at unless a legacy row needs backfill.
            conn.execute(
                "INSERT OR IGNORE INTO transactions "
                "(id, reference, status, retry_count, created_at, state) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    transaction.id,
                    transaction.reference,
                    transaction.status,
                    transaction.retry_count,
                    now_iso,
                    state_json,
                ),
            )
            conn.execute(
                "UPDATE transactions SET reference = ?, status = ?, retry_count = ?, "
                "created_at = CASE WHEN created_at = '' THEN ? ELSE created_at END, "
                "state = ? "
                "WHERE id = ?",
                (
                    transaction.reference,
                    transaction.status,
                    transaction.retry_count,
                    now_iso,
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
    finally:
        conn.close()


def load_transaction(transaction_id: str, db_path: str = "rpacore.db") -> Transaction:
    """Load a transaction from the database.

    Crash recovery: any skill with status IN_PROGRESS is reset to FAILED,
    because an in-progress skill at load time means the process was interrupted.
    """
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT id, reference, status, retry_count, state FROM transactions WHERE id = ?",
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
            raw_status = sr["status"]
            # Crash recovery: IN_PROGRESS at load time means the process was interrupted.
            skill.status = Status.FAILED if raw_status == Status.IN_PROGRESS else Status(raw_status)

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
        if tx_status == Status.IN_PROGRESS:
            tx_status = Status.FAILED

        return Transaction(
            reference=row["reference"],
            id=row["id"],
            status=tx_status,
            retry_count=row["retry_count"],
            state=transaction_state,
            skills=skills,
        )
    finally:
        conn.close()


def list_transactions(
    db_path: str = "rpacore.db",
    *,
    status: Status | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[Transaction]:
    """Return transactions matching optional filters, newest first.

    Args:
        db_path: Path to the SQLite database.
        status:  Only return transactions with this status. Returns all if None.
        since:   Only return transactions created at or after this datetime.
        limit:   Maximum number of results. Defaults to 100.
    """
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
        query += " ORDER BY created_at DESC, id ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        transaction_ids = [row["id"] for row in rows]
    finally:
        conn.close()

    return [load_transaction(tx_id, db_path) for tx_id in transaction_ids]
