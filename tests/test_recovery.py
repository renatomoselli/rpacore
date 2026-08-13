"""Tests for rpacore.recovery."""

import pytest

from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, DefinitionIdentityError, SystemException
from rpacore.persistence import load_transaction, save_transaction as _save_transaction
from rpacore.recovery import resume_transaction as _resume_transaction
from rpacore.serialization import serialize_transaction
from rpacore.step import Step
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


_DEFINITION_IDENTITY = "tests.recovery/v1"


def save_transaction(transaction: Transaction, db_path: str) -> None:
    if not transaction.definition_identity:
        transaction.definition_identity = _DEFINITION_IDENTITY
    _save_transaction(transaction, db_path)


def resume_transaction(
    tx_id: str,
    steps: list[Step],
    **kwargs: object,
) -> Transaction:
    kwargs.setdefault("definition_identity", _DEFINITION_IDENTITY)
    return _resume_transaction(tx_id, steps, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


class _TrackingStep(Step):
    def __init__(self, name: str, execution_order: int, counts: dict[str, int]) -> None:
        super().__init__(name, execution_order)
        self._counts = counts

    def execute(self, ctx: ProcessContext) -> None:
        self._counts[self.name] = self._counts.get(self.name, 0) + 1


class _StoppingBusinessStep(Step):
    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException(
            "stop", action=self.name, halts_remaining_steps=True
        )


def _save_stopped_transaction(db_path: str) -> Transaction:
    transaction = Transaction(
        reference="stopping-business-failure",
        steps=[
            _StoppingBusinessStep("validate", 1),
            Step("downstream", 2),
        ],
    )
    Engine().run(ProcessContext(transaction=transaction))
    save_transaction(transaction, db_path)
    return transaction


def test_resume_reruns_only_non_successful_steps(db_path) -> None:
    counts: dict[str, int] = {}

    first = Step("first", 1)
    first.status = Status.SUCCESSFUL
    second = Step("second", 2)
    second.status = Status.FAILED
    second.exceptions.append(SystemException("timeout", action="second"))
    third = Step("third", 3)
    third.status = Status.PENDING

    tx = Transaction(
        reference="REF-001",
        status=Status.FAILED,
        steps=[first, second, third],
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingStep("first", 1, counts),
            _TrackingStep("second", 2, counts),
            _TrackingStep("third", 3, counts),
        ],
        db_path=db_path,
    )

    assert resumed.status is Status.PENDING
    assert resumed.started_at is None
    assert resumed.finished_at is None
    assert resumed.history[-1].event is HistoryEvent.TRANSACTION_RESUMED
    assert [step.status for step in resumed.steps] == [
        Status.SUCCESSFUL,
        Status.PENDING,
        Status.PENDING,
    ]
    assert len(resumed.steps[1].exceptions) == 1

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"second": 1, "third": 1}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_resets_retry_count_for_new_engine_run(db_path) -> None:
    failed = Step("failed", 1)
    failed.status = Status.FAILED
    failed.exceptions.append(SystemException("timeout", action="failed"))
    tx = Transaction(
        reference="REF-001",
        status=Status.FAILED,
        retry_count=3,
        steps=[failed],
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [_TrackingStep("failed", 1, {})],
        db_path=db_path,
    )

    assert resumed.retry_count == 0


def test_load_shows_interrupted_state_before_explicit_resume(db_path) -> None:
    first = Step("first", 1)
    first.status = Status.SUCCESSFUL
    second = Step("second", 2)
    second.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        steps=[first, second],
        state={"first": "done"},
    )
    save_transaction(tx, db_path)

    loaded = load_transaction(tx.id, db_path)

    assert loaded.status is Status.IN_PROGRESS
    assert [step.status for step in loaded.steps] == [
        Status.SUCCESSFUL,
        Status.IN_PROGRESS,
    ]
    assert loaded.state == {"first": "done"}


