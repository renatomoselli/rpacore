"""Tests for rpacore/queue.py and rpacore/runner.py."""

from __future__ import annotations

import threading
import time
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, SystemException
from rpacore.queue import QueueItem, QueueProvider, QueueStatus, SqliteQueue
from rpacore.runner import run_queue_loop
from rpacore.skill import Skill
from rpacore.transaction import Transaction


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
        with pytest.raises(TypeError) as exc_info:
            SqliteQueue({"db_path": 123})

        assert str(exc_info.value) == "queue.db_path expected str; got int value=123"

    def test_bad_claim_timeout_type(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(TypeError) as exc_info:
            SqliteQueue({"db_path": db, "claim_timeout": "30"})

        assert str(exc_info.value) == (
            "queue.claim_timeout expected int; got str value='30'"
        )

    def test_claim_timeout_bool_rejected(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(TypeError, match="claim_timeout"):
            SqliteQueue({"db_path": db, "claim_timeout": True})

    def test_bad_max_retries_type(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(TypeError) as exc_info:
            SqliteQueue({"db_path": db, "max_retries": 3.0})

        assert str(exc_info.value) == (
            "queue.max_retries expected int; got float value=3.0"
        )

    def test_max_retries_bool_rejected(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(TypeError, match="max_retries"):
            SqliteQueue({"db_path": db, "max_retries": False})

    def test_claim_timeout_zero(self, tmp_path):
        db = str(tmp_path / "q.db")
        with pytest.raises(ValueError) as exc_info:
            SqliteQueue({"db_path": db, "claim_timeout": 0})

        assert str(exc_info.value) == (
            "queue.claim_timeout expected int > 0; got int value=0"
        )

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

    def test_add_rejects_non_json_safe_payload(self, tmp_path):
        q = make_queue(tmp_path)
        item = make_item("bad", {"client": object()})

        with pytest.raises(TypeError) as exc_info:
            q.add(item)

        assert "queue item payload['client'] expected JSON value" in str(exc_info.value)

    def test_add_rejects_circular_payload(self, tmp_path):
        q = make_queue(tmp_path)
        payload: dict[str, object] = {}
        payload["self"] = payload
        item = make_item("bad", payload)

        with pytest.raises(TypeError) as exc_info:
            q.add(item)

        assert "queue item payload['self'] expected acyclic JSON value" in str(exc_info.value)

    def test_add_rejects_non_object_payload(self, tmp_path):
        q = make_queue(tmp_path)
        item = make_item("bad")
        item.payload = ["invoice"]  # type: ignore[assignment]

        with pytest.raises(TypeError) as exc_info:
            q.add(item)

        assert "queue item payload expected JSON object" in str(exc_info.value)

    def test_next_item_rejects_pre_existing_non_object_payload(self, tmp_path):
        q = make_queue(tmp_path)
        conn = sqlite3.connect(q.db_path)
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO queue_items "
                "(id, reference, payload, status, retry_count, created_at, claimed_by, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("bad-payload", "bad", "[]", "pending", 0, now, "", None),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(TypeError) as exc_info:
            q.next_item("worker")

        assert "queue item payload expected JSON object" in str(exc_info.value)
        conn = sqlite3.connect(q.db_path)
        try:
            status = conn.execute(
                "SELECT status FROM queue_items WHERE id = ?", ("bad-payload",)
            ).fetchone()[0]
        finally:
            conn.close()
        assert status == "pending"

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

    def test_fail_can_mark_terminal_without_retry(self, tmp_path):
        q = make_queue(tmp_path, max_retries=3)
        q.add(make_item())
        item = q.next_item()
        q.fail(item.id, retry=False)
        stored = q.get_item(item.id)
        assert stored is not None
        assert stored.status == QueueStatus.FAILED
        assert stored.retry_count == 1

    def test_fail_unknown_id_is_noop(self, tmp_path):
        q = make_queue(tmp_path)
        q.fail("nonexistent-id")  # should not raise


# ---------------------------------------------------------------------------
# TestSqliteQueueIntrospection
# ---------------------------------------------------------------------------

class TestSqliteQueueIntrospection:
    def test_list_items_returns_deterministic_order(self, tmp_path):
        q = make_queue(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later = make_item("later")
        later.id = "b"
        later.created_at = base + timedelta(seconds=1)
        first_tie = make_item("first-tie")
        first_tie.id = "a"
        first_tie.created_at = base
        second_tie = make_item("second-tie")
        second_tie.id = "c"
        second_tie.created_at = base

        q.add(later)
        q.add(second_tie)
        q.add(first_tie)

        assert [item.reference for item in q.list_items()] == [
            "first-tie",
            "second-tie",
            "later",
        ]

    def test_list_items_filters_by_status(self, tmp_path):
        q = make_queue(tmp_path, max_retries=0)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        pending = make_item("pending")
        pending.created_at = base
        successful = make_item("successful")
        successful.created_at = base + timedelta(seconds=1)
        failed = make_item("failed")
        failed.created_at = base + timedelta(seconds=2)
        q.add(pending)
        q.add(successful)
        q.add(failed)

        claimed_success = q.next_item("worker")
        q.complete(claimed_success.id, claimed_by="worker")
        claimed_failed = q.next_item("worker")
        q.fail(claimed_failed.id, retry=False, claimed_by="worker")

        assert [item.reference for item in q.list_items(statuses=[QueueStatus.PENDING])] == ["failed"]
        assert [item.reference for item in q.list_items(statuses=[QueueStatus.SUCCESSFUL])] == ["pending"]

    def test_list_items_accepts_empty_statuses(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))

        assert q.list_items(statuses=[]) == []

    def test_list_items_accepts_generator_statuses(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))

        statuses = (status for status in [QueueStatus.PENDING])

        assert [item.reference for item in q.list_items(statuses=statuses)] == ["invoice-1"]

    def test_list_items_can_limit_and_offset(self, tmp_path):
        q = make_queue(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index, reference in enumerate(["one", "two", "three"]):
            item = make_item(reference)
            item.created_at = base + timedelta(seconds=index)
            q.add(item)

        assert [item.reference for item in q.list_items(limit=2)] == ["one", "two"]
        assert [item.reference for item in q.list_items(limit=2, offset=1)] == ["two", "three"]

    def test_has_reference_filters_by_status(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))

        assert q.has_reference("invoice-1")
        assert q.has_reference("invoice-1", statuses=[QueueStatus.PENDING])
        assert not q.has_reference("invoice-1", statuses=[QueueStatus.SUCCESSFUL])
        assert not q.has_reference("invoice-2")

    def test_has_reference_accepts_empty_statuses(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))

        assert not q.has_reference("invoice-1", statuses=[])

    def test_has_reference_accepts_generator_statuses(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))

        statuses = (status for status in [QueueStatus.PENDING])

        assert q.has_reference("invoice-1", statuses=statuses)

    def test_add_once_skips_active_duplicate_reference(self, tmp_path):
        q = make_queue(tmp_path)

        assert q.add_once(make_item("invoice-1")) is True
        assert q.add_once(make_item("invoice-1")) is False

        items = q.list_items()
        assert len(items) == 1
        assert items[0].reference == "invoice-1"

    def test_add_once_skips_in_progress_duplicate_reference(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))
        claimed = q.next_item("worker")
        assert claimed is not None

        assert q.add_once(make_item("invoice-1")) is False

        items = q.list_items()
        assert len(items) == 1
        assert items[0].status is QueueStatus.IN_PROGRESS

    def test_add_once_allows_terminal_reference_by_default(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))
        claimed = q.next_item("worker")
        assert claimed is not None
        q.complete(claimed.id, claimed_by="worker")

        assert q.add_once(make_item("invoice-1")) is True

        items = q.list_items()
        assert [item.status for item in items] == [
            QueueStatus.SUCCESSFUL,
            QueueStatus.PENDING,
        ]

    def test_add_once_can_treat_terminal_reference_as_active(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))
        claimed = q.next_item("worker")
        assert claimed is not None
        q.complete(claimed.id, claimed_by="worker")

        inserted = q.add_once(
            make_item("invoice-1"),
            active_statuses=[QueueStatus.PENDING, QueueStatus.IN_PROGRESS, QueueStatus.SUCCESSFUL],
        )

        assert inserted is False
        assert len(q.list_items()) == 1

    def test_add_once_with_active_statuses_none_checks_any_status(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("invoice-1"))
        claimed = q.next_item("worker")
        assert claimed is not None
        q.complete(claimed.id, claimed_by="worker")

        inserted = q.add_once(make_item("invoice-1"), active_statuses=None)

        assert inserted is False
        assert len(q.list_items()) == 1

    def test_add_once_with_empty_active_statuses_inserts_unconditionally(self, tmp_path):
        q = make_queue(tmp_path)

        assert q.add_once(make_item("invoice-1"), active_statuses=[]) is True
        assert q.add_once(make_item("invoice-1"), active_statuses=[]) is True
        assert len(q.list_items()) == 2

    def test_queue_schema_has_introspection_indexes(self, tmp_path):
        q = make_queue(tmp_path)
        conn = sqlite3.connect(q.db_path)
        try:
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(queue_items)").fetchall()}
        finally:
            conn.close()

        assert "idx_queue_items_created_at_id" in indexes
        assert "idx_queue_items_status_created_at_id" in indexes
        assert "idx_queue_items_reference_status" in indexes

    def test_component_schema_version_recorded(self, tmp_path):
        q = make_queue(tmp_path)
        conn = sqlite3.connect(q.db_path)
        try:
            version = conn.execute(
                "SELECT version FROM rpacore_schema_versions WHERE component = 'queue'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert version == 1

    def test_add_recreates_schema_if_database_file_is_deleted_after_init(self, tmp_path):
        q = make_queue(tmp_path)
        Path(q.db_path).unlink()

        q.add(make_item("after-delete"))

        assert [item.reference for item in q.list_items()] == ["after-delete"]


# ---------------------------------------------------------------------------
# TestSqliteQueueFIFO
# ---------------------------------------------------------------------------

class TestSqliteQueueFIFO:
    def test_fifo_order(self, tmp_path):
        from datetime import timedelta
        q = make_queue(tmp_path)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i, ref in enumerate(["first", "second", "third"]):
            item = make_item(ref)
            item.created_at = base + timedelta(seconds=i)
            q.add(item)
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

    def test_locked_database_exposes_lock_error(self, tmp_path):
        q = make_queue(tmp_path)
        q.add(make_item("locked"))

        db_path = str(tmp_path / "queue.db")
        lock_conn = sqlite3.connect(db_path, timeout=1)
        try:
            lock_conn.execute("BEGIN IMMEDIATE")

            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                q.next_item("blocked-worker")
        finally:
            lock_conn.rollback()
            lock_conn.close()


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

    def test_complete_rejects_stale_original_claim(self, tmp_path):
        q = make_queue(tmp_path, claim_timeout=1)
        q.add(make_item("stale-complete"))
        item = q.next_item("worker-a")
        assert item is not None

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

        with pytest.raises(RuntimeError, match="no longer claimed"):
            q.complete(item.id, claimed_by="worker-a")

        stored = q.get_item(item.id)
        assert stored is not None
        assert stored.status is QueueStatus.IN_PROGRESS
        assert stored.claimed_by == "worker-b"

    def test_fail_rejects_stale_original_claim(self, tmp_path):
        q = make_queue(tmp_path, claim_timeout=1)
        q.add(make_item("stale-fail"))
        item = q.next_item("worker-a")
        assert item is not None

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

        with pytest.raises(RuntimeError, match="no longer claimed"):
            q.fail(item.id, claimed_by="worker-a")

        stored = q.get_item(item.id)
        assert stored is not None
        assert stored.status is QueueStatus.IN_PROGRESS
        assert stored.retry_count == 0
        assert stored.claimed_by == "worker-b"


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


class _BusinessFailSkill(Skill):
    def __init__(self) -> None:
        super().__init__("business_fail", 1)

    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException("bad data", action=self.name)


class _SystemFailSkill(Skill):
    def __init__(self) -> None:
        super().__init__("system_fail", 1)

    def execute(self, ctx: ProcessContext) -> None:
        raise SystemException("service unavailable", action=self.name)


class TestRunQueueLoop:
    def _make_ctx_parts(self):
        engine = Engine(max_retries=0)
        from rpacore.credentials import EnvCredentialProvider
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

    def test_business_failure_does_not_requeue_by_default(self, tmp_path):
        q = make_queue(tmp_path, max_retries=3)
        item = make_item("ref-business")
        q.add(item)

        engine, credentials, config = self._make_ctx_parts()

        def build_transaction(qi: QueueItem) -> Transaction:
            return Transaction(reference=qi.reference, skills=[_BusinessFailSkill()])

        run_queue_loop(q, engine, build_transaction, config, credentials)
        stored = q.get_item(item.id)
        assert stored is not None
        assert stored.status == QueueStatus.FAILED
        assert stored.retry_count == 1
        assert q.next_item("worker-2") is None

    def test_system_failure_still_follows_queue_retry_policy(self, tmp_path):
        q = make_queue(tmp_path, max_retries=3)
        item = make_item("ref-system")
        q.add(item)

        engine, credentials, config = self._make_ctx_parts()
        stop_event = threading.Event()

        def build_transaction(qi: QueueItem) -> Transaction:
            return Transaction(reference=qi.reference, skills=[_SystemFailSkill()])

        run_queue_loop(
            q,
            engine,
            build_transaction,
            config,
            credentials,
            after_item=lambda item, tx, err: stop_event.set(),
            stop_event=stop_event,
        )
        stored = q.get_item(item.id)
        assert stored is not None
        assert stored.status == QueueStatus.PENDING
        assert stored.retry_count == 1

    def test_business_failure_can_follow_queue_retry_policy(self, tmp_path):
        q = make_queue(tmp_path, max_retries=3)
        item = make_item("ref-business-retry")
        q.add(item)

        engine, credentials, config = self._make_ctx_parts()
        stop_event = threading.Event()

        def build_transaction(qi: QueueItem) -> Transaction:
            return Transaction(reference=qi.reference, skills=[_BusinessFailSkill()])

        run_queue_loop(
            q,
            engine,
            build_transaction,
            config,
            credentials,
            after_item=lambda item, tx, err: stop_event.set(),
            stop_event=stop_event,
            retry_business_failures=True,
        )
        stored = q.get_item(item.id)
        assert stored is not None
        assert stored.status == QueueStatus.PENDING
        assert stored.retry_count == 1

    def test_payload_available_in_transaction_state(self, tmp_path):
        q = make_queue(tmp_path, max_retries=0)
        q.add(make_item("ref-data", payload={"key": "value"}))

        engine, credentials, config = self._make_ctx_parts()
        captured: list[dict] = []

        class _CaptureSkill(Skill):
            def __init__(self) -> None:
                super().__init__("capture", 1)

            def execute(self, ctx: ProcessContext) -> None:
                captured.append(dict(ctx.state))

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

    def test_notifier_called_after_item(self, tmp_path):
        """Notifiers receive a TransactionReport after each engine run."""
        from rpacore.notify import Notifier
        from rpacore.report import TransactionReport

        sent: list[TransactionReport] = []

        class _RecordingNotifier:
            def send(self, report: TransactionReport) -> None:
                sent.append(report)

        q = make_queue(tmp_path, max_retries=0)
        q.add(make_item("ref-notify"))
        engine, credentials, config = self._make_ctx_parts()

        def build_transaction(qi: QueueItem) -> Transaction:
            t = Transaction(reference=qi.reference)
            t.skills = [_SuccessSkill()]
            return t

        run_queue_loop(q, engine, build_transaction, config, credentials, notifiers=[_RecordingNotifier()])
        assert len(sent) == 1
        assert sent[0].reference == "ref-notify"

    def test_notifiers_default_is_empty(self, tmp_path):
        """run_queue_loop works without a notifiers argument (backward compat)."""
        q = make_queue(tmp_path, max_retries=0)
        q.add(make_item("ref-no-notify"))
        engine, credentials, config = self._make_ctx_parts()

        def build_transaction(qi: QueueItem) -> Transaction:
            t = Transaction(reference=qi.reference)
            t.skills = [_SuccessSkill()]
            return t

        # Should not raise even though no notifiers are wired.
        run_queue_loop(q, engine, build_transaction, config, credentials)

    def test_stop_event_prevents_claiming_next_sqlite_item(self, tmp_path):
        from datetime import timedelta
        q = make_queue(tmp_path, max_retries=0)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = make_item("ref-stop-1")
        first.created_at = base
        second = make_item("ref-stop-2")
        second.created_at = base + timedelta(seconds=1)
        q.add(first)
        q.add(second)

        engine, credentials, config = self._make_ctx_parts()
        stop_event = threading.Event()
        seen: list[str] = []

        def build_transaction(qi: QueueItem) -> Transaction:
            t = Transaction(reference=qi.reference)
            t.skills = [_SuccessSkill()]
            return t

        def after_item(item: QueueItem, tx: Transaction | None, err: Exception | None) -> None:
            seen.append(item.id)
            stop_event.set()

        run_queue_loop(
            q,
            engine,
            build_transaction,
            config,
            credentials,
            after_item=after_item,
            stop_event=stop_event,
        )

        first_stored = q.get_item(first.id)
        second_stored = q.get_item(second.id)
        assert seen == [first.id]
        assert first_stored is not None
        assert first_stored.status == QueueStatus.SUCCESSFUL
        assert second_stored is not None
        assert second_stored.status == QueueStatus.PENDING
        assert second_stored.claimed_by == ""
        assert second_stored.claimed_at is None
