"""Private execution-transition seam used to prove shared-kernel composition."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Iterable

from rpacore._json_state import validate_json_object
from rpacore.outcome import OutcomeCategory
from rpacore.skill import Skill
from rpacore.transaction import HistoryEntry, HistoryEvent, Transaction


_TRANSITION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _ExecutionTransition:
    """Immutable, minimized fact emitted after one lifecycle mutation."""

    schema_version: int
    transition_id: str
    sequence: int
    occurred_at: str
    event: str
    transaction_id: str
    transaction_reference: str
    definition_identity: str
    transaction_status: str
    execution_pass: int
    skill_name: str
    skill_execution_order: int | None
    skill_status: str
    outcome_category: str
    failure_code: str
    retry_recommended: bool | None
    checkpoint_state_json: str

    def to_record(self) -> dict[str, object]:
        """Return a fresh JSON-safe record without coordinator disposition."""
        return {
            "schema_version": self.schema_version,
            "transition_id": self.transition_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "event": self.event,
            "transaction_id": self.transaction_id,
            "transaction_reference": self.transaction_reference,
            "definition_identity": self.definition_identity,
            "transaction_status": self.transaction_status,
            "execution_pass": self.execution_pass,
            "skill_name": self.skill_name,
            "skill_execution_order": self.skill_execution_order,
            "skill_status": self.skill_status,
            "outcome_category": self.outcome_category,
            "failure_code": self.failure_code,
            "retry_recommended": self.retry_recommended,
            "checkpoint_state": json.loads(self.checkpoint_state_json),
        }


_TransitionSink = Callable[[_ExecutionTransition], None]


class _TransitionEmitter:
    """Append durable history and optionally emit a minimized immutable fact."""

    def __init__(
        self,
        sink: _TransitionSink | None = None,
        *,
        checkpoint_state_fields: Iterable[str] = (),
    ) -> None:
        fields = tuple(checkpoint_state_fields)
        if any(not isinstance(field, str) or not field for field in fields):
            raise ValueError("checkpoint_state_fields must contain non-empty strings")
        if len(fields) != len(set(fields)):
            raise ValueError("checkpoint_state_fields must not contain duplicates")
        self._sink = sink
        self._checkpoint_state_fields = fields

    def append(
        self,
        transaction: Transaction,
        event: HistoryEvent,
        *,
        skill: Skill | None = None,
        include_checkpoint_state: bool = True,
    ) -> HistoryEntry:
        """Append history, then synchronously publish its immutable transition."""
        entry = transaction.append_history(event, skill=skill)
        if self._sink is None:
            return entry
        self._sink(
            self._build_transition(
                transaction,
                entry,
                skill=skill,
                include_checkpoint_state=include_checkpoint_state,
            )
        )
        return entry

    def _build_transition(
        self,
        transaction: Transaction,
        entry: HistoryEntry,
        *,
        skill: Skill | None,
        include_checkpoint_state: bool,
    ) -> _ExecutionTransition:
        checkpoint_state: dict[str, object] = {}
        if include_checkpoint_state:
            checkpoint_state = {
                field: deepcopy(transaction.state[field])
                for field in self._checkpoint_state_fields
                if field in transaction.state
            }
            validate_json_object(
                checkpoint_state,
                path="kernel_transition.checkpoint_state",
            )
        checkpoint_state_json = json.dumps(
            checkpoint_state,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        retry_recommended: bool | None = None
        if entry.event is HistoryEvent.TRANSACTION_COMPLETED:
            if transaction.outcome_category is OutcomeCategory.SYSTEM_FAILED:
                retry_recommended = True
            elif transaction.outcome_category in {
                OutcomeCategory.SUCCESSFUL,
                OutcomeCategory.BUSINESS_FAILED,
                OutcomeCategory.VALIDATION_FAILED,
            }:
                retry_recommended = False

        return _ExecutionTransition(
            schema_version=_TRANSITION_SCHEMA_VERSION,
            transition_id=f"{transaction.id}:{entry.sequence}",
            sequence=entry.sequence,
            occurred_at=entry.timestamp.isoformat(),
            event=entry.event.value,
            transaction_id=transaction.id,
            transaction_reference=transaction.reference,
            definition_identity=transaction.definition_identity,
            transaction_status=transaction.status.value,
            execution_pass=entry.retry_number,
            skill_name=entry.skill_name,
            skill_execution_order=entry.skill_execution_order,
            skill_status="" if skill is None else skill.status.value,
            outcome_category=transaction.outcome_category.value,
            failure_code=transaction.failure_code,
            retry_recommended=retry_recommended,
            checkpoint_state_json=checkpoint_state_json,
        )
