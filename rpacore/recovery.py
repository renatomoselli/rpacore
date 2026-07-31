"""Recovery helpers for resuming persisted transactions."""

from __future__ import annotations

from copy import copy
from collections.abc import Sequence

from rpacore._definition import validate_definition_identity
from rpacore.exceptions import BusinessException, DefinitionIdentityError
from rpacore.persistence import load_transaction
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


def resume_transaction(
    tx_id: str,
    skills: list[Skill],
    *,
    db_path: str = "rpacore.db",
    retry_business_failures: bool = False,
    definition_identity: str | None = None,
) -> Transaction:
    """Load a persisted transaction and reattach executable skills.

    The caller supplies concrete skill instances matched by ``(name,
    execution_order)``. A non-successful transaction resumes only when the
    caller's exact definition identity matches the immutable persisted identity.
    ``SUCCESSFUL`` skills are left intact so calling this on an already-successful
    transaction is effectively a no-op, though callers should still check
    transaction status first to avoid unnecessary work.
    """
    transaction = load_transaction(tx_id, db_path)
    if transaction.status is Status.SUCCESSFUL:
        return transaction

    expected_identity = validate_definition_identity(
        definition_identity,
        field="definition_identity",
        required=True,
    )
    if not transaction.definition_identity:
        raise DefinitionIdentityError(
            f"Persisted transaction {tx_id!r} has no definition identity and "
            "cannot be resumed by this runtime"
        )
    if transaction.definition_identity != expected_identity:
        raise DefinitionIdentityError(
            f"Persisted transaction {tx_id!r} definition identity "
            f"{transaction.definition_identity!r} does not match "
            f"{expected_identity!r}"
        )
    transaction.validate_for_execution()
    interrupted = _is_interrupted(transaction)
    already_resumed = (
        bool(transaction.history)
        and transaction.history[-1].event is HistoryEvent.TRANSACTION_RESUMED
    )
    preserve_recovery_state = interrupted or already_resumed
    stop_propagated_skips = _stop_propagated_skipped_skill_keys(transaction)
    preserved_skips = {
        (skill.name, skill.execution_order)
        for skill in transaction.skills
        if preserve_recovery_state and skill.status is Status.SKIPPED
    }
    if not preserve_recovery_state:
        preserved_skips.update(stop_propagated_skips)
    if retry_business_failures:
        preserved_skips.difference_update(stop_propagated_skips)

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
            preserve_skipped=key in preserved_skips,
            retry_business_failures=retry_business_failures,
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
        transaction.retry_count = 0
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
    exceptions: Sequence[BaseException],
    *,
    preserve_skipped: bool,
    retry_business_failures: bool,
) -> Status:
    """Return the skill status to use after explicit resume."""
    if status is Status.SUCCESSFUL:
        return Status.SUCCESSFUL
    if preserve_skipped and status is Status.SKIPPED:
        return Status.SKIPPED
    if status is Status.FAILED and exceptions:
        if isinstance(exceptions[-1], BusinessException):
            return Status.PENDING if retry_business_failures else Status.FAILED
    return Status.PENDING


def _stop_propagated_skipped_skill_keys(
    transaction: Transaction,
) -> set[tuple[str, int]]:
    """Return causal skips from engine-emitted ``SKILL_SKIPPED`` history."""
    stopping_failures = {
        (skill.execution_order, skill.exceptions[-1].retry_number)
        for skill in transaction.skills
        if skill.status is Status.FAILED
        and skill.exceptions
        and isinstance(skill.exceptions[-1], BusinessException)
        and skill.exceptions[-1].stops_execution
    }
    if not stopping_failures:
        return set()

    return {
        (entry.skill_name, entry.skill_execution_order)
        for entry in transaction.history
        if entry.event is HistoryEvent.SKILL_SKIPPED
        and entry.skill_execution_order is not None
        and any(
            stopping_order < entry.skill_execution_order
            and stopping_retry == entry.retry_number
            for stopping_order, stopping_retry in stopping_failures
        )
    }
