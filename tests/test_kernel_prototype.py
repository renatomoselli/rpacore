"""BL-034 proof for one private kernel transition seam and two adapters."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from rpacore._kernel import _ExecutionTransition
from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
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


class _ProtocolRejected(ValueError):
    pass


class _ServerAuthorityReducer:
    """Small in-process reducer; deliberately not a service implementation."""

    def __init__(self, *, fence: str = "fence-1") -> None:
        self.fence = fence
        self.revision = 0
        self.last_sequence = 0
        self.records: list[dict[str, object]] = []
        self._idempotency: dict[str, tuple[str, int]] = {}

    def rotate_fence(self, fence: str) -> None:
        self.fence = fence

    def accept(
        self,
        transition: _ExecutionTransition,
        *,
        fence: str,
        expected_revision: int,
    ) -> int:
        return self.accept_record(
            transition.to_record(),
            fence=fence,
            expected_revision=expected_revision,
        )

    def accept_record(
        self,
        record: dict[str, object],
        *,
        fence: str,
        expected_revision: int,
    ) -> int:
        if set(record) != _RECORD_FIELDS:
            raise _ProtocolRejected("transition record has unknown or missing fields")
        if record["schema_version"] != 1:
            raise _ProtocolRejected("unsupported transition schema")
        if fence != self.fence:
            raise _ProtocolRejected("stale assignment fence")

        transition_id = record["transition_id"]
        if not isinstance(transition_id, str) or not transition_id:
            raise _ProtocolRejected("invalid transition id")
        payload_hash = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous = self._idempotency.get(transition_id)
        if previous is not None:
            previous_hash, previous_revision = previous
            if previous_hash != payload_hash:
                raise _ProtocolRejected("idempotency payload conflict")
            return previous_revision

        if expected_revision != self.revision:
            raise _ProtocolRejected("server revision conflict")
        sequence = record["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise _ProtocolRejected("invalid transition sequence")
        if sequence != self.last_sequence + 1:
            raise _ProtocolRejected("transition sequence gap or reorder")

        self.revision += 1
        self.last_sequence = sequence
        accepted = dict(record)
        self.records.append(accepted)
        self._idempotency[transition_id] = (payload_hash, self.revision)
        return self.revision


class _ConnectedTestAdapter:
    def __init__(self, reducer: _ServerAuthorityReducer, *, fence: str = "fence-1") -> None:
        self.reducer = reducer
        self.fence = fence
        self.available = True

    def __call__(self, transition: _ExecutionTransition) -> None:
        if not self.available:
            raise RuntimeError("simulated Cloud outage")
        self.reducer.accept(
            transition,
            fence=self.fence,
            expected_revision=self.reducer.revision,
        )


class _SetStateSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state["public"] = "done"
        ctx.state["secret"] = "must-not-leave-worker"


class _SystemFailureSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise SystemException(
            "private failure text",
            action=self.name,
            code="tests.kernel.system_failure",
        )


def _ctx(transaction: Transaction) -> ProcessContext:
    return ProcessContext(
        transaction=transaction,
        config={"private_config": "not-a-transition"},
        resources={"private_handle": object()},
    )


def _transaction(*skills: Skill) -> Transaction:
    return Transaction(
        reference="kernel-proof",
        skills=list(skills),
        definition_identity="tests.kernel/v1",
        metadata={"private_metadata": "not-a-transition"},
    )


def _normalized(transitions: list[_ExecutionTransition]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for transition in transitions:
        record = transition.to_record()
        for field in ("transition_id", "transaction_id", "occurred_at"):
            record.pop(field)
        normalized.append(record)
    return normalized


def test_local_and_server_authority_adapters_share_ordered_kernel_facts(
    tmp_path: Path,
) -> None:
    local_tx = _transaction(_SetStateSkill("set-state", 1))
    connected_tx = _transaction(_SetStateSkill("set-state", 1))
    local_transitions: list[_ExecutionTransition] = []
    connected_transitions: list[_ExecutionTransition] = []
    local_db = tmp_path / "local.db"

    Engine()._run_with_transition_sink(
        _ctx(local_tx),
        transition_sink=local_transitions.append,
        checkpoint=lambda tx: save_transaction(tx, str(local_db)),
        checkpoint_state_fields=("public",),
    )
    Engine()._run_with_transition_sink(
        _ctx(connected_tx),
        transition_sink=connected_transitions.append,
        checkpoint_state_fields=("public",),
    )

    assert _normalized(local_transitions) == _normalized(connected_transitions)
    assert load_transaction(local_tx.id, str(local_db)).status is Status.SUCCESSFUL
    assert connected_tx.status is Status.SUCCESSFUL
    assert [item.event for item in connected_transitions] == [
        HistoryEvent.TRANSACTION_STARTED,
        HistoryEvent.SKILL_STARTED,
        HistoryEvent.SKILL_SUCCEEDED,
        HistoryEvent.TRANSACTION_COMPLETED,
    ]


def test_server_reducer_guards_duplicate_reorder_revision_and_fence() -> None:
    transitions: list[_ExecutionTransition] = []
    Engine()._run_with_transition_sink(
        _ctx(_transaction(_SetStateSkill("set-state", 1))),
        transition_sink=transitions.append,
    )
    first, second = transitions[:2]
    reducer = _ServerAuthorityReducer()

    assert reducer.accept(first, fence="fence-1", expected_revision=0) == 1
    assert reducer.accept(first, fence="fence-1", expected_revision=0) == 1
    assert reducer.revision == 1

    conflicting = replace(first, event=HistoryEvent.SKILL_FAILED.value)
    with pytest.raises(_ProtocolRejected, match="idempotency payload conflict"):
        reducer.accept(conflicting, fence="fence-1", expected_revision=1)
    with pytest.raises(_ProtocolRejected, match="server revision conflict"):
        reducer.accept(second, fence="fence-1", expected_revision=0)

    reordered = replace(
        second,
        sequence=second.sequence + 1,
        transition_id=f"{second.transaction_id}:{second.sequence + 1}",
    )
    with pytest.raises(_ProtocolRejected, match="sequence gap or reorder"):
        reducer.accept(reordered, fence="fence-1", expected_revision=1)

    reducer.rotate_fence("fence-2")
    with pytest.raises(_ProtocolRejected, match="stale assignment fence"):
        reducer.accept(second, fence="fence-1", expected_revision=1)
    assert reducer.revision == 1


def test_reducer_rejects_poison_and_non_transition_records() -> None:
    reducer = _ServerAuthorityReducer()
    with pytest.raises(_ProtocolRejected, match="unknown or missing"):
        reducer.accept_record(
            {"schema_version": 1, "event": "diagnostic_log", "message": "not truth"},
            fence="fence-1",
            expected_revision=0,
        )

    transitions: list[_ExecutionTransition] = []
    Engine()._run_with_transition_sink(
        _ctx(_transaction(_SetStateSkill("set-state", 1))),
        transition_sink=transitions.append,
    )
    unknown_version = transitions[0].to_record()
    unknown_version["schema_version"] = 99
    with pytest.raises(_ProtocolRejected, match="unsupported transition schema"):
        reducer.accept_record(
            unknown_version,
            fence="fence-1",
            expected_revision=0,
        )
    assert reducer.revision == 0


def test_transition_is_minimized_allowlisted_and_detached_from_mutable_state() -> None:
    transitions: list[_ExecutionTransition] = []
    tx = _transaction(_SetStateSkill("set-state", 1))
    Engine()._run_with_transition_sink(
        _ctx(tx),
        transition_sink=transitions.append,
        checkpoint_state_fields=("public",),
    )

    terminal = transitions[-1]
    record = terminal.to_record()
    assert set(record) == _RECORD_FIELDS
    assert record["checkpoint_state"] == {"public": "done"}
    serialized = json.dumps(record, sort_keys=True)
    assert "must-not-leave-worker" not in serialized
    assert "private_metadata" not in serialized
    assert "private_config" not in serialized
    assert "private_handle" not in serialized
    assert "private failure text" not in serialized
    assert "retry_disposition" not in record

    tx.state["public"] = "mutated-after-emission"
    assert terminal.to_record()["checkpoint_state"] == {"public": "done"}


def test_connected_system_failure_reports_recommendation_not_coordinator_truth() -> None:
    transitions: list[_ExecutionTransition] = []
    tx = _transaction(_SystemFailureSkill("fail", 1))
    Engine(max_retries=0)._run_with_transition_sink(
        _ctx(tx),
        transition_sink=transitions.append,
    )

    terminal = transitions[-1].to_record()
    assert tx.status is Status.FAILED
    assert tx.outcome_category is OutcomeCategory.SYSTEM_FAILED
    assert tx.retry_disposition is RetryDisposition.RETRY_EXHAUSTED
    assert terminal["outcome_category"] == OutcomeCategory.SYSTEM_FAILED.value
    assert terminal["failure_code"] == "tests.kernel.system_failure"
    assert terminal["retry_recommended"] is True
    assert "retry_disposition" not in terminal


def test_business_validation_interruption_retry_and_skip_facts_are_closed() -> None:
    business: list[_ExecutionTransition] = []
    business_tx = _transaction(
        Skill("unused", 1),
    )
    business_tx.skills[0].execute = lambda ctx: (_ for _ in ()).throw(  # type: ignore[method-assign]
        BusinessException("business", action="unused", stop=True)
    )
    Engine()._run_with_transition_sink(
        _ctx(business_tx),
        transition_sink=business.append,
    )
    assert business[-1].retry_recommended is False

    invalid: list[_ExecutionTransition] = []
    invalid_tx = _transaction(Skill("same", 1), Skill("same", 2))
    with pytest.raises(ExecutionValidationError):
        Engine()._run_with_transition_sink(
            _ctx(invalid_tx),
            transition_sink=invalid.append,
            checkpoint_state_fields=("bad",),
        )
    assert [item.event for item in invalid] == [HistoryEvent.TRANSACTION_COMPLETED]
    assert invalid[0].outcome_category == OutcomeCategory.VALIDATION_FAILED.value
    assert invalid[0].checkpoint_state_json == "{}"

    interrupted: list[_ExecutionTransition] = []
    interrupted_tx = _transaction(Skill("interrupt", 1))
    interrupted_tx.skills[0].execute = lambda ctx: (_ for _ in ()).throw(  # type: ignore[method-assign]
        MemoryError("interrupt")
    )
    with pytest.raises(MemoryError):
        Engine()._run_with_transition_sink(
            _ctx(interrupted_tx),
            transition_sink=interrupted.append,
        )
    assert HistoryEvent.SKILL_INTERRUPTED in [item.event for item in interrupted]
    assert interrupted[-1].outcome_category == OutcomeCategory.INTERRUPTED.value

    calls = 0

    class _FlakySkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SystemException("temporary", action=self.name)

    retried: list[_ExecutionTransition] = []
    Engine(max_retries=1)._run_with_transition_sink(
        _ctx(_transaction(_FlakySkill("flaky", 1))),
        transition_sink=retried.append,
    )
    assert HistoryEvent.RETRY_SCHEDULED in [item.event for item in retried]
    assert retried[-1].transaction_status == Status.SUCCESSFUL.value


def test_strict_adapter_outage_stops_before_unrecorded_effect() -> None:
    effects = 0

    class _EffectSkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal effects
            effects += 1

    reducer = _ServerAuthorityReducer()
    adapter = _ConnectedTestAdapter(reducer)

    def fail_before_effect(transition: _ExecutionTransition) -> None:
        if transition.event == HistoryEvent.SKILL_STARTED.value:
            adapter.available = False
        adapter(transition)

    with pytest.raises(RuntimeError, match="simulated Cloud outage"):
        Engine()._run_with_transition_sink(
            _ctx(_transaction(_EffectSkill("effect", 1))),
            transition_sink=fail_before_effect,
        )
    assert effects == 0
    assert [record["event"] for record in reducer.records] == [
        HistoryEvent.TRANSACTION_STARTED.value
    ]


def test_cloud_removal_keeps_ordinary_public_engine_path() -> None:
    tx = _transaction(_SetStateSkill("set-state", 1))
    Engine().run(_ctx(tx))
    assert tx.status is Status.SUCCESSFUL
    assert tx.state == {
        "public": "done",
        "secret": "must-not-leave-worker",
    }
