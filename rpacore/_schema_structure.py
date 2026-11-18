"""Read-only structural comparison against the framework's own migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from functools import cache
from types import MappingProxyType
from typing import Literal

import rpacore.persistence as persistence_module
import rpacore.queue as queue_module


SchemaComponent = Literal["queue", "transactions"]

_TABLES_SQL = (
    "SELECT name FROM sqlite_master "
    "WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
)
_COLUMNS_SQL = "SELECT name FROM pragma_table_info(?)"


def missing_schema_objects(
    connection: sqlite3.Connection,
    component: SchemaComponent,
) -> bool:
    """Return whether a current database lacks a table or column the migrations create.

    Extra tables and columns are ignored. Only ``sqlite_master`` and
    ``pragma_table_info`` are read from ``connection``, and a ``sqlite3.Error``
    from reading it propagates. A ``sqlite3.Error`` while building the
    reference is re-raised as ``RuntimeError`` so a caller cannot mistake a
    framework failure for a fault in the inspected database.
    """
    reference = _reference_structure(component)
    inspected_tables = _table_names(connection)
    return any(
        table not in inspected_tables or not columns <= _column_names(connection, table)
        for table, columns in reference.items()
    )


@cache
def _reference_structure(component: SchemaComponent) -> Mapping[str, frozenset[str]]:
    """Run the component's migrations on an in-memory database and read it back.

    The result is cached for the process, so it is returned read-only.
    """
    try:
        reference = sqlite3.connect(":memory:")
        try:
            # The migrations read PRAGMA rows by column name.
            reference.row_factory = sqlite3.Row
            if component == "queue":
                queue_module._ensure_schema(reference)
            else:
                persistence_module._ensure_schema(reference)
            return MappingProxyType(
                {table: _column_names(reference, table) for table in sorted(_table_names(reference))}
            )
        finally:
            reference.close()
    except sqlite3.Error as exc:
        raise RuntimeError("Could not build the schema reference") from exc


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute(_TABLES_SQL)}


def _column_names(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[0]) for row in connection.execute(_COLUMNS_SQL, (table,)))
