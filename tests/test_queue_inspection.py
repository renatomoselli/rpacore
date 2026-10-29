"""Read-only queue contracts, including real SQLite failure paths."""

import base64
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rpacore import (
    QueueItem, QueuePage, QueueStatus, SqliteQueue, Transaction,
    get_queue_item, query_queue_attempts, query_queue_items,
    query_queue_poison_events, save_transaction,
)
from rpacore._sqlite import QUEUE_SCHEMA_VERSION, TRANSACTION_SCHEMA_VERSION


READS = (
    lambda path: get_queue_item("item-0000", str(path)),
    lambda path: query_queue_items(str(path)),
    lambda path: query_queue_attempts(str(path)),
    lambda path: query_queue_poison_events(str(path)),
)
STAMP = "2026-10-22T12:00:00+00:00"


def snapshot(path):
    """Include absent sidecars, before the first inspection connection."""
    return {
        suffix: candidate.read_bytes() if candidate.exists() else None
        for suffix in ("", "-journal", "-wal", "-shm")
        for candidate in (Path(f"{path}{suffix}"),)
    }


@pytest.fixture
def queue_path(tmp_path):
    path = tmp_path / "queue.db"
    SqliteQueue({"db_path": str(path)})
    # Bulk fixture creation avoids thousands of writable constructor/migration calls.
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO queue_items (id, reference, payload, status, created_at) VALUES (?, ?, ?, ?, ?)",
            [(f"item-{i:04}", "fixture", '{"private":"payload-secret"}',
              list(QueueStatus)[i % 4].value, STAMP) for i in range(1005)],
        )
        conn.executemany(
            "INSERT INTO queue_attempts (item_id, claim_token, claimed_by, started_at) VALUES (?, ?, ?, ?)",
            [(f"item-{i:04}", f"token-secret-{i}", "fixture-worker", STAMP) for i in range(1005)],
        )
        conn.executemany(
            "INSERT INTO queue_poison_events (item_id, error_type, created_at) VALUES (?, ?, ?)",
            [(f"item-{i:04}", "JSONDecodeError", STAMP) for i in range(1005)],
        )
    return path


@pytest.mark.parametrize("query,method", [
    (query_queue_items, "list_items"),
    (query_queue_attempts, "list_attempts"),
    (query_queue_poison_events, "list_poison_events"),
])
@pytest.mark.parametrize("limit", [1, 1000])
def test_pages_match_writable_order_without_mutation(queue_path, query, method, limit):
    writable = SqliteQueue({"db_path": str(queue_path)})
    expected = getattr(writable, method)()
    before = snapshot(queue_path)
    records = []
    cursor = None
    while True:
        page = query(str(queue_path), cursor=cursor, limit=limit)
        assert isinstance(page, QueuePage) and page.format_version == 2
        assert isinstance(page.records, tuple) and len(page.records) <= limit
        records.extend(page.records)
        assert page.has_more == (len(records) < len(expected))
        assert bool(page.next_cursor) == page.has_more
        if not page.has_more:
            break
        assert "payload-secret" not in page.next_cursor
        assert "token-secret" not in page.next_cursor
        cursor = page.next_cursor
    assert records == expected
    assert snapshot(queue_path) == before
    assert len(query(str(queue_path)).records) == 100
    assert len(getattr(writable, method)()) == 1005


def test_status_union_lookup_and_exact_event_selectors(queue_path):
    before = snapshot(queue_path)
    assert get_queue_item("missing", str(queue_path)) is None
    item = get_queue_item("item-0000", str(queue_path))
    assert item.payload == {"private": "payload-secret"}
    writable = SqliteQueue({"db_path": str(queue_path)})
    statuses = [QueueStatus.FAILED, QueueStatus.PENDING, QueueStatus.PENDING]
    assert list(query_queue_items(str(queue_path), statuses=iter(statuses), limit=1000).records) == writable.list_items(statuses=statuses)
    assert query_queue_items(str(queue_path), statuses=[]).records == ()
    page = query_queue_items(str(queue_path), statuses=statuses, limit=1)
    assert query_queue_items(str(queue_path), statuses=reversed(statuses), cursor=page.next_cursor).records
    for query in (query_queue_attempts, query_queue_poison_events):
        assert len(query(str(queue_path), item_id="item-0000").records) == 1
        assert query(str(queue_path), item_id="item-000").records == ()
        assert query(str(queue_path), item_id="missing").records == ()
    assert snapshot(queue_path) == before


