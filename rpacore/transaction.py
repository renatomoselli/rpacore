"""Transaction — a group of skills to execute."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from rpacore._definition import validate_definition_identity
from rpacore._json_state import validate_json_object
from rpacore.exceptions import ExecutionValidationError
from rpacore.outcome import OutcomeCategory, RetryDisposition, validate_failure_code
from rpacore.skill import Skill
from rpacore.status import Status


class HistoryEvent(StrEnum):
    """Closed v0.1.0 vocabulary for durable transaction history."""

    TRANSACTION_STARTED = "transaction_started"
    SKILL_STARTED = "skill_started"
    SKILL_SUCCEEDED = "skill_succeeded"
    SKILL_FAILED = "skill_failed"
    SKILL_SKIPPED = "skill_skipped"
    SKILL_INTERRUPTED = "skill_interrupted"
    RETRY_SCHEDULED = "retry_scheduled"
    TRANSACTION_RESUMED = "transaction_resumed"
    TRANSACTION_COMPLETED = "transaction_completed"


@dataclass(frozen=True)
class HistoryEntry:
    """An immutable durable audit entry for a transaction state transition."""

    sequence: int
    timestamp: datetime
    event: HistoryEvent
    status: Status
    retry_number: int
    skill_name: str = ""
    skill_execution_order: int | None = None


@dataclass
class Artifact:
    """A durable audit record for a generated or captured file path."""

    name: str
    path: str
    kind: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.metadata = dict(self.metadata or {})


@dataclass
class Transaction:
    """A transaction groups skills into a single executable unit.

    Skills are stored in insertion order. Use ordered_skills() to get
    them sorted by execution_order, and failed_skills() to get only
    the ones that failed.
    """

    reference: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: Status = Status.PENDING
    retry_count: int = 0
    created_at: datetime | None = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    state: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    outcome_category: OutcomeCategory = OutcomeCategory.UNKNOWN
    retry_disposition: RetryDisposition = RetryDisposition.UNKNOWN
    failure_code: str = ""
    definition_identity: str = ""

    def __post_init__(self) -> None:
        if self.retry_count < 0:
            raise ValueError(f"retry_count must be >= 0, got {self.retry_count}")
        if not isinstance(self.outcome_category, OutcomeCategory):
            raise TypeError("outcome_category must be an OutcomeCategory")
        if not isinstance(self.retry_disposition, RetryDisposition):
            raise TypeError("retry_disposition must be a RetryDisposition")
        self.failure_code = validate_failure_code(self.failure_code)
        self.definition_identity = validate_definition_identity(
            self.definition_identity,
            field="transaction.definition_identity",
        )
        self.state = dict(self.state)
        self.metadata = dict(self.metadata)
        self.artifacts = list(self.artifacts)
        self.skills = list(self.skills)
        self.history = list(self.history)

    def ordered_skills(self) -> list[Skill]:
        """Return skills sorted by execution_order."""
        return sorted(self.skills, key=lambda s: s.execution_order)

    def failed_skills(self) -> list[Skill]:
        """Return skills with status FAILED."""
        return [s for s in self.skills if s.status == Status.FAILED]

    def append_history(
        self,
        event: HistoryEvent,
        *,
        status: Status | None = None,
        retry_number: int | None = None,
        skill: Skill | None = None,
        timestamp: datetime | None = None,
    ) -> HistoryEntry:
        """Append and return a transaction-local durable history entry."""
        entry = HistoryEntry(
            sequence=len(self.history) + 1,
            timestamp=timestamp or datetime.now(timezone.utc),
            event=event,
            status=status if status is not None else self.status,
            retry_number=retry_number if retry_number is not None else self.retry_count,
            skill_name="" if skill is None else skill.name,
            skill_execution_order=None if skill is None else skill.execution_order,
        )
        self.history.append(entry)
        return entry

    def validate_for_execution(self) -> None:
        """Validate transaction wiring and durable data before execution."""
        if not isinstance(self.reference, str):
            raise ExecutionValidationError(
                f"transaction.reference must be a non-empty str, got {self.reference!r}"
            )
        if not self.reference.strip():
            raise ExecutionValidationError("transaction.reference must be a non-empty str")

        names: set[str] = set()
        orders: set[int] = set()
        for skill in self.skills:
            if not isinstance(skill.name, str):
                raise ExecutionValidationError(
                    f"skill.name must be a non-empty str, got {skill.name!r}"
                )
            if not skill.name.strip():
                raise ExecutionValidationError("skill.name must be a non-empty str")
            if skill.name in names:
                raise ExecutionValidationError(
                    f"skill.name must be unique within a transaction: {skill.name!r}"
                )
            names.add(skill.name)

            order = skill.execution_order
            if isinstance(order, bool) or not isinstance(order, int):
                raise ExecutionValidationError(
                    f"skill.execution_order must be a positive int, got {order!r}"
                )
            if order <= 0:
                raise ExecutionValidationError(
                    f"skill.execution_order must be a positive int, got {order!r}"
                )
            if order in orders:
                raise ExecutionValidationError(
                    "skill.execution_order must be unique within a transaction: "
                    f"{order!r}"
                )
            orders.add(order)

        self.validate_durable_data()

    def validate_durable_data(self) -> None:
        """Validate every JSON-backed value owned by the transaction."""
        if not isinstance(self.outcome_category, OutcomeCategory):
            raise TypeError("transaction.outcome_category must be an OutcomeCategory")
        if not isinstance(self.retry_disposition, RetryDisposition):
            raise TypeError("transaction.retry_disposition must be a RetryDisposition")
        self.failure_code = validate_failure_code(self.failure_code)
        self.definition_identity = validate_definition_identity(
            self.definition_identity,
            field="transaction.definition_identity",
        )
        validate_json_object(self.state, path="transaction.state")
        validate_json_object(self.metadata, path="transaction.metadata")
        for index, artifact in enumerate(self.artifacts):
            validate_json_object(
                artifact.metadata,
                path=f"transaction.artifacts[{index}].metadata",
            )
        for skill in self.skills:
            validate_json_object(
                skill.arguments,
                path=f"transaction.skills[{skill.name!r}].arguments",
            )
