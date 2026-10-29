"""Real-process SQLite policy probes, also copied into installed-wheel gates."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from rpacore._queue_checkpoint import _save_queue_transaction_fenced

from rpacore import (
    QueueItem, QueueStatus, SqliteQueue, Status, Transaction, TransactionFenceError,
    load_transaction, save_transaction,
)
from rpacore.persistence import _prepare_transaction_database


_CLAIM_WORKER = """
import json, logging, sqlite3, sys, time
from pathlib import Path
from rpacore import (Engine, EnvCredentialProvider, SqliteQueue, Step, Transaction,
                     run_queue_loop)

root, recover = Path(sys.argv[1]), sys.argv[2] == 'True'
def wait_for(name):
    deadline = time.monotonic() + 20
    while not (root / name).exists():
        if time.monotonic() > deadline:
            raise TimeoutError('parent did not release claim probe')
        time.sleep(.02)

events, summaries = [], []
class Capture(logging.Handler):
    def emit(self, record):
        events.append({'event': getattr(record, 'event', ''), 'level': record.levelname})
        if getattr(record, 'event', '') == 'queue_claim_retry':
            (root / 'retry').touch()
            if recover:
                wait_for('release')
class Effect(Step):
    def execute(self, ctx):
        with (root / 'effect').open('a') as output:
            output.write('effect\\n')

log = logging.getLogger('claim-probe'); log.setLevel(logging.INFO)
log.addHandler(Capture()); log.propagate = False
queue = SqliteQueue({'db_path': str(root / 'queue.db')})
(root / 'ready').touch()
wait_for('start')
error = None
try:
    run_queue_loop(queue, Engine(), lambda item: Transaction(item.reference,
                   steps=[Effect('effect', 1)]), {}, EnvCredentialProvider(),
                   worker_id='claim-probe', logger=log, on_finish=summaries.append)
except sqlite3.OperationalError as exc:
    error = exc.sqlite_errorname
(root / 'result.json').write_text(json.dumps({
    'error': error, 'events': events,
    'summaries': [vars(summary) for summary in summaries],
}), encoding='utf-8')
sys.exit(0 if error is None else 7)
"""


@pytest.mark.parametrize("recover", [True, False], ids=["released-lock", "exhausted-lock"])
def test_runner_claim_contention_with_real_process(tmp_path, recover):
    queue_path = tmp_path / "queue.db"
    queue = SqliteQueue({"db_path": str(queue_path)})
    item = QueueItem(reference="claim-contention", payload={})
    queue.add(item)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen([sys.executable, "-c", _CLAIM_WORKER, str(tmp_path), str(recover)], env=env)
    try:
        _wait_ready(process, tmp_path / "ready")
        with closing(sqlite3.connect(queue_path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            (tmp_path / "start").touch()
            _wait_ready(process, tmp_path / "retry")
            if recover:
                lock.rollback()
                (tmp_path / "release").touch()
            assert process.wait(timeout=20) == (0 if recover else 7)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["error"] == (None if recover else "SQLITE_BUSY")
    summary, = result["summaries"]
    assert summary["processed"] == summary["completed"] == int(recover)
    assert all(value == 0 for name, value in summary.items() if name not in {"processed", "completed"})
    retries = [event for event in result["events"] if event["event"] == "queue_claim_retry"]
    assert len(retries) == (1 if recover else 2)
    terminal = [event for event in result["events"] if event["event"].startswith("queue_run_")]
    assert terminal == [{"event": "queue_run_completed" if recover else "queue_run_aborted",
                         "level": "INFO" if recover else "ERROR"}]
    stored = queue.get_item(item.id)
    assert stored.status is (QueueStatus.SUCCESSFUL if recover else QueueStatus.PENDING)
    assert stored.retry_count == 0 and stored.transaction_id == ""
    assert len(queue.list_attempts(item.id)) == int(recover)
    if recover:
        assert (tmp_path / "effect").read_text() == "effect\n"
        assert queue.next_item("other-worker") is None
    else:
        assert stored.claim_token == stored.claimed_by == ""
        assert not (tmp_path / "effect").exists()


_WORKER = """
import sys, threading, time
from pathlib import Path
from rpacore import (Engine, EnvCredentialProvider, SqliteQueue, Step, Transaction,
                     run_queue_loop)

