"""Transaction — a group of skills to execute."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from rpacore.exceptions import ExecutionValidationError
from rpacore.skill import Skill
from rpacore.status import Status


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
    skills: list[Skill] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.retry_count < 0:
            raise ValueError(f"retry_count must be >= 0, got {self.retry_count}")
        self.skills = list(self.skills)

    def ordered_skills(self) -> list[Skill]:
        """Return skills sorted by execution_order."""
        return sorted(self.skills, key=lambda s: s.execution_order)

    def failed_skills(self) -> list[Skill]:
        """Return skills with status FAILED."""
        return [s for s in self.skills if s.status == Status.FAILED]

    def validate_for_execution(self) -> None:
        """Validate transaction wiring before Engine starts skill execution."""
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
