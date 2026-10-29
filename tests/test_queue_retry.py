"""Durable queue retry timing, races, migrations and operator truth."""

import base64
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

import rpacore.queue as queue_module
from rpacore import (
    Engine, EnvCredentialProvider, QueueAttemptOutcome, QueueItem,
    QueueLeaseLostError, QueueStatus, SqliteQueue, Step, SystemException,
    Transaction, get_queue_item, load_transaction, query_queue_items, run_queue_loop,
)
from rpacore._queue_retry import retry_available_at
from rpacore.cli import main


STAMP = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = STAMP

    def now_utc(self):
        return self.now

    def sleep(self, seconds):
        pytest.fail("delivery eligibility must not sleep")


def queue_at(tmp_path, **config):
    queue = SqliteQueue({"db_path": str(tmp_path / "queue.db"), **config})
    queue._clock = Clock()
    return queue


def fail(queue, item):
    return queue.fail(item.id, claimed_by=item.claimed_by, claim_token=item.claim_token)


@pytest.mark.parametrize("key,value,error", [
    ("retry_delay", True, TypeError), ("retry_backoff", False, TypeError),
    ("retry_delay", "1", TypeError), ("retry_backoff", None, TypeError),
    ("retry_delay", -1, ValueError), ("retry_delay", 86401, ValueError),
    ("retry_backoff", .9, ValueError),
    *[(key, value, ValueError) for key in ("retry_delay", "retry_backoff")
      for value in (float("nan"), float("inf"), -float("inf"), 10**1000)],
])
def test_invalid_policy_does_not_open_database(tmp_path, key, value, error):
    with pytest.raises(error, match=f"queue.{key}"):
        queue_at(tmp_path, **{key: value})
    assert not (tmp_path / "queue.db").exists()


@pytest.mark.parametrize("delay,backoff,count,seconds", [
    (1, 2, 1, 1), (3, 2, 3, 12), (.125, 2, 2, .25),
    (0, 1e308, 2**63-1, 0), (1, 1e308, 100, 86400),
    (86400, 2, 1, 86400),
    (1e-9, 1, 1, .000001), (1, 1, 2**63-1, 1),
    (1.1e-6, 1, 1, .000002),
])
def test_delay_formula_cap_and_precision(delay, backoff, count, seconds):
    due = retry_available_at(STAMP, count, delay, backoff)
    assert due == (None if seconds == 0 else (STAMP + timedelta(seconds=seconds)).isoformat(timespec="microseconds"))


def test_tiny_base_and_large_backoff_do_not_saturate_prematurely():
    due = retry_available_at(STAMP, 3, 1e-320, 1e160)
    # The subnormal base's float representation differs slightly from 10**-320.
    assert (datetime.fromisoformat(due) - STAMP).total_seconds() == pytest.approx(1, abs=.0001)


def test_default_delay_survives_reopen_and_other_workers_config(tmp_path):
    first = queue_at(tmp_path)
    assert (first.retry_delay, first.retry_backoff) == (1, 2)
    first.add(QueueItem(id="old", reference="old", payload={}, created_at=STAMP))
    claimed = first.next_item("one")
    assert fail(first, claimed) is QueueAttemptOutcome.RETRY_SCHEDULED
    due = get_queue_item("old", first.db_path).available_at
    assert due == STAMP + timedelta(seconds=1)
    # Reopening with zero delay cannot bypass an already durable deadline.
    other = queue_at(tmp_path, retry_delay=0)
    assert other.next_item("two") is None
    first.add(QueueItem(id="new", reference="new", payload={}, created_at=STAMP + timedelta(seconds=1)))
    assert other.next_item("two").id == "new"
    other._clock.now = due - timedelta(microseconds=1)
    assert other.next_item("two") is None
    other._clock.now = due
    reclaimed = other.next_item("two")
    assert reclaimed.id == "old" and reclaimed.available_at is None
    assert reclaimed.claim_token != claimed.claim_token
    with pytest.raises(QueueLeaseLostError):
        fail(first, claimed)


def test_repeated_failures_backoff_and_terminal_budget(tmp_path):
    queue = queue_at(tmp_path, retry_delay=3, retry_backoff=2, max_retries=2)
    queue.add(QueueItem(reference="retry", payload={}))
    for delay in (3, 6):
        item = queue.next_item("worker")
        assert fail(queue, item) is QueueAttemptOutcome.RETRY_SCHEDULED
        due = queue.get_item(item.id).available_at
        assert due == queue._clock.now + timedelta(seconds=delay)
        before = queue.get_item(item.id)
        with pytest.raises(QueueLeaseLostError):
            fail(queue, item)
        assert queue.get_item(item.id) == before
        assert queue.next_item("worker") is None
        queue._clock.now = due
    item = queue.next_item("worker")
    assert fail(queue, item) is QueueAttemptOutcome.FAILED
    assert queue.get_item(item.id).available_at is None
    assert queue.get_item(item.id).status is QueueStatus.FAILED
    assert [a.outcome for a in queue.list_attempts()] == [
        QueueAttemptOutcome.RETRY_SCHEDULED, QueueAttemptOutcome.RETRY_SCHEDULED,
        QueueAttemptOutcome.FAILED,
    ]