def test_resume_interrupted_transaction_skips_successful_and_preserves_state(db_path) -> None:
    counts: dict[str, int] = {}
    first = Step("first", 1)
    first.status = Status.SUCCESSFUL
    second = Step("second", 2)
    second.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        steps=[first, second],
        state={"first": "done"},
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingStep("first", 1, counts),
            _TrackingStep("second", 2, counts),
        ],
        db_path=db_path,
    )

    assert resumed.status is Status.PENDING
    assert resumed.state == {"first": "done"}
    assert [step.status for step in resumed.steps] == [
        Status.SUCCESSFUL,
        Status.PENDING,
    ]

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"second": 1}
    assert resumed.state == {"first": "done"}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_interrupted_transaction_preserves_skipped_steps(db_path) -> None:
    counts: dict[str, int] = {}
    skipped = Step("optional", 1)
    skipped.status = Status.SKIPPED
    running = Step("main", 2)
    running.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        steps=[skipped, running],
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingStep("optional", 1, counts),
            _TrackingStep("main", 2, counts),
        ],
        db_path=db_path,
    )

    assert [step.status for step in resumed.steps] == [
        Status.SKIPPED,
        Status.PENDING,
    ]

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"main": 1}


def test_repeated_resume_does_not_duplicate_resume_history(db_path) -> None:
    running = Step("main", 1)
    running.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        steps=[running],
    )
    save_transaction(tx, db_path)

    first = resume_transaction(
        tx.id,
        [_TrackingStep("main", 1, {})],
        db_path=db_path,
    )
    save_transaction(first, db_path)
    second = resume_transaction(
        tx.id,
        [_TrackingStep("main", 1, {})],
        db_path=db_path,
    )

    assert [entry.event for entry in second.history].count(
        HistoryEvent.TRANSACTION_RESUMED
    ) == 1


def test_repeated_resume_preserves_skipped_recovery_decision(db_path) -> None:
    skipped = Step("optional", 1)
    skipped.status = Status.SKIPPED
    running = Step("main", 2)
    running.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        steps=[skipped, running],
    )
    save_transaction(tx, db_path)

    first = resume_transaction(
        tx.id,
        [
            _TrackingStep("optional", 1, {}),
            _TrackingStep("main", 2, {}),
        ],
        db_path=db_path,
    )
    save_transaction(first, db_path)
    second = resume_transaction(
        tx.id,
        [
            _TrackingStep("optional", 1, {}),
            _TrackingStep("main", 2, {}),
        ],
        db_path=db_path,
    )

    assert [step.status for step in second.steps] == [
        Status.SKIPPED,
        Status.PENDING,
    ]


def test_resume_does_not_rerun_business_failed_steps(db_path) -> None:
    counts: dict[str, int] = {}

    failed = Step("failed", 1)
    failed.status = Status.FAILED
    failed.exceptions.append(BusinessException("bad data", action="failed"))
    next_step = Step("next", 2)
    next_step.status = Status.PENDING

    tx = Transaction(
        reference="REF-001",
        status=Status.FAILED,
        steps=[failed, next_step],
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingStep("failed", 1, counts),
            _TrackingStep("next", 2, counts),
        ],
        db_path=db_path,
    )

    assert resumed.steps[0].status is Status.FAILED
    assert resumed.steps[1].status is Status.PENDING

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"next": 1}
    assert resumed.steps[0].status is Status.FAILED
    assert resumed.status is Status.FAILED


def test_resume_preserves_downstream_skip_from_stopping_business_failure(
    db_path,
) -> None:
    transaction = _save_stopped_transaction(db_path)
    counts: dict[str, int] = {}

    resumed = resume_transaction(
        transaction.id,
        [
            _TrackingStep("validate", 1, counts),
            _TrackingStep("downstream", 2, counts),
        ],
        db_path=db_path,
    )

    assert [step.status for step in resumed.steps] == [
        Status.FAILED,
        Status.SKIPPED,
    ]

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {}
    assert resumed.status is Status.FAILED


