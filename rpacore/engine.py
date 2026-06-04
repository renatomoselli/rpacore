"""Engine — sequential skill execution with status tracking."""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
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

    def run(self, ctx: ProcessContext) -> None:
        """Execute all skills in the transaction, retrying retryable failed skills up to max_retries times."""
        transaction = ctx.transaction
        transaction.status = Status.IN_PROGRESS
        self._log_transaction_started(transaction)

        executor = ThreadPoolExecutor(max_workers=1) if any(skill.timeout is not None for skill in transaction.skills) else None
        try:
            self._execute_pass(ctx, blocked=None, executor=executor)

            while transaction.retry_count < self.max_retries:
                retryable = self._retryable_failed_skills(transaction)
                if not retryable:
                    break
                for skill in retryable:
                    skill.status = Status.PENDING
                self._sleep_before_retry(transaction.retry_count)
                transaction.retry_count += 1
                # Block only business-failed skills; PENDING skills that never ran should also execute.
                business_failed = {
                    id(s) for s in transaction.failed_skills()
                    if s.exceptions and isinstance(s.exceptions[-1], BusinessException)
                }
                self._execute_pass(ctx, blocked=business_failed, executor=executor)
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

        if all(s.status in (Status.SUCCESSFUL, Status.SKIPPED) for s in transaction.skills):
            transaction.status = Status.SUCCESSFUL
        else:
            transaction.status = Status.FAILED
        self._log_transaction_completed(transaction)

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

    def _execute_pass(
        self,
        ctx: ProcessContext,
        blocked: set[int] | None,
        executor: ThreadPoolExecutor | None,
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
                self._execute_skill(skill, ctx, executor)
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
                if exc.stops_execution:
                    self._skip_downstream_pending_skills(transaction, skill)
                    break
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

    def _execute_skill(
        self,
        skill: Skill,
        ctx: ProcessContext,
        executor: ThreadPoolExecutor | None,
    ) -> None:
        """Execute a skill directly or through the configured timeout boundary.

        A timeout stops the engine from waiting but cannot forcibly terminate the
        running worker thread. Timed skill code must remain safe if it finishes later.
        """
        if skill.timeout is None:
            skill.execute(ctx)
            return
        if executor is None:
            raise RuntimeError("Timed skill requires timeout executor")

        future = executor.submit(skill.execute, ctx)
        try:
            future.result(timeout=skill.timeout)
        except FutureTimeoutError as exc:
            if future.done():
                future.result()
                return
            future.cancel()
            raise SystemException("Skill timed out", action=skill.name) from exc

    def _skip_downstream_pending_skills(self, transaction: Transaction, failed_skill: Skill) -> None:
        """Mark pending skills after a stopping business failure as skipped."""
        should_skip = False
        for skill in transaction.ordered_skills():
            if skill is failed_skill:
                should_skip = True
                continue
            if should_skip and skill.status is Status.PENDING:
                skill.status = Status.SKIPPED

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
