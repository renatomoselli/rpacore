"""Tests for rpacore.runner — QueueRunSummary and after_item callback."""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

import pytest

from rpacore.context import ProcessContext
from rpacore.credentials import EnvCredentialProvider
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
from rpacore.persistence import list_transactions, load_transaction
from rpacore.queue import QueueItem
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

    def next_item(self, worker_id: str = "") -> QueueItem | None:
        return self._items.pop(0) if self._items else None

    def complete(self, item_id: str, *, claimed_by: str | None = None) -> None:
        self.completed.append(item_id)

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

    def test_persistence_failure_is_counted_and_logged(self, monkeypatch, caplog) -> None:
        def _fail_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            raise RuntimeError("sqlite locked")

        monkeypatch.setattr(runner_module, "save_transaction", _fail_save)
        logger = logging.getLogger("test.runner.persistence")

        with caplog.at_level(logging.ERROR, logger=logger.name):
            summary, queue = _run([_item("ok")], transaction_db_path="broken.db", logger=logger)

        assert summary.persistence_errors == 1
        assert summary.completed == 1
        assert summary.failed == 0
        assert queue.completed == ["ok"]
        assert any(
            record.__dict__.get("event") == "transaction_persistence_error"
            and record.__dict__.get("queue_item_id") == "ok"
            and record.__dict__.get("transaction_reference") == "ok"
            for record in caplog.records
        )

    def test_transient_sqlite_persistence_failure_is_retried(self, monkeypatch) -> None:
        attempts: list[str] = []
        sleeps: list[float] = []

        def _save_after_two_failures(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            attempts.append(transaction.reference)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(runner_module, "save_transaction", _save_after_two_failures)
        monkeypatch.setattr(runner_module.time, "sleep", lambda delay: sleeps.append(delay))

        summary, queue = _run([_item("ok")], transaction_db_path="transactions.db")

        assert summary.persistence_errors == 0
        assert queue.completed == ["ok"]
        assert attempts == ["ok", "ok", "ok"]
        assert sleeps == [0.05, 0.1]

    def test_persistence_memory_error_propagates(self, monkeypatch) -> None:
        def _fail_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            raise MemoryError("out of memory")

        monkeypatch.setattr(runner_module, "save_transaction", _fail_save)

        with pytest.raises(MemoryError, match="out of memory"):
            _run([_item("ok")], transaction_db_path="transactions.db")

    def test_persistence_failure_visible_to_after_item_without_changing_queue_outcome(self, monkeypatch) -> None:
        def _fail_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
            raise RuntimeError("write failed")

        errors: list[Exception | None] = []
        monkeypatch.setattr(runner_module, "save_transaction", _fail_save)

        summary, queue = _run(
            [_item("ok")],
            after_item=lambda item, tx, err: errors.append(err),
            transaction_db_path="transactions.db",
        )

        assert summary.persistence_errors == 1
        assert summary.completed == 1
        assert summary.failed == 0
        assert queue.completed == ["ok"]
        assert isinstance(errors[0], RuntimeError)
        assert str(errors[0]) == "write failed"

    def test_business_failure_stays_terminal_when_persistence_fails(self, monkeypatch) -> None:
        def _fail_save(transaction: Transaction, db_path: str = "rpacore.db") -> None:
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
