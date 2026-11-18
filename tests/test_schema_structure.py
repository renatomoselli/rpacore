"""Structural comparison of a current database against the migrations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import rpacore.persistence as persistence_module
import rpacore.queue as queue_module
from rpacore import QueueItem, SqliteQueue, Transaction, save_transaction
from rpacore._schema_structure import (
    SchemaComponent,
    _reference_structure,
    missing_schema_objects,
)
from rpacore._sqlite import connect_sqlite
from tests.test_queue_retry import legacy_database


ENUMERATION_ANCHORS = {
    "queue": {"queue_items.claim_token", "queue_items.available_at", "queue_poison_events"},
    "transactions": {"transaction_artifacts", "transactions.definition_identity"},
}


def public_database(tmp_path: Path, component: SchemaComponent) -> Path:
    """Create a database through the public queue or transaction path."""
    db_path = tmp_path / f"{component}.db"
    if component == "queue":
        SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="structure", payload={}))
    else:
        save_transaction(Transaction(reference="structure"), str(db_path))
    return db_path


def catalogue(connection: sqlite3.Connection) -> dict[str, frozenset[str]]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        )
    ]
    return {
        table: frozenset(row[0] for row in connection.execute("SELECT name FROM pragma_table_info(?)", (table,)))
        for table in tables
    }


def memory_copy(db_path: Path) -> sqlite3.Connection:
    copy = sqlite3.connect(":memory:")
    with closing(sqlite3.connect(db_path)) as source:
        source.backup(copy)
    return copy


@pytest.mark.parametrize("component", ["queue", "transactions"])
def test_public_database_matches_reference(tmp_path: Path, component: SchemaComponent) -> None:
    db_path = public_database(tmp_path, component)

    with closing(sqlite3.connect(db_path)) as connection:
        assert catalogue(connection) == _reference_structure(component)
        assert missing_schema_objects(connection, component) is False


@pytest.mark.parametrize("component", ["queue", "transactions"])
def test_every_dropped_reference_object_is_detected(tmp_path: Path, component: SchemaComponent) -> None:
    db_path = public_database(tmp_path, component)
    with closing(sqlite3.connect(db_path)) as connection:
        public = catalogue(connection)
    assert public
    enumerated = set(public) | {f"{table}.{column}" for table, columns in public.items() for column in columns}
    assert ENUMERATION_ANCHORS[component] <= enumerated

    undetected: list[str] = []
    for table, columns in public.items():
        with closing(memory_copy(db_path)) as copy:
            copy.execute(f'DROP TABLE "{table}"')
            if missing_schema_objects(copy, component) is not True:
                undetected.append(table)
        for column in sorted(columns):
            kept = ", ".join(f'"{name}"' for name in sorted(columns - {column}))
            with closing(memory_copy(db_path)) as copy:
                # Rebuild without the column: DROP COLUMN refuses indexed or constrained columns.
                copy.execute(f'CREATE TABLE "{table}_rebuilt" AS SELECT {kept} FROM "{table}"')
                copy.execute(f'DROP TABLE "{table}"')
                copy.execute(f'ALTER TABLE "{table}_rebuilt" RENAME TO "{table}"')
                if missing_schema_objects(copy, component) is not True:
                    undetected.append(f"{table}.{column}")
    assert undetected == []


def test_queue_reference_matches_hand_written_v5_oracle(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    legacy_database(db_path, 5)

    with closing(sqlite3.connect(db_path)) as connection:
        assert catalogue(connection) == _reference_structure("queue")
        assert missing_schema_objects(connection, "queue") is False


@pytest.mark.parametrize("damaged", [False, True])
def test_structure_check_is_read_only(tmp_path: Path, damaged: bool) -> None:
    db_path = public_database(tmp_path, "queue")
    if damaged:
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("DROP TABLE queue_poison_events")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    with closing(connect_sqlite(str(db_path), field="test.queue_db_path", readonly=True)) as connection:
        changes = connection.total_changes
        assert missing_schema_objects(connection, "queue") is damaged
        assert connection.total_changes == changes

    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("component", ["queue", "transactions"])
def test_reference_build_failure_is_runtime_error_and_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: SchemaComponent
) -> None:
    db_path = public_database(tmp_path, component)
    module = queue_module if component == "queue" else persistence_module
    original = module._ensure_schema

    def failing_builder(_connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("builder failure")

    _reference_structure.cache_clear()
    try:
        monkeypatch.setattr(module, "_ensure_schema", failing_builder)
        with closing(sqlite3.connect(db_path)) as connection:
            with pytest.raises(RuntimeError) as excinfo:
                missing_schema_objects(connection, component)
            assert not isinstance(excinfo.value, sqlite3.Error)
            assert isinstance(excinfo.value.__cause__, sqlite3.Error)

            monkeypatch.setattr(module, "_ensure_schema", original)
            assert missing_schema_objects(connection, component) is False
    finally:
        _reference_structure.cache_clear()