def test_zero_delay_explicitly_retains_immediate_redelivery(tmp_path):
    queue = queue_at(tmp_path, retry_delay=0)
    queue.add(QueueItem(reference="zero", payload={}))
    fail(queue, queue.next_item("worker"))
    assert queue.next_item("worker").retry_count == 1


@pytest.mark.parametrize("method", ["add", "add_once"])
def test_inserted_future_deadline_is_respected_and_cleared_on_claim(tmp_path, method):
    queue = queue_at(tmp_path)
    due = STAMP + timedelta(seconds=1)
    item = QueueItem(id="future", reference="future", payload={}, available_at=due.astimezone(timezone(timedelta(hours=-3))))
    getattr(queue, method)(item)
    queue._clock.now = due - timedelta(microseconds=1)
    assert queue.next_item("worker") is None
    assert queue.get_item(item.id).available_at == due
    queue._clock.now = due
    claimed = queue.next_item("worker")
    assert claimed.id == item.id and claimed.available_at is None
    assert queue.get_item(item.id).available_at is None


def test_claim_rechecks_deadline_after_stale_selection(tmp_path, monkeypatch):
    queue = queue_at(tmp_path)
    competitor = queue_at(tmp_path)
    queue.add(QueueItem(reference="race", payload={}))
    original = queue_module.load_payload
    raced = False

    def decode(raw):
        nonlocal raced
        if not raced:
            raced = True
            item = competitor.next_item("competitor")
            fail(competitor, item)
        return original(raw)

    monkeypatch.setattr(queue_module, "load_payload", decode)
    assert queue.next_item("stale-reader") is None
    assert len(queue.list_attempts()) == 1
    assert queue.list_items()[0].available_at == STAMP + timedelta(seconds=1)


def test_quarantine_rechecks_deadline_after_stale_selection(tmp_path, monkeypatch):
    queue = queue_at(tmp_path)
    queue.add(QueueItem(id="poison", reference="race", payload={}))
    original = queue_module.load_payload
    raced = False

    def decode(raw):
        nonlocal raced
        if not raced:
            raced = True
            # Simulate a peer scheduling this row after the reader selected it.
            with sqlite3.connect(queue.db_path) as conn:
                conn.execute("UPDATE queue_items SET available_at=?", ((STAMP + timedelta(seconds=1)).isoformat(timespec="microseconds"),))
            raise json.JSONDecodeError("stale malformed payload", raw, 0)
        return original(raw)

    monkeypatch.setattr(queue_module, "load_payload", decode)
    assert queue.next_item("stale-reader") is None
    assert queue.get_item("poison").status is QueueStatus.PENDING
    assert queue.list_poison_events() == []


def test_retry_scheduling_rolls_back_with_attempt_close(tmp_path, monkeypatch):
    queue = queue_at(tmp_path)
    queue.add(QueueItem(reference="atomic", payload={}))
    item = queue.next_item("worker")

    def broken(*args, **kwargs):
        raise RuntimeError("close failed")

    monkeypatch.setattr(queue_module, "_close_attempt", broken)
    with pytest.raises(RuntimeError, match="close failed"):
        fail(queue, item)
    assert queue.get_item(item.id) == item
    assert queue.list_attempts()[0].outcome is None


def test_fail_loses_claim_between_read_and_update_without_changing_winner(tmp_path, monkeypatch):
    queue = queue_at(tmp_path)
    competitor = queue_at(tmp_path, retry_delay=0)
    queue.add(QueueItem(id="race", reference="race", payload={}))
    original = queue.next_item("original")
    winner = []

    def lose_claim():
        competitor.force_fail(original.id, reason="take over", retry=True)
        winner.append(competitor.next_item("winner"))
        return STAMP

    monkeypatch.setattr(queue._clock, "now_utc", lose_claim)
    with pytest.raises(QueueLeaseLostError):
        fail(queue, original)
    assert queue.get_item(original.id) == winner[0]
    assert queue.get_item(original.id).available_at is None
    assert [a.outcome for a in queue.list_attempts()] == [QueueAttemptOutcome.ADMIN_OVERRIDE, None]


