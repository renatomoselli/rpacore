"""Engine — sequential skill execution with status tracking."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Callable, Literal

from rpacore._json_state import validate_json_object
from rpacore.context import ProcessContext
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
from rpacore.logger import bind_log_context, get_logger
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.screenshot import capture_screenshot
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


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
        Retry happens within the same Engine.run() call after the configured
        retry delay and backoff.
    """

    def __init__(
        self,
        max_retries: int = 0,
        *,
        retry_delay: float = 0.0,
        retry_backoff: float = 1.0,
        logger: logging.Logger | None = None,
        screenshot_dir: str = "",
    ) -> None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise TypeError(f"max_retries must be an int >= 0, got {max_retries!r}")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)):
            raise TypeError(f"retry_delay must be a number >= 0, got {retry_delay!r}")
        if retry_delay < 0 or not math.isfinite(retry_delay):
            raise ValueError(f"retry_delay must be >= 0, got {retry_delay}")
        if isinstance(retry_backoff, bool) or not isinstance(retry_backoff, (int, float)):
            raise TypeError(f"retry_backoff must be a number >= 1, got {retry_backoff!r}")
        if retry_backoff < 1 or not math.isfinite(retry_backoff):
            raise ValueError(f"retry_backoff must be >= 1, got {retry_backoff}")
        self.max_retries: int = max_retries
        self.retry_delay: float = float(retry_delay)
        self.retry_backoff: float = float(retry_backoff)
        self.logger: logging.Logger = logger if logger is not None else get_logger()
        self.screenshot_dir: str = screenshot_dir

    def run(
        self,
        ctx: ProcessContext,
        *,
        checkpoint: Callable[[Transaction], None] | None = None,
    ) -> None:
        """Execute all skills in the transaction, retrying retryable failed skills up to max_retries times."""
        transaction = ctx.transaction
        log_context: dict[str, str] = {"transaction_id": transaction.id}
        if transaction.reference:
            log_context["transaction_reference"] = transaction.reference
        with bind_log_context(**log_context):
            self._run(ctx, checkpoint=checkpoint)

    def _run(
        self,
        ctx: ProcessContext,
        *,
        checkpoint: Callable[[Transaction], None] | None = None,
    ) -> None:
        """Run the transaction while its correlation context is bound."""
        transaction = ctx.transaction
        try:
            transaction.validate_for_execution()
        except ExecutionValidationError:
            transaction.status = Status.FAILED
            transaction.outcome_category = OutcomeCategory.VALIDATION_FAILED
            transaction.retry_disposition = RetryDisposition.NOT_REQUESTED
            transaction.failure_code = "rpacore.validation.execution"
            transaction.finished_at = datetime.now(timezone.utc)
            transaction.append_history(HistoryEvent.TRANSACTION_COMPLETED)
            raise
        initial_blocked = self._initial_blocked_skill_ids(transaction)
        self._reset_skipped_skills(transaction)
        transaction.status = Status.IN_PROGRESS
        transaction.outcome_category = OutcomeCategory.UNKNOWN
        transaction.retry_disposition = RetryDisposition.UNKNOWN
        transaction.failure_code = ""
        if transaction.started_at is None:
            transaction.started_at = datetime.now(timezone.utc)
        transaction.finished_at = None
        transaction.append_history(HistoryEvent.TRANSACTION_STARTED)
        self._log_transaction_started(transaction)
        self._checkpoint(transaction, checkpoint)

        try:
            self._execute_pass(ctx, blocked=initial_blocked, checkpoint=checkpoint)

            while transaction.retry_count < self.max_retries:
                retryable = self._retryable_failed_skills(transaction)
                if not retryable:
                    break
                for skill in retryable:
                    skill.status = Status.PENDING
                self._sleep_before_retry(transaction.retry_count)
                transaction.retry_count += 1
                transaction.append_history(HistoryEvent.RETRY_SCHEDULED)
                # Block only business-failed skills; PENDING skills that never ran should also execute.
                self._execute_pass(
                    ctx,
                    blocked=self._business_failed_skill_ids(transaction),
                    checkpoint=checkpoint,
                )
                self._checkpoint(transaction, checkpoint)
        except MemoryError:
            transaction.status = Status.FAILED
            transaction.outcome_category = OutcomeCategory.INTERRUPTED
            transaction.retry_disposition = RetryDisposition.UNKNOWN
            transaction.failure_code = ""
            if transaction.finished_at is None:
                transaction.finished_at = datetime.now(timezone.utc)
            transaction.append_history(HistoryEvent.TRANSACTION_COMPLETED)
            try:
                self._checkpoint(transaction, checkpoint)
            except MemoryError:
                raise
            except Exception:
                pass
            raise

        if all(s.status in (Status.SUCCESSFUL, Status.SKIPPED) for s in transaction.skills):
            transaction.status = Status.SUCCESSFUL
            transaction.outcome_category = OutcomeCategory.SUCCESSFUL
            transaction.retry_disposition = RetryDisposition.NOT_APPLICABLE
            transaction.failure_code = ""
        else:
            transaction.status = Status.FAILED
            terminal_exception = self._last_failed_exception(transaction)
            if isinstance(terminal_exception, BusinessException):
                transaction.outcome_category = OutcomeCategory.BUSINESS_FAILED
                transaction.retry_disposition = RetryDisposition.NOT_REQUESTED
            else:
                transaction.outcome_category = OutcomeCategory.SYSTEM_FAILED
                transaction.retry_disposition = RetryDisposition.RETRY_EXHAUSTED
            transaction.failure_code = "" if terminal_exception is None else terminal_exception.code
        transaction.finished_at = datetime.now(timezone.utc)
        transaction.append_history(HistoryEvent.TRANSACTION_COMPLETED)
        self._log_transaction_completed(transaction)
        self._checkpoint(transaction, checkpoint)

    def _checkpoint(
        self,
        transaction: Transaction,
        checkpoint: Callable[[Transaction], None] | None,
    ) -> None:
        """Validate durable state and run a strict checkpoint when configured."""
        if checkpoint is None:
            return
        validate_json_object(transaction.state, path="transaction.state")
        checkpoint(transaction)
        self.logger.info(
            "Transaction checkpoint completed",
            extra={
                "event": "transaction_checkpoint",
                "transaction_id": transaction.id,
                "transaction_reference": transaction.reference,
                "transaction_status": transaction.status,
                "retry_count": transaction.retry_count,
            },
        )

    def _sleep_before_retry(self, completed_retry_passes: int) -> None:
        """Delay before a retry pass when configured."""
        if self.retry_delay == 0:
            return
        delay = self.retry_delay * (self.retry_backoff ** completed_retry_passes)
        time.sleep(delay)

    def _retryable_failed_skills(self, transaction: Transaction) -> list[Skill]:
        """Return failed skills eligible for retry (last exception is SystemException)."""
        return [
            s for s in transaction.failed_skills()
            if s.exceptions and isinstance(s.exceptions[-1], SystemException)
        ]

    def _business_failed_skill_ids(self, transaction: Transaction) -> set[int]:
        """Return failed skills that must not be re-executed."""
        return {
            id(s) for s in transaction.failed_skills()
            if s.exceptions and isinstance(s.exceptions[-1], BusinessException)
        }

    def _initial_blocked_skill_ids(self, transaction: Transaction) -> set[int] | None:
        """Return initial-pass blocks for recovered transactions."""
        if transaction.history and transaction.history[-1].event is HistoryEvent.TRANSACTION_RESUMED:
            return self._business_failed_skill_ids(transaction)
        return None

    def _reset_skipped_skills(self, transaction: Transaction) -> None:
        """Make skipped work runnable when a failed transaction is explicitly re-run."""
        if transaction.history and transaction.history[-1].event is HistoryEvent.TRANSACTION_RESUMED:
            return
        for skill in transaction.skills:
            if skill.status is Status.SKIPPED:
                skill.status = Status.PENDING

    def _execute_pass(
        self,
        ctx: ProcessContext,
        blocked: set[int] | None,
        checkpoint: Callable[[Transaction], None] | None,
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
            transaction.append_history(HistoryEvent.SKILL_STARTED, skill=skill)
            self._log_skill_started(transaction, skill)
            self._checkpoint(transaction, checkpoint)
            try:
                skill.execute(ctx)
            except BusinessException as exc:
                exc.retry_number = transaction.retry_count
                skill.status = Status.FAILED
                skill.exceptions.append(exc)
                transaction.append_history(HistoryEvent.SKILL_FAILED, skill=skill)
                if self.screenshot_dir:
                    exc.screenshot_path = capture_screenshot(self.screenshot_dir)
                    self._register_screenshot_artifact(ctx, skill, exc.screenshot_path)
                self._log_skill_failed(transaction, skill, exc, level="warning")
                self._checkpoint(transaction, checkpoint)
                if exc.stops_execution:
                    self._skip_downstream_pending_skills(transaction, skill, checkpoint=checkpoint)
                    break
            except SystemException as exc:
                exc.retry_number = transaction.retry_count
                skill.status = Status.FAILED
                skill.exceptions.append(exc)
                transaction.append_history(HistoryEvent.SKILL_FAILED, skill=skill)
                if self.screenshot_dir:
                    exc.screenshot_path = capture_screenshot(self.screenshot_dir)
                    self._register_screenshot_artifact(ctx, skill, exc.screenshot_path)
                self._log_skill_failed(transaction, skill, exc, level="error")
                self._checkpoint(transaction, checkpoint)
                break
            except MemoryError:
                skill.status = Status.FAILED
                transaction.append_history(HistoryEvent.SKILL_INTERRUPTED, skill=skill)
                self._checkpoint(transaction, checkpoint)
                raise
            except Exception as exc:
                wrapped = SystemException(
                    str(exc),
                    action=skill.name,
                    retry_number=transaction.retry_count,
                    code="rpacore.system.unexpected",
                )
                skill.status = Status.FAILED
                skill.exceptions.append(wrapped)
                transaction.append_history(HistoryEvent.SKILL_FAILED, skill=skill)
                if self.screenshot_dir:
                    wrapped.screenshot_path = capture_screenshot(self.screenshot_dir)
                    self._register_screenshot_artifact(ctx, skill, wrapped.screenshot_path)
                self._log_skill_failed(transaction, skill, wrapped, level="error")
                self._checkpoint(transaction, checkpoint)
                break
            else:
                if skill.status is not Status.SKIPPED:
                    skill.status = Status.SUCCESSFUL
                    transaction.append_history(HistoryEvent.SKILL_SUCCEEDED, skill=skill)
                else:
                    transaction.append_history(HistoryEvent.SKILL_SKIPPED, skill=skill)
                self._log_skill_completed(transaction, skill)
                self._checkpoint(transaction, checkpoint)

    def _last_failed_exception(
        self,
        transaction: Transaction,
    ) -> BusinessException | SystemException | None:
        """Return the exception recorded by the last durable skill failure."""
        skills = {
            (skill.name, skill.execution_order): skill
            for skill in transaction.skills
        }
        for entry in reversed(transaction.history):
            if entry.event is not HistoryEvent.SKILL_FAILED:
                continue
            skill = skills.get((entry.skill_name, entry.skill_execution_order))
            if skill is None:
                continue
            for exc in reversed(skill.exceptions):
                if exc.retry_number == entry.retry_number:
                    return exc
        return None

    def _register_screenshot_artifact(
        self,
        ctx: ProcessContext,
        skill: Skill,
        screenshot_path: str,
    ) -> None:
        """Register a captured screenshot path without inspecting file contents."""
        if not screenshot_path:
            return
        ctx.add_artifact(
            f"{skill.name} screenshot",
            screenshot_path,
            kind="screenshot",
            metadata={
                "skill_name": skill.name,
                "skill_execution_order": skill.execution_order,
            },
        )

    def _skip_downstream_pending_skills(
        self,
        transaction: Transaction,
        failed_skill: Skill,
        *,
        checkpoint: Callable[[Transaction], None] | None,
    ) -> None:
        """Mark pending skills after a stopping business failure as skipped."""
        should_skip = False
        for skill in transaction.ordered_skills():
            if skill is failed_skill:
                should_skip = True
                continue
            if should_skip and skill.status is Status.PENDING:
                skill.status = Status.SKIPPED
                transaction.append_history(HistoryEvent.SKILL_SKIPPED, skill=skill)
                self._log_skill_completed(transaction, skill)
        self._checkpoint(transaction, checkpoint)

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
