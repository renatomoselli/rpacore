"""Private emitter for closed execution-transition facts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy

from rpacore.outcome import OutcomeCategory
from rpacore.step import Step
from rpacore.transaction import HistoryEntry, HistoryEvent, Transaction
from rpacore.transition import (
    EXECUTION_TRANSITION_SCHEMA_VERSION,
    ExecutionTransition,
)


_TransitionSink = Callable[[ExecutionTransition], None]


class _TransitionEmitter:
    """Append durable history and optionally emit a minimized immutable fact."""

    def __init__(
        self,
        sink: _TransitionSink | None = None,
        *,
        transition_state_fields: Iterable[str] = (),
    ) -> None:
        if sink is not None and not callable(sink):
            raise TypeError("transition_sink must be callable or None")
        if isinstance(transition_state_fields, (str, bytes)):
            raise TypeError("transition_state_fields must be an iterable of strings")
        fields = tuple(transition_state_fields)
        if any(not isinstance(field, str) or not field for field in fields):
            raise ValueError("transition_state_fields must contain non-empty strings")
        if len(fields) != len(set(fields)):
            raise ValueError("transition_state_fields must not contain duplicates")
        if fields and sink is None:
            raise ValueError("transition_state_fields requires transition_sink")
        self._sink = sink
        self._transition_state_fields = fields

    def append(
        self,
        transaction: Transaction,
        event: HistoryEvent,
        *,
        step: Step | None = None,
        include_checkpoint_state: bool = True,
    ) -> HistoryEntry:
        """Append history, then synchronously publish its immutable transition."""
        entry = transaction.append_history(event, step=step)
        if self._sink is None:
            return entry
        self._sink(
            self._build_transition(
                transaction,
                entry,
                step=step,
                include_checkpoint_state=include_checkpoint_state,
            )
        )
        return entry

    def _build_transition(
        self,
        transaction: Transaction,
        entry: HistoryEntry,
        *,
        step: Step | None,
        include_checkpoint_state: bool,
    ) -> ExecutionTransition:
        checkpoint_state: dict[str, object] = {}
        if include_checkpoint_state:
            checkpoint_state = {
                field: deepcopy(transaction.state[field])
                for field in self._transition_state_fields
                if field in transaction.state
            }
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

        return ExecutionTransition(
            schema_version=EXECUTION_TRANSITION_SCHEMA_VERSION,
            transition_id=f"{transaction.id}:{entry.sequence}",
            sequence=entry.sequence,
            occurred_at=entry.timestamp.isoformat(),
            event=entry.event.value,
            transaction_id=transaction.id,
            transaction_reference=transaction.reference,
            definition_identity=transaction.definition_identity,
            transaction_status=transaction.status.value,
            execution_pass=entry.retry_number,
            step_name=entry.step_name,
            step_execution_order=entry.step_execution_order,
            step_status="" if step is None else step.status.value,
            outcome_category=transaction.outcome_category.value,
            failure_code=transaction.failure_code,
            retry_recommended=retry_recommended,
            checkpoint_state=checkpoint_state,
        )
