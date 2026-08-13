"""Engine — sequential step execution with status tracking."""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Literal

from rpacore._json_state import validate_json_object
from rpacore._kernel import _TransitionEmitter
from rpacore.context import ProcessContext
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
from rpacore.logger import bind_log_context, get_logger
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.screenshot import capture_screenshot
from rpacore.step import Step
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction
from rpacore.transition import ExecutionTransition


class Engine:
    """Executes a transaction's steps in order, with optional retry.

    Lifecycle:
        Transaction: PENDING → IN_PROGRESS → SUCCESSFUL or FAILED
        Each step:  PENDING → IN_PROGRESS → SUCCESSFUL or FAILED

    Exception semantics:
        BusinessException → step FAILED, execution continues
        SystemException   → step FAILED, execution stops
        Any other exception → wrapped as SystemException, execution stops

    Retry semantics:
        After all steps run, retryable failed steps are reset and re-run.
        Only steps whose last recorded exception is a SystemException are retried.
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
        transition_sink: Callable[[ExecutionTransition], None] | None = None,
        transition_state_fields: Iterable[str] = (),
    ) -> None:
        """Execute steps and optionally emit closed lifecycle transitions.

        The synchronous transition sink runs after each lifecycle mutation and
        history append but before the matching strict checkpoint callback.
        ``transition_state_fields`` selects the only durable state keys copied
        into each emitted fact.
        """
        transitions = _TransitionEmitter(
            transition_sink,
            transition_state_fields=transition_state_fields,
        )
        transaction = ctx.transaction
        log_context: dict[str, str] = {"transaction_id": transaction.id}
        if transaction.reference:
            log_context["transaction_reference"] = transaction.reference
        with bind_log_context(**log_context):
            self._run(
                ctx,
                checkpoint=checkpoint,
                transitions=transitions,
            )

    def _run(
        self,
        ctx: ProcessContext,
        *,
        checkpoint: Callable[[Transaction], None] | None = None,
        transitions: _TransitionEmitter,
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
            transitions.append(
                transaction,
                HistoryEvent.TRANSACTION_COMPLETED,
                include_checkpoint_state=False,
            )
            raise
        initial_blocked = self._initial_blocked_step_ids(transaction)
        self._reset_skipped_steps(transaction)
        transaction.status = Status.IN_PROGRESS
        transaction.outcome_category = OutcomeCategory.UNKNOWN
        transaction.retry_disposition = RetryDisposition.UNKNOWN
        transaction.failure_code = ""
        if transaction.started_at is None:
            transaction.started_at = datetime.now(timezone.utc)
        transaction.finished_at = None
        transitions.append(transaction, HistoryEvent.TRANSACTION_STARTED)
        self._log_transaction_started(transaction)
        self._checkpoint(transaction, checkpoint)

        try:
            self._execute_pass(
                ctx,
                blocked=initial_blocked,
                checkpoint=checkpoint,
                transitions=transitions,
            )

            while transaction.retry_count < self.max_retries:
                retryable = self._retryable_failed_steps(transaction)
                if not retryable:
                    break
                for step in retryable:
                    step.status = Status.PENDING
                self._sleep_before_retry(transaction.retry_count)
                transaction.retry_count += 1
                transitions.append(transaction, HistoryEvent.RETRY_SCHEDULED)
                # Block only business-failed steps; PENDING steps that never ran should also execute.
                self._execute_pass(
                    ctx,
                    blocked=self._business_failed_step_ids(transaction),
                    checkpoint=checkpoint,
                    transitions=transitions,
                )
                self._checkpoint(transaction, checkpoint)
        except MemoryError:
            transaction.status = Status.FAILED
            transaction.outcome_category = OutcomeCategory.INTERRUPTED
            transaction.retry_disposition = RetryDisposition.UNKNOWN
            transaction.failure_code = ""
            if transaction.finished_at is None:
                transaction.finished_at = datetime.now(timezone.utc)
            transitions.append(transaction, HistoryEvent.TRANSACTION_COMPLETED)
            try:
                self._checkpoint(transaction, checkpoint)
            except MemoryError:
                raise
            except Exception:
                pass
            raise

        if all(s.status in (Status.SUCCESSFUL, Status.SKIPPED) for s in transaction.steps):
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
        transitions.append(transaction, HistoryEvent.TRANSACTION_COMPLETED)
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

    def _retryable_failed_steps(self, transaction: Transaction) -> list[Step]:
        """Return failed steps eligible for retry (last exception is SystemException)."""
        return [
            s for s in transaction.failed_steps()
            if s.exceptions and isinstance(s.exceptions[-1], SystemException)
        ]

    def _business_failed_step_ids(self, transaction: Transaction) -> set[int]:
        """Return failed steps that must not be re-executed."""
        return {
            id(s) for s in transaction.failed_steps()
            if s.exceptions and isinstance(s.exceptions[-1], BusinessException)
        }

    def _initial_blocked_step_ids(self, transaction: Transaction) -> set[int] | None:
        """Return initial-pass blocks for recovered transactions."""
        if transaction.history and transaction.history[-1].event is HistoryEvent.TRANSACTION_RESUMED:
            return self._business_failed_step_ids(transaction)
        return None

    def _reset_skipped_steps(self, transaction: Transaction) -> None:
        """Make skipped work runnable when a failed transaction is explicitly re-run."""
        if transaction.history and transaction.history[-1].event is HistoryEvent.TRANSACTION_RESUMED:
            return
        for step in transaction.steps:
            if step.status is Status.SKIPPED:
                step.status = Status.PENDING

    def _execute_pass(
        self,
        ctx: ProcessContext,
        blocked: set[int] | None,
        checkpoint: Callable[[Transaction], None] | None,
        transitions: _TransitionEmitter,
    ) -> None:
        """Run one execution pass.

        blocked: set of id(step) to skip. Use None to run all non-complete steps.
        Business-failed steps are passed as blocked on retry passes so they are
        not re-executed, while still-PENDING steps that were stopped by a prior
        SystemException are allowed to run.
        """
        transaction = ctx.transaction
        for step in transaction.ordered_steps():
            if step.status in (Status.SUCCESSFUL, Status.SKIPPED):
                continue
            if blocked is not None and id(step) in blocked:
                continue

            step.status = Status.IN_PROGRESS
            transitions.append(transaction, HistoryEvent.STEP_STARTED, step=step)
            self._log_step_started(transaction, step)
            self._checkpoint(transaction, checkpoint)
            try:
                step.execute(ctx)
            except BusinessException as exc:
                exc.retry_number = transaction.retry_count
                step.status = Status.FAILED
                step.exceptions.append(exc)
                transitions.append(transaction, HistoryEvent.STEP_FAILED, step=step)
                if self.screenshot_dir:
                    exc.screenshot_path = capture_screenshot(self.screenshot_dir)
                    self._register_screenshot_artifact(ctx, step, exc.screenshot_path)
                self._log_step_failed(transaction, step, exc, level="warning")
                self._checkpoint(transaction, checkpoint)
                if exc.halts_remaining_steps:
                    self._skip_downstream_pending_steps(
                        transaction,
                        step,
                        checkpoint=checkpoint,
                        transitions=transitions,
                    )
                    break
            except SystemException as exc:
                exc.retry_number = transaction.retry_count
                step.status = Status.FAILED
                step.exceptions.append(exc)
                transitions.append(transaction, HistoryEvent.STEP_FAILED, step=step)
                if self.screenshot_dir:
                    exc.screenshot_path = capture_screenshot(self.screenshot_dir)
                    self._register_screenshot_artifact(ctx, step, exc.screenshot_path)
                self._log_step_failed(transaction, step, exc, level="error")
                self._checkpoint(transaction, checkpoint)
                break
            except MemoryError:
                step.status = Status.FAILED
                transitions.append(
                    transaction,
                    HistoryEvent.STEP_INTERRUPTED,
                    step=step,
                )
                self._checkpoint(transaction, checkpoint)
                raise
            except Exception as exc:
                wrapped = SystemException(
                    str(exc),
                    action=step.name,
                    retry_number=transaction.retry_count,
                    code="rpacore.system.unexpected",
                )
                step.status = Status.FAILED
                step.exceptions.append(wrapped)
                transitions.append(transaction, HistoryEvent.STEP_FAILED, step=step)
                if self.screenshot_dir:
                    wrapped.screenshot_path = capture_screenshot(self.screenshot_dir)
                    self._register_screenshot_artifact(ctx, step, wrapped.screenshot_path)
                self._log_step_failed(transaction, step, wrapped, level="error")
                self._checkpoint(transaction, checkpoint)
                break
            else:
                if step.status is not Status.SKIPPED:
                    step.status = Status.SUCCESSFUL
                    transitions.append(
                        transaction,
                        HistoryEvent.STEP_SUCCEEDED,
                        step=step,
                    )
                else:
                    transitions.append(
                        transaction,
                        HistoryEvent.STEP_SKIPPED,
                        step=step,
                    )
                self._log_step_completed(transaction, step)
                self._checkpoint(transaction, checkpoint)

    def _last_failed_exception(
        self,
        transaction: Transaction,
    ) -> BusinessException | SystemException | None:
        """Return the exception recorded by the last durable step failure."""
        steps = {
            (step.name, step.execution_order): step
            for step in transaction.steps
        }
        for entry in reversed(transaction.history):
            if entry.event is not HistoryEvent.STEP_FAILED:
                continue
            if entry.step_execution_order is None:
                continue
            step = steps.get((entry.step_name, entry.step_execution_order))
            if step is None:
                continue
            for exc in reversed(step.exceptions):
                if exc.retry_number == entry.retry_number:
                    return exc
        return None

    def _register_screenshot_artifact(
        self,
        ctx: ProcessContext,
        step: Step,
        screenshot_path: str,
    ) -> None:
        """Register a captured screenshot path without inspecting file contents."""
        if not screenshot_path:
            return
        ctx.add_artifact(
            f"{step.name} screenshot",
            screenshot_path,
            kind="screenshot",
            metadata={
                "step_name": step.name,
                "step_execution_order": step.execution_order,
            },
        )

    def _skip_downstream_pending_steps(
        self,
        transaction: Transaction,
        failed_step: Step,
        *,
        checkpoint: Callable[[Transaction], None] | None,
        transitions: _TransitionEmitter,
    ) -> None:
        """Mark pending steps after a halting business failure as skipped."""
        should_skip = False
        for step in transaction.ordered_steps():
            if step is failed_step:
                should_skip = True
                continue
            if should_skip and step.status is Status.PENDING:
                step.status = Status.SKIPPED
                transitions.append(
                    transaction,
                    HistoryEvent.STEP_SKIPPED,
                    step=step,
                )
                self._log_step_completed(transaction, step)
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

    def _log_step_started(self, transaction: Transaction, step: Step) -> None:
        self.logger.info(
            "Step started",
            extra={
                "event": "step_started",
                "transaction_id": transaction.id,
                "step_name": step.name,
                "step_execution_order": step.execution_order,
                "step_status": step.status,
                "retry_count": transaction.retry_count,
            },
        )

    def _log_step_completed(self, transaction: Transaction, step: Step) -> None:
        self.logger.info(
            "Step completed",
            extra={
                "event": "step_completed",
                "transaction_id": transaction.id,
                "step_name": step.name,
                "step_execution_order": step.execution_order,
                "step_status": step.status,
                "retry_count": transaction.retry_count,
            },
        )

    def _log_step_failed(
        self,
        transaction: Transaction,
        step: Step,
        exc: BusinessException | SystemException,
        *,
        level: Literal["warning", "error"],
    ) -> None:
        getattr(self.logger, level)(
            f"Step failed: {exc}",
            extra={
                "event": "step_failed",
                "transaction_id": transaction.id,
                "step_name": step.name,
                "step_execution_order": step.execution_order,
                "step_status": step.status,
                "retry_count": transaction.retry_count,
                "retry_number": exc.retry_number,
                "exception_type": type(exc).__name__,
            },
        )
