"""Engine — sequential skill execution with status tracking."""

from __future__ import annotations

import logging
from typing import Literal

from rpacore.context import ProcessContext
from rpacore.exceptions import BusinessException, SystemException
from rpacore.logger import get_logger
from rpacore.screenshot import capture_screenshot
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction


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

    def __init__(
        self,
        max_retries: int = 0,
        *,
        logger: logging.Logger | None = None,
        screenshot_dir: str = "",
    ) -> None:
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        self.max_retries: int = max_retries
        self.logger: logging.Logger = logger if logger is not None else get_logger()
        self.screenshot_dir: str = screenshot_dir

    def run(self, ctx: ProcessContext) -> None:
        """Execute all skills in the transaction, retrying retryable failed skills up to max_retries times."""
        transaction = ctx.transaction
        transaction.status = Status.IN_PROGRESS
        self._log_transaction_started(transaction)

        self._execute_pass(ctx, blocked=None)

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
            self._execute_pass(ctx, blocked=business_failed)

        if all(s.status in (Status.SUCCESSFUL, Status.SKIPPED) for s in transaction.skills):
            transaction.status = Status.SUCCESSFUL
        else:
            transaction.status = Status.FAILED
        self._log_transaction_completed(transaction)

    def _retryable_failed_skills(self, transaction: Transaction) -> list[Skill]:
        """Return failed skills eligible for retry (last exception is SystemException)."""
        return [
            s for s in transaction.failed_skills()
            if s.exceptions and isinstance(s.exceptions[-1], SystemException)
        ]

    def _execute_pass(
        self,
        ctx: ProcessContext,
        blocked: set[int] | None,
    ) -> None:
        """Run one execution pass.

        blocked: set of id(skill) to skip. Use None to run all non-complete skills.
        Business-failed skills are passed as blocked on retry passes so they are
        not re-executed, while still-PENDING skills that were stopped by a prior
        SystemException are allowed to run.
        """
        transaction = ctx.transaction
        for skill in transaction.ordered_skills():
            if skill.status in (Status.SUCCESSFUL, Status.SKIPPED):
                continue
            if blocked is not None and id(skill) in blocked:
                continue

            skill.status = Status.IN_PROGRESS
            self._log_skill_started(transaction, skill)
            try:
                skill.execute(ctx)
                if skill.status is not Status.SKIPPED:
                    skill.status = Status.SUCCESSFUL
                self._log_skill_completed(transaction, skill)
            except BusinessException as exc:
                exc.retry_number = transaction.retry_count
                if self.screenshot_dir:
                    exc.screenshot_path = capture_screenshot(self.screenshot_dir)
                skill.status = Status.FAILED
                skill.exceptions.append(exc)
                self._log_skill_failed(transaction, skill, exc, level="warning")
            except SystemException as exc:
                exc.retry_number = transaction.retry_count
                if self.screenshot_dir:
                    exc.screenshot_path = capture_screenshot(self.screenshot_dir)
                skill.status = Status.FAILED
                skill.exceptions.append(exc)
                self._log_skill_failed(transaction, skill, exc, level="error")
                break
            except Exception as exc:
                wrapped = SystemException(
                    str(exc),
                    action=skill.name,
                    retry_number=transaction.retry_count,
                )
                if self.screenshot_dir:
                    wrapped.screenshot_path = capture_screenshot(self.screenshot_dir)
                skill.status = Status.FAILED
                skill.exceptions.append(wrapped)
                self._log_skill_failed(transaction, skill, wrapped, level="error")
                break

    def _log_transaction_started(self, transaction: Transaction) -> None:
        self.logger.info(
            "Transaction started",
            extra={
                "event": "transaction_started",
                "transaction_id": transaction.id,
                "transaction_reference": transaction.reference,
                "transaction_status": transaction.status,
                "retry_count": transaction.retry_count,
            },
        )

    def _log_transaction_completed(self, transaction: Transaction) -> None:
        self.logger.info(
            "Transaction completed",
            extra={
                "event": "transaction_completed",
                "transaction_id": transaction.id,
                "transaction_reference": transaction.reference,
                "transaction_status": transaction.status,
                "retry_count": transaction.retry_count,
            },
        )

    def _log_skill_started(self, transaction: Transaction, skill: Skill) -> None:
        self.logger.info(
            "Skill started",
            extra={
                "event": "skill_started",
                "transaction_id": transaction.id,
                "skill_name": skill.name,
                "skill_execution_order": skill.execution_order,
                "skill_status": skill.status,
                "retry_count": transaction.retry_count,
            },
        )

    def _log_skill_completed(self, transaction: Transaction, skill: Skill) -> None:
        self.logger.info(
            "Skill completed",
            extra={
                "event": "skill_completed",
                "transaction_id": transaction.id,
                "skill_name": skill.name,
                "skill_execution_order": skill.execution_order,
                "skill_status": skill.status,
                "retry_count": transaction.retry_count,
            },
        )

    def _log_skill_failed(
        self,
        transaction: Transaction,
        skill: Skill,
        exc: BusinessException | SystemException,
        *,
        level: Literal["warning", "error"],
    ) -> None:
        getattr(self.logger, level)(
            f"Skill failed: {exc}",
            extra={
                "event": "skill_failed",
                "transaction_id": transaction.id,
                "skill_name": skill.name,
                "skill_execution_order": skill.execution_order,
                "skill_status": skill.status,
                "retry_count": transaction.retry_count,
                "retry_number": exc.retry_number,
                "exception_type": type(exc).__name__,
            },
        )