def mutate_cursor(cursor, **changes):
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    payload.update(changes)
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize("query", [query_queue_items, query_queue_attempts, query_queue_poison_events])
def test_cursor_rejects_malformed_version_kind_filter_and_position(queue_path, query):
    cursor = query(str(queue_path), limit=1).next_cursor
    bad = ["", "!", "e30", cursor + "!", mutate_cursor(cursor, version=2),
           mutate_cursor(cursor, version=True), mutate_cursor(cursor, kind="other"),
           mutate_cursor(cursor, filters="other"), mutate_cursor(cursor, position={}),
           mutate_cursor(cursor, position={"sequence": True})]
    before = snapshot(queue_path)
    for value in bad:
        with pytest.raises(ValueError):
            query(str(queue_path), cursor=value)
    if query is query_queue_items:
        # None (all) and [] (none) must never share cursor identity.
        with pytest.raises(ValueError):
            query(str(queue_path), statuses=[], cursor=cursor)
    else:
        with pytest.raises(ValueError):
            query(str(queue_path), item_id="item-0000", cursor=cursor)
    assert snapshot(queue_path) == before


@pytest.mark.parametrize("query", [query_queue_items, query_queue_attempts, query_queue_poison_events])
def test_cursor_rejects_json_with_wrong_shape_before_opening_storage(queue_path, query, monkeypatch):
    import rpacore._queue_read as reads
    cursor = query(str(queue_path), limit=1).next_cursor
    valid = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    malformed = [[], 123, "cursor", None]
    malformed.extend({**valid, "position": position} for position in ("position", [], None))
    before = snapshot(queue_path)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid cursor shape must be rejected before opening storage")
    monkeypatch.setattr(reads, "open_readonly_queue", forbidden)
    for payload in malformed:
        invalid = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        with pytest.raises(ValueError, match="not a valid queue query cursor"):
            query(str(queue_path), cursor=invalid)
        assert snapshot(queue_path) == before


@pytest.mark.parametrize("db_path", [123, None, b"queue.db", "", "  ", ":memory:"])
def test_invalid_database_path_arguments_fail_before_filesystem_access(tmp_path, monkeypatch, db_path):
    import rpacore._sqlite as sqlite_policy
    monkeypatch.chdir(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid database path must be rejected before filesystem access")
    monkeypatch.setattr(sqlite_policy, "Path", forbidden)
    error = TypeError if not isinstance(db_path, str) else ValueError
    for read in (
        lambda: get_queue_item("opaque ID", db_path),
        lambda: query_queue_items(db_path),
        lambda: query_queue_attempts(db_path),
        lambda: query_queue_poison_events(db_path),
    ):
        with pytest.raises(error, match="queue.db_path"):
            read()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "1"])
@pytest.mark.parametrize("query", [query_queue_items, query_queue_attempts, query_queue_poison_events])
def test_limit_validation_before_storage(tmp_path, query, limit):
    with pytest.raises((TypeError, ValueError), match="limit"):
        query(str(tmp_path / "absent" / "queue.db"), limit=limit)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("item_id", ["", "  ", 123])
def test_id_validation_before_storage(tmp_path, item_id):
    path = str(tmp_path / "absent.db")
    with pytest.raises((TypeError, ValueError)):
        get_queue_item(item_id, path)
    for query in (query_queue_attempts, query_queue_poison_events):
        with pytest.raises((TypeError, ValueError)):
            query(path, item_id=item_id)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("version", range(QUEUE_SCHEMA_VERSION + 2))
def test_all_noncurrent_queue_schemas_rejected_without_migration(queue_path, version):
    if version == QUEUE_SCHEMA_VERSION:
        return
    with sqlite3.connect(queue_path) as conn:
        conn.execute("UPDATE rpacore_schema_versions SET version = ? WHERE component = 'queue'", (version,))
    before = snapshot(queue_path)
    for read in READS:
        with pytest.raises(RuntimeError, match="schema version"):
            read(queue_path)
        assert snapshot(queue_path) == before


@pytest.mark.parametrize("component,version", [("unknown-private-component", 1), ("transactions", TRANSACTION_SCHEMA_VERSION + 1)])
def test_unknown_and_future_mixed_components_are_rejected(queue_path, component, version):
    with sqlite3.connect(queue_path) as conn:
        conn.execute("INSERT INTO rpacore_schema_versions VALUES (?, ?)", (component, version))
    before = snapshot(queue_path)
    for read in READS:
        with pytest.raises(RuntimeError):
            read(queue_path)
    assert snapshot(queue_path) == before


def test_current_mixed_components_and_empty_schema(tmp_path, queue_path):
    save_transaction(Transaction(reference="mixed"), str(queue_path))
    before = snapshot(queue_path)
    for read in READS:
        read(queue_path)
    assert snapshot(queue_path) == before
    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    before = snapshot(empty)
    for read in READS:
        with pytest.raises(RuntimeError, match="schema version 0"):
            read(empty)
    assert snapshot(empty) == before


@pytest.mark.parametrize("shape", ["missing", "missing-parent", "directory", "not-sqlite"])
def test_invalid_storage_does_not_create_or_modify_files(tmp_path, shape):
    path = tmp_path / ("parent/queue.db" if shape == "missing-parent" else "queue.db")
    if shape == "directory":
        path.mkdir()
    elif shape == "not-sqlite":
        path.write_bytes(b"not sqlite: private contents")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    for read in READS:
        with pytest.raises((OSError, RuntimeError)):
            read(path)
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not (tmp_path / "parent").exists()