queue_path, transaction_path, ready_path, release_path = sys.argv[1:]
class First(Step):
    def execute(self, ctx):
        ctx.state['first_runs'] = ctx.state.get('first_runs', 0) + 1
        assert ctx.state['first_runs'] == 1, 'completed step replayed'
class Second(Step):
    def execute(self, ctx):
        Path(ready_path).touch()
        deadline = time.monotonic() + 30
        while not Path(release_path).exists():
            if time.monotonic() > deadline:
                raise TimeoutError('parent did not release worker')
            time.sleep(0.02)
        ctx.state['finished'] = True
def build(item):
    return Transaction(item.reference, definition_identity='sqlite-runtime/v1',
                       steps=[First('first', 1), Second('second', 2)])
stop = threading.Event()
summary = run_queue_loop(
    SqliteQueue({'db_path': queue_path, 'lease_timeout': 60, 'retry_delay': 0}), Engine(), build,
    {}, EnvCredentialProvider(), worker_id=ready_path, transaction_db_path=transaction_path,
    stop_event=stop, after_item=lambda *args: stop.set())
assert summary.completed == 1 and summary.failed == 0, summary
"""


_INITIAL_PAUSE = """
import sqlite3, sys, time
from pathlib import Path
import rpacore.persistence as persistence
import rpacore.runner as runner

phase = sys.argv.pop()
active_transaction = None
def pause(transaction):
    Path(sys.argv[3]).write_text(transaction.id, encoding='utf-8')
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(.02)
    raise TimeoutError('parent did not kill worker')

write = persistence._write_transaction_rows
def paused_write(connection, transaction, **kwargs):
    global active_transaction
    active_transaction = transaction
    if phase == 'before-rows':
        pause(transaction)
    revision = write(connection, transaction, **kwargs)
    if phase == 'after-rows':
        pause(transaction)
    return revision
persistence._write_transaction_rows = paused_write
save = runner._save_queue_transaction_fenced
def paused_save(transaction, **kwargs):
    revision = save(transaction, **kwargs)
    if phase == 'after-commit':
        pause(transaction)
    return revision
runner._save_queue_transaction_fenced = paused_save
connect = sqlite3.connect
class PausedConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if phase == 'before-commit' and sql == 'COMMIT' and active_transaction is not None:
            schema = 'queue_guard' if any(row[1] == 'queue_guard' for row in
                super().execute('PRAGMA database_list')) else 'main'
            binding = super().execute(f'SELECT transaction_id FROM {schema}.queue_items').fetchone()[0]
            assert binding == active_transaction.id
            pause(active_transaction)
        return super().execute(sql, *args, **kwargs)
def paused_connect(*args, **kwargs):
    return connect(*args, factory=PausedConnection, **kwargs)
