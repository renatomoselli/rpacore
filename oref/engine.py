"""Engine — sequential skill execution with status tracking."""

from oref.exceptions import BusinessException, SystemException
from oref.status import Status
from oref.transaction import Transaction


class Engine:
    """Executes a transaction's skills in order.

    Lifecycle:
        Transaction: PENDING → IN_PROGRESS → SUCCESSFUL or FAILED
        Each skill:  PENDING → IN_PROGRESS → SUCCESSFUL or FAILED

    Exception semantics:
        BusinessException → skill FAILED, execution continues
        SystemException   → skill FAILED, execution stops
    """

    def run(self, transaction: Transaction, context: dict[str, object] | None = None) -> None:
        """Execute all skills in the transaction sequentially."""
        ctx: dict[str, object] = context if context is not None else {}
        transaction.status = Status.IN_PROGRESS

        for skill in transaction.ordered_skills():
            if skill.status in (Status.SUCCESSFUL, Status.SKIPPED):
                continue

            skill.status = Status.IN_PROGRESS
            try:
                skill.execute(ctx)
                skill.status = Status.SUCCESSFUL
            except BusinessException as exc:
                skill.status = Status.FAILED
                skill.exceptions.append(exc)
            except SystemException as exc:
                skill.status = Status.FAILED
                skill.exceptions.append(exc)
                break
            except Exception as exc:
                wrapped = SystemException(
                    str(exc),
                    action=skill.name,
                )
                skill.status = Status.FAILED
                skill.exceptions.append(wrapped)
                break

        if transaction.failed_skills():
            transaction.status = Status.FAILED
        else:
            transaction.status = Status.SUCCESSFUL
