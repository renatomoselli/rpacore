"""Skill — the unit of work in rpacore."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rpacore.exceptions import BusinessException, SystemException
from rpacore.status import Status

if TYPE_CHECKING:
    from rpacore.context import ProcessContext


class Skill:
    """A single unit of work within a transaction.

    Users subclass Skill and implement execute(ctx: ProcessContext).
    """

    def __init__(
        self,
        name: str,
        execution_order: int,
        *,
        arguments: dict[str, object] | None = None,
    ) -> None:
        self.name: str = name
        self.execution_order: int = execution_order
        self.status: Status = Status.PENDING
        self.arguments: dict[str, object] = dict(arguments) if arguments is not None else {}
        self.exceptions: list[BusinessException | SystemException] = []

    def execute(self, ctx: ProcessContext) -> None:
        """Execute this skill's logic.

        Subclasses must override this method.
        """
        raise NotImplementedError(
            f"Skill '{self.name}' must implement execute()"
        )
