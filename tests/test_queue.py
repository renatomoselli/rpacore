"""Tests for oref/queue.py and oref/runner.py."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from oref.context import ProcessContext
from oref.engine import Engine
from oref.queue import QueueItem, QueueProvider, QueueStatus, SqliteQueue
from oref.runner import run_queue_loop
from oref.skill import Skill
from oref.transaction import Transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_queue(tmp_path, **kwargs) -> SqliteQueue:
    db = str(tmp_path / "queue.db")
    return SqliteQueue({"db_path": db, **kwargs})


def make_item(reference: str = "ref", payload: dict | None = None) -> QueueItem:
    return QueueItem(reference=reference, payload=payload or {})


# ---------------------------------------------------------------------------
# TestSqliteQueueConfig
# ---------------------------------------------------------------------------

class TestSqliteQueueConfig:
    def test_default_config(self, tmp_path):
        q = make_queue(tmp_path)
        assert q.claim_timeout == 30
        assert q.max_retries == 3

    def test_custom_config(self, tmp_path):
        q = make_queue(tmp_path, claim_timeout=60, max_retries=5)
        assert q.claim_timeout == 60
        assert q.max_retries == 5

    def test_bad_db_path_type(self):
        with pytest.raises(TypeError, match="db_path"):
            SqliteQueue({"db_path": 123})

    def test_bad_claim_timeout_type(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(TypeError, match="claim_timeout"):
            SqliteQueue({"db_path": db, "claim_timeout": "30"})

    def test_claim_timeout_bool_rejected(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(TypeError, match="claim_timeout"):
            SqliteQueue({"db_path": db, "claim_timeout": True})

    def test_bad_max_retries_type(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(TypeError, match="max_retries"):
            SqliteQueue({"db_path": db, "max_retries": 3.0})

    def test_max_retries_bool_rejected(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(TypeError, match="max_retries"):
            SqliteQueue({"db_path": db, "max_retries": False})

    def test_claim_timeout_zero(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(ValueError, match="claim_timeout"):
            SqliteQueue({"db_path": db, "claim_timeout": 0})

    def test_max_retries_negative(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(ValueError, match="max_retries"):
            SqliteQueue({"db_path": db, "max_retries": -1})


# ---------------------------------------------------------------------------
# TestSqliteQueueCRUD
# ---------------------------------------------------------------------------

class TestSqliteQueueCRUD:
    def test_add_and_next_item(self, tmp_path):
        q = make_queue(tmp_path)
        item = make_item("order-1", {"amount": 42})
        q.add(item)
        claimed = q.next_item("worker-1")
        assert claimed is not None
        assert claimed.reference == "order-1"
        assert claimed.payload == {"amount": 42}
        assert claimed.status == QueueStatus.IN_PROGRESS
        assert claimed.claimed_by == "worker-1"

    def test_next_item_empty_returns_none(self, tmp_path):
        q = make_queue(tmp_path)
        assert q.next_item() is None

    def test_get_item_returns_item(self, tmp_path):
        q = make_queue(tmp_path)
        item = make_item("ref-get")
        q.add(item)
        fetched = q.get_item(item.id)
        assert fetched is not None
        assert fetched.reference == "ref-get"
        assert fetched.status == QueueStatus.PENDING

    def test_get_item_unknown_returns_none(self, tmp_path):
        q = make_queue(tmp_path)
        assert q.get_item("no-such-id") is None

    def test_complete_marks_successful(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item())
        item = q.next_item()
        q.complete(item.id)
        stored = q.get_item(item.id)
        assert stored.status == QueueStatus.SUCCESSFUL

    def test_fail_resets_to_pending_under_max(self, tmp_path):
        q = make_queue(tmp_path, max_retries=2)
        q.add(make_item())
        item = q.next_item()
        q.fail(item.id)
        retried = q.next_item()
        assert retried is not None
        assert retried.retry_count == 1
        assert retried.status == QueueStatus.IN_PROGRESS

    def test_fail_marks_failed_at_max_retries(self, tmp_path):
        q = make_queue(tmp_path, max_retries=1)
        q.add(make_item())
        item = q.next_item()
        q.fail(item.id)   # retry_count → 1, still <= max
        item2 = q.next_item()
        q.fail(item2.id)  # retry_count → 2, > max → FAILED
        stored = q.get_item(item.id)
        assert stored.status == QueueStatus.FAILED

    def test_fail_unknown_id_is_noop(self, tmp_path):
        q = make_queue(tmp_path)
        q.fail("nonexistent-id")  # should not raise


# ---------------------------------------------------------------------------
# TestSqliteQueueFIFO
# ---------------------------------------------------------------------------

class TestSqliteQueueFIFO:
    def test_fifo_order(self, tmp_path):
        q = make_queue(tmp_path)
        for ref in ["first", "second", "third"]:
            q.add(make_item(ref))
        for expected in ["first", "second", "third"]:
            item = q.next_item()
            assert item.reference == expected
            q.complete(item.id)


# ---------------------------------------------------------------------------
# TestSqliteQueueAtomicClaim
# ---------------------------------------------------------------------------

class TestSqliteQueueAtomicClaim:
    def test_concurrent_workers_claim_distinct_items(self, tmp_path):
        """Two threads racing on next_item() must not claim the same item."""
        q = make_queue(tmp_path)
        for i in range(5):
            q.add(make_item(f"item-{i}"))

        claimed_ids: list[str] = []
        lock = threading.Lock()

        def worker():
            item = q.next_item()
            if item:
                with lock:
                    claimed_ids.append(item.id)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(claimed_ids) == len(set(claimed_ids)), "Duplicate claims detected"


# ---------------------------------------------------------------------------
# TestSqliteQueueStaleReclaim
# ---------------------------------------------------------------------------

class TestSqliteQueueStaleReclaim:
    def test_stale_item_is_reclaimed(self, tmp_path):
        q = make_queue(tmp_path, claim_timeout=1)
        q.add(make_item("stale"))
        item = q.next_item("worker-a")
        assert item is not None

        # Simulate staleness by back-dating claimed_at in the DB.
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(tmp_path / "queue.db"))
        conn.execute(
            "UPDATE queue_items SET claimed_at = datetime('now', '-10 seconds') WHERE id = ?",
            (item.id,),
        )
        conn.commit()
        conn.close()

        reclaimed = q.next_item("worker-b")
        assert reclaimed is not None
        assert reclaimed.id == item.id
        assert reclaimed.claimed_by == "worker-b"


# ---------------------------------------------------------------------------
# TestQueueProtocol
# ---------------------------------------------------------------------------

class TestQueueProtocol:
    def test_sqlite_queue_satisfies_protocol(self, tmp_path):
        q = make_queue(tmp_path)
        assert isinstance(q, QueueProvider)


# ---------------------------------------------------------------------------
# TestRunQueueLoop
# ---------------------------------------------------------------------------

class _SuccessSkill(Skill):
    def __init__(self) -> None:
        super().__init__("success", 1)

    def execute(self, ctx: ProcessContext) -> None:
        pass


class _FailSkill(Skill):
    def __init__(self) -> None:
        super().__init__("fail", 1)

    def execute(self, ctx: ProcessContext) -> None:
        raise RuntimeError("skill boom")


class TestRunQueueLoop:
    def _make_ctx_parts(self):
        engine = Engine(max_retries=0)
        from oref.credentials import EnvCredentialProvider
        credentials = EnvCredentialProvider()
        config: dict = {}
        return engine, credentials, config

    def test_full_cycle_completes_item(self, tmp_path):
        q = make_queue(tmp_path, max_retries=0)
        item = make_item("ref-1")
        q.add(item)

        engine, credentials, config = self._make_ctx_parts()

        def build_transaction(qi: QueueItem) -> Transaction:
            t = Transaction(reference=qi.reference)
            t.skills = [_SuccessSkill()]
            return t

        run_queue_loop(q, engine, build_transaction, config, credentials)
        stored = q.get_item(item.id)
        assert stored.status == QueueStatus.SUCCESSFUL

    def test_engine_exception_calls_fail(self, tmp_path):
        q = make_queue(tmp_path, max_retries=0)
        item = make_item("ref-fail")
        q.add(item)

        engine, credentials, config = self._make_ctx_parts()

        def build_transaction(qi: QueueItem) -> Transaction:
            t = Transaction(reference=qi.reference)
            t.skills = [_FailSkill()]
            return t

        run_queue_loop(q, engine, build_transaction, config, credentials)
        stored = q.get_item(item.id)
        assert stored.status == QueueStatus.FAILED, f"Expected failed, got {stored.status}"

    def test_payload_available_in_ctx_data(self, tmp_path):
        q = make_queue(tmp_path, max_retries=0)
        q.add(make_item("ref-data", payload={"key": "value"}))

        engine, credentials, config = self._make_ctx_parts()
        captured: list[dict] = []

        class _CaptureSkill(Skill):
            def __init__(self) -> None:
                super().__init__("capture", 1)

            def execute(self, ctx: ProcessContext) -> None:
                captured.append(dict(ctx.data))

        def build_transaction(item: QueueItem) -> Transaction:
            t = Transaction(reference=item.reference)
            t.skills = [_CaptureSkill()]
            return t

        run_queue_loop(q, engine, build_transaction, config, credentials)
        assert captured == [{"key": "value"}]

    def test_empty_queue_returns_immediately(self, tmp_path):
        q = make_queue(tmp_path)
        engine, credentials, config = self._make_ctx_parts()
        run_queue_loop(q, engine, lambda item: Transaction(reference=item.reference), config, credentials)
        # No exception = pass

    def test_worker_id_propagated(self, tmp_path):
        q = make_queue(tmp_path, max_retries=0)
        item = make_item("ref-worker")
        q.add(item)

        engine, credentials, config = self._make_ctx_parts()

        def build_transaction(qi: QueueItem) -> Transaction:
            t = Transaction(reference=qi.reference)
            t.skills = [_SuccessSkill()]
            return t

        run_queue_loop(q, engine, build_transaction, config, credentials, worker_id="test-worker")

        stored = q.get_item(item.id)
        assert stored.status == QueueStatus.SUCCESSFUL
        assert stored.claimed_by == "test-worker"

    def test_default_worker_id_resolves_to_hostname(self, tmp_path):
        """When worker_id is omitted, claimed_by is set to the actual hostname."""
        import socket
        q = make_queue(tmp_path, max_retries=0)
        item = make_item("ref-hostname")
        q.add(item)

        engine, credentials, config = self._make_ctx_parts()

        def build_transaction(qi: QueueItem) -> Transaction:
            t = Transaction(reference=qi.reference)
            t.skills = [_SuccessSkill()]
            return t

        run_queue_loop(q, engine, build_transaction, config, credentials)

        stored = q.get_item(item.id)
        assert stored.status == QueueStatus.SUCCESSFUL
        assert stored.claimed_by == socket.gethostname()