def test_resume_does_not_infer_stop_causality_without_skip_history(db_path) -> None:
    failed = Step("validate", 1)
    failed.status = Status.FAILED
    failed.exceptions.append(
        BusinessException(
            "stop", action="validate", halts_remaining_steps=True
        )
    )
    skipped = Step("downstream", 2)
    skipped.status = Status.SKIPPED
    transaction = Transaction(
        reference="manual-skip",
        status=Status.FAILED,
        steps=[failed, skipped],
    )
    save_transaction(transaction, db_path)

    resumed = resume_transaction(
        transaction.id,
        [Step("validate", 1), Step("downstream", 2)],
        db_path=db_path,
    )

    assert [step.status for step in resumed.steps] == [
        Status.FAILED,
        Status.PENDING,
    ]


def test_repeated_resume_preserves_stopping_business_skip(db_path) -> None:
    transaction = _save_stopped_transaction(db_path)

    first = resume_transaction(
        transaction.id,
        [Step("validate", 1), Step("downstream", 2)],
        db_path=db_path,
    )
    save_transaction(first, db_path)
    second = resume_transaction(
        transaction.id,
        [Step("validate", 1), Step("downstream", 2)],
        db_path=db_path,
    )

    assert [step.status for step in second.steps] == [
        Status.FAILED,
        Status.SKIPPED,
    ]


def test_resume_can_retry_business_failed_steps_when_policy_allows_it(db_path) -> None:
    counts: dict[str, int] = {}
    failed = Step("failed", 1)
    failed.status = Status.FAILED
    failed.exceptions.append(BusinessException("bad data", action="failed"))
    tx = Transaction(reference="REF-001", status=Status.FAILED, steps=[failed])
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [_TrackingStep("failed", 1, counts)],
        db_path=db_path,
        retry_business_failures=True,
    )

    assert resumed.steps[0].status is Status.PENDING

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"failed": 1}
    assert resumed.status is Status.SUCCESSFUL


def test_business_retry_policy_resets_stopping_failure_and_causal_skip(
    db_path,
) -> None:
    transaction = _save_stopped_transaction(db_path)
    counts: dict[str, int] = {}

    resumed = resume_transaction(
        transaction.id,
        [
            _TrackingStep("validate", 1, counts),
            _TrackingStep("downstream", 2, counts),
        ],
        db_path=db_path,
        retry_business_failures=True,
    )

    assert [step.status for step in resumed.steps] == [
        Status.PENDING,
        Status.PENDING,
    ]

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"validate": 1, "downstream": 1}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_successful_transaction_is_effective_no_op(db_path) -> None:
    counts: dict[str, int] = {}

    step = Step("done", 1)
    step.status = Status.SUCCESSFUL
    tx = Transaction(reference="REF-001", status=Status.SUCCESSFUL, steps=[step])
    tx.started_at = tx.created_at
    tx.finished_at = tx.created_at
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [_TrackingStep("done", 1, counts)],
        db_path=db_path,
    )

    assert resumed.status is Status.SUCCESSFUL
    assert resumed.steps[0].status is Status.SUCCESSFUL
    assert resumed.started_at == tx.started_at
    assert resumed.finished_at == tx.finished_at

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_rejects_missing_identity_without_mutating_persisted_record(db_path) -> None:
    step = Step("running", 1)
    step.status = Status.IN_PROGRESS
    transaction = Transaction(
        reference="identified",
        status=Status.IN_PROGRESS,
        steps=[step],
        definition_identity=_DEFINITION_IDENTITY,
    )
    _save_transaction(transaction, db_path)
    before = serialize_transaction(load_transaction(transaction.id, db_path))

    with pytest.raises(DefinitionIdentityError, match="non-empty str"):
        _resume_transaction(
            transaction.id,
            [Step("running", 1)],
            db_path=db_path,
        )

    assert serialize_transaction(load_transaction(transaction.id, db_path)) == before


def test_resume_rejects_mismatched_identity_without_mutating_persisted_record(
    db_path,
) -> None:
    step = Step("running", 1)
    step.status = Status.IN_PROGRESS
    transaction = Transaction(
        reference="identified",
        status=Status.IN_PROGRESS,
        steps=[step],
        definition_identity=_DEFINITION_IDENTITY,
    )
    _save_transaction(transaction, db_path)
    before = serialize_transaction(load_transaction(transaction.id, db_path))

    with pytest.raises(DefinitionIdentityError, match="does not match"):
        _resume_transaction(
            transaction.id,
            [Step("running", 1)],
            db_path=db_path,
            definition_identity="tests.recovery/v2",
        )

    assert serialize_transaction(load_transaction(transaction.id, db_path)) == before


