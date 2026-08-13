"""Closed execution-transition facts emitted by :class:`rpacore.Engine`."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final, Literal

from rpacore._json_state import validate_json_object


EXECUTION_TRANSITION_SCHEMA_VERSION: Final[Literal[2]] = 2


@dataclass(frozen=True, slots=True, init=False)
class ExecutionTransition:
    """Immutable, minimized fact emitted after one lifecycle mutation.

    The checkpoint state is stored as canonical JSON so freezing this object
    also freezes nested values. ``checkpoint_state`` and ``to_record()`` each
    return a fresh mutable view.
    """

    schema_version: Literal[2]
    transition_id: str
    sequence: int
    occurred_at: str
    event: str
    transaction_id: str
    transaction_reference: str
    definition_identity: str
    transaction_status: str
    execution_pass: int
    step_name: str
    step_execution_order: int | None
    step_status: str
    outcome_category: str
    failure_code: str
    retry_recommended: bool | None
    _checkpoint_state_json: str = field(init=False, repr=False)

    def __init__(
        self,
        *,
        schema_version: Literal[2],
        transition_id: str,
        sequence: int,
        occurred_at: str,
        event: str,
        transaction_id: str,
        transaction_reference: str,
        definition_identity: str,
        transaction_status: str,
        execution_pass: int,
        step_name: str,
        step_execution_order: int | None,
        step_status: str,
        outcome_category: str,
        failure_code: str,
        retry_recommended: bool | None,
        checkpoint_state: dict[str, object],
    ) -> None:
        if (
            type(schema_version) is not int
            or schema_version != EXECUTION_TRANSITION_SCHEMA_VERSION
        ):
            raise ValueError(
                "schema_version must be "
                f"{EXECUTION_TRANSITION_SCHEMA_VERSION}, got {schema_version!r}"
            )
        validate_json_object(
            checkpoint_state,
            path="execution_transition.checkpoint_state",
        )
        checkpoint_state_json = json.dumps(
            checkpoint_state,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "transition_id", transition_id)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "transaction_id", transaction_id)
        object.__setattr__(self, "transaction_reference", transaction_reference)
        object.__setattr__(self, "definition_identity", definition_identity)
        object.__setattr__(self, "transaction_status", transaction_status)
        object.__setattr__(self, "execution_pass", execution_pass)
        object.__setattr__(self, "step_name", step_name)
        object.__setattr__(self, "step_execution_order", step_execution_order)
        object.__setattr__(self, "step_status", step_status)
        object.__setattr__(self, "outcome_category", outcome_category)
        object.__setattr__(self, "failure_code", failure_code)
        object.__setattr__(self, "retry_recommended", retry_recommended)
        object.__setattr__(self, "_checkpoint_state_json", checkpoint_state_json)

    @property
    def checkpoint_state(self) -> dict[str, object]:
        """Return a fresh decoded view of the minimized checkpoint state."""
        return json.loads(self._checkpoint_state_json)

    def to_record(self) -> dict[str, object]:
        """Return a fresh JSON-safe version-2 record."""
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
            "step_name": self.step_name,
            "step_execution_order": self.step_execution_order,
            "step_status": self.step_status,
            "outcome_category": self.outcome_category,
            "failure_code": self.failure_code,
            "retry_recommended": self.retry_recommended,
            "checkpoint_state": self.checkpoint_state,
        }