sqlite3.connect = paused_connect
"""


@contextmanager
def _worker(queue_path, transaction_path, ready, release, *, phase=None):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [sys.executable, "-c", (_INITIAL_PAUSE if phase else "") + _WORKER,
         str(queue_path), str(transaction_path), str(ready), str(release),
         *([phase] if phase else [])],
        env=env,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def _wait_ready(process, ready):
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert process.poll() is None, f"worker exited early: {process.returncode}"
        assert time.monotonic() < deadline, "worker did not reach its checkpoint"
        time.sleep(0.02)


def _assert_storage(queue, transaction_path, count):
    items = queue.list_items()
    assert len(items) == count
    assert len({item.transaction_id for item in items}) == count
    for item in items:
        assert item.status == QueueStatus.SUCCESSFUL
        transaction = load_transaction(item.transaction_id, str(transaction_path), readonly=True)
        assert transaction.status == Status.SUCCESSFUL
        assert transaction.state == {"first_runs": 1, "finished": True}
        assert all(step.status == Status.SUCCESSFUL for step in transaction.steps)
        assert len(queue.list_attempts(item.id)) >= 1
    for path in {Path(queue.db_path), transaction_path}:
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("shared", [False, True], ids=["separate-files", "shared-file"])
@pytest.mark.parametrize("phase", ["before-rows", "after-rows", "before-commit", "after-commit"])
def test_initial_transaction_and_binding_survive_or_roll_back_together(tmp_path, shared, phase):
    queue_path = tmp_path / "queue.db"
    transaction_path = queue_path if shared else tmp_path / "transactions.db"
    queue = SqliteQueue({"db_path": str(queue_path), "lease_timeout": 60})
    queue.add(QueueItem(reference="initial-crash", payload={}))
    ready, release = tmp_path / "ready", tmp_path / "release"
    with _worker(queue_path, transaction_path, ready, release, phase=phase) as interrupted:
        _wait_ready(interrupted, ready)
        interrupted_id = ready.read_text(encoding="utf-8")
        interrupted.kill()
        assert interrupted.wait(timeout=10) != 0
    stale = queue.list_items()[0]
    committed = phase == "after-commit"
    assert stale.transaction_id == (interrupted_id if committed else "")
    with closing(sqlite3.connect(transaction_path)) as connection:
        rows = connection.execute("SELECT id, revision, status, state FROM transactions").fetchall()
        assert rows == ([(interrupted_id, 1, "pending", "{}")] if committed else [])
        assert connection.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == (2 if committed else 0)
        for table in ("transaction_history", "exceptions", "transaction_metadata", "transaction_artifacts"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert not release.exists()
    # Reclaim must resume the committed ID; a rolled-back initialization may create one.
    with closing(sqlite3.connect(queue_path)) as connection, connection:
        connection.execute("UPDATE queue_items SET claimed_at = '2000-01-01T00:00:00+00:00'")
    with _worker(queue_path, transaction_path, tmp_path / "resumed", release) as successor:
        _wait_ready(successor, tmp_path / "resumed")
        current = queue.list_items()[0]
        assert current.claim_token != stale.claim_token
        assert (current.transaction_id == interrupted_id) is committed
        release.touch()
        assert successor.wait(timeout=20) == 0
    _assert_storage(queue, transaction_path, 1)
    with closing(sqlite3.connect(transaction_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    attempts = queue.list_attempts(stale.id)
    assert len(attempts) == 2
    assert attempts[0].claim_token != attempts[1].claim_token


@pytest.mark.parametrize("shared", [False, True], ids=["separate-files", "shared-file"])
def test_two_processes_checkpoint_distinct_claims(tmp_path, shared):
    queue_path = tmp_path / "queue.db"
    transaction_path = queue_path if shared else tmp_path / "transactions.db"
    queue = SqliteQueue({"db_path": str(queue_path)})
    _prepare_transaction_database(str(transaction_path))
    for index in range(2):
        queue.add(QueueItem(reference=f"item-{index}", payload={}))
    release = tmp_path / "release"
    ready_a, ready_b = tmp_path / "ready-a", tmp_path / "ready-b"
    with _worker(queue_path, transaction_path, ready_a, release) as first:
        with _worker(queue_path, transaction_path, ready_b, release) as second:
            _wait_ready(first, ready_a)
            _wait_ready(second, ready_b)
            items = queue.list_items()
            assert all(item.status == QueueStatus.IN_PROGRESS for item in items)
            assert len({item.claim_token for item in items}) == 2
            assert len({item.transaction_id for item in items}) == 2
            for item in items:
                checkpoint = load_transaction(item.transaction_id, str(transaction_path), readonly=True)
                assert checkpoint.steps[0].status == Status.SUCCESSFUL
                assert checkpoint.steps[1].status == Status.IN_PROGRESS
            release.touch()
            assert first.wait(timeout=20) == 0
            assert second.wait(timeout=20) == 0
    _assert_storage(queue, transaction_path, 2)


@pytest.mark.parametrize("shared", [False, True], ids=["separate-files", "shared-file"])
def test_killed_worker_resumes_bound_checkpoint_and_fences_old_claim(tmp_path, shared):
    queue_path = tmp_path / "queue.db"
    transaction_path = queue_path if shared else tmp_path / "transactions.db"
    queue = SqliteQueue({"db_path": str(queue_path), "lease_timeout": 60})
    queue.add(QueueItem(reference="restart", payload={}))
    ready, release = tmp_path / "ready", tmp_path / "release"
    with _worker(queue_path, transaction_path, ready, release) as interrupted:
        _wait_ready(interrupted, ready)
        stale = queue.list_items()[0]
        snapshot = load_transaction(stale.transaction_id, str(transaction_path), readonly=True)
        assert snapshot.steps[0].status == Status.SUCCESSFUL
        with closing(sqlite3.connect(transaction_path)) as connection:
            revision = connection.execute(
                "SELECT revision FROM transactions WHERE id = ?", (snapshot.id,)
            ).fetchone()[0]
        interrupted.kill()
        assert interrupted.wait(timeout=10) != 0
    # Simulate elapsed downtime without sleeping through a real lease timeout.
    with closing(sqlite3.connect(queue_path)) as connection, connection:
        connection.execute("UPDATE queue_items SET claimed_at = '2000-01-01T00:00:00+00:00'")
    ready = tmp_path / "resumed"
    with _worker(queue_path, transaction_path, ready, release) as successor:
        _wait_ready(successor, ready)
        current = queue.get_item(stale.id)
        assert current.transaction_id == stale.transaction_id
        assert current.claim_token != stale.claim_token
        before = transaction_path.read_bytes()
        snapshot.state["stale"] = True
        with pytest.raises(TransactionFenceError, match="claim is stale"):
            _save_queue_transaction_fenced(
                snapshot, db_path=str(transaction_path), queue_db_path=str(queue_path),
                queue_item_id=stale.id, claimed_by=stale.claimed_by,
                claim_token=stale.claim_token, expected_revision=revision,
            )
        assert transaction_path.read_bytes() == before
        release.touch()
        assert successor.wait(timeout=20) == 0
    _assert_storage(queue, transaction_path, 1)
    assert len(queue.list_attempts(stale.id)) == 2


@pytest.mark.parametrize("wal_target", ["queue", "transactions", "shared"])
def test_wal_conversion_and_active_checkpoint_rejection(tmp_path, wal_target):
    queue_path = tmp_path / "queue.db"
    transaction_path = queue_path if wal_target == "shared" else tmp_path / "transactions.db"
    queue = SqliteQueue({"db_path": str(queue_path)})
    queued = QueueItem(reference="wal", payload={})
    queue.add(queued)
    transaction = Transaction("wal", state={"original": True})
    save_transaction(transaction, str(transaction_path))
    for path in {queue_path, transaction_path}:
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    queue = SqliteQueue({"db_path": str(queue_path)})
    _prepare_transaction_database(str(transaction_path))
    for path in {queue_path, transaction_path}:
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert queue.get_item(queued.id).reference == "wal"
    assert load_transaction(transaction.id, str(transaction_path), readonly=True).state == {"original": True}
    claim = queue.next_item("worker")
    target = transaction_path if wal_target == "transactions" else queue_path
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    before = transaction_path.read_bytes()
    transaction.state["unexpected"] = True
    with pytest.raises(RuntimeError, match="rollback journal mode"):
        _save_queue_transaction_fenced(
            transaction, db_path=str(transaction_path), queue_db_path=str(queue_path),
            queue_item_id=claim.id, claimed_by=claim.claimed_by,
            claim_token=claim.claim_token, expected_revision=1,
        )
    assert transaction_path.read_bytes() == before
    assert load_transaction(transaction.id, str(transaction_path), readonly=True).state == {"original": True}
