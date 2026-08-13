"""Recovery helpers for resuming persisted transactions."""

from __future__ import annotations

from copy import copy
from collections.abc import Sequence

from rpacore._definition import validate_definition_identity
from rpacore.exceptions import BusinessException, DefinitionIdentityError
from rpacore.persistence import load_transaction
from rpacore.step import Step
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


def resume_transaction(
    tx_id: str,
    steps: list[Step],
    *,
    db_path: str = "rpacore.db",
    retry_business_failures: bool = False,
    definition_identity: str | None = None,
) -> Transaction:
    """Load a persisted transaction and reattach executable steps.

    The caller supplies concrete step instances matched by ``(name,
    execution_order)``. A non-successful transaction resumes only when the
    caller's exact definition identity matches the immutable persisted identity.
    ``SUCCESSFUL`` steps are left intact so calling this on an already-successful
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
    halt_propagated_skips = _halt_propagated_skipped_step_keys(transaction)
    preserved_skips = {
        (step.name, step.execution_order)
        for step in transaction.steps
        if preserve_recovery_state and step.status is Status.SKIPPED
    }
    if not preserve_recovery_state:
        preserved_skips.update(halt_propagated_skips)
    if retry_business_failures:
        preserved_skips.difference_update(halt_propagated_skips)

    provided: dict[tuple[str, int], Step] = {}
    for step in steps:
        key = (step.name, step.execution_order)
        if key in provided:
            raise ValueError(
                f"Duplicate recovery step provided for name={step.name!r}, "
                f"execution_order={step.execution_order}"
            )
        provided[key] = step

    restored: list[Step] = []
    for loaded_step in transaction.ordered_steps():
        key = (loaded_step.name, loaded_step.execution_order)
        try:
            concrete = provided.pop(key)
        except KeyError as exc:
            raise KeyError(
                f"Missing recovery step for name={loaded_step.name!r}, "
                f"execution_order={loaded_step.execution_order}"
            ) from exc

        concrete.arguments = dict(loaded_step.arguments)
        concrete.exceptions = [copy(exc) for exc in loaded_step.exceptions]
        concrete.status = _resumed_step_status(
            loaded_step.status,
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
        raise KeyError(f"Recovery steps did not match persisted transaction: {extra}")

    transaction.steps = restored
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
        or any(step.status is Status.IN_PROGRESS for step in transaction.steps)
    )


def _resumed_step_status(
    status: Status,
    exceptions: Sequence[BaseException],
    *,
    preserve_skipped: bool,
    retry_business_failures: bool,
) -> Status:
    """Return the step status to use after explicit resume."""
    if status is Status.SUCCESSFUL:
        return Status.SUCCESSFUL
    if preserve_skipped and status is Status.SKIPPED:
        return Status.SKIPPED
    if status is Status.FAILED and exceptions:
        if isinstance(exceptions[-1], BusinessException):
            return Status.PENDING if retry_business_failures else Status.FAILED
    return Status.PENDING


def _halt_propagated_skipped_step_keys(
    transaction: Transaction,
) -> set[tuple[str, int]]:
    """Return causal skips from engine-emitted ``STEP_SKIPPED`` history."""
    halting_failures = {
        (step.execution_order, step.exceptions[-1].retry_number)
        for step in transaction.steps
        if step.status is Status.FAILED
        and step.exceptions
        and isinstance(step.exceptions[-1], BusinessException)
        and step.exceptions[-1].halts_remaining_steps
    }
    if not halting_failures:
        return set()

    return {
        (entry.step_name, entry.step_execution_order)
        for entry in transaction.history
        if entry.event is HistoryEvent.STEP_SKIPPED
        and entry.step_execution_order is not None
        and any(
            halting_order < entry.step_execution_order
            and halting_retry == entry.retry_number
            for halting_order, halting_retry in halting_failures
        )
    }
