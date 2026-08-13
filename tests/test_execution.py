"""Tests for public one-off transaction execution helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import rpacore.execution as execution_module
from rpacore import (
    BusinessException,
    Engine,
    ExecutionValidationError,
    OutcomeCategory,
    ProcessContext,
    RetryDisposition,
    Step,
    Status,
    SystemException,
    Transaction,
    execute_transaction,
    load_transaction,
)


class _SuccessStep(Step):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state["done"] = True


class _ResourceStep(Step):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state["resource"] = ctx.resources["session"]
        ctx.resources["mutated"] = True


class _SystemThenSuccessStep(Step):
    def execute(self, ctx: ProcessContext) -> None:
        attempts = int(ctx.state.get("attempts", 0)) + 1
        ctx.state["attempts"] = attempts
        if attempts == 1:
            raise SystemException("try again", action=self.name)


class _BusinessFailStep(Step):
    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException("bad input", action=self.name)


def _transaction(*steps: Step) -> Transaction:
    return Transaction(
        reference="tx",
        steps=list(steps),
        definition_identity="tests.execution/v1",
    )


@contextmanager
def _resource_scope(value: object) -> Iterator[dict[str, object]]:
    yield {"session": value}


class TestExecuteTransaction:
    def test_transaction_db_path_requires_definition_identity_before_mutation(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "rpacore.db"
        transaction = Transaction(
            reference="unidentified",
            steps=[_SuccessStep("step", 1)],
        )

        with pytest.raises(
            ExecutionValidationError,
            match="definition_identity must be a non-empty str",
        ):
            execute_transaction(transaction, transaction_db_path=db_path)

        assert transaction.status is Status.PENDING
        assert transaction.history == []
        assert not db_path.exists()

    def test_transaction_db_path_persists_strict_checkpoints(self, tmp_path: Path) -> None:
        db_path = tmp_path / "rpacore.db"
        transaction = _transaction(_SuccessStep("step", 1))

        execute_transaction(transaction, transaction_db_path=db_path)

        loaded = load_transaction(transaction.id, db_path=str(db_path))
        assert loaded.status is Status.SUCCESSFUL
        assert loaded.outcome_category is OutcomeCategory.SUCCESSFUL
        assert loaded.retry_disposition is RetryDisposition.NOT_APPLICABLE
        assert loaded.state == {"done": True}
        assert loaded.steps[0].status is Status.SUCCESSFUL

    def test_transaction_db_path_accepts_string_paths(self, tmp_path: Path) -> None:
        db_path = tmp_path / "rpacore.db"
        transaction = _transaction(_SuccessStep("step", 1))

        execute_transaction(transaction, transaction_db_path=str(db_path))

        assert load_transaction(transaction.id, db_path=str(db_path)).status is Status.SUCCESSFUL

    def test_transaction_db_path_retries_transient_sqlite_locks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = 0
        sleeps: list[float] = []
        transaction = _transaction(_SuccessStep("step", 1))

        def _flaky_save_transaction(
            transaction: Transaction,
            db_path: str = "rpacore.db",
        ) -> None:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(execution_module, "save_transaction", _flaky_save_transaction)
        monkeypatch.setattr(execution_module.time, "sleep", sleeps.append)

        execute_transaction(transaction, transaction_db_path=tmp_path / "rpacore.db")

        assert transaction.status is Status.SUCCESSFUL
        assert calls == 6
        assert sleeps == [0.05, 0.1]

    def test_transaction_db_path_does_not_retry_non_transient_sqlite_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = 0
        transaction = _transaction(_SuccessStep("step", 1))

        def _fail_save_transaction(
            transaction: Transaction,
            db_path: str = "rpacore.db",
        ) -> None:
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(execution_module, "save_transaction", _fail_save_transaction)

        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            execute_transaction(transaction, transaction_db_path=tmp_path / "rpacore.db")

        assert calls == 1

    def test_transaction_db_path_retries_busy_sqlite_errors_then_propagates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = 0
        sleeps: list[float] = []
        transaction = _transaction(_SuccessStep("step", 1))

        def _busy_save_transaction(
            transaction: Transaction,
            db_path: str = "rpacore.db",
        ) -> None:
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("database is busy")

        monkeypatch.setattr(execution_module, "save_transaction", _busy_save_transaction)
        monkeypatch.setattr(execution_module.time, "sleep", sleeps.append)

        with pytest.raises(sqlite3.OperationalError, match="database is busy"):
            execute_transaction(transaction, transaction_db_path=tmp_path / "rpacore.db")

        assert calls == 3
        assert sleeps == [0.05, 0.1]

    def test_transaction_db_path_memory_error_propagates_without_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = 0
        sleeps: list[float] = []
        transaction = _transaction(_SuccessStep("step", 1))
        memory_error = MemoryError("out of memory")

        def _fail_save_transaction(
            transaction: Transaction,
            db_path: str = "rpacore.db",
        ) -> None:
            nonlocal calls
            calls += 1
            raise memory_error

        monkeypatch.setattr(execution_module, "save_transaction", _fail_save_transaction)
        monkeypatch.setattr(execution_module.time, "sleep", sleeps.append)

        with pytest.raises(MemoryError) as exc_info:
            execute_transaction(transaction, transaction_db_path=tmp_path / "rpacore.db")

        assert exc_info.value is memory_error
        assert calls == 1
        assert sleeps == []

    def test_checkpoint_and_transaction_db_path_are_mutually_exclusive(self, tmp_path: Path) -> None:
        transaction = _transaction(_SuccessStep("step", 1))

        with pytest.raises(ValueError, match="mutually exclusive"):
            execute_transaction(
                transaction,
                checkpoint=lambda tx: None,
                transaction_db_path=tmp_path / "rpacore.db",
            )

    def test_custom_checkpoint_still_controls_persistence_boundary(self) -> None:
        transaction = _transaction(_SuccessStep("step", 1))
        statuses: list[Status] = []

        execute_transaction(transaction, checkpoint=lambda tx: statuses.append(tx.status))

        assert statuses[0] is Status.IN_PROGRESS
        assert statuses[-1] is Status.SUCCESSFUL

    def test_omitted_checkpoint_runs_in_memory(self) -> None:
        transaction = _transaction(_SuccessStep("step", 1))

        execute_transaction(transaction)

        assert transaction.status is Status.SUCCESSFUL
        assert transaction.state == {"done": True}

    def test_configured_engine_is_used(self) -> None:
        transaction = _transaction(_SystemThenSuccessStep("flaky", 1))

        execute_transaction(transaction, engine=Engine(max_retries=1))

        assert transaction.status is Status.SUCCESSFUL
        assert transaction.state["attempts"] == 2

    def test_resource_scope_populates_context_resources(self) -> None:
        resource = "browser-session"
        transaction = _transaction(_ResourceStep("step", 1))

        execute_transaction(transaction, resource_scope=_resource_scope(resource))

        assert transaction.status is Status.SUCCESSFUL
        assert transaction.state == {"resource": resource}

    def test_resource_scope_mapping_is_shallow_copied(self) -> None:
        resources = {"session": "shared"}
        transaction = _transaction(_ResourceStep("step", 1))

        @contextmanager
        def _scope() -> Iterator[dict[str, object]]:
            yield resources

        execute_transaction(transaction, resource_scope=_scope())

        assert resources == {"session": "shared"}

    def test_resource_scope_none_preserves_empty_resources(self) -> None:
        seen: list[dict[str, object]] = []

        class _CaptureStep(Step):
            def execute(self, ctx: ProcessContext) -> None:
                seen.append(dict(ctx.resources))

        @contextmanager
        def _scope() -> Iterator[None]:
            yield None

        execute_transaction(_transaction(_CaptureStep("step", 1)), resource_scope=_scope())

        assert seen == [{}]

    def test_invalid_resource_scope_yield_raises_before_step_execution(self) -> None:
        ran = False

        class _TrackStep(Step):
            def execute(self, ctx: ProcessContext) -> None:
                nonlocal ran
                ran = True

        @contextmanager
        def _scope() -> Iterator[object]:
            yield "invalid"

        with pytest.raises(TypeError) as exc_info:
            execute_transaction(_transaction(_TrackStep("step", 1)), resource_scope=_scope())

        assert str(exc_info.value) == "resource_scope yield expected dict | None; got str value='invalid'"
        assert ran is False

    def test_resource_scope_setup_failure_prevents_step_execution(self) -> None:
        ran = False

        class _TrackStep(Step):
            def execute(self, ctx: ProcessContext) -> None:
                nonlocal ran
                ran = True

        @contextmanager
        def _scope() -> Iterator[dict[str, object]]:
            raise RuntimeError("setup failed")
            yield {}

        with pytest.raises(RuntimeError, match="setup failed"):
            execute_transaction(_transaction(_TrackStep("step", 1)), resource_scope=_scope())

        assert ran is False

    def test_resource_scope_cleanup_runs_after_checkpoint_failure(self) -> None:
        events: list[str] = []

        @contextmanager
        def _scope() -> Iterator[dict[str, object]]:
            events.append("setup")
            try:
                yield {}
            finally:
                events.append("cleanup")

        def _fail_checkpoint(transaction: Transaction) -> None:
            raise RuntimeError("checkpoint failed")

        with pytest.raises(RuntimeError, match="checkpoint failed"):
            execute_transaction(
                _transaction(_SuccessStep("step", 1)),
                checkpoint=_fail_checkpoint,
                resource_scope=_scope(),
            )

        assert events == ["setup", "cleanup"]

    def test_resource_scope_cleanup_failure_propagates_after_transaction_outcome(self) -> None:
        transaction = _transaction(_BusinessFailStep("step", 1))

        @contextmanager
        def _scope() -> Iterator[dict[str, object]]:
            try:
                yield {}
            finally:
                raise RuntimeError("cleanup failed")

        with pytest.raises(RuntimeError, match="cleanup failed"):
            execute_transaction(transaction, resource_scope=_scope())

        assert transaction.status is Status.FAILED

    def test_resource_scope_cleanup_failure_chains_original_execution_error(self) -> None:
        @contextmanager
        def _scope() -> Iterator[dict[str, object]]:
            try:
                yield {}
            finally:
                raise RuntimeError("cleanup failed")

        def _fail_checkpoint(transaction: Transaction) -> None:
            raise RuntimeError("checkpoint failed")

        with pytest.raises(RuntimeError, match="cleanup failed") as exc_info:
            execute_transaction(
                _transaction(_SuccessStep("step", 1)),
                checkpoint=_fail_checkpoint,
                resource_scope=_scope(),
            )

        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert str(exc_info.value.__cause__) == "checkpoint failed"

    def test_resource_scope_can_suppress_execution_validation_error(self) -> None:
        transaction = Transaction(reference="")
        seen: list[type[BaseException] | None] = []

        class _SuppressScope:
            def __enter__(self) -> dict[str, object]:
                return {}

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: object,
            ) -> bool:
                seen.append(exc_type)
                return True

        execute_transaction(transaction, resource_scope=_SuppressScope())

        assert seen == [ExecutionValidationError]
        assert transaction.status is Status.FAILED
