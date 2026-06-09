"""Tests for rpacore.runner — QueueRunSummary and after_item callback."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest

from rpacore.context import ProcessContext
from rpacore.credentials import EnvCredentialProvider
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
from rpacore.persistence import list_transactions, load_transaction
from rpacore.queue import QueueItem, QueueStatus, SqliteQueue
import rpacore.runner as runner_module
from rpacore.runner import QueueRunSummary, run_queue_loop
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeQueue:
    """In-memory queue stub that drains a fixed list of items."""

    def __init__(self, items: list[QueueItem]) -> None:
        self._items = list(items)
        self.completed: list[str] = []
        self.failed: list[str] = []
        self.fail_retries: list[bool] = []
        self.bindings: list[tuple[str, str, str]] = []
        self.renewals: list[tuple[str, str]] = []

    def next_item(self, worker_id: str = "") -> QueueItem | None:
        return self._items.pop(0) if self._items else None

    def complete(self, item_id: str, *, claimed_by: str | None = None) -> None:
        self.completed.append(item_id)

    def bind_transaction(self, item_id: str, transaction_id: str, *, claimed_by: str) -> None:
        self.bindings.append((item_id, transaction_id, claimed_by))

    def renew_lease(self, item_id: str, *, claimed_by: str) -> None:
        self.renewals.append((item_id, claimed_by))

    def fail(self, item_id: str, *, retry: bool = True, claimed_by: str | None = None) -> None:
        self.failed.append(item_id)
        self.fail_retries.append(retry)


def _item(ref: str) -> QueueItem:
    return QueueItem(id=ref, reference=ref, payload={})


class _SuccessSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        pass


class _BusinessFailSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException("bad data", action=self.name)


class _SystemFailSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise SystemException("system down", action=self.name)


class _StateSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state[self.name] = "done"


def _make_engine_with(skill_cls: type[Skill]) -> tuple[Engine, Transaction]:
    """Return (engine, transaction) pre-loaded with one skill instance."""
    tx = Transaction(reference="T", skills=[skill_cls("step", 1)])
    return Engine(), tx


_CREDS = EnvCredentialProvider()


def _run(
    items: list[QueueItem],
    skill_cls: type[Skill] = _SuccessSkill,
    *,
    build_raises: Exception | None = None,
    after_item=None,
    stop_event: threading.Event | None = None,
    retry_business_failures: bool = False,
    transaction_db_path: str | None = None,
    logger=None,
    resource_scope=None,
    on_finish=None,
) -> tuple[QueueRunSummary, _FakeQueue]:
    """Run the queue loop with default stubs, forwarding optional runner behavior."""
    queue = _FakeQueue(items)

    def _build(item: QueueItem) -> Transaction:
        if build_raises is not None:
            raise build_raises
        return Transaction(reference=item.reference, skills=[skill_cls("step", 1)])

    summary = run_queue_loop(
        queue=queue,
        engine=Engine(),
        build_transaction=_build,
        config={},
        credentials=_CREDS,
        worker_id="test-worker",
        after_item=after_item,
        stop_event=stop_event,
        retry_business_failures=retry_business_failures,
        transaction_db_path=transaction_db_path,
        logger=logger,
        resource_scope=resource_scope,
        on_finish=on_finish,
    )
    return summary, queue


@contextmanager
def _resource_scope(resources: dict[str, object] | None):
    yield resources


# ---------------------------------------------------------------------------
# QueueRunSummary counts
# ---------------------------------------------------------------------------

class TestQueueRunSummary:
    def test_empty_queue_returns_zero_counts(self) -> None:
        summary, _ = _run([])
        assert summary == QueueRunSummary(processed=0, completed=0, failed=0)

    def test_all_successful(self) -> None:
        summary, queue = _run([_item("a"), _item("b"), _item("c")])
        assert summary.processed == 3
        assert summary.completed == 3
        assert summary.failed == 0
        assert queue.completed == ["a", "b", "c"]
        assert queue.failed == []

    def test_business_fail_counted_as_failed(self) -> None:
        summary, queue = _run(
            [_item("a"), _item("b")],
            skill_cls=_BusinessFailSkill,
        )
        assert summary.processed == 2
        assert summary.completed == 0
        assert summary.failed == 2
        assert queue.failed == ["a", "b"]
        assert queue.fail_retries == [False, False]

    def test_system_fail_retries_queue_item(self) -> None:
        summary, queue = _run(
            [_item("a"), _item("b")],
            skill_cls=_SystemFailSkill,
        )
        assert summary.processed == 2
        assert summary.completed == 0
        assert summary.failed == 2
        assert queue.failed == ["a", "b"]
        assert queue.fail_retries == [True, True]

    def test_business_failures_can_use_queue_retry_policy(self) -> None:
        summary, queue = _run(
            [_item("a")],
            skill_cls=_BusinessFailSkill,
            retry_business_failures=True,
        )
        assert summary.processed == 1
        assert summary.completed == 0
        assert summary.failed == 1
        assert queue.failed == ["a"]
        assert queue.fail_retries == [True]

    def test_build_transaction_error_counted_as_failed(self) -> None:
        summary, queue = _run(
            [_item("x")],
            build_raises=RuntimeError("boom"),
        )
        assert summary.processed == 1
        assert summary.completed == 0
        assert summary.failed == 1
        assert queue.failed == ["x"]
        assert queue.fail_retries == [True]

    def test_execution_validation_error_is_terminal_queue_failure(self) -> None:
        queue = _FakeQueue([_item("invalid")])

        def _build_invalid(item: QueueItem) -> Transaction:
            return Transaction(reference=item.reference, skills=[Skill("", 1)])

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=_build_invalid,
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
        )

        assert summary.processed == 1
        assert summary.completed == 0
        assert summary.failed == 1
        assert queue.failed == ["invalid"]
        assert queue.fail_retries == [False]

    def test_build_transaction_validation_error_is_terminal_queue_failure(self) -> None:
        summary, queue = _run(
            [_item("invalid")],
            build_raises=ExecutionValidationError("invalid transaction wiring"),
        )

        assert summary.failed == 1
        assert queue.failed == ["invalid"]
        assert queue.fail_retries == [False]

    def test_mixed_outcomes(self) -> None:
        queue = _FakeQueue([_item("ok"), _item("bad")])
        calls = 0

        def build(item: QueueItem) -> Transaction:
            nonlocal calls
            calls += 1
            skill = _SuccessSkill("step", 1) if item.reference == "ok" else _BusinessFailSkill("step", 1)
            return Transaction(reference=item.reference, skills=[skill])

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=build,
            config={},
            credentials=_CREDS,
            worker_id="w",
        )
        assert summary.processed == 2
        assert summary.completed == 1
        assert summary.failed == 1

    def test_mixed_business_and_system_failure_retries_queue_item(self) -> None:
        queue = _FakeQueue([_item("mixed")])

        def build(item: QueueItem) -> Transaction:
            return Transaction(
                reference=item.reference,
                skills=[
                    _BusinessFailSkill("validate", 1),
                    _SystemFailSkill("submit", 2),
                ],
            )

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=build,
            config={},
            credentials=_CREDS,
            worker_id="w",
        )

        assert summary.processed == 1
        assert summary.completed == 0
        assert summary.failed == 1
        assert queue.failed == ["mixed"]
        assert queue.fail_retries == [True]

    def test_returns_queue_run_summary_instance(self) -> None:
        summary, _ = _run([])
        assert isinstance(summary, QueueRunSummary)


# ---------------------------------------------------------------------------
# after_item callback
# ---------------------------------------------------------------------------

class TestAfterItem:
    def test_fires_once_per_item(self) -> None:
        calls: list[tuple] = []
        _run(
            [_item("a"), _item("b"), _item("c")],
            after_item=lambda item, tx, err: calls.append((item.id, tx, err)),
        )
        assert len(calls) == 3
        assert [c[0] for c in calls] == ["a", "b", "c"]

    def test_success_path_transaction_set_error_none(self) -> None:
        calls: list[tuple] = []
        _run(
            [_item("ok")],
            after_item=lambda item, tx, err: calls.append((item, tx, err)),
        )
        item, tx, err = calls[0]
        assert tx is not None
        assert tx.status is Status.SUCCESSFUL
        assert err is None

    def test_business_fail_transaction_set_error_none(self) -> None:
        """Business failures are normal paths — error should be None."""
        calls: list[tuple] = []
        _run(
            [_item("bad")],
            skill_cls=_BusinessFailSkill,
            after_item=lambda item, tx, err: calls.append((item, tx, err)),
        )
        item, tx, err = calls[0]
        assert tx is not None
        assert tx.status is Status.FAILED
        assert err is None

    def test_build_raises_transaction_none_error_set(self) -> None:
        calls: list[tuple] = []
        exc = RuntimeError("build failed")
        _run(
            [_item("crash")],
            build_raises=exc,
            after_item=lambda item, tx, err: calls.append((item, tx, err)),
        )
        item, tx, err = calls[0]
        assert tx is None
        assert err is exc

    def test_not_provided_loop_completes_normally(self) -> None:
        summary, _ = _run([_item("a"), _item("b")])
        assert summary.completed == 2

    def test_callback_exception_does_not_abort_loop(self) -> None:
        """A crashing after_item on a success path does not stop the loop."""
        def _bad_callback(item, tx, err):
            raise RuntimeError("callback crash")

        summary, queue = _run(
            [_item("a"), _item("b"), _item("c")],
            after_item=_bad_callback,
        )
        assert summary.processed == 3
        assert summary.completed == 3
        assert summary.failed == 0
        assert summary.callback_errors == 3
        assert queue.completed == ["a", "b", "c"]
        assert queue.failed == []

    def test_callback_exception_on_success_path_still_completes(self) -> None:
        """If after_item raises for an otherwise-successful item, the item is still
        marked complete in the queue (the automation ran). The error is counted in
        callback_errors so callers can inspect post-processing failures."""
        completed_items: list[str] = []

        def _fail_first(item, tx, err):
            if item.id == "a":
                raise RuntimeError("persist failed")
            completed_items.append(item.id)

        summary, queue = _run(
            [_item("a"), _item("b")],
            after_item=_fail_first,
        )
        assert summary.processed == 2
        assert summary.completed == 2
        assert summary.failed == 0
        assert summary.callback_errors == 1
        assert queue.completed == ["a", "b"]
        assert queue.failed == []
        assert completed_items == ["b"]

    def test_report_generation_exception_on_success_path_still_completes(self, monkeypatch) -> None:
        """Framework post-processing failures must not retry already completed work."""
        errors: list[Exception | None] = []

        def _boom_report(tx: Transaction) -> object:
            raise RuntimeError("report crash")

        monkeypatch.setattr(runner_module, "generate_report", _boom_report)

        summary, queue = _run(
            [_item("a")],
            after_item=lambda item, tx, err: errors.append(err),
        )
        assert summary.processed == 1
        assert summary.completed == 1
        assert summary.failed == 0
        assert summary.callback_errors == 0
        assert queue.completed == ["a"]
        assert queue.failed == []
        assert isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == "report crash"

    def test_report_generation_memory_error_propagates(self, monkeypatch) -> None:
        def _boom_report(tx: Transaction) -> object:
            raise MemoryError("out of memory")

        monkeypatch.setattr(runner_module, "generate_report", _boom_report)

        with pytest.raises(MemoryError, match="out of memory"):
            _run([_item("a")])

    def test_callback_exception_on_failure_path_still_fails(self) -> None:
        """If after_item raises for a failing item, queue.fail() is still called."""
        def _bad_callback(item, tx, err):
            raise RuntimeError("callback crash")

        summary, queue = _run(
            [_item("a"), _item("b")],
            skill_cls=_BusinessFailSkill,
            after_item=_bad_callback,
        )
        assert summary.processed == 2
        assert summary.completed == 0
        assert summary.failed == 2
        assert summary.callback_errors == 0
        assert queue.failed == ["a", "b"]

    def test_no_callback_errors_when_callback_succeeds(self) -> None:
        summary, _ = _run(
            [_item("a"), _item("b")],
            after_item=lambda item, tx, err: None,
        )
        assert summary.callback_errors == 0

    def test_after_item_memory_error_propagates(self) -> None:
        def _fail_callback(item, tx, err):
            raise MemoryError("out of memory")

        with pytest.raises(MemoryError, match="out of memory"):
            _run([_item("a")], after_item=_fail_callback)

    def test_after_item_runs_after_notification_before_final_transition(self) -> None:
        events: list[str] = []

        class RecordingQueue(_FakeQueue):
            def complete(self, item_id: str, *, claimed_by: str | None = None) -> None:
                events.append("complete")
                super().complete(item_id, claimed_by=claimed_by)

        class RecordingNotifier:
            def send(self, report) -> None:
                events.append("notify")

        queue = RecordingQueue([_item("ordered")])

        run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_SuccessSkill("step", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
            notifiers=[RecordingNotifier()],
            after_item=lambda item, tx, err: events.append("after_item"),
        )

        assert events == ["notify", "after_item", "complete"]

    def test_after_item_runs_after_failure_notification_before_fail_transition(self) -> None:
        events: list[str] = []

        class RecordingQueue(_FakeQueue):
            def fail(self, item_id: str, *, retry: bool = True, claimed_by: str | None = None) -> None:
                events.append("fail")
                super().fail(item_id, retry=retry, claimed_by=claimed_by)

        class RecordingNotifier:
            def send(self, report) -> None:
                events.append("notify")

        queue = RecordingQueue([_item("ordered-fail")])

        run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_BusinessFailSkill("step", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
            notifiers=[RecordingNotifier()],
            after_item=lambda item, tx, err: events.append("after_item"),
        )

        assert events == ["notify", "after_item", "fail"]

    def test_after_item_receives_transaction_but_no_resource_mapping(self) -> None:
        callback_args: list[tuple[QueueItem, Transaction | None, Exception | None]] = []

        class _UseResourceSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                assert ctx.resources["session"] == "shared"

        _run(
            [_item("resources")],
            skill_cls=_UseResourceSkill,
            resource_scope=_resource_scope({"session": "shared"}),
            after_item=lambda item, tx, err: callback_args.append((item, tx, err)),
        )

        item, tx, err = callback_args[0]
        assert item.id == "resources"
        assert tx is not None
        assert tx.status is Status.SUCCESSFUL
        assert err is None


# ---------------------------------------------------------------------------
# runner-managed transaction persistence
# ---------------------------------------------------------------------------

class TestRunnerManagedTransactionPersistence:
    def test_successful_transaction_saved_without_after_item(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")

        summary, queue = _run([_item("ok")], transaction_db_path=db_path)

        assert summary == QueueRunSummary(processed=1, completed=1, failed=0)
        assert queue.completed == ["ok"]
        transactions = list_transactions(db_path=db_path)
        assert len(transactions) == 1
        assert transactions[0].reference == "ok"
        assert transactions[0].status is Status.SUCCESSFUL
        assert transactions[0].skills[0].status is Status.SUCCESSFUL

    def test_failed_transaction_saved_without_after_item(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")

        summary, queue = _run(
            [_item("bad")],
            skill_cls=_BusinessFailSkill,
            transaction_db_path=db_path,
        )

        assert summary == QueueRunSummary(processed=1, completed=0, failed=1)
        assert queue.failed == ["bad"]
        transactions = list_transactions(db_path=db_path)
        assert len(transactions) == 1
        assert transactions[0].reference == "bad"
        assert transactions[0].status is Status.FAILED
        assert transactions[0].skills[0].status is Status.FAILED
        assert isinstance(transactions[0].skills[0].exceptions[0], BusinessException)

    def test_callback_failure_does_not_prevent_persistence(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")

        def _bad_callback(item, tx, err):
            raise RuntimeError("callback failed")

        summary, queue = _run(
            [_item("ok")],
            after_item=_bad_callback,
            transaction_db_path=db_path,
        )

        assert summary.callback_errors == 1
        assert summary.persistence_errors == 0
        assert queue.completed == ["ok"]
        transactions = list_transactions(db_path=db_path)
        assert len(transactions) == 1
        assert transactions[0].reference == "ok"

    def test_checkpoint_failure_is_counted_logged_and_prevents_completion(self, monkeypatch, caplog) -> None:
        def _fail_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            if transaction.history:
                raise RuntimeError("sqlite locked")

        monkeypatch.setattr(runner_module, "save_transaction", _fail_save)
        logger = logging.getLogger("test.runner.persistence")

        with caplog.at_level(logging.ERROR, logger=logger.name):
            summary, queue = _run([_item("ok")], transaction_db_path="broken.db", logger=logger)

        assert summary.persistence_errors == 1
        assert summary.completed == 0
        assert summary.failed == 1
        assert queue.completed == []
        assert queue.failed == ["ok"]
        assert queue.fail_retries == [True]
        assert any(
            record.__dict__.get("event") == "transaction_checkpoint_error"
            and record.__dict__.get("queue_item_id") == "ok"
            and record.__dict__.get("transaction_reference") == "ok"
            for record in caplog.records
        )

    def test_transient_sqlite_persistence_failure_is_retried(self, monkeypatch, caplog) -> None:
        attempts: list[str] = []
        sleeps: list[float] = []
        logger = logging.getLogger("test.runner.sqlite.retry")

        def _save_after_two_failures(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            attempts.append(transaction.reference)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(runner_module, "save_transaction", _save_after_two_failures)
        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))

        with caplog.at_level(logging.WARNING, logger=logger.name):
            summary, queue = _run(
                [_item("ok")],
                transaction_db_path="transactions.db",
                logger=logger,
            )

        assert summary.persistence_errors == 0
        assert queue.completed == ["ok"]
        assert attempts == ["ok", "ok", "ok", "ok", "ok", "ok", "ok"]
        assert sleeps == [0.05, 0.1]
        assert [
            record.__dict__.get("retry_delay_seconds")
            for record in caplog.records
            if record.__dict__.get("event") == "transaction_initial_persistence_retry"
        ] == [0.05, 0.1]

    def test_non_transient_sqlite_persistence_failure_is_not_retried(self, monkeypatch) -> None:
        attempts: list[str] = []
        sleeps: list[float] = []

        def _fail_with_non_transient_operational_error(
            transaction: Transaction,
            db_path: str = "rpacore.db",
        ) -> None:
            attempts.append(transaction.reference)
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(runner_module, "save_transaction", _fail_with_non_transient_operational_error)
        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))

        summary, queue = _run([_item("ok")], transaction_db_path="transactions.db")

        assert summary.persistence_errors == 1
        assert queue.failed == ["ok"]
        assert queue.fail_retries == [True]
        assert attempts == ["ok"]
        assert sleeps == []

    def test_complete_transition_retries_transient_sqlite_lock(self, monkeypatch) -> None:
        sleeps: list[float] = []

        class LockedCompleteQueue(_FakeQueue):
            def __init__(self, items: list[QueueItem]) -> None:
                super().__init__(items)
                self.complete_attempts = 0

            def complete(self, item_id: str, *, claimed_by: str | None = None) -> None:
                self.complete_attempts += 1
                if self.complete_attempts < 3:
                    raise sqlite3.OperationalError("database is locked")
                super().complete(item_id, claimed_by=claimed_by)

        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))
        queue = LockedCompleteQueue([_item("ok")])

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_SuccessSkill("step", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
        )

        assert summary.completed == 1
        assert queue.complete_attempts == 3
        assert queue.completed == ["ok"]
        assert sleeps == [0.05, 0.1]

    def test_fail_transition_retries_transient_sqlite_lock(self, monkeypatch) -> None:
        sleeps: list[float] = []

        class LockedFailQueue(_FakeQueue):
            def __init__(self, items: list[QueueItem]) -> None:
                super().__init__(items)
                self.fail_attempts = 0

            def fail(self, item_id: str, *, retry: bool = True, claimed_by: str | None = None) -> None:
                self.fail_attempts += 1
                if self.fail_attempts < 3:
                    raise sqlite3.OperationalError("database is busy")
                super().fail(item_id, retry=retry, claimed_by=claimed_by)

        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))
        queue = LockedFailQueue([_item("bad")])

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_SystemFailSkill("step", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
        )

        assert summary.failed == 1
        assert queue.fail_attempts == 3
        assert queue.failed == ["bad"]
        assert queue.fail_retries == [True]
        assert sleeps == [0.05, 0.1]

    def test_non_transient_complete_transition_failure_is_loud(self, monkeypatch) -> None:
        sleeps: list[float] = []

        class BrokenCompleteQueue(_FakeQueue):
            def complete(self, item_id: str, *, claimed_by: str | None = None) -> None:
                raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))
        queue = BrokenCompleteQueue([_item("ok")])

        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            run_queue_loop(
                queue=queue,
                engine=Engine(),
                build_transaction=lambda item: Transaction(
                    reference=item.reference,
                    skills=[_SuccessSkill("step", 1)],
                ),
                config={},
                credentials=_CREDS,
                worker_id="test-worker",
            )

        assert queue.completed == []
        assert sleeps == []

    def test_heartbeat_memory_error_propagates_to_main_thread(self, monkeypatch) -> None:
        class MemoryErrorRenewQueue(_FakeQueue):
            lease_timeout = 1

            def renew_lease(self, item_id: str, *, claimed_by: str) -> None:
                raise MemoryError("heartbeat exhausted memory")

        monkeypatch.setattr(runner_module, "_lease_renewal_interval", lambda queue: 0.01)
        queue = MemoryErrorRenewQueue([_item("memory")])

        class _WaitSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    time.sleep(0.01)

        with pytest.raises(MemoryError, match="heartbeat exhausted memory"):
            run_queue_loop(
                queue=queue,
                engine=Engine(),
                build_transaction=lambda item: Transaction(
                    reference=item.reference,
                    skills=[_WaitSkill("wait", 1)],
                ),
                config={},
                credentials=_CREDS,
                worker_id="test-worker",
            )

    def test_complete_transition_memory_error_propagates(self) -> None:
        class MemoryErrorCompleteQueue(_FakeQueue):
            def complete(self, item_id: str, *, claimed_by: str | None = None) -> None:
                raise MemoryError("complete exhausted memory")

        with pytest.raises(MemoryError, match="complete exhausted memory"):
            run_queue_loop(
                queue=MemoryErrorCompleteQueue([_item("ok")]),
                engine=Engine(),
                build_transaction=lambda item: Transaction(
                    reference=item.reference,
                    skills=[_SuccessSkill("step", 1)],
                ),
                config={},
                credentials=_CREDS,
                worker_id="test-worker",
            )

    def test_fail_transition_memory_error_propagates(self) -> None:
        class MemoryErrorFailQueue(_FakeQueue):
            def fail(self, item_id: str, *, retry: bool = True, claimed_by: str | None = None) -> None:
                raise MemoryError("fail exhausted memory")

        with pytest.raises(MemoryError, match="fail exhausted memory"):
            run_queue_loop(
                queue=MemoryErrorFailQueue([_item("bad")]),
                engine=Engine(),
                build_transaction=lambda item: Transaction(
                    reference=item.reference,
                    skills=[_SystemFailSkill("step", 1)],
                ),
                config={},
                credentials=_CREDS,
                worker_id="test-worker",
            )

    def test_persistence_memory_error_propagates(self, monkeypatch) -> None:
        def _fail_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            raise MemoryError("out of memory")

        monkeypatch.setattr(runner_module, "save_transaction", _fail_save)

        with pytest.raises(MemoryError, match="out of memory"):
            _run([_item("ok")], transaction_db_path="transactions.db")

    def test_checkpoint_failure_visible_to_after_item_and_fails_queue_item(self, monkeypatch) -> None:
        def _fail_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            if transaction.history:
                raise RuntimeError("write failed")

        errors: list[Exception | None] = []
        monkeypatch.setattr(runner_module, "save_transaction", _fail_save)

        summary, queue = _run(
            [_item("ok")],
            after_item=lambda item, tx, err: errors.append(err),
            transaction_db_path="transactions.db",
        )

        assert summary.persistence_errors == 1
        assert summary.completed == 0
        assert summary.failed == 1
        assert queue.completed == []
        assert queue.failed == ["ok"]
        assert queue.fail_retries == [True]
        assert isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == "write failed"

    def test_success_checkpoint_error_fails_without_queue_retry(self, monkeypatch) -> None:
        def _fail_after_success(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            if (
                transaction.history
                and transaction.history[-1].event == "skill_succeeded"
            ):
                raise RuntimeError("write failed")

        monkeypatch.setattr(runner_module, "save_transaction", _fail_after_success)

        summary, queue = _run([_item("ok")], transaction_db_path="transactions.db")

        assert summary.persistence_errors == 1
        assert summary.completed == 0
        assert summary.failed == 1
        assert queue.completed == []
        assert queue.failed == ["ok"]
        assert queue.fail_retries == [False]

    def test_business_failure_checkpoint_error_fails_without_queue_retry(self, monkeypatch) -> None:
        def _fail_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            if transaction.skills[0].status is Status.FAILED:
                raise RuntimeError("write failed")

        errors: list[Exception | None] = []
        monkeypatch.setattr(runner_module, "save_transaction", _fail_save)

        summary, queue = _run(
            [_item("bad")],
            skill_cls=_BusinessFailSkill,
            after_item=lambda item, tx, err: errors.append(err),
            transaction_db_path="transactions.db",
        )

        assert summary.persistence_errors == 1
        assert summary.completed == 0
        assert summary.failed == 1
        assert queue.failed == ["bad"]
        assert queue.fail_retries == [False]
        assert isinstance(errors[0], RuntimeError)

    def test_runner_checkpoints_successful_skill_before_later_failure(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")
        queue = _FakeQueue([_item("checkpoint")])

        def build(item: QueueItem) -> Transaction:
            return Transaction(
                reference=item.reference,
                skills=[_StateSkill("first", 1), _SystemFailSkill("second", 2)],
            )

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=build,
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
            transaction_db_path=db_path,
        )

        loaded = list_transactions(db_path)[0]
        assert summary.failed == 1
        assert queue.failed == ["checkpoint"]
        assert loaded.skills[0].status is Status.SUCCESSFUL
        assert loaded.skills[1].status is Status.FAILED
        assert loaded.state == {"first": "done"}

    def test_initial_transaction_is_persisted_and_bound_before_skill_execution(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")
        events: list[str] = []

        class BindingQueue(_FakeQueue):
            def bind_transaction(self, item_id: str, transaction_id: str, *, claimed_by: str) -> None:
                loaded = load_transaction(transaction_id, db_path)
                assert loaded.status is Status.PENDING
                assert loaded.history == []
                events.append("bind")
                super().bind_transaction(item_id, transaction_id, claimed_by=claimed_by)

        class RecordingSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                events.append("execute")

        item = _item("ordered")
        queue = BindingQueue([item])

        run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda queue_item: Transaction(
                reference=queue_item.reference,
                skills=[RecordingSkill("record", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
            transaction_db_path=db_path,
        )

        assert events == ["bind", "execute"]
        assert queue.bindings[0][1] == item.transaction_id

    def test_bind_failure_removes_initial_transaction_and_fails_without_retry(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")
        errors: list[Exception | None] = []

        class FailingBindQueue(_FakeQueue):
            def bind_transaction(self, item_id: str, transaction_id: str, *, claimed_by: str) -> None:
                raise RuntimeError("lost claim")

        queue = FailingBindQueue([_item("bind-fail")])

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_SuccessSkill("step", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
            transaction_db_path=db_path,
            after_item=lambda item, tx, err: errors.append(err),
        )

        assert summary.failed == 1
        assert summary.completed == 0
        assert summary.persistence_errors == 0
        assert queue.failed == ["bind-fail"]
        assert queue.fail_retries == [False]
        assert list_transactions(db_path) == []
        assert errors
        assert "could not bind transaction" in str(errors[0])

    def test_initial_transaction_persistence_failure_retries_without_binding(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        db_path = str(tmp_path / "transactions.db")
        errors: list[Exception | None] = []

        def _fail_initial_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            raise RuntimeError("initial save failed")

        monkeypatch.setattr(runner_module, "save_transaction", _fail_initial_save)

        summary, queue = _run(
            [_item("initial-save-fail")],
            transaction_db_path=db_path,
            after_item=lambda item, tx, err: errors.append(err),
        )

        assert summary.failed == 1
        assert summary.persistence_errors == 1
        assert queue.failed == ["initial-save-fail"]
        assert queue.fail_retries == [True]
        assert queue.bindings == []
        assert list_transactions(db_path) == []
        assert errors
        assert "initial save failed" in str(errors[0])

    def test_bind_transaction_retries_transient_sqlite_lock(
        self,
        monkeypatch,
        tmp_path,
        caplog,
    ) -> None:
        db_path = str(tmp_path / "transactions.db")
        sleeps: list[float] = []
        logger = logging.getLogger("test.runner.bind.retry")

        class LockedThenBindingQueue(_FakeQueue):
            def __init__(self, items: list[QueueItem]) -> None:
                super().__init__(items)
                self.attempts = 0

            def bind_transaction(self, item_id: str, transaction_id: str, *, claimed_by: str) -> None:
                self.attempts += 1
                if self.attempts < 3:
                    raise sqlite3.OperationalError("database is locked")
                super().bind_transaction(item_id, transaction_id, claimed_by=claimed_by)

        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))
        queue = LockedThenBindingQueue([_item("bind-lock")])

        with caplog.at_level(logging.WARNING, logger=logger.name):
            summary = run_queue_loop(
                queue=queue,
                engine=Engine(),
                build_transaction=lambda item: Transaction(
                    reference=item.reference,
                    skills=[_SuccessSkill("step", 1)],
                ),
                config={},
                credentials=_CREDS,
                worker_id="test-worker",
                transaction_db_path=db_path,
                logger=logger,
            )

        assert summary.completed == 1
        assert summary.failed == 0
        assert queue.attempts == 3
        assert queue.bindings
        assert sleeps == [0.05, 0.1]
        bind_retries = [
            record for record in caplog.records
            if record.__dict__.get("event") == "queue_bind_transaction_retry"
        ]
        assert [record.__dict__.get("queue_item_id") for record in bind_retries] == ["bind-lock", "bind-lock"]
        assert [record.__dict__.get("worker_id") for record in bind_retries] == ["test-worker", "test-worker"]

    def test_non_transient_initial_cleanup_error_is_not_retried(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        db_path = str(tmp_path / "transactions.db")
        cleanup_attempts: list[str] = []
        sleeps: list[float] = []

        class FailingBindQueue(_FakeQueue):
            def bind_transaction(self, item_id: str, transaction_id: str, *, claimed_by: str) -> None:
                raise RuntimeError("bind failed")

        def _fail_delete(transaction_id: str, *, db_path: str) -> None:
            cleanup_attempts.append(transaction_id)
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(runner_module, "_delete_transaction", _fail_delete)
        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))

        summary = run_queue_loop(
            queue=FailingBindQueue([_item("cleanup")]),
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_SuccessSkill("step", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
            transaction_db_path=db_path,
        )

        assert summary.failed == 1
        assert summary.persistence_errors == 1
        assert len(cleanup_attempts) == 1
        assert sleeps == []

    def test_sqlite_retry_helper_logs_shape_and_delay(self, monkeypatch, caplog) -> None:
        sleeps: list[float] = []
        logger = logging.getLogger("test.runner.retry.helper")
        error = sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))

        with caplog.at_level(logging.WARNING, logger=logger.name):
            runner_module._sleep_before_sqlite_retry(
                2,
                delay_seconds=0.25,
                log=logger,
                event="retry_event",
                operation="retry_operation",
                queue_item_id="item-1",
                worker_id="worker-1",
                error=error,
                max_attempts=7,
            )

        assert sleeps == [1.0]
        record = caplog.records[0]
        assert record.__dict__.get("event") == "retry_event"
        assert record.__dict__.get("operation") == "retry_operation"
        assert record.__dict__.get("queue_item_id") == "item-1"
        assert record.__dict__.get("worker_id") == "worker-1"
        assert record.__dict__.get("attempt") == 3
        assert record.__dict__.get("max_attempts") == 7
        assert record.__dict__.get("retry_delay_seconds") == 1.0

    def test_persisted_queue_retry_resumes_same_transaction_and_skips_successful_skills(
        self,
        tmp_path,
    ) -> None:
        db_path = str(tmp_path / "transactions.db")
        item = _item("retry")
        queue = _FakeQueue([item, item])
        counts: dict[str, int] = {"first": 0, "second": 0}
        build_calls = 0

        class FirstSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts["first"] += 1
                ctx.state["first"] = "done"

        class FailsOnceSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts["second"] += 1
                if counts["second"] == 1:
                    raise SystemException("temporary", action=self.name)

        def build(queue_item: QueueItem) -> Transaction:
            nonlocal build_calls
            build_calls += 1
            return Transaction(
                reference=queue_item.reference,
                skills=[FirstSkill("first", 1), FailsOnceSkill("second", 2)],
            )

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=build,
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
            transaction_db_path=db_path,
        )

        transactions = list_transactions(db_path)
        assert summary.processed == 2
        assert summary.failed == 1
        assert summary.completed == 1
        assert build_calls == 2
        assert len(transactions) == 1
        assert transactions[0].id == item.transaction_id
        assert transactions[0].status is Status.SUCCESSFUL
        assert counts == {"first": 1, "second": 2}
        assert queue.fail_retries == [True]
        assert queue.completed == ["retry"]

    def test_bound_queue_retry_does_not_reapply_payload_state(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")
        item = _item("payload")
        item.payload = {"invoice": "payload"}
        queue = _FakeQueue([item, item])
        attempts = 0

        class DurableStateSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                ctx.state["invoice"] = "durable"

        class FailsOnceSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise SystemException("temporary", action=self.name)

        def build(queue_item: QueueItem) -> Transaction:
            return Transaction(
                reference=queue_item.reference,
                skills=[DurableStateSkill("state", 1), FailsOnceSkill("retry", 2)],
            )

        run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=build,
            config={},
            credentials=_CREDS,
            worker_id="test-worker",
            transaction_db_path=db_path,
        )

        loaded = load_transaction(item.transaction_id, db_path)
        assert loaded.state["invoice"] == "durable"

    def test_missing_bound_transaction_fails_loudly_without_replacement(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")
        item = _item("missing")
        item.transaction_id = "missing-transaction"
        errors: list[Exception | None] = []

        summary, queue = _run(
            [item],
            transaction_db_path=db_path,
            after_item=lambda item, tx, err: errors.append(err),
        )

        assert summary.failed == 1
        assert queue.failed == ["missing"]
        assert queue.fail_retries == [False]
        assert list_transactions(db_path) == []
        assert errors
        assert "missing-transaction" in str(errors[0])

    def test_bound_transaction_sqlite_resume_error_fails_without_retry(
        self,
        monkeypatch,
        tmp_path,
    ) -> None:
        db_path = str(tmp_path / "transactions.db")
        item = _item("sqlite-resume")
        item.transaction_id = "tx-bound"
        errors: list[Exception | None] = []

        def _fail_resume(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(runner_module, "resume_transaction", _fail_resume)

        summary, queue = _run(
            [item],
            transaction_db_path=db_path,
            after_item=lambda item, tx, err: errors.append(err),
        )

        assert summary.failed == 1
        assert queue.failed == ["sqlite-resume"]
        assert queue.fail_retries == [False]
        assert errors
        assert "tx-bound" in str(errors[0])

    def test_default_transaction_db_path_preserves_current_behavior(self, monkeypatch) -> None:
        calls: list[Transaction] = []

        def _record_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            calls.append(transaction)

        monkeypatch.setattr(runner_module, "save_transaction", _record_save)

        summary, queue = _run([_item("ok")])

        assert summary == QueueRunSummary(processed=1, completed=1, failed=0)
        assert queue.completed == ["ok"]
        assert calls == []

    def test_queue_payload_is_saved_as_transaction_state(self, tmp_path) -> None:
        item = _item("ok")
        item.payload = {"invoice_id": 42}
        db_path = str(tmp_path / "transactions.db")

        summary, queue = _run([item], transaction_db_path=db_path)

        loaded = list_transactions(db_path)[0]
        assert summary.completed == 1
        assert queue.completed == ["ok"]
        assert loaded.reference == "ok"
        assert loaded.state == {"invoice_id": 42}

    def test_resources_are_not_persisted_with_transaction_state(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")
        transaction_ids: list[str] = []

        class _UseResourceSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                assert "session" in ctx.resources
                ctx.state["done"] = True
                transaction_ids.append(ctx.transaction.id)

        _run(
            [_item("ok")],
            skill_cls=_UseResourceSkill,
            transaction_db_path=db_path,
            resource_scope=_resource_scope({"session": object()}),
        )

        loaded = load_transaction(transaction_ids[0], db_path)
        assert loaded.state == {"done": True}

    def test_non_json_safe_queue_payload_fails_before_state_seed(self) -> None:
        item = _item("bad")
        item.payload = {"client": object()}
        errors: list[Exception | None] = []

        summary, queue = _run(
            [item],
            after_item=lambda item, tx, err: errors.append(err),
        )

        assert summary.failed == 1
        assert queue.failed == ["bad"]
        assert isinstance(errors[0], TypeError)
        assert "queue item payload['client'] expected JSON value" in str(errors[0])

    def test_non_json_safe_final_state_prevents_queue_completion(self, tmp_path) -> None:
        errors: list[Exception | None] = []

        class _BadStateSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                ctx.state["client"] = object()

        summary, queue = _run(
            [_item("bad-state")],
            skill_cls=_BadStateSkill,
            transaction_db_path=str(tmp_path / "transactions.db"),
            after_item=lambda item, tx, err: errors.append(err),
        )

        assert summary.completed == 0
        assert summary.failed == 1
        assert summary.persistence_errors == 0
        assert queue.completed == []
        assert queue.failed == ["bad-state"]
        assert queue.fail_retries == [False]
        assert isinstance(errors[0], TypeError)
        assert "transaction.state['client'] expected JSON value" in str(errors[0])

    def test_payload_state_collision_is_logged_at_warning(self) -> None:
        item = _item("ok")
        item.payload = {"invoice_id": "payload"}
        warning_extras: list[dict[str, object]] = []

        class _CaptureLogger:
            def debug(self, message: str, *, extra: dict[str, object]) -> None:
                pass

            def info(self, message: str, *, extra: dict[str, object]) -> None:
                pass

            def warning(self, message: str, *, extra: dict[str, object]) -> None:
                warning_extras.append(extra)

            def error(self, *args, **kwargs) -> None:
                pass

            def exception(self, *args, **kwargs) -> None:
                pass

        def _build(queue_item: QueueItem) -> Transaction:
            return Transaction(
                reference=queue_item.reference,
                state={"invoice_id": "prebuilt"},
                skills=[_SuccessSkill("step", 1)],
            )

        run_queue_loop(
            queue=_FakeQueue([item]),
            engine=Engine(),
            build_transaction=_build,
            config={},
            credentials=_CREDS,
            worker_id="worker",
            logger=_CaptureLogger(),  # type: ignore[arg-type]
        )

        assert len(warning_extras) == 1
        assert warning_extras[0]["event"] == "queue_payload_state_collision"
        assert warning_extras[0]["state_keys"] == ["invoice_id"]


# ---------------------------------------------------------------------------
# queue lease heartbeat
# ---------------------------------------------------------------------------

class TestQueueLeaseHeartbeat:
    def test_long_running_sqlite_item_keeps_lease(self, monkeypatch, tmp_path) -> None:
        db_path = str(tmp_path / "queue.db")
        queue = SqliteQueue({"db_path": db_path, "lease_timeout": 1, "max_retries": 0})
        queue.add(QueueItem(reference="slow", payload={}))
        monkeypatch.setattr(runner_module, "_lease_renewal_interval", lambda queue: 0.05)

        class _SlowSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                time.sleep(1.2)

        summary = run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_SlowSkill("slow", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="worker-a",
        )

        stored = queue.list_items()[0]
        assert summary.completed == 1
        assert stored.status is QueueStatus.SUCCESSFUL
        assert queue.next_item("worker-b") is None

    def test_lease_loss_during_running_skill_prevents_final_transition(
        self,
        monkeypatch,
        tmp_path,
        caplog,
    ) -> None:
        db_path = str(tmp_path / "queue.db")
        queue = SqliteQueue({"db_path": db_path, "lease_timeout": 30, "max_retries": 0})
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = QueueItem(id="a-first", reference="first", payload={}, created_at=base)
        second = QueueItem(
            id="b-second",
            reference="second",
            payload={},
            created_at=base + timedelta(seconds=1),
        )
        queue.add(first)
        queue.add(second)
        monkeypatch.setattr(runner_module, "_lease_renewal_interval", lambda queue: 0.01)
        started = threading.Event()
        release = threading.Event()
        summaries: list[QueueRunSummary] = []
        errors: list[BaseException] = []
        after_item_calls: list[str] = []

        class _BlockingSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                started.set()
                assert release.wait(timeout=2)

        def _run_worker() -> None:
            try:
                summaries.append(
                    run_queue_loop(
                        queue=queue,
                        engine=Engine(),
                        build_transaction=lambda item: Transaction(
                            reference=item.reference,
                            skills=[_BlockingSkill("block", 1)],
                        ),
                        config={},
                        credentials=_CREDS,
                        worker_id="worker-a",
                        logger=logging.getLogger("test.runner.lease"),
                        after_item=lambda item, tx, err: after_item_calls.append(item.id),
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=_run_worker)
        with caplog.at_level(logging.ERROR, logger="test.runner.lease"):
            worker.start()
            assert started.wait(timeout=2)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE queue_items SET claimed_by = ?, claimed_at = ? WHERE id = ?",
                    ("worker-b", "2026-01-01T00:00:00+00:00", first.id),
                )
                conn.commit()
            finally:
                conn.close()
            time.sleep(0.05)
            release.set()
            worker.join(timeout=2)

        assert not worker.is_alive()
        assert errors == []
        assert summaries == [QueueRunSummary(processed=1, failed=1)]
        assert after_item_calls == []
        stored_first = queue.get_item(first.id)
        stored_second = queue.get_item(second.id)
        assert stored_first is not None
        assert stored_first.status is QueueStatus.IN_PROGRESS
        assert stored_first.claimed_by == "worker-b"
        assert stored_second is not None
        assert stored_second.status is QueueStatus.PENDING
        assert any(record.__dict__.get("event") == "queue_item_lease_lost" for record in caplog.records)


# ---------------------------------------------------------------------------
# stop_event
# ---------------------------------------------------------------------------

class TestStopEvent:
    def test_stop_event_set_before_loop_processes_nothing(self) -> None:
        event = threading.Event()
        event.set()
        summary, queue = _run([_item("a"), _item("b"), _item("c")], stop_event=event)
        assert summary.processed == 0
        assert queue.completed == []
        assert queue.failed == []

    def test_stop_event_set_after_n_items_exits_early(self) -> None:
        event = threading.Event()
        calls: list[str] = []

        def _after(item, tx, err):
            calls.append(item.id)
            if len(calls) >= 2:
                event.set()

        summary, queue = _run(
            [_item("a"), _item("b"), _item("c")],
            after_item=_after,
            stop_event=event,
        )
        assert summary.processed == 2
        assert summary.completed == 2
        assert summary.failed == 0
        assert queue.completed == ["a", "b"]
        assert queue.failed == []

    def test_no_stop_event_loop_drains_normally(self) -> None:
        summary, queue = _run([_item("a"), _item("b"), _item("c")])
        assert summary.processed == 3
        assert queue.completed == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# lifecycle hooks
# ---------------------------------------------------------------------------

class TestLifecycleHooks:
    def test_resource_scope_resources_appear_in_every_item_context(self) -> None:
        seen: list[dict[str, object]] = []
        queue = _FakeQueue([_item("a"), _item("b")])

        class _CaptureSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                seen.append(dict(ctx.resources))

        run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_CaptureSkill("capture", 1)],
            ),
            config={"session_name": "shared"},
            credentials=_CREDS,
            worker_id="worker",
            resource_scope=_resource_scope({"session": "shared"}),
        )

        assert seen == [{"session": "shared"}, {"session": "shared"}]

    def test_item_payload_populates_state_while_resource_scope_populates_resources(self) -> None:
        item = _item("a")
        item.payload = {"value": "item"}
        seen: list[tuple[dict[str, object], dict[str, object]]] = []

        class _CaptureSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                seen.append((dict(ctx.state), dict(ctx.resources)))

        run_queue_loop(
            queue=_FakeQueue([item]),
            engine=Engine(),
            build_transaction=lambda queue_item: Transaction(
                reference=queue_item.reference,
                skills=[_CaptureSkill("capture", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="worker",
            resource_scope=_resource_scope({"value": "resource", "shared_only": True}),
        )

        assert seen == [({"value": "item"}, {"value": "resource", "shared_only": True})]

    def test_item_contexts_receive_independent_resource_dicts(self) -> None:
        seen: list[dict[str, object]] = []
        queue = _FakeQueue([_item("a"), _item("b")])

        class _MutateSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                seen.append(dict(ctx.resources))
                ctx.resources["mutated"] = True

        run_queue_loop(
            queue=queue,
            engine=Engine(),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                skills=[_MutateSkill("mutate", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="worker",
            resource_scope=_resource_scope({"shared": True}),
        )

        assert seen == [{"shared": True}, {"shared": True}]

    def test_nested_resource_scope_values_retain_shared_identity(self) -> None:
        resource = {"session_id": "shared"}
        seen: list[object] = []

        class _CaptureSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                seen.append(ctx.resources["resource"])

        _run(
            [_item("a"), _item("b")],
            skill_cls=_CaptureSkill,
            resource_scope=_resource_scope({"resource": resource}),
        )

        assert seen == [resource, resource]
        assert seen[0] is resource
        assert seen[1] is resource

    def test_resource_scope_none_preserves_payload_only_state_behavior(self) -> None:
        item = _item("a")
        item.payload = {"value": "item"}
        seen: list[tuple[dict[str, object], dict[str, object]]] = []

        class _CaptureSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                seen.append((dict(ctx.state), dict(ctx.resources)))

        run_queue_loop(
            queue=_FakeQueue([item]),
            engine=Engine(),
            build_transaction=lambda queue_item: Transaction(
                reference=queue_item.reference,
                skills=[_CaptureSkill("capture", 1)],
            ),
            config={},
            credentials=_CREDS,
            worker_id="worker",
            resource_scope=_resource_scope(None),
        )

        assert seen == [({"value": "item"}, {})]

    def test_resource_scope_setup_exception_propagates_before_processing(self) -> None:
        summaries: list[QueueRunSummary] = []
        next_calls = 0

        class _CountingQueue(_FakeQueue):
            def next_item(self, worker_id: str = "") -> QueueItem | None:
                nonlocal next_calls
                next_calls += 1
                return super().next_item(worker_id)
        queue = _CountingQueue([_item("a")])

        @contextmanager
        def _fail_scope():
            raise RuntimeError("login failed")
            yield {}

        with pytest.raises(RuntimeError, match="login failed"):
            run_queue_loop(
                queue=queue,
                engine=Engine(),
                build_transaction=lambda item: Transaction(reference=item.reference),
                config={},
                credentials=_CREDS,
                worker_id="worker",
                resource_scope=_fail_scope(),
                on_finish=lambda summary: summaries.append(summary),
            )

        assert queue.completed == []
        assert queue.failed == []
        assert next_calls == 0
        assert summaries == [QueueRunSummary()]

    def test_invalid_resource_scope_yield_raises_before_processing(self) -> None:
        queue = _FakeQueue([_item("a")])
        summaries: list[QueueRunSummary] = []

        with pytest.raises(TypeError) as exc_info:
            run_queue_loop(
                queue=queue,
                engine=Engine(),
                build_transaction=lambda item: Transaction(reference=item.reference),
                config={},
                credentials=_CREDS,
                worker_id="worker",
                resource_scope=_resource_scope("invalid"),  # type: ignore[arg-type]
                on_finish=lambda summary: summaries.append(summary),
            )

        assert str(exc_info.value) == "resource_scope yield expected dict | None; got str value='invalid'"
        assert queue.completed == []
        assert queue.failed == []
        assert summaries == [QueueRunSummary()]

    def test_resource_scope_setup_and_cleanup_happen_once_on_normal_run(self) -> None:
        events: list[str] = []

        @contextmanager
        def _scope():
            events.append("setup")
            try:
                yield {"session": object()}
            finally:
                events.append("cleanup")

        summary, queue = _run([_item("a"), _item("b")], resource_scope=_scope())

        assert summary.processed == 2
        assert queue.completed == ["a", "b"]
        assert events == ["setup", "cleanup"]

    def test_resource_scope_setup_and_cleanup_happen_once_on_empty_queue(self) -> None:
        events: list[str] = []

        @contextmanager
        def _scope():
            events.append("setup")
            try:
                yield {}
            finally:
                events.append("cleanup")

        summary, queue = _run([], resource_scope=_scope())

        assert summary.processed == 0
        assert queue.completed == []
        assert events == ["setup", "cleanup"]

    def test_resource_scope_cleanup_happens_once_after_processing_error(self) -> None:
        events: list[str] = []

        @contextmanager
        def _scope():
            events.append("setup")
            try:
                yield {}
            finally:
                events.append("cleanup")

        summary, queue = _run(
            [_item("a")],
            build_raises=RuntimeError("cannot build"),
            resource_scope=_scope(),
        )

        assert summary.failed == 1
        assert queue.failed == ["a"]
        assert events == ["setup", "cleanup"]

    def test_resource_scope_cleanup_error_propagates_after_queue_outcome(self) -> None:
        summaries: list[QueueRunSummary] = []
        queue = _FakeQueue([_item("a")])

        @contextmanager
        def _scope():
            yield {}
            raise RuntimeError("cleanup failed")

        with pytest.raises(RuntimeError, match="cleanup failed"):
            run_queue_loop(
                queue=queue,
                engine=Engine(),
                build_transaction=lambda item: Transaction(
                    reference=item.reference,
                    skills=[_SuccessSkill("step", 1)],
                ),
                config={},
                credentials=_CREDS,
                worker_id="worker",
                resource_scope=_scope(),
                on_finish=lambda summary: summaries.append(summary),
            )

        assert queue.completed == ["a"]
        assert queue.failed == []
        assert summaries == [QueueRunSummary(processed=1, completed=1)]

    def test_on_start_keyword_is_not_accepted(self) -> None:
        with pytest.raises(TypeError, match="on_start"):
            run_queue_loop(
                queue=_FakeQueue([]),
                engine=Engine(),
                build_transaction=lambda item: Transaction(reference=item.reference),
                config={},
                credentials=_CREDS,
                on_start=lambda config: {},  # type: ignore[call-arg]
            )

    def test_on_finish_fires_once_with_final_summary(self) -> None:
        summaries: list[QueueRunSummary] = []

        summary, queue = _run(
            [_item("a"), _item("b")],
            on_finish=lambda final_summary: summaries.append(final_summary),
        )

        assert queue.completed == ["a", "b"]
        assert summaries == [summary]
        assert summaries[0].processed == 2
        assert summaries[0].completed == 2

    def test_on_finish_fires_when_stop_event_is_already_set(self) -> None:
        event = threading.Event()
        event.set()
        summaries: list[QueueRunSummary] = []

        summary, queue = _run(
            [_item("a")],
            stop_event=event,
            on_finish=lambda final_summary: summaries.append(final_summary),
        )

        assert queue.completed == []
        assert summaries == [summary]
        assert summary.processed == 0

    def test_on_finish_fires_when_queue_processing_raises(self) -> None:
        summaries: list[QueueRunSummary] = []

        class _FailingQueue(_FakeQueue):
            def next_item(self, worker_id: str = "") -> QueueItem | None:
                raise RuntimeError("queue unavailable")

        with pytest.raises(RuntimeError, match="queue unavailable"):
            run_queue_loop(
                queue=_FailingQueue([]),
                engine=Engine(),
                build_transaction=lambda item: Transaction(reference=item.reference),
                config={},
                credentials=_CREDS,
                worker_id="worker",
                on_finish=lambda final_summary: summaries.append(final_summary),
            )

        assert len(summaries) == 1
        assert summaries[0] == QueueRunSummary()

    def test_on_finish_exception_is_logged_and_swallowed(self, caplog) -> None:
        logger = logging.getLogger("test.runner.on_finish")

        def _fail_finish(summary: QueueRunSummary) -> None:
            raise RuntimeError("cleanup failed")

        with caplog.at_level(logging.ERROR, logger=logger.name):
            summary, queue = _run(
                [_item("a")],
                logger=logger,
                on_finish=_fail_finish,
            )

        assert summary.completed == 1
        assert summary.lifecycle_errors == 1
        assert queue.completed == ["a"]
        assert any(
            record.__dict__.get("event") == "on_finish_error"
            and record.getMessage() == "on_finish callback raised during lifecycle cleanup"
            for record in caplog.records
        )

    def test_on_finish_memory_error_propagates(self) -> None:
        def _fail_finish(summary: QueueRunSummary) -> None:
            raise MemoryError("out of memory")

        with pytest.raises(MemoryError, match="out of memory"):
            _run([_item("a")], on_finish=_fail_finish)

    def test_successful_on_finish_does_not_increment_lifecycle_errors(self) -> None:
        summary, _ = _run([_item("a")], on_finish=lambda final_summary: None)

        assert summary.lifecycle_errors == 0

    def test_omitted_hooks_preserve_current_behavior(self) -> None:
        summary, queue = _run([_item("a")])

        assert summary == QueueRunSummary(processed=1, completed=1, failed=0)
        assert queue.completed == ["a"]