@pytest.mark.parametrize("kind", ["lease", "admin"])
def test_all_requeue_paths_delay_and_terminal_paths_clear_due(tmp_path, kind):
    queue = queue_at(tmp_path, lease_timeout=1)
    queue.add(QueueItem(reference=kind, payload={}))
    claimed = queue.next_item("worker")
    if kind == "lease":
        queue._clock.now += timedelta(seconds=2)
        assert queue.next_item("other") is None
        outcome = QueueAttemptOutcome.LEASE_EXPIRED
    else:
        queue.force_fail(claimed.id, reason="retry", retry=True)
        outcome = QueueAttemptOutcome.ADMIN_OVERRIDE
    pending = queue.get_item(claimed.id)
    assert pending.status is QueueStatus.PENDING
    assert pending.available_at == queue._clock.now + timedelta(seconds=1)
    assert queue.list_attempts()[0].outcome is outcome
    assert queue.next_item("worker") is None
    queue.force_complete(claimed.id, reason="resolved")
    assert queue.get_item(claimed.id).available_at is None


@pytest.mark.parametrize("retry", [False, True])
def test_admin_terminal_failure_clears_existing_deadline(tmp_path, retry):
    queue = queue_at(tmp_path, max_retries=1)
    queue.add(QueueItem(id="terminal", reference="terminal", payload={}))
    fail(queue, queue.next_item("worker"))
    assert queue.get_item("terminal").available_at is not None
    queue.force_fail("terminal", reason="stop retrying", retry=retry)
    item = queue.get_item("terminal")
    assert item.status is QueueStatus.FAILED and item.retry_count == 2
    assert item.available_at is None
    event, = queue.list_admin_events()
    assert event.previous_status is QueueStatus.PENDING
    assert event.new_status is QueueStatus.FAILED


def test_expired_lease_exhaustion_is_terminal_without_deadline(tmp_path):
    queue = queue_at(tmp_path, max_retries=0, lease_timeout=1)
    queue.add(QueueItem(id="expired", reference="expired", payload={}))
    queue.next_item("worker")
    queue._clock.now += timedelta(seconds=2)
    assert queue.next_item("replacement") is None
    item = queue.get_item("expired")
    assert item.status is QueueStatus.FAILED and item.retry_count == 1
    assert item.available_at is None
    assert queue.list_attempts()[0].outcome is QueueAttemptOutcome.LEASE_EXPIRED


def test_inspection_exposes_due_without_mutation(tmp_path, capsys):
    queue = queue_at(tmp_path)
    queue.add(QueueItem(id="delayed", reference="private-safe", payload={"secret": "omit-me"}))
    fail(queue, queue.next_item("worker"))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert query_queue_items(queue.db_path).records[0].available_at == STAMP + timedelta(seconds=1)
    for command in (["list"], ["show", "delayed"]):
        for flags in ([], ["--json"]):
            assert main(["queue", *command, "--db", queue.db_path, *flags]) == 0
            output = capsys.readouterr()
            assert not output.err and "omit-me" not in output.out
            assert "2026-10-28T12:00:01" in output.out
            if flags:
                record = json.loads(output.out)
                assert record["schema_version"] == 2
                item = record["item"] if command[0] == "show" else record["records"][0]
                assert item["available_at"] == "2026-10-28T12:00:01.000000+00:00"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_page_v2_keeps_cursor_v1_positions(tmp_path):
    queue = queue_at(tmp_path)
    for name in ("a", "b"):
        queue.add(QueueItem(id=name, reference=name, payload={}, created_at=STAMP))
    page = query_queue_items(queue.db_path, limit=1)
    token = json.loads(base64.urlsafe_b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4)))
    assert token["version"] == 1 and page.format_version == 2
    assert [item.id for item in query_queue_items(queue.db_path, cursor=page.next_cursor).records] == ["b"]


@pytest.mark.parametrize("value,error", [(STAMP.replace(tzinfo=None), ValueError), ("tomorrow", TypeError)])
@pytest.mark.parametrize("method", ["add", "add_once"])
def test_due_requires_aware_datetime(tmp_path, value, error, method):
    queue = queue_at(tmp_path)
    with pytest.raises(error, match="queue.available_at"):
        getattr(queue, method)(QueueItem(reference="bad", payload={}, available_at=value))
    assert queue.list_items() == []
    getattr(queue, method)(QueueItem(reference="valid", payload={}, available_at=STAMP))
    assert len(queue.list_items()) == 1


