"""Focused tests for the shared SQLite schema helpers in rpacore._sqlite."""

import sqlite3
from contextlib import closing

import pytest

from rpacore import QueueItem, SqliteQueue, Transaction, save_transaction
from rpacore._sqlite import ensure_column, table_columns


# Stored text pinned by P9-161 criterion 2 (plan Problem table), not derived
# from the helper implementation.
_EXPECTED_VERSION_DDL = (
    "CREATE TABLE rpacore_schema_versions (\n"
    "            component TEXT PRIMARY KEY,\n"
    "            version   INTEGER NOT NULL\n"
    "        )"
)


def _stored_version_ddl(db_path: str) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'rpacore_schema_versions'"
        ).fetchone()
    assert row is not None
    return str(row[0])


def _version_rows(db_path: str) -> set[tuple[str, int]]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {
            (str(component), int(version))
            for component, version in conn.execute(
                "SELECT component, version FROM rpacore_schema_versions"
            )
        }


def _alter_statements(statements: list[str]) -> list[str]:
    return [statement for statement in statements if statement.upper().startswith("ALTER TABLE")]


@pytest.mark.parametrize(
    "case",
    ["queue_only", "transactions_only", "queue_then_transactions"],
)
def test_version_table_ddl_is_unchanged(tmp_path, case):
    db_path = str(tmp_path / "schema.db")

    if case in ("queue_only", "queue_then_transactions"):
        SqliteQueue({"db_path": db_path}).add(QueueItem(reference="ref", payload={}))
    if case in ("transactions_only", "queue_then_transactions"):
        save_transaction(Transaction(reference="ref"), db_path)

    expected_rows = {
        "queue_only": {("queue", 5)},
        "transactions_only": {("transactions", 10)},
        "queue_then_transactions": {("queue", 5), ("transactions", 10)},
    }[case]

    assert _stored_version_ddl(db_path) == _EXPECTED_VERSION_DDL
    assert _version_rows(db_path) == expected_rows


def test_ensure_column_existing_column_runs_no_alter(tmp_path):
    db_path = str(tmp_path / "columns.db")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.execute("INSERT INTO t (a) VALUES ('x')")

        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        ensure_column(conn, "t", "a", "a TEXT")

        assert _alter_statements(statements) == []
        assert table_columns(conn, "t") == {"a"}


def test_ensure_column_missing_column_alters_once_then_noop(tmp_path):
    db_path = str(tmp_path / "columns.db")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.execute("INSERT INTO t (a) VALUES ('x')")

        statements: list[str] = []
        conn.set_trace_callback(statements.append)

        ensure_column(conn, "t", "b", "b TEXT NOT NULL DEFAULT ''")
        assert _alter_statements(statements) == [
            "ALTER TABLE t ADD COLUMN b TEXT NOT NULL DEFAULT ''"
        ]
        assert table_columns(conn, "t") == {"a", "b"}
        assert conn.execute("SELECT b FROM t").fetchone()[0] == ""

        statements.clear()
        ensure_column(conn, "t", "b", "b TEXT NOT NULL DEFAULT ''")
        assert _alter_statements(statements) == []


@pytest.mark.parametrize(
    "row_factory",
    [None, sqlite3.Row],
    ids=["plain_rows", "row_factory_rows"],
)
def test_table_columns_reads_names_with_any_row_factory(tmp_path, row_factory):
    db_path = str(tmp_path / "factory.db")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE t (a TEXT)")
        if row_factory is not None:
            conn.row_factory = row_factory
        assert table_columns(conn, "t") == {"a"}
