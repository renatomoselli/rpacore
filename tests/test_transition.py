"""Public execution-transition contract and Engine integration tests."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from rpacore import ExecutionTransition
from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import (
    BusinessException,
    ExecutionValidationError,
    SystemException,
)
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.persistence import load_transaction, save_transaction
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


_RECORD_FIELDS = {
    "schema_version",
    "transition_id",
    "sequence",
    "occurred_at",
    "event",
    "transaction_id",
    "transaction_reference",
    "definition_identity",
    "transaction_status",
    "execution_pass",
    "skill_name",
    "skill_execution_order",
    "skill_status",
    "outcome_category",
    "failure_code",
    "retry_recommended",
    "checkpoint_state",
}


class _SetStateSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state["public"] = "done"
        ctx.state["secret"] = "must-not-be-emitted"


class _SystemFailureSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise SystemException(
            "sensitive failure text",
            action=self.name,
            code="tests.transition.system_failure",
        )


def _ctx(transaction: Transaction) -> ProcessContext:
    return ProcessContext(
        transaction=transaction,
        config={"config_only": "not-a-transition"},
        resources={"runtime_handle": object()},
    )


def _transaction(*skills: Skill) -> Transaction:
    return Transaction(
        reference="transition-test",
        skills=list(skills),
        definition_identity="tests.transition/v1",
        metadata={"metadata_only": "not-a-transition"},
    )


def _normalized(transitions: list[ExecutionTransition]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for transition in transitions:
        record = transition.to_record()
        for field in ("transition_id", "transaction_id", "occurred_at"):
            record.pop(field)
        normalized.append(record)
    return normalized


def test_transition_facts_are_storage_neutral_with_or_without_checkpoint(
    tmp_path: Path,
) -> None:
    persisted_tx = _transaction(_SetStateSkill("set-state", 1))
    memory_tx = _transaction(_SetStateSkill("set-state", 1))
    persisted: list[ExecutionTransition] = []
    in_memory: list[ExecutionTransition] = []
    database = tmp_path / "transactions.db"

    Engine().run(
        _ctx(persisted_tx),
        transition_sink=persisted.append,
        transition_state_fields=("public",),
        checkpoint=lambda transaction: save_transaction(transaction, str(database)),
    )
    Engine().run(
        _ctx(memory_tx),
        transition_sink=in_memory.append,
        transition_state_fields=("public",),
    )

    assert _normalized(persisted) == _normalized(in_memory)
    assert load_transaction(persisted_tx.id, str(database)).status is Status.SUCCESSFUL
    assert [item.event for item in in_memory] == [
        HistoryEvent.TRANSACTION_STARTED,
        HistoryEvent.SKILL_STARTED,
        HistoryEvent.SKILL_SUCCEEDED,
        HistoryEvent.TRANSACTION_COMPLETED,
    ]


def test_transition_is_minimized_allowlisted_and_detached_from_mutable_state() -> None:
    transitions: list[ExecutionTransition] = []
    tx = _transaction(_SetStateSkill("set-state", 1))
    nested_state = {"items": ["original"]}
    tx.state["nested"] = nested_state
    Engine().run(
        _ctx(tx),
        transition_sink=transitions.append,
        transition_state_fields=("public", "nested", "absent"),
    )

    terminal = transitions[-1]
    record = terminal.to_record()
    expected_state = {
        "nested": {"items": ["original"]},
        "public": "done",
    }
    assert set(record) == _RECORD_FIELDS
    assert record["checkpoint_state"] == expected_state
    assert "absent" not in record["checkpoint_state"]
    serialized = json.dumps(record, sort_keys=True)
    assert "must-not-be-emitted" not in serialized
    assert "metadata_only" not in serialized
    assert "config_only" not in serialized
    assert "runtime_handle" not in serialized
    assert "sensitive failure text" not in serialized
    assert "retry_disposition" not in record

    tx.state["public"] = "mutated-after-emission"
    nested_state["items"].append("mutated-after-emission")
    assert terminal.to_record()["checkpoint_state"] == expected_state
    mutable_view = terminal.checkpoint_state
    mutable_view["public"] = "mutated-decoded-view"
    nested_view = mutable_view["nested"]
    assert isinstance(nested_view, dict)
    nested_items = nested_view["items"]
    assert isinstance(nested_items, list)
    nested_items.append("mutated-decoded-view")
    assert terminal.checkpoint_state == expected_state
    with pytest.raises(FrozenInstanceError):
        terminal.event = "mutated"  # type: ignore[misc]


def test_system_failure_recommends_retry_without_claiming_coordinator_truth() -> None:
    transitions: list[ExecutionTransition] = []
    tx = _transaction(_SystemFailureSkill("fail", 1))
    Engine(max_retries=0).run(_ctx(tx), transition_sink=transitions.append)

    terminal = transitions[-1].to_record()
    assert tx.status is Status.FAILED
    assert tx.outcome_category is OutcomeCategory.SYSTEM_FAILED
    assert tx.retry_disposition is RetryDisposition.RETRY_EXHAUSTED
    assert terminal["outcome_category"] == OutcomeCategory.SYSTEM_FAILED.value
    assert terminal["failure_code"] == "tests.transition.system_failure"
    assert terminal["retry_recommended"] is True
    assert "retry_disposition" not in terminal


def test_business_validation_interruption_retry_and_skip_facts_are_closed() -> None:
    business: list[ExecutionTransition] = []
    business_tx = _transaction(Skill("unused", 1), Skill("downstream", 2))
    business_tx.skills[0].execute = lambda ctx: (_ for _ in ()).throw(  # type: ignore[method-assign]
        BusinessException("business", action="unused", stop=True)
    )
    Engine().run(_ctx(business_tx), transition_sink=business.append)
    assert business[-1].retry_recommended is False
    assert [item.event for item in business] == [
        HistoryEvent.TRANSACTION_STARTED,
        HistoryEvent.SKILL_STARTED,
        HistoryEvent.SKILL_FAILED,
        HistoryEvent.SKILL_SKIPPED,
        HistoryEvent.TRANSACTION_COMPLETED,
    ]

    invalid: list[ExecutionTransition] = []
    invalid_tx = _transaction(Skill("same", 1), Skill("same", 2))
    with pytest.raises(ExecutionValidationError):
        Engine().run(
            _ctx(invalid_tx),
            transition_sink=invalid.append,
            transition_state_fields=("bad",),
        )
    assert [item.event for item in invalid] == [HistoryEvent.TRANSACTION_COMPLETED]
    assert invalid[0].outcome_category == OutcomeCategory.VALIDATION_FAILED.value
    assert invalid[0].checkpoint_state == {}

    interrupted: list[ExecutionTransition] = []
    interrupted_tx = _transaction(Skill("interrupt", 1))
    interrupted_tx.skills[0].execute = lambda ctx: (_ for _ in ()).throw(  # type: ignore[method-assign]
        MemoryError("interrupt")
    )
    with pytest.raises(MemoryError):
        Engine().run(_ctx(interrupted_tx), transition_sink=interrupted.append)
    assert HistoryEvent.SKILL_INTERRUPTED in [item.event for item in interrupted]
    assert interrupted[-1].outcome_category == OutcomeCategory.INTERRUPTED.value
    assert interrupted[-1].retry_recommended is None

    calls = 0

    class _FlakySkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SystemException("temporary", action=self.name)

    retried: list[ExecutionTransition] = []
    Engine(max_retries=1).run(
        _ctx(_transaction(_FlakySkill("flaky", 1))),
        transition_sink=retried.append,
    )
    assert HistoryEvent.RETRY_SCHEDULED in [item.event for item in retried]
    assert retried[-1].transaction_status == Status.SUCCESSFUL.value


def test_no_sink_keeps_ordinary_engine_behavior() -> None:
    tx = _transaction(_SetStateSkill("set-state", 1))
    Engine().run(_ctx(tx))
    assert tx.status is Status.SUCCESSFUL
    assert tx.state == {
        "public": "done",
        "secret": "must-not-be-emitted",
    }


def test_transition_options_fail_before_transaction_mutation() -> None:
    cases: list[tuple[dict[str, object], type[Exception], str]] = [
        (
            {"transition_state_fields": ("public",)},
            ValueError,
            "transition_state_fields requires transition_sink",
        ),
        (
            {"transition_sink": object()},
            TypeError,
            "transition_sink must be callable or None",
        ),
        (
            {
                "transition_sink": lambda transition: None,
                "transition_state_fields": "public",
            },
            TypeError,
            "transition_state_fields must be an iterable of strings",
        ),
        (
            {
                "transition_sink": lambda transition: None,
                "transition_state_fields": ("public", "public"),
            },
            ValueError,
            "transition_state_fields must not contain duplicates",
        ),
        (
            {
                "transition_sink": lambda transition: None,
                "transition_state_fields": ("",),
            },
            ValueError,
            "transition_state_fields must contain non-empty strings",
        ),
    ]

    for kwargs, error, message in cases:
        tx = _transaction(_SetStateSkill("set-state", 1))
        with pytest.raises(error, match=message):
            Engine().run(_ctx(tx), **kwargs)  # type: ignore[arg-type]
        assert tx.status is Status.PENDING
        assert tx.history == []


def test_transition_sink_precedes_checkpoint_and_checkpoint_failure() -> None:
    effects = 0
    observed: list[tuple[str, str]] = []

    class _EffectSkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal effects
            effects += 1

    def sink(transition: ExecutionTransition) -> None:
        observed.append(("sink", transition.event))

    def checkpoint(transaction: Transaction) -> None:
        event = transaction.history[-1].event.value
        observed.append(("checkpoint", event))
        if event == HistoryEvent.SKILL_STARTED.value:
            raise RuntimeError("checkpoint failed after sink")

    tx = _transaction(_EffectSkill("effect", 1))
    with pytest.raises(RuntimeError, match="checkpoint failed after sink"):
        Engine().run(
            _ctx(tx),
            transition_sink=sink,
            checkpoint=checkpoint,
        )

    assert effects == 0
    assert observed == [
        ("sink", HistoryEvent.TRANSACTION_STARTED.value),
        ("checkpoint", HistoryEvent.TRANSACTION_STARTED.value),
        ("sink", HistoryEvent.SKILL_STARTED.value),
        ("checkpoint", HistoryEvent.SKILL_STARTED.value),
    ]


def test_sink_failure_before_effect_stops_execution_and_checkpoint() -> None:
    effects = 0
    delivered: list[str] = []
    checkpoints: list[str] = []

    class _EffectSkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal effects
            effects += 1

    def fail_before_effect(transition: ExecutionTransition) -> None:
        if transition.event == HistoryEvent.SKILL_STARTED.value:
            raise RuntimeError("transition unavailable")
        delivered.append(transition.event)

    with pytest.raises(RuntimeError, match="transition unavailable"):
        Engine().run(
            _ctx(_transaction(_EffectSkill("effect", 1))),
            transition_sink=fail_before_effect,
            checkpoint=lambda transaction: checkpoints.append(
                transaction.history[-1].event.value
            ),
        )
    assert effects == 0
    assert delivered == [HistoryEvent.TRANSACTION_STARTED.value]
    assert checkpoints == [HistoryEvent.TRANSACTION_STARTED.value]


def test_sink_failure_after_effect_propagates_without_reexecution() -> None:
    effects = 0
    delivered: list[str] = []

    class _EffectSkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal effects
            effects += 1

    def fail_after_effect(transition: ExecutionTransition) -> None:
        if transition.event == HistoryEvent.SKILL_SUCCEEDED.value:
            raise RuntimeError("after-effect transition unavailable")
        delivered.append(transition.event)

    tx = _transaction(_EffectSkill("effect", 1))
    with pytest.raises(RuntimeError, match="after-effect transition unavailable"):
        Engine().run(_ctx(tx), transition_sink=fail_after_effect)

    assert effects == 1
    assert tx.status is Status.IN_PROGRESS
    assert tx.skills[0].status is Status.SUCCESSFUL
    assert tx.history[-1].event is HistoryEvent.SKILL_SUCCEEDED
    assert delivered == [
        HistoryEvent.TRANSACTION_STARTED.value,
        HistoryEvent.SKILL_STARTED.value,
    ]


def test_retry_scheduled_sink_failure_stops_before_retry_pass() -> None:
    calls = 0
    observed: list[str] = []

    class _FlakySkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal calls
            calls += 1
            raise SystemException("temporary", action=self.name)

    def fail_on_retry(transition: ExecutionTransition) -> None:
        observed.append(transition.event)
        if transition.event == HistoryEvent.RETRY_SCHEDULED.value:
            raise RuntimeError("retry transition unavailable")

    tx = _transaction(_FlakySkill("flaky", 1))
    with pytest.raises(RuntimeError, match="retry transition unavailable"):
        Engine(max_retries=1).run(_ctx(tx), transition_sink=fail_on_retry)

    assert calls == 1
    assert tx.status is Status.IN_PROGRESS
    assert tx.retry_count == 1
    assert tx.skills[0].status is Status.PENDING
    assert tx.history[-1].event is HistoryEvent.RETRY_SCHEDULED
    assert observed[-1] == HistoryEvent.RETRY_SCHEDULED.value


def test_skipped_skill_sink_failure_exposes_exact_partial_state() -> None:
    first = Skill("first", 1)
    second = Skill("second", 2)
    third = Skill("third", 3)
    first.execute = lambda ctx: (_ for _ in ()).throw(  # type: ignore[method-assign]
        BusinessException("stop", action="first", stop=True)
    )

    def fail_on_skip(transition: ExecutionTransition) -> None:
        if transition.event == HistoryEvent.SKILL_SKIPPED.value:
            raise RuntimeError("skip transition unavailable")

    tx = _transaction(first, second, third)
    with pytest.raises(RuntimeError, match="skip transition unavailable"):
        Engine().run(_ctx(tx), transition_sink=fail_on_skip)

    assert tx.status is Status.IN_PROGRESS
    assert [skill.status for skill in tx.skills] == [
        Status.FAILED,
        Status.SKIPPED,
        Status.PENDING,
    ]
    assert tx.history[-1].event is HistoryEvent.SKILL_SKIPPED
    assert tx.history[-1].skill_name == "second"


def test_validation_sink_failure_supersedes_validation_error_after_mutation() -> None:
    observed: list[ExecutionTransition] = []
    checkpoints: list[HistoryEvent] = []

    def fail_on_validation(transition: ExecutionTransition) -> None:
        observed.append(transition)
        raise RuntimeError("validation transition unavailable")

    tx = _transaction(Skill("same", 1), Skill("same", 2))
    with pytest.raises(RuntimeError, match="validation transition unavailable"):
        Engine().run(
            _ctx(tx),
            transition_sink=fail_on_validation,
            checkpoint=lambda transaction: checkpoints.append(
                transaction.history[-1].event
            ),
        )

    assert tx.status is Status.FAILED
    assert tx.outcome_category is OutcomeCategory.VALIDATION_FAILED
    assert tx.finished_at is not None
    assert [entry.event for entry in tx.history] == [HistoryEvent.TRANSACTION_COMPLETED]
    assert [transition.event for transition in observed] == [
        HistoryEvent.TRANSACTION_COMPLETED
    ]
    assert checkpoints == []


def test_terminal_sink_failure_keeps_terminal_state_and_skips_checkpoint() -> None:
    observed: list[HistoryEvent] = []
    checkpoints: list[HistoryEvent] = []

    def fail_on_completion(transition: ExecutionTransition) -> None:
        observed.append(HistoryEvent(transition.event))
        if transition.event == HistoryEvent.TRANSACTION_COMPLETED.value:
            raise RuntimeError("terminal transition unavailable")

    tx = _transaction(_SetStateSkill("set-state", 1))
    with pytest.raises(RuntimeError, match="terminal transition unavailable"):
        Engine().run(
            _ctx(tx),
            transition_sink=fail_on_completion,
            checkpoint=lambda transaction: checkpoints.append(
                transaction.history[-1].event
            ),
        )

    assert tx.status is Status.SUCCESSFUL
    assert tx.outcome_category is OutcomeCategory.SUCCESSFUL
    assert tx.finished_at is not None
    assert tx.history[-1].event is HistoryEvent.TRANSACTION_COMPLETED
    assert observed[-1] is HistoryEvent.TRANSACTION_COMPLETED
    assert HistoryEvent.TRANSACTION_COMPLETED not in checkpoints
    assert checkpoints[-1] is HistoryEvent.SKILL_SUCCEEDED