@pytest.mark.parametrize("stored", ["2026-10-28T12:00:01", "2026-10-28T12:00:01+00:00", "2026-10-28T09:00:01.000000-03:00"])
def test_inspection_rejects_noncanonical_deadline_without_writes(tmp_path, stored):
    queue = queue_at(tmp_path)
    queue.add(QueueItem(id="bad", reference="bad-time", payload={}))
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE queue_items SET available_at=?", (stored,))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    for query in (lambda: get_queue_item("bad", queue.db_path), lambda: query_queue_items(queue.db_path)):
        with pytest.raises(ValueError, match="queue.available_at"):
            query()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize("payload", ["{}", "not-json"])
def test_claim_rejects_noncanonical_deadline_before_claim_or_quarantine(tmp_path, payload):
    queue = queue_at(tmp_path)
    queue.add(QueueItem(id="bad", reference="bad-time", payload={}))
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE queue_items SET available_at=?,payload=?", ("2026-10-28T09:00:01.000000-03:00", payload))
    queue._clock.now = STAMP + timedelta(seconds=.5)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="queue.available_at"):
        queue.next_item("worker")
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert queue.list_attempts() == []
    assert queue.list_poison_events() == []


@pytest.mark.parametrize("poison", [False, True])
def test_stale_selection_cannot_clear_a_changed_noncanonical_deadline(tmp_path, monkeypatch, poison):
    queue = queue_at(tmp_path)
    queue.add(QueueItem(id="changed", reference="changed", payload={}))
    bad_due = "2026-10-28T09:00:01.000000-03:00"
    original_decode = queue_module.load_payload

    def change_deadline(raw):
        with sqlite3.connect(queue.db_path) as conn:
            conn.execute("UPDATE queue_items SET available_at=? WHERE id='changed'", (bad_due,))
        if poison:
            raise json.JSONDecodeError("stale payload", raw, 0)
        return original_decode(raw)

    monkeypatch.setattr(queue_module, "load_payload", change_deadline)
    with pytest.raises(ValueError, match="queue.available_at"):
        queue.next_item("worker")
    with sqlite3.connect(queue.db_path) as conn:
        assert conn.execute("SELECT status,available_at FROM queue_items").fetchone() == ("pending", bad_due)
    assert queue.list_attempts() == queue.list_poison_events() == []


def test_insert_normalizes_utc_and_poison_clears_due(tmp_path):
    queue = queue_at(tmp_path)
    local = STAMP.astimezone(timezone(timedelta(hours=-3)))
    queue.add(QueueItem(id="poison", reference="bad", payload={}, available_at=local))
    assert queue.get_item("poison").available_at == STAMP
    with sqlite3.connect(queue.db_path) as conn:
        conn.execute("UPDATE queue_items SET payload='not-json'")
    assert queue.next_item("worker") is None
    with sqlite3.connect(queue.db_path) as conn:
        assert conn.execute("SELECT status,available_at FROM queue_items").fetchone() == ("failed", None)
    assert len(queue.list_poison_events()) == 1


def test_drain_once_then_resume_same_bound_transaction(tmp_path):
    class Effect(Step):
        def execute(self, ctx):
            if not ctx.state.get("retried"):
                ctx.state["retried"] = True
                raise SystemException("temporary")
            ctx.state["done"] = True

    def build(item):
        return Transaction(item.reference, definition_identity="retry/v1", steps=[Effect("effect", 1)])

    queue = queue_at(tmp_path)
    queue.add(QueueItem(id="job", reference="job", payload={}))
    db = str(tmp_path / "transactions.db")
    args = dict(engine=Engine(), build_transaction=build, config={}, credentials=EnvCredentialProvider(), transaction_db_path=db)
    summary = run_queue_loop(queue=queue, **args)
    assert summary.processed == summary.retry_scheduled == 1
    bound = queue.get_item("job").transaction_id
    reopened = queue_at(tmp_path)
    assert run_queue_loop(queue=reopened, **args).processed == 0
    reopened._clock.now += timedelta(seconds=1)
    assert run_queue_loop(queue=reopened, **args).completed == 1
    assert reopened.get_item("job").transaction_id == bound
    assert load_transaction(bound, db).state["done"] is True