def test_resume_rejects_unidentified_legacy_record(db_path) -> None:
    step = Step("running", 1)
    step.status = Status.IN_PROGRESS
    transaction = Transaction(
        reference="legacy",
        status=Status.IN_PROGRESS,
        steps=[step],
    )
    _save_transaction(transaction, db_path)

    with pytest.raises(DefinitionIdentityError, match="has no definition identity"):
        _resume_transaction(
            transaction.id,
            [Step("running", 1)],
            db_path=db_path,
            definition_identity=_DEFINITION_IDENTITY,
        )


def test_successful_transaction_is_no_op_without_identity_comparison(db_path) -> None:
    step = Step("done", 1)
    step.status = Status.SUCCESSFUL
    transaction = Transaction(
        reference="done",
        status=Status.SUCCESSFUL,
        steps=[step],
        definition_identity=_DEFINITION_IDENTITY,
    )
    _save_transaction(transaction, db_path)

    resumed = _resume_transaction(
        transaction.id,
        [Step("changed", 99)],
        db_path=db_path,
        definition_identity="different/v2",
    )

    assert resumed.status is Status.SUCCESSFUL
    assert resumed.definition_identity == _DEFINITION_IDENTITY


def test_resume_missing_step_mapping_raises_clear_error(db_path) -> None:
    tx = Transaction(reference="REF-001", steps=[Step("only", 1)])
    save_transaction(tx, db_path)

    with pytest.raises(KeyError, match="Missing recovery step"):
        resume_transaction(tx.id, [], db_path=db_path)


def test_resume_duplicate_step_mapping_raises_clear_error(db_path) -> None:
    tx = Transaction(reference="REF-001", steps=[Step("dup", 1)])
    save_transaction(tx, db_path)

    with pytest.raises(ValueError, match="Duplicate recovery step provided"):
        resume_transaction(
            tx.id,
            [Step("dup", 1), Step("dup", 1)],
            db_path=db_path,
        )


def test_resume_extra_step_mapping_raises_clear_error(db_path) -> None:
    tx = Transaction(reference="REF-001", steps=[Step("persisted", 1)])
    save_transaction(tx, db_path)

    with pytest.raises(KeyError, match="did not match persisted transaction"):
        resume_transaction(
            tx.id,
            [Step("persisted", 1), Step("extra", 2)],
            db_path=db_path,
        )


def test_resume_resets_skipped_steps_to_pending(db_path) -> None:
    counts: dict[str, int] = {}

    skipped = Step("optional", 1)
    skipped.status = Status.SKIPPED
    pending = Step("main", 2)
    pending.status = Status.PENDING

    tx = Transaction(reference="REF-001", status=Status.FAILED, steps=[skipped, pending])
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingStep("optional", 1, counts),
            _TrackingStep("main", 2, counts),
        ],
        db_path=db_path,
    )

    assert resumed.steps[0].status is Status.PENDING
    assert resumed.steps[1].status is Status.PENDING

    Engine().run(ProcessContext(transaction=resumed))

    assert counts.get("optional", 0) == 1
    assert counts.get("main", 0) == 1
    assert resumed.status is Status.SUCCESSFUL


def test_resume_copies_exception_objects(db_path) -> None:
    failed = Step("failed", 1)
    failed.status = Status.FAILED
    original = SystemException("timeout", action="failed")
    failed.exceptions.append(original)

    tx = Transaction(reference="REF-001", status=Status.FAILED, steps=[failed])
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [Step("failed", 1)],
        db_path=db_path,
    )

    recovered = resumed.steps[0].exceptions[0]
    assert recovered is not original
    assert str(recovered) == str(original)
    assert recovered.action == original.action

    recovered.action = "mutated"
    assert original.action == "failed"
