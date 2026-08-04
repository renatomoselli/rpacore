"""P9-115 proof for one public transition seam and two adapters."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from rpacore import ExecutionTransition
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
_EVENT_VALUES = {event.value for event in HistoryEvent}
_STATUS_VALUES = {status.value for status in Status}
_OUTCOME_VALUES = {outcome.value for outcome in OutcomeCategory}
_SKILL_EVENTS = {
    HistoryEvent.SKILL_STARTED,
    HistoryEvent.SKILL_SUCCEEDED,
    HistoryEvent.SKILL_FAILED,
    HistoryEvent.SKILL_SKIPPED,
    HistoryEvent.SKILL_INTERRUPTED,
}
_EXPECTED_SKILL_STATUS = {
    HistoryEvent.SKILL_STARTED: Status.IN_PROGRESS,
    HistoryEvent.SKILL_SUCCEEDED: Status.SUCCESSFUL,
    HistoryEvent.SKILL_FAILED: Status.FAILED,
    HistoryEvent.SKILL_SKIPPED: Status.SKIPPED,
    HistoryEvent.SKILL_INTERRUPTED: Status.FAILED,
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
        self._transaction_id: str | None = None
        self._definition_identity: str | None = None
        self._transaction_started = False
        self._transaction_completed = False
        self._active_skill: tuple[str, int] | None = None
        self._execution_pass = 0

    def rotate_fence(self, fence: str) -> None:
        self.fence = fence

    def accept(
        self,
        transition: ExecutionTransition,
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
        try:
            payload_hash = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise _ProtocolRejected("transition record is not JSON-safe") from exc
        previous = self._idempotency.get(transition_id)
        if previous is not None:
            previous_hash, previous_revision = previous
            if previous_hash != payload_hash:
                raise _ProtocolRejected("idempotency payload conflict")
            return previous_revision

        self._validate_vocabulary(record)
        if expected_revision != self.revision:
            raise _ProtocolRejected("server revision conflict")
        sequence = record["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise _ProtocolRejected("invalid transition sequence")
        if sequence != self.last_sequence + 1:
            raise _ProtocolRejected("transition sequence gap or reorder")
        next_state = self._next_protocol_state(record)

        self.revision += 1
        self.last_sequence = sequence
        (
            self._transaction_id,
            self._definition_identity,
            self._transaction_started,
            self._transaction_completed,
            self._active_skill,
            self._execution_pass,
        ) = next_state
        accepted = dict(record)
        self.records.append(accepted)
        self._idempotency[transition_id] = (payload_hash, self.revision)
        return self.revision

    def _validate_vocabulary(self, record: dict[str, object]) -> None:
        transition_id = record["transition_id"]
        if not isinstance(transition_id, str) or not transition_id:
            raise _ProtocolRejected("invalid transition id")
        transaction_id = record["transaction_id"]
        if not isinstance(transaction_id, str) or not transaction_id:
            raise _ProtocolRejected("invalid transaction id")
        definition_identity = record["definition_identity"]
        if not isinstance(definition_identity, str) or not definition_identity:
            raise _ProtocolRejected("invalid definition identity")
        if record["event"] not in _EVENT_VALUES:
            raise _ProtocolRejected("unknown transition event")
        if record["event"] == HistoryEvent.TRANSACTION_RESUMED.value:
            raise _ProtocolRejected("resume transition is outside this proof protocol")
        if record["transaction_status"] not in _STATUS_VALUES:
            raise _ProtocolRejected("invalid transaction status")
        if record["outcome_category"] not in _OUTCOME_VALUES:
            raise _ProtocolRejected("invalid outcome category")
        execution_pass = record["execution_pass"]
        if (
            not isinstance(execution_pass, int)
            or isinstance(execution_pass, bool)
            or execution_pass < 0
        ):
            raise _ProtocolRejected("invalid execution pass")
        retry_recommended = record["retry_recommended"]
        if retry_recommended is not None and not isinstance(retry_recommended, bool):
            raise _ProtocolRejected("invalid retry recommendation")
        if not isinstance(record["failure_code"], str):
            raise _ProtocolRejected("invalid failure code")
        if not isinstance(record["checkpoint_state"], dict):
            raise _ProtocolRejected("invalid checkpoint state")

        event = HistoryEvent(record["event"])
        skill_name = record["skill_name"]
        skill_order = record["skill_execution_order"]
        skill_status = record["skill_status"]
        if event in _SKILL_EVENTS:
            if not isinstance(skill_name, str) or not skill_name:
                raise _ProtocolRejected("skill transition requires a skill name")
            if (
                not isinstance(skill_order, int)
                or isinstance(skill_order, bool)
                or skill_order <= 0
            ):
                raise _ProtocolRejected("skill transition requires a positive skill order")
            expected_status = _EXPECTED_SKILL_STATUS[event].value
            if skill_status != expected_status:
                raise _ProtocolRejected("skill transition has an invalid skill status")
        elif skill_name != "" or skill_order is not None or skill_status != "":
            raise _ProtocolRejected("transaction transition must not identify a skill")

        transaction_status = record["transaction_status"]
        outcome = record["outcome_category"]
        if event is HistoryEvent.TRANSACTION_COMPLETED:
            if transaction_status not in {Status.SUCCESSFUL.value, Status.FAILED.value}:
                raise _ProtocolRejected("completed transition has a nonterminal status")
            if outcome == OutcomeCategory.UNKNOWN.value:
                raise _ProtocolRejected("completed transition has an unknown outcome")
        else:
            if transaction_status != Status.IN_PROGRESS.value:
                raise _ProtocolRejected("nonterminal transition has an invalid status")
            if outcome != OutcomeCategory.UNKNOWN.value:
                raise _ProtocolRejected("nonterminal transition has a terminal outcome")
            if retry_recommended is not None:
                raise _ProtocolRejected("nonterminal transition recommends retry")

    def _next_protocol_state(
        self,
        record: dict[str, object],
    ) -> tuple[str, str, bool, bool, tuple[str, int] | None, int]:
        transaction_id = record["transaction_id"]
        definition_identity = record["definition_identity"]
        execution_pass = record["execution_pass"]
        assert isinstance(transaction_id, str)
        assert isinstance(definition_identity, str)
        assert isinstance(execution_pass, int)
        if self._transaction_id is not None and transaction_id != self._transaction_id:
            raise _ProtocolRejected("transition changed transaction identity")
        if (
            self._definition_identity is not None
            and definition_identity != self._definition_identity
        ):
            raise _ProtocolRejected("transition changed definition identity")
        if self._transaction_completed:
            raise _ProtocolRejected("transition follows terminal completion")

        event = HistoryEvent(record["event"])
        started = self._transaction_started
        completed = self._transaction_completed
        active_skill = self._active_skill
        current_pass = self._execution_pass

        if event is HistoryEvent.TRANSACTION_STARTED:
            if started:
                raise _ProtocolRejected("transaction started more than once")
            if execution_pass != 0:
                raise _ProtocolRejected("transaction started on a nonzero pass")
            started = True
        elif event is HistoryEvent.TRANSACTION_COMPLETED:
            if not started:
                if record["outcome_category"] != OutcomeCategory.VALIDATION_FAILED.value:
                    raise _ProtocolRejected("transaction completed before it started")
                if execution_pass != 0:
                    raise _ProtocolRejected("validation failure used a nonzero pass")
            elif active_skill is not None:
                raise _ProtocolRejected("transaction completed with an active skill")
            elif execution_pass != current_pass:
                raise _ProtocolRejected("completion used the wrong execution pass")
            completed = True
        else:
            if not started:
                raise _ProtocolRejected("transition occurred before transaction start")
            if execution_pass != current_pass:
                if event is not HistoryEvent.RETRY_SCHEDULED:
                    raise _ProtocolRejected("transition used the wrong execution pass")

            if event is HistoryEvent.SKILL_STARTED:
                if active_skill is not None:
                    raise _ProtocolRejected("skill started while another skill was active")
                active_skill = self._record_skill_identity(record)
            elif event in {
                HistoryEvent.SKILL_SUCCEEDED,
                HistoryEvent.SKILL_FAILED,
                HistoryEvent.SKILL_INTERRUPTED,
            }:
                if active_skill != self._record_skill_identity(record):
                    raise _ProtocolRejected("skill terminal event has no matching start")
                active_skill = None
            elif event is HistoryEvent.SKILL_SKIPPED:
                skipped_skill = self._record_skill_identity(record)
                if active_skill is not None and active_skill != skipped_skill:
                    raise _ProtocolRejected("skipped event does not match the active skill")
                active_skill = None
            elif event is HistoryEvent.RETRY_SCHEDULED:
                if active_skill is not None:
                    raise _ProtocolRejected("retry scheduled with an active skill")
                if execution_pass != current_pass + 1:
                    raise _ProtocolRejected("retry did not advance the execution pass")
                current_pass = execution_pass
            else:
                raise _ProtocolRejected("unsupported transition event")

        return (
            transaction_id,
            definition_identity,
            started,
            completed,
            active_skill,
            current_pass,
        )

    @staticmethod
    def _record_skill_identity(record: dict[str, object]) -> tuple[str, int]:
        skill_name = record["skill_name"]
        skill_order = record["skill_execution_order"]
        assert isinstance(skill_name, str)
        assert isinstance(skill_order, int)
        return skill_name, skill_order


class _ConnectedTestAdapter:
    def __init__(self, reducer: _ServerAuthorityReducer, *, fence: str = "fence-1") -> None:
        self.reducer = reducer
        self.fence = fence
        self.available = True

    def __call__(self, transition: ExecutionTransition) -> None:
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


def _normalized(transitions: list[ExecutionTransition]) -> list[dict[str, object]]:
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
    local_transitions: list[ExecutionTransition] = []
    connected_transitions: list[ExecutionTransition] = []
    local_db = tmp_path / "local.db"

    Engine().run(
        _ctx(local_tx),
        transition_sink=local_transitions.append,
        checkpoint=lambda tx: save_transaction(tx, str(local_db)),
        transition_state_fields=("public",),
    )
    Engine().run(
        _ctx(connected_tx),
        transition_sink=connected_transitions.append,
        transition_state_fields=("public",),
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
    transitions: list[ExecutionTransition] = []
    Engine().run(
        _ctx(_transaction(_SetStateSkill("set-state", 1))),
        transition_sink=transitions.append,
    )
    first, second = transitions[:2]
    reducer = _ServerAuthorityReducer()

    assert reducer.accept(first, fence="fence-1", expected_revision=0) == 1
    assert reducer.accept(first, fence="fence-1", expected_revision=0) == 1
    assert reducer.revision == 1

    conflicting = first.to_record()
    conflicting["event"] = HistoryEvent.SKILL_FAILED.value
    with pytest.raises(_ProtocolRejected, match="idempotency payload conflict"):
        reducer.accept_record(conflicting, fence="fence-1", expected_revision=1)
    with pytest.raises(_ProtocolRejected, match="server revision conflict"):
        reducer.accept(second, fence="fence-1", expected_revision=0)

    reordered = second.to_record()
    reordered["sequence"] = second.sequence + 1
    reordered["transition_id"] = f"{second.transaction_id}:{second.sequence + 1}"
    with pytest.raises(_ProtocolRejected, match="sequence gap or reorder"):
        reducer.accept_record(reordered, fence="fence-1", expected_revision=1)

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

    transitions: list[ExecutionTransition] = []
    Engine().run(
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


def test_reducer_rejects_illegal_transition_grammar_and_vocabulary() -> None:
    transitions: list[ExecutionTransition] = []
    Engine().run(
        _ctx(_transaction(_SetStateSkill("set-state", 1))),
        transition_sink=transitions.append,
    )
    started, _, succeeded, completed = transitions

    illegal_records = []
    completed_first = completed.to_record()
    completed_first["sequence"] = 1
    completed_first["transition_id"] = (
        f"{completed.transaction_id}:illegal-completed-first"
    )
    illegal_records.append((completed_first, "completed before it started"))
    succeeded_without_start = succeeded.to_record()
    succeeded_without_start["sequence"] = 1
    succeeded_without_start["transition_id"] = (
        f"{succeeded.transaction_id}:illegal-succeeded-first"
    )
    illegal_records.append((succeeded_without_start, "before transaction start"))
    bogus_status = started.to_record()
    bogus_status["transaction_status"] = "bogus"
    illegal_records.append((bogus_status, "invalid transaction status"))
    unknown_event = started.to_record()
    unknown_event["event"] = "not_a_kernel_event"
    illegal_records.append((unknown_event, "unknown transition event"))
    unknown_outcome = started.to_record()
    unknown_outcome["outcome_category"] = "not_an_outcome"
    illegal_records.append((unknown_outcome, "invalid outcome category"))
    non_bool_retry = started.to_record()
    non_bool_retry["retry_recommended"] = "maybe"
    illegal_records.append((non_bool_retry, "invalid retry recommendation"))

    for record, message in illegal_records:
        reducer = _ServerAuthorityReducer()
        with pytest.raises(_ProtocolRejected, match=message):
            reducer.accept_record(record, fence="fence-1", expected_revision=0)
        assert reducer.revision == 0

    validation_transitions: list[ExecutionTransition] = []
    invalid_tx = _transaction(Skill("same", 1), Skill("same", 2))
    with pytest.raises(ExecutionValidationError):
        Engine().run(
            _ctx(invalid_tx),
            transition_sink=validation_transitions.append,
        )
    validation_reducer = _ServerAuthorityReducer()
    assert validation_reducer.accept(
        validation_transitions[0],
        fence="fence-1",
        expected_revision=0,
    ) == 1


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
    assert set(record) == _RECORD_FIELDS
    expected_state = {
        "nested": {"items": ["original"]},
        "public": "done",
    }
    assert record["checkpoint_state"] == expected_state
    assert "absent" not in record["checkpoint_state"]
    serialized = json.dumps(record, sort_keys=True)
    assert "must-not-leave-worker" not in serialized
    assert "private_metadata" not in serialized
    assert "private_config" not in serialized
    assert "private_handle" not in serialized
    assert "private failure text" not in serialized
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


def test_connected_system_failure_reports_recommendation_not_coordinator_truth() -> None:
    transitions: list[ExecutionTransition] = []
    tx = _transaction(_SystemFailureSkill("fail", 1))
    Engine(max_retries=0).run(
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
    business: list[ExecutionTransition] = []
    business_tx = _transaction(
        Skill("unused", 1),
        Skill("downstream", 2),
    )
    business_tx.skills[0].execute = lambda ctx: (_ for _ in ()).throw(  # type: ignore[method-assign]
        BusinessException("business", action="unused", stop=True)
    )
    Engine().run(
        _ctx(business_tx),
        transition_sink=business.append,
    )
    assert business[-1].retry_recommended is False
    skipped = [item for item in business if item.event == HistoryEvent.SKILL_SKIPPED]
    assert len(skipped) == 1
    assert skipped[0].skill_name == "downstream"
    assert skipped[0].skill_status == Status.SKIPPED.value
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
        Engine().run(
            _ctx(interrupted_tx),
            transition_sink=interrupted.append,
        )
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


def test_strict_adapter_outage_stops_before_unrecorded_effect() -> None:
    effects = 0
    checkpoints: list[str] = []

    class _EffectSkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal effects
            effects += 1

    reducer = _ServerAuthorityReducer()
    adapter = _ConnectedTestAdapter(reducer)

    def fail_before_effect(transition: ExecutionTransition) -> None:
        if transition.event == HistoryEvent.SKILL_STARTED.value:
            adapter.available = False
        adapter(transition)

    with pytest.raises(RuntimeError, match="simulated Cloud outage"):
        Engine().run(
            _ctx(_transaction(_EffectSkill("effect", 1))),
            transition_sink=fail_before_effect,
            checkpoint=lambda transaction: checkpoints.append(
                transaction.history[-1].event.value
            ),
        )
    assert effects == 0
    assert [record["event"] for record in reducer.records] == [
        HistoryEvent.TRANSACTION_STARTED.value
    ]
    assert checkpoints == [HistoryEvent.TRANSACTION_STARTED.value]


def test_after_effect_sink_outage_propagates_without_reexecution() -> None:
    effects = 0

    class _EffectSkill(Skill):
        def execute(self, ctx: ProcessContext) -> None:
            nonlocal effects
            effects += 1

    tx = _transaction(_EffectSkill("effect", 1))
    reducer = _ServerAuthorityReducer()
    adapter = _ConnectedTestAdapter(reducer)

    def fail_after_effect(transition: ExecutionTransition) -> None:
        if transition.event == HistoryEvent.SKILL_SUCCEEDED:
            raise RuntimeError("simulated after-effect Cloud outage")
        adapter(transition)

    with pytest.raises(RuntimeError, match="after-effect Cloud outage"):
        Engine().run(
            _ctx(tx),
            transition_sink=fail_after_effect,
        )

    assert effects == 1
    assert tx.status is Status.IN_PROGRESS
    assert tx.skills[0].status is Status.SUCCESSFUL
    assert tx.history[-1].event is HistoryEvent.SKILL_SUCCEEDED
    assert [record["event"] for record in reducer.records] == [
        HistoryEvent.TRANSACTION_STARTED.value,
        HistoryEvent.SKILL_STARTED.value,
    ]


def test_cloud_removal_keeps_ordinary_public_engine_path() -> None:
    tx = _transaction(_SetStateSkill("set-state", 1))
    Engine().run(_ctx(tx))
    assert tx.status is Status.SUCCESSFUL
    assert tx.state == {
        "public": "done",
        "secret": "must-not-leave-worker",
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
    assert [entry.event for entry in tx.history] == [
        HistoryEvent.TRANSACTION_COMPLETED
    ]
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