def legacy_database(path, version):
    """Published storage shapes, independent from current migration helpers."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE queue_items (id TEXT PRIMARY KEY, reference TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, claimed_by TEXT NOT NULL DEFAULT '', claimed_at TEXT)")
    if version:
        conn.execute("CREATE TABLE rpacore_schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)")
        conn.execute("INSERT INTO rpacore_schema_versions VALUES ('queue',?)", (version,))
    if version >= 2:
        conn.execute("ALTER TABLE queue_items ADD COLUMN transaction_id TEXT NOT NULL DEFAULT ''")
    if version >= 3:
        conn.execute("ALTER TABLE queue_items ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE TABLE queue_admin_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,previous_status TEXT NOT NULL,new_status TEXT NOT NULL,created_at TEXT NOT NULL)")
    if version >= 4:
        conn.execute("CREATE TABLE queue_attempts (sequence INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,claim_token TEXT NOT NULL,claimed_by TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,outcome TEXT NOT NULL DEFAULT '',UNIQUE(item_id,claim_token))")
        conn.execute("CREATE TABLE queue_poison_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,error_type TEXT NOT NULL,created_at TEXT NOT NULL)")
    for index, status in enumerate(QueueStatus):
        conn.execute("INSERT INTO queue_items (id,reference,status,created_at,claimed_at) VALUES (?,?,?,?,?)", (f"{index:032x}", status.value, status.value, STAMP.isoformat(), STAMP.isoformat()))
    if version >= 2:
        conn.execute("UPDATE queue_items SET transaction_id='binding'")
    if version >= 3:
        conn.execute("UPDATE queue_items SET claim_token='existing-token' WHERE status='in_progress'")
    if version >= 4:
        conn.execute("INSERT INTO queue_attempts (item_id,claim_token,claimed_by,started_at) VALUES (?,?,?,?)", (f"{1:032x}", "existing-token", "", STAMP.isoformat()))
        conn.execute("INSERT INTO queue_poison_events (item_id,error_type,created_at) VALUES (?,?,?)", (f"{3:032x}", "JSONDecodeError", STAMP.isoformat()))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("version", range(5))
def test_every_legacy_schema_preserves_identity_binding_history(tmp_path, version):
    path = tmp_path / "queue.db"
    legacy_database(path, version)
    queue = queue_at(tmp_path)
    items = queue.list_items()
    assert [item.id for item in items] == [f"{i:032x}" for i in range(4)]
    assert all(item.available_at is None for item in items)
    assert all(item.transaction_id == ("binding" if version >= 2 else "") for item in items)
    assert items[1].status is (QueueStatus.PENDING if version < 3 else QueueStatus.IN_PROGRESS)
    assert len(queue.list_attempts()) == int(version == 4)
    assert len(queue.list_poison_events()) == int(version == 4)
    assert queue.next_item("worker").id == f"{0:032x}"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM rpacore_schema_versions WHERE component='queue'").fetchone()[0] == 5


def test_migration_failure_after_ddl_rolls_back_column_and_marker(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    legacy_database(path, 4)
    original = queue_module._record_queue_schema_version

    def record(conn, version):
        if version == 5:
            assert "available_at" in {r[1] for r in conn.execute("PRAGMA table_info(queue_items)")}
            raise RuntimeError("marker failed")
        original(conn, version)

    monkeypatch.setattr(queue_module, "_record_queue_schema_version", record)
    with pytest.raises(RuntimeError, match="marker failed"):
        queue_at(tmp_path)
    with sqlite3.connect(path) as conn:
        assert "available_at" not in {r[1] for r in conn.execute("PRAGMA table_info(queue_items)")}
        assert conn.execute("SELECT version FROM rpacore_schema_versions").fetchone()[0] == 4
    monkeypatch.setattr(queue_module, "_record_queue_schema_version", original)
    assert len(queue_at(tmp_path).list_items()) == 4


def test_mismarked_v4_database_is_refused_without_adopting_existing_v5_column(tmp_path):
    path = tmp_path / "queue.db"
    legacy_database(path, 4)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE queue_items ADD COLUMN available_at TEXT")
        conn.execute("UPDATE queue_items SET available_at='externally-added'")
    before = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        queue_at(tmp_path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM rpacore_schema_versions").fetchone()[0] == 4
        assert conn.execute("SELECT DISTINCT available_at FROM queue_items").fetchall() == [("externally-added",)]


def test_concurrent_migrations_reread_version_under_lock(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    legacy_database(path, 2)
    original = queue_module.ensure_supported_schema
    barrier = threading.Barrier(2)
    seen = set()
    errors = []

    def synchronized(conn, **kwargs):
        version = original(conn, **kwargs)
        if not conn.in_transaction and threading.get_ident() not in seen:
            seen.add(threading.get_ident())
            barrier.wait(timeout=5)
        return version

    monkeypatch.setattr(queue_module, "ensure_supported_schema", synchronized)

    def migrate():
        conn = queue_module._connect(str(path))
        try:
            queue_module._ensure_schema(conn)
        except Exception as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=migrate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM rpacore_schema_versions").fetchone()[0] == 5