def test_wal_refused_before_connect_and_sidecars_unchanged(queue_path, monkeypatch):
    import rpacore._queue_read as reads
    conn = sqlite3.connect(queue_path)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("UPDATE queue_items SET retry_count=1")
        conn.commit()
        before = snapshot(queue_path)
        def forbidden(*args, **kwargs):
            pytest.fail("WAL inspection must not connect")
        monkeypatch.setattr(reads, "connect_sqlite", forbidden)
        for read in READS:
            with pytest.raises(RuntimeError, match="WAL"):
                read(queue_path)
        assert snapshot(queue_path) == before
    finally:
        conn.close()


def test_hot_journal_needs_recovery_but_inspection_does_not_recover(queue_path):
    # A process crash leaves an actual hot rollback journal, not a mocked error.
    child = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA cache_size=1')
conn.execute('BEGIN IMMEDIATE')
conn.execute("UPDATE queue_items SET payload = ?", ('x' * 6000,))
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", child, str(queue_path)], check=True)
    before = snapshot(queue_path)
    assert before["-journal"] and before["-journal"][:8] == bytes.fromhex("d9d505f920a163d7")
    for read in READS:
        with pytest.raises(sqlite3.OperationalError):
            read(queue_path)
        assert snapshot(queue_path) == before


def test_exclusive_lock_fails_without_mutation(queue_path):
    conn = sqlite3.connect(queue_path)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        before = snapshot(queue_path)
        for read in READS:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                read(queue_path)
        assert snapshot(queue_path) == before
    finally:
        conn.rollback()
        conn.close()


def test_permission_failure_is_propagated_and_connections_close(queue_path, monkeypatch):
    import rpacore._queue_read as reads
    original = reads.connect_sqlite
    connections = []
    def track(*args, **kwargs):
        conn = original(*args, **kwargs)
        connections.append(conn)
        return conn
    monkeypatch.setattr(reads, "connect_sqlite", track)
    for read in READS:
        read(queue_path)
    with sqlite3.connect(queue_path) as conn:
        conn.execute("UPDATE rpacore_schema_versions SET version=0")
    for read in READS:
        with pytest.raises(RuntimeError):
            read(queue_path)
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    def denied(*args, **kwargs):
        raise PermissionError("private-file-marker")
    monkeypatch.setattr(reads, "database_header_journal_mode", denied)
    before = snapshot(queue_path)
    for read in READS:
        with pytest.raises(PermissionError):
            read(queue_path)
    assert snapshot(queue_path) == before


def test_corrupt_payload_and_expired_claim_are_not_quarantined_or_reclaimed(queue_path):
    with sqlite3.connect(queue_path) as conn:
        conn.execute("UPDATE queue_items SET payload='not-json-secret' WHERE id='item-0001'")
        conn.execute("UPDATE queue_items SET status='in_progress', claimed_at='2000-01-01T00:00:00+00:00', claim_token='expired-secret' WHERE id='item-0000'")
    before = snapshot(queue_path)
    assert get_queue_item("item-0000", str(queue_path)).claim_token == "expired-secret"
    with pytest.raises(json.JSONDecodeError):
        query_queue_items(str(queue_path))
    with pytest.raises(json.JSONDecodeError):
        get_queue_item("item-0001", str(queue_path))
    assert len(query_queue_attempts(str(queue_path), limit=1000).records) == 1000
    assert len(query_queue_poison_events(str(queue_path), limit=1000).records) == 1000
    assert snapshot(queue_path) == before


def test_claim_between_pages_and_writer_checkpoint_do_not_hold_reader_locks(tmp_path):
    path = str(tmp_path / "live.db")
    queue = SqliteQueue({"db_path": path})
    for name in ("a", "b", "c", "d"):
        queue.add(QueueItem(id=name, reference=name, payload={}, created_at=datetime(2026, 10, 22, tzinfo=timezone.utc)))
    first = query_queue_items(path, statuses=[QueueStatus.PENDING], limit=2)
    assert [item.id for item in first.records] == ["a", "b"]
    before_writer = snapshot(path)
    assert queue.next_item("worker").id == "a"
    save_transaction(Transaction(reference="checkpoint"), path)
    # A legitimate writer committed these changes while the page object is alive.
    after_writer = snapshot(path)
    assert after_writer != before_writer
    second = query_queue_items(path, statuses=[QueueStatus.PENDING], cursor=first.next_cursor)
    assert [item.id for item in second.records] == ["c", "d"]
    assert snapshot(path) == after_writer
    queue.force_fail("b", reason="fixture", retry=True)
    assert query_queue_items(path, statuses=[QueueStatus.PENDING], cursor=first.next_cursor).records == second.records
