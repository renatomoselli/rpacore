"""Shared concrete SQLite connection and compatibility policy."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rpacore._validation import type_error, value_error


SCHEMA_VERSION_TABLE = "rpacore_schema_versions"
TRANSACTION_SCHEMA_VERSION = 5
QUEUE_SCHEMA_VERSION = 2
_SUPPORTED_SCHEMA_COMPONENTS = {
    "transactions": (TRANSACTION_SCHEMA_VERSION, "transaction"),
    "queue": (QUEUE_SCHEMA_VERSION, "queue"),
}


def validate_durable_sqlite_path(db_path: str, *, field: str) -> str:
    """Return a durable SQLite path or reject transient multi-connection paths."""
    if not isinstance(db_path, str):
        raise type_error(field, "str", db_path)
    if not db_path.strip():
        raise value_error(field, "non-empty SQLite file path", db_path)
    if db_path == ":memory:":
        raise value_error(field, "SQLite file path (not :memory:)", db_path)
    return db_path


def connect_sqlite(
    db_path: str,
    *,
    field: str,
    readonly: bool = False,
) -> sqlite3.Connection:
    """Open a validated SQLite file with common connection-local safety settings."""
    validated = validate_durable_sqlite_path(db_path, field=field)
    if readonly:
        resolved = Path(validated).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"SQLite database not found: {resolved}")
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro",
            uri=True,
            timeout=1,
        )
    else:
        connection = sqlite3.connect(validated, timeout=1)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


def component_schema_version(
    connection: sqlite3.Connection,
    component: str,
) -> int:
    """Read a component version without creating or changing schema objects."""
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (SCHEMA_VERSION_TABLE,),
    ).fetchone()
    if table_exists is None:
        return 0
    row = connection.execute(
        f"SELECT version FROM {SCHEMA_VERSION_TABLE} WHERE component = ?",
        (component,),
    ).fetchone()
    return 0 if row is None else int(row["version"])


def ensure_database_compatibility(connection: sqlite3.Connection) -> None:
    """Reject any future or unknown framework component before file mutation."""
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (SCHEMA_VERSION_TABLE,),
    ).fetchone()
    if table_exists is None:
        return
    rows = connection.execute(
        f"SELECT component, version FROM {SCHEMA_VERSION_TABLE} ORDER BY component"
    ).fetchall()
    for row in rows:
        component = str(row["component"])
        version = int(row["version"])
        supported = _SUPPORTED_SCHEMA_COMPONENTS.get(component)
        if supported is None:
            raise RuntimeError(
                f"Unsupported SQLite schema component {component!r}; "
                "this database requires a newer RPA Core version"
            )
        supported_version, label = supported
        if version > supported_version:
            raise RuntimeError(
                f"Unsupported {label} schema version {version}; "
                f"expected at most {supported_version}"
            )


def ensure_supported_schema(
    connection: sqlite3.Connection,
    *,
    component: str,
    supported_version: int,
    label: str | None = None,
) -> int:
    """Reject future component schemas before any migration or journal mutation."""
    current_version = component_schema_version(connection, component)
    if current_version > supported_version:
        display_name = label if label is not None else component
        raise RuntimeError(
            f"Unsupported {display_name} schema version {current_version}; "
            f"expected at most {supported_version}"
        )
    return current_version


def require_current_schema(
    connection: sqlite3.Connection,
    *,
    component: str,
    supported_version: int,
    label: str | None = None,
) -> None:
    """Require an inspection database to already use the current schema."""
    current_version = component_schema_version(connection, component)
    if current_version != supported_version:
        display_name = label if label is not None else component
        raise RuntimeError(
            f"Unsupported {display_name} schema version {current_version}; "
            f"expected {supported_version}; inspection does not migrate databases"
        )


def configure_rollback_journal(connection: sqlite3.Connection) -> str:
    """Use rollback journal until supported runtimes contain the WAL-reset fix."""
    journal_mode = str(
        connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    ).lower()
    if journal_mode != "delete":
        raise RuntimeError(
            f"Could not configure safe SQLite rollback journal; got {journal_mode!r}"
        )
    return journal_mode
