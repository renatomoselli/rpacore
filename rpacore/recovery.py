"""Recovery helpers for resuming persisted transactions."""

from __future__ import annotations

from copy import copy

from rpacore.exceptions import BusinessException
from rpacore.persistence import load_transaction
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


def resume_transaction(
    tx_id: str,
    skills: list[Skill],
    *,
    db_path: str = "rpacore.db",
) -> Transaction:
    """Load a persisted transaction and reattach executable skills.

    The caller supplies concrete skill instances matched by ``(name,
    execution_order)``. ``SUCCESSFUL`` skills are left intact so calling this on
    an already-successful transaction is effectively a no-op, though callers
    should still check transaction status first to avoid unnecessary work.
    """
    transaction = load_transaction(tx_id, db_path)
    interrupted = _is_interrupted(transaction)
    already_resumed = (
        bool(transaction.history)
        and transaction.history[-1].event is HistoryEvent.TRANSACTION_RESUMED
    )
    preserve_recovery_state = interrupted or already_resumed

    provided: dict[tuple[str, int], Skill] = {}
    for skill in skills:
        key = (skill.name, skill.execution_order)
        if key in provided:
            raise ValueError(
                f"Duplicate recovery skill provided for name={skill.name!r}, "
                f"execution_order={skill.execution_order}"
            )
        provided[key] = skill

    restored: list[Skill] = []
    for loaded_skill in transaction.ordered_skills():
        key = (loaded_skill.name, loaded_skill.execution_order)
        try:
            concrete = provided.pop(key)
        except KeyError as exc:
            raise KeyError(
                f"Missing recovery skill for name={loaded_skill.name!r}, "
                f"execution_order={loaded_skill.execution_order}"
            ) from exc

        concrete.arguments = dict(loaded_skill.arguments)
        concrete.exceptions = [copy(exc) for exc in loaded_skill.exceptions]
        concrete.status = _resumed_skill_status(
            loaded_skill.status,
            concrete.exceptions,
            preserve_recovery_state=preserve_recovery_state,
        )

        restored.append(concrete)

    if provided:
        extra = ", ".join(
            f"name={name!r}, execution_order={order}"
            for name, order in sorted(provided)
        )
        raise KeyError(f"Recovery skills did not match persisted transaction: {extra}")

    transaction.skills = restored
    if transaction.status is not Status.SUCCESSFUL:
        transaction.status = Status.PENDING
        transaction.started_at = None
        transaction.finished_at = None
        if not already_resumed:
            transaction.append_history(HistoryEvent.TRANSACTION_RESUMED)
    return transaction


def _is_interrupted(transaction: Transaction) -> bool:
    """Return True when persisted state shows in-progress work."""
    return (
        transaction.status is Status.IN_PROGRESS
        or any(skill.status is Status.IN_PROGRESS for skill in transaction.skills)
    )


def _resumed_skill_status(
    status: Status,
    exceptions: list[BaseException],
    *,
    preserve_recovery_state: bool,
) -> Status:
    """Return the skill status to use after explicit resume."""
    if status is Status.SUCCESSFUL:
        return Status.SUCCESSFUL
    if preserve_recovery_state and status is Status.SKIPPED:
        return Status.SKIPPED
    if status is Status.FAILED and exceptions:
        if isinstance(exceptions[-1], BusinessException):
            return Status.FAILED
    return Status.PENDING
