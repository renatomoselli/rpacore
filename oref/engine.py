"""Engine — sequential skill execution with status tracking."""

from oref.exceptions import BusinessException, SystemException
from oref.skill import Skill
from oref.status import Status
from oref.transaction import Transaction


class Engine:
    """Executes a transaction's skills in order, with optional retry.

    Lifecycle:
        Transaction: PENDING → IN_PROGRESS → SUCCESSFUL or FAILED
        Each skill:  PENDING → IN_PROGRESS → SUCCESSFUL or FAILED

    Exception semantics:
        BusinessException → skill FAILED, execution continues
        SystemException   → skill FAILED, execution stops
        Any other exception → wrapped as SystemException, execution stops

    Retry semantics:
        After all skills run, retryable failed skills are reset and re-run.
        Only skills whose last recorded exception is a SystemException are retried.
        BusinessException failures are permanent — retrying won't fix bad data.
        Each retry pass increments transaction.retry_count.
        Retry is immediate, within the same Engine.run() call.
    """

    def __init__(self, max_retries: int = 0) -> None:
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self.max_retries: int = max_retries

    def run(self, transaction: Transaction, context: dict[str, object] | None = None) -> None:
        """Execute all skills in the transaction, retrying retryable failed skills up to max_retries times."""
        ctx: dict[str, object] = context if context is not None else {}
        transaction.status = Status.IN_PROGRESS

        self._execute_pass(transaction, ctx, blocked=None)

        while transaction.retry_count < self.max_retries:
            retryable = self._retryable_failed_skills(transaction)
            if not retryable:
                break
            for skill in retryable:
                skill.status = Status.PENDING
            transaction.retry_count += 1
            # Block only business-failed skills; PENDING skills that never ran should also execute.
            business_failed = {
                id(s) for s in transaction.failed_skills()
                if s.exceptions and isinstance(s.exceptions[-1], BusinessException)
            }
            self._execute_pass(transaction, ctx, blocked=business_failed)

        if all(s.status in (Status.SUCCESSFUL, Status.SKIPPED) for s in transaction.skills):
            transaction.status = Status.SUCCESSFUL
        else:
            transaction.status = Status.FAILED

    def _retryable_failed_skills(self, transaction: Transaction) -> list[Skill]:
        """Return failed skills eligible for retry (last exception is SystemException)."""
        return [
            s for s in transaction.failed_skills()
            if s.exceptions and isinstance(s.exceptions[-1], SystemException)
        ]

    def _execute_pass(
        self,
        transaction: Transaction,
        ctx: dict[str, object],
        blocked: set[int] | None,
    ) -> None:
        """Run one execution pass.

        blocked: set of id(skill) to skip. Use None to run all non-complete skills.
        Business-failed skills are passed as blocked on retry passes so they are
        not re-executed, while still-PENDING skills that were stopped by a prior
        SystemException are allowed to run.
        """
        for skill in transaction.ordered_skills():
            if skill.status in (Status.SUCCESSFUL, Status.SKIPPED):
                continue
            if blocked is not None and id(skill) in blocked:
                continue

            skill.status = Status.IN_PROGRESS
            try:
                skill.execute(ctx)
                skill.status = Status.SUCCESSFUL
            except BusinessException as exc:
                exc.retry_number = transaction.retry_count
                skill.status = Status.FAILED
                skill.exceptions.append(exc)
            except SystemException as exc:
                exc.retry_number = transaction.retry_count
                skill.status = Status.FAILED
                skill.exceptions.append(exc)
                break
            except Exception as exc:
                wrapped = SystemException(
                    str(exc),
                    action=skill.name,
                    retry_number=transaction.retry_count,
                )
                skill.status = Status.FAILED
                skill.exceptions.append(wrapped)
                break

