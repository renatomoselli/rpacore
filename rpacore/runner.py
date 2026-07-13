"""Runner — queue-driven execution loop."""

from __future__ import annotations

import logging
import socket
import sqlite3
import sys
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Callable

from rpacore._json_state import JsonStateError, validate_json_object
from rpacore._sqlite_retry import is_transient_sqlite_lock, sqlite_retry_delay
from rpacore._validation import type_error
from rpacore.context import ProcessContext
from rpacore.credentials import CredentialProvider
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
from rpacore.engine import Engine
from rpacore.logger import get_logger
from rpacore.notify import Notifier, dispatch
from rpacore.persistence import (
    _delete_unbound_pending_transaction,
    load_transaction,
    save_transaction,
)
from rpacore.queue import QueueItem, QueueLeaseLostError, QueueProvider
from rpacore.recovery import resume_transaction
from rpacore.report import generate_report
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


_PERSISTENCE_SAVE_ATTEMPTS = 3
_PERSISTENCE_RETRY_DELAY_SECONDS = 0.05
_LEASE_RENEW_ATTEMPTS = 3
_LEASE_RETRY_DELAY_SECONDS = 0.05
_DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS = 10.0

# Cleanup precedence:
# - no active error + cleanup error: propagate the cleanup error
# - active ordinary/fatal error + cleanup error: preserve the active error and
#   attach the cleanup error as a note
# - fatal error + truthy resource_scope.__exit__: propagate the fatal error
_FATAL_SIGNALS = (MemoryError, KeyboardInterrupt, SystemExit)


class _CheckpointError(RuntimeError):
    """Internal wrapper carrying queue retry policy for checkpoint failures."""

    def __init__(self, original: Exception, *, retry: bool) -> None:
        super().__init__(str(original))
        self.original = original
        self.retry = retry


class _DurableTransactionBindingError(RuntimeError):
    """Raised when a persisted queue transaction binding cannot be honored."""


class _LeaseRenewalError(RuntimeError):
    """Raised when the runner cannot renew a queue lease for an unknown reason."""


@dataclass
class _LeaseHeartbeat:
    """Runner-owned heartbeat state for one claimed queue item."""

    stop_event: threading.Event
    thread: threading.Thread
    error: BaseException | None = None

    def raise_if_failed(self) -> None:
        if self.error is not None:
            raise self.error


@dataclass
class QueueRunSummary:
    """Counts from a completed run_queue_loop() call."""

    processed: int = field(default=0)
    completed: int = field(default=0)
    failed: int = field(default=0)
    callback_errors: int = field(default=0)
    persistence_errors: int = field(default=0)
    lifecycle_errors: int = field(default=0)
    notification_errors: int = field(default=0)


def run_queue_loop(
    queue: QueueProvider,
    engine: Engine,
    build_transaction: Callable[[QueueItem], Transaction],
    config: dict[str, object],
    credentials: CredentialProvider,
    *,
    worker_id: str = "",
    notifiers: list[Notifier] | None = None,
    logger: logging.Logger | None = None,
    after_item: Callable[[QueueItem, Transaction | None, Exception | None], None] | None = None,
    stop_event: threading.Event | None = None,
    retry_business_failures: bool = False,
    transaction_db_path: str | None = None,
    resource_scope: AbstractContextManager[dict[str, object] | None] | None = None,
    on_finish: Callable[[QueueRunSummary], None] | None = None,
) -> QueueRunSummary:
    """Drain a queue by running each item through the engine.

    Claims items one at a time until the queue is empty. For each item:
    - Calls build_transaction(item) to construct a Transaction (user responsibility).
    - Builds a ProcessContext with the item's payload in transaction.state.
    - Runs engine.run(ctx).
        - Supplies a strict transaction checkpoint when transaction_db_path is set.
        - Generates a report and dispatches notifiers after engine.run(ctx).
    - Calls queue.complete() if transaction.status is SUCCESSFUL.
    - Calls queue.fail() with retry=False for business-only transaction failures
      unless retry_business_failures=True.
    - Calls queue.fail() with retry=True for system failures and unexpected errors.
    - Also calls queue.fail() if build_transaction() or any unexpected error raises.
    - Calls after_item(item, transaction, error) once per item on ordinary item
      outcomes after execution, checkpointing, reporting, and notification.
            transaction is None if build_transaction() raised; error is None on normal paths
            and set for unexpected processing, reporting, or transaction persistence failures.
      Fires before the final queue state transition (complete/fail). If the callback
      raises on a success-path item, the item is still marked complete (the automation
      ran) but the error is counted in QueueRunSummary.callback_errors. If the callback
      raises on a failure-path item, queue.fail() is called as normal. Ordinary
      callback failures do not stop the loop. Confirmed lease loss skips the callback
      because the worker no longer owns the item outcome.
    - Enters resource_scope before claiming any items. Returned resources are
      shallow-copied into every item context. Top-level resource names are isolated;
      nested resource objects retain shared identity.
    - Calls on_finish(summary) exactly once after the loop exits. Exceptions are logged
      and swallowed so cleanup cannot change the run outcome.
    - Starts one runner-owned heartbeat thread per claimed item. The heartbeat
      only renews the queue lease. If the lease is lost, the next checkpoint or
      final transition raises QueueLeaseLostError, skips the final queue
      transition, logs the loss, and stops the worker from claiming more work.

    Args:
        queue:             The queue to drain.
        engine:            Configured Engine instance.
        build_transaction: User-supplied callable mapping a QueueItem to a Transaction.
        config:            Framework config dict (passed to ProcessContext).
        credentials:       Credential provider (passed to ProcessContext).
        worker_id:         Worker identifier passed to queue.next_item(). Defaults to hostname.
        notifiers:         Optional list of notifiers to call after each transaction. Defaults to [].
                           Notification failures are logged and counted but never change
                           transaction or queue outcomes.
        logger:            Optional logger. Defaults to the rpacore logger.
        after_item:        Optional callback fired after each item. Receives (item, transaction, error).
        stop_event:        Optional threading.Event. When set, the loop stops before claiming
                           the next item. The item currently in-flight completes normally.
                           The caller is responsible for setting the event (e.g. from a
                           signal handler). The library never calls signal.signal().
        retry_business_failures:
                           If True, retry failed business transactions according to the
                           queue retry policy. Defaults to False so deterministic business
                           failures are terminal queue outcomes.
        transaction_db_path:
                           Optional SQLite transaction database path. When set, the
                           runner supplies strict checkpoints throughout engine.run(ctx).
                           Checkpoint failures stop execution and prevent queue completion.
        resource_scope:    Optional context manager entered before queue claims and
                           exited after processing. It may return shared resources
                           shallow-copied into every item context. Cleanup failures
                           propagate after already-decided queue outcomes.
        on_finish:         Optional callback fired exactly once after the loop exits.
                           Receives the final summary. Exceptions are logged and swallowed.

    Returns:
        QueueRunSummary with counts of processed, completed, and failed items.
    """
    log = logger if logger is not None else get_logger()
    if not worker_id:
        worker_id = socket.gethostname()
    _notifiers: list[Notifier] = notifiers if notifiers is not None else []
    summary = QueueRunSummary()

    try:
        shared_resources: dict[str, object] = {}
        if resource_scope is None:
            return _run_items(
                queue,
                engine,
                build_transaction,
                config,
                credentials,
                worker_id=worker_id,
                notifiers=_notifiers,
                log=log,
                after_item=after_item,
                stop_event=stop_event,
                retry_business_failures=retry_business_failures,
                transaction_db_path=transaction_db_path,
                shared_resources=shared_resources,
                summary=summary,
            )

        scope_resources = resource_scope.__enter__()
        try:
            if scope_resources is not None:
                if not isinstance(scope_resources, dict):
                    raise type_error("resource_scope yield", "dict | None", scope_resources)
                shared_resources = dict(scope_resources)
            _log_resource_scope(log, "resource_scope_acquired", worker_id, shared_resources)
            result = _run_items(
                queue,
                engine,
                build_transaction,
                config,
                credentials,
                worker_id=worker_id,
                notifiers=_notifiers,
                log=log,
                after_item=after_item,
                stop_event=stop_event,
                retry_business_failures=retry_business_failures,
                transaction_db_path=transaction_db_path,
                shared_resources=shared_resources,
                summary=summary,
            )
        except BaseException as error:
            exc_info = sys.exc_info()
            if isinstance(error, _FATAL_SIGNALS):
                try:
                    resource_scope.__exit__(*exc_info)
                except BaseException as cleanup_error:
                    _add_cleanup_failure_note(error, "resource_scope", cleanup_error)
                raise
            if not resource_scope.__exit__(*exc_info):
                raise
            result = summary
        else:
            resource_scope.__exit__(None, None, None)
        _log_resource_scope(log, "resource_scope_released", worker_id, shared_resources)
        return result
    finally:
        active_error = sys.exception()
        if on_finish is not None:
            try:
                on_finish(summary)
            except _FATAL_SIGNALS as cleanup_error:
                if isinstance(active_error, _FATAL_SIGNALS):
                    _add_cleanup_failure_note(active_error, "on_finish", cleanup_error)
                else:
                    raise
            except Exception:
                summary.lifecycle_errors += 1
                log.exception(
                    "on_finish callback raised during lifecycle cleanup",
                    extra={"event": "on_finish_error", "worker_id": worker_id},
                )


def _add_cleanup_failure_note(
    active_error: BaseException,
    cleanup_name: str,
    cleanup_error: BaseException,
) -> None:
    """Record a secondary cleanup failure without replacing an active fatal signal."""
    active_error.add_note(
        f"{cleanup_name} cleanup also raised "
        f"{type(cleanup_error).__name__}: {cleanup_error}"
    )


def _run_items(
    queue: QueueProvider,
    engine: Engine,
    build_transaction: Callable[[QueueItem], Transaction],
    config: dict[str, object],
    credentials: CredentialProvider,
    *,
    worker_id: str,
    notifiers: list[Notifier],
    log: logging.Logger,
    after_item: Callable[[QueueItem, Transaction | None, Exception | None], None] | None,
    stop_event: threading.Event | None,
    retry_business_failures: bool,
    transaction_db_path: str | None,
    shared_resources: dict[str, object],
    summary: QueueRunSummary,
) -> QueueRunSummary:
    """Process queue items after lifecycle startup has completed."""
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        item = queue.next_item(worker_id)
        if item is None:
            break

        summary.processed += 1
        log.info(
            "Processing queue item",
            extra={"event": "queue_item_start", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
        )
        heartbeat = _start_lease_heartbeat(queue, item, worker_id=worker_id, log=log)
        try:
            should_continue = _run_claimed_item(
                queue=queue,
                engine=engine,
                build_transaction=build_transaction,
                config=config,
                credentials=credentials,
                item=item,
                worker_id=worker_id,
                notifiers=notifiers,
                log=log,
                after_item=after_item,
                retry_business_failures=retry_business_failures,
                transaction_db_path=transaction_db_path,
                shared_resources=shared_resources,
                summary=summary,
                heartbeat=heartbeat,
            )
        finally:
            _stop_lease_heartbeat_preserving_error(heartbeat)

        if not should_continue:
            break

    return summary


def _run_claimed_item(
    queue: QueueProvider,
    engine: Engine,
    build_transaction: Callable[[QueueItem], Transaction],
    config: dict[str, object],
    credentials: CredentialProvider,
    item: QueueItem,
    *,
    worker_id: str,
    notifiers: list[Notifier],
    log: logging.Logger,
    after_item: Callable[[QueueItem, Transaction | None, Exception | None], None] | None,
    retry_business_failures: bool,
    transaction_db_path: str | None,
    shared_resources: dict[str, object],
    summary: QueueRunSummary,
    heartbeat: _LeaseHeartbeat,
) -> bool:
    """Process one claimed item; return whether the queue loop should continue."""
    transaction: Transaction | None = None
    ctx: ProcessContext | None = None
    error: Exception | None = None
    originally_intended_complete = False
    callback_failed = False
    lease_lost = False

    try:
        transaction = _transaction_for_queue_item(
            queue,
            item,
            build_transaction,
            transaction_db_path=transaction_db_path,
            retry_business_failures=retry_business_failures,
            worker_id=worker_id,
            log=log,
            summary=summary,
        )
        ctx = ProcessContext(
            transaction=transaction,
            config=config,
            resources=dict(shared_resources),
            credentials=credentials,
        )
        if transaction_db_path is not None:
            checkpoint = _lease_checked_checkpoint(
                heartbeat,
                _strict_transaction_checkpoint(
                    db_path=transaction_db_path,
                    item=item,
                    worker_id=worker_id,
                    log=log,
                    summary=summary,
                ),
            )
        else:
            checkpoint = _lease_only_checkpoint(heartbeat)
        engine.run(ctx, checkpoint=checkpoint)
        heartbeat.raise_if_failed()
        validate_json_object(ctx.transaction.state, path="transaction.state")
        originally_intended_complete = ctx.transaction.status == Status.SUCCESSFUL
    except _FATAL_SIGNALS:
        raise
    except Exception as exc:
        error = exc
        lease_lost = isinstance(exc, QueueLeaseLostError)
        log.exception(
            "Unexpected error processing queue item",
            extra={"event": "queue_item_error", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
        )

    if not lease_lost:
        try:
            heartbeat.raise_if_failed()
        except QueueLeaseLostError as exc:
            error = exc
            lease_lost = True
        except _FATAL_SIGNALS:
            raise
        except Exception as exc:
            if error is None:
                error = exc

    if ctx is not None and not lease_lost:
        try:
            report = generate_report(ctx.transaction)
        except _FATAL_SIGNALS:
            raise
        except Exception as exc:
            if error is None:
                error = exc
            log.exception(
                "Post-run report generation failed; preserving queue outcome",
                extra={"event": "queue_item_postprocess_error", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
            )
        else:
            def count_notification_error(notifier: str) -> None:
                summary.notification_errors += 1

            dispatch(
                notifiers,
                report,
                logger=log,
                on_failure=count_notification_error,
            )

    if after_item is not None and not lease_lost:
        try:
            after_item(item, transaction, error)
        except _FATAL_SIGNALS:
            raise
        except Exception:
            callback_failed = True
            log.exception(
                "after_item callback raised during post-processing",
                extra={"event": "after_item_error", "queue_item_id": item.id, "worker_id": worker_id},
            )

    if not lease_lost:
        try:
            heartbeat.raise_if_failed()
        except QueueLeaseLostError as exc:
            error = exc
            lease_lost = True
        except _FATAL_SIGNALS:
            raise
        except Exception as exc:
            if error is None:
                error = exc

    if lease_lost:
        summary.failed += 1
        _log_lease_lost(log, item, worker_id, error)
        return False

    if originally_intended_complete:
        transition_error = _complete_queue_item_with_retries(
            queue,
            item,
            worker_id=worker_id,
            log=log,
        )
        if transition_error is not None:
            raise transition_error
        summary.completed += 1
        if callback_failed:
            summary.callback_errors += 1
        log.info(
            "Completed queue item",
            extra={"event": "queue_item_complete", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
        )
        return True

    if isinstance(error, _CheckpointError):
        retry = error.retry
    elif isinstance(error, _DurableTransactionBindingError):
        retry = False
    elif isinstance(error, _LeaseRenewalError):
        retry = True
    elif isinstance(error, (ExecutionValidationError, JsonStateError)):
        retry = False
    else:
        retry = (
            retry_business_failures
            or error is not None
            or not _transaction_has_only_business_failures(transaction)
        )
    transition_error = _fail_queue_item_with_retries(
        queue,
        item,
        retry=retry,
        worker_id=worker_id,
        log=log,
    )
    if transition_error is not None:
        raise transition_error
    summary.failed += 1
    if not error and not callback_failed and ctx is not None:
        log.warning(
            "Queue item failed (transaction status: %s)",
            ctx.transaction.status,
            extra={"event": "queue_item_fail", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
        )
    return True


def _lease_checked_checkpoint(
    heartbeat: _LeaseHeartbeat,
    checkpoint: Callable[[Transaction], None],
) -> Callable[[Transaction], None]:
    """Return a checkpoint that fails before persistence after lease loss."""

    def checked_checkpoint(transaction: Transaction) -> None:
        heartbeat.raise_if_failed()
        checkpoint(transaction)
        heartbeat.raise_if_failed()

    return checked_checkpoint


def _log_resource_scope(
    log: logging.Logger,
    event: str,
    worker_id: str,
    resources: dict[str, object],
) -> None:
    log.info(
        "Resource scope event",
        extra={
            "event": event,
            "worker_id": worker_id,
            "resource_count": len(resources),
            "resource_names": sorted(resources),
        },
    )


def _lease_only_checkpoint(heartbeat: _LeaseHeartbeat) -> Callable[[Transaction], None]:
    """Return a checkpoint that only enforces lease loss boundaries."""

    def checkpoint(transaction: Transaction) -> None:
        heartbeat.raise_if_failed()

    return checkpoint


def _start_lease_heartbeat(
    queue: QueueProvider,
    item: QueueItem,
    *,
    worker_id: str,
    log: logging.Logger,
) -> _LeaseHeartbeat:
    """Start one runner-owned heartbeat thread for a claimed queue item."""
    stop_event = threading.Event()
    heartbeat = _LeaseHeartbeat(
        stop_event=stop_event,
        thread=threading.Thread(target=lambda: None),
    )

    def run() -> None:
        while not stop_event.is_set():
            error = _renew_lease_with_retries(
                queue,
                item.id,
                claimed_by=item.claimed_by,
                log=log,
                worker_id=worker_id,
            )
            if error is not None:
                if isinstance(error, (QueueLeaseLostError, MemoryError)):
                    heartbeat.error = error
                else:
                    heartbeat.error = _LeaseRenewalError(
                        f"Queue item {item.id!r} lease renewal failed: {error}"
                    )
                return
            if stop_event.wait(_lease_renewal_interval(queue)):
                return

    heartbeat.thread = threading.Thread(
        name=f"rpacore-lease-{item.id}",
        target=run,
        daemon=True,
    )
    heartbeat.thread.start()
    return heartbeat


def _stop_lease_heartbeat(heartbeat: _LeaseHeartbeat) -> None:
    heartbeat.stop_event.set()
    heartbeat.thread.join()


def _stop_lease_heartbeat_preserving_error(heartbeat: _LeaseHeartbeat) -> None:
    """Stop a heartbeat without replacing an exception already in flight."""
    active_error = sys.exception()
    try:
        _stop_lease_heartbeat(heartbeat)
    except BaseException as cleanup_error:
        if active_error is None:
            raise
        _add_cleanup_failure_note(active_error, "lease heartbeat", cleanup_error)


def _lease_renewal_interval(queue: QueueProvider) -> float:
    lease_timeout = getattr(queue, "lease_timeout", None)
    if (
        isinstance(lease_timeout, (int, float))
        and not isinstance(lease_timeout, bool)
        and lease_timeout > 0
    ):
        return min(
            max(float(lease_timeout) / 3.0, 0.1),
            _DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS,
        )
    return _DEFAULT_LEASE_RENEWAL_INTERVAL_SECONDS


def _renew_lease_with_retries(
    queue: QueueProvider,
    item_id: str,
    *,
    claimed_by: str,
    log: logging.Logger,
    worker_id: str,
) -> Exception | None:
    for attempt in range(_LEASE_RENEW_ATTEMPTS):
        try:
            queue.renew_lease(item_id, claimed_by=claimed_by)
            return None
        except sqlite3.OperationalError as exc:
            if not is_transient_sqlite_lock(exc) or attempt == _LEASE_RENEW_ATTEMPTS - 1:
                return exc
            _sleep_before_sqlite_retry(
                attempt,
                delay_seconds=_LEASE_RETRY_DELAY_SECONDS,
                log=log,
                event="queue_lease_renewal_retry",
                operation="renew_lease",
                queue_item_id=item_id,
                worker_id=worker_id,
                error=exc,
                max_attempts=_LEASE_RENEW_ATTEMPTS,
            )
        except MemoryError as exc:
            return exc
        except Exception as exc:
            return exc
    return None


def _complete_queue_item_with_retries(
    queue: QueueProvider,
    item: QueueItem,
    *,
    worker_id: str,
    log: logging.Logger,
) -> Exception | None:
    """Complete a queue item, retrying short-lived SQLite lock failures."""
    for attempt in range(_LEASE_RENEW_ATTEMPTS):
        try:
            queue.complete(item.id, claimed_by=item.claimed_by)
            return None
        except sqlite3.OperationalError as exc:
            if not is_transient_sqlite_lock(exc) or attempt == _LEASE_RENEW_ATTEMPTS - 1:
                return exc
            _sleep_before_sqlite_retry(
                attempt,
                delay_seconds=_LEASE_RETRY_DELAY_SECONDS,
                log=log,
                event="queue_transition_retry",
                operation="complete",
                queue_item_id=item.id,
                worker_id=worker_id,
                error=exc,
                max_attempts=_LEASE_RENEW_ATTEMPTS,
            )
        except MemoryError:
            raise
        except Exception as exc:
            return exc
    return None


def _fail_queue_item_with_retries(
    queue: QueueProvider,
    item: QueueItem,
    *,
    retry: bool,
    worker_id: str,
    log: logging.Logger,
) -> Exception | None:
    """Fail a queue item, retrying short-lived SQLite lock failures."""
    for attempt in range(_LEASE_RENEW_ATTEMPTS):
        try:
            queue.fail(item.id, retry=retry, claimed_by=item.claimed_by)
            return None
        except sqlite3.OperationalError as exc:
            if not is_transient_sqlite_lock(exc) or attempt == _LEASE_RENEW_ATTEMPTS - 1:
                return exc
            _sleep_before_sqlite_retry(
                attempt,
                delay_seconds=_LEASE_RETRY_DELAY_SECONDS,
                log=log,
                event="queue_transition_retry",
                operation="fail",
                queue_item_id=item.id,
                worker_id=worker_id,
                error=exc,
                max_attempts=_LEASE_RENEW_ATTEMPTS,
            )
        except MemoryError:
            raise
        except Exception as exc:
            return exc
    return None


def _sleep_before_sqlite_retry(
    attempt: int,
    *,
    delay_seconds: float,
    log: logging.Logger,
    event: str,
    operation: str,
    queue_item_id: str,
    worker_id: str,
    error: sqlite3.OperationalError,
    max_attempts: int,
) -> None:
    delay = sqlite_retry_delay(attempt, base_delay_seconds=delay_seconds)
    log.warning(
        "Retrying transient SQLite operation after lock/busy error",
        extra={
            "event": event,
            "operation": operation,
            "queue_item_id": queue_item_id,
            "worker_id": worker_id,
            "attempt": attempt + 1,
            "max_attempts": max_attempts,
            "retry_delay_seconds": delay,
        },
        exc_info=(type(error), error, error.__traceback__),
    )
    time.sleep(delay)


def _log_lease_lost(
    log: logging.Logger,
    item: QueueItem,
    worker_id: str,
    error: Exception | None,
) -> None:
    log.error(
        "Queue item lease lost; skipping final queue transition and stopping worker",
        extra={
            "event": "queue_item_lease_lost",
            "queue_item_id": item.id,
            "queue_reference": item.reference,
            "claimed_by": item.claimed_by,
            "worker_id": worker_id,
        },
        exc_info=(type(error), error, error.__traceback__) if error is not None else None,
    )


def _transaction_for_queue_item(
    queue: QueueProvider,
    item: QueueItem,
    build_transaction: Callable[[QueueItem], Transaction],
    *,
    transaction_db_path: str | None,
    retry_business_failures: bool,
    worker_id: str,
    log: logging.Logger,
    summary: QueueRunSummary,
) -> Transaction:
    if transaction_db_path is None:
        transaction = build_transaction(item)
        _seed_transaction_state_from_payload(transaction, item, worker_id=worker_id, log=log)
        return transaction

    if item.transaction_id:
        try:
            candidate = build_transaction(item)
            return resume_transaction(
                item.transaction_id,
                candidate.skills,
                db_path=transaction_db_path,
                retry_business_failures=retry_business_failures,
            )
        except (KeyError, sqlite3.Error, SystemException, ValueError) as exc:
            raise _DurableTransactionBindingError(
                f"Queue item {item.id!r} is bound to transaction "
                f"{item.transaction_id!r}, but it cannot be resumed: {exc}"
            ) from exc

    transaction = build_transaction(item)
    _seed_transaction_state_from_payload(transaction, item, worker_id=worker_id, log=log)
    error = _save_transaction_with_retries(
        transaction,
        db_path=transaction_db_path,
        log=log,
        event="transaction_initial_persistence_retry",
        queue_item_id=item.id,
        worker_id=worker_id,
    )
    if error is not None:
        summary.persistence_errors += 1
        log.error(
            "Initial transaction persistence failed",
            extra={
                "event": "transaction_initial_persistence_error",
                "queue_item_id": item.id,
                "queue_reference": item.reference,
                "transaction_id": transaction.id,
                "transaction_reference": transaction.reference,
                "worker_id": worker_id,
            },
            exc_info=(type(error), error, error.__traceback__),
        )
        raise _CheckpointError(error, retry=True) from error
    try:
        error = _bind_transaction_with_retries(
            queue,
            item.id,
            transaction.id,
            claimed_by=item.claimed_by,
            log=log,
            worker_id=worker_id,
        )
        if error is not None:
            raise error
    except BaseException as exc:
        try:
            cleanup_error: BaseException | None = _delete_transaction_with_retries(
                transaction.id,
                db_path=transaction_db_path,
                log=log,
                queue_item_id=item.id,
                worker_id=worker_id,
            )
        except BaseException as raised_cleanup_error:
            cleanup_error = raised_cleanup_error
        if cleanup_error is not None:
            summary.persistence_errors += 1
            log.error(
                "Initial transaction cleanup failed after queue binding error",
                extra={
                    "event": "transaction_initial_cleanup_error",
                    "queue_item_id": item.id,
                    "queue_reference": item.reference,
                    "transaction_id": transaction.id,
                    "transaction_reference": transaction.reference,
                    "worker_id": worker_id,
                },
                exc_info=(type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
            )
        if isinstance(exc, _FATAL_SIGNALS):
            if cleanup_error is not None:
                _add_cleanup_failure_note(exc, "initial transaction", cleanup_error)
            raise
        if isinstance(cleanup_error, _FATAL_SIGNALS):
            raise cleanup_error
        if not isinstance(exc, Exception):
            raise
        raise _DurableTransactionBindingError(
            f"Queue item {item.id!r} could not bind transaction {transaction.id!r}: {exc}"
        ) from exc
    item.transaction_id = transaction.id
    return transaction


def _bind_transaction_with_retries(
    queue: QueueProvider,
    item_id: str,
    transaction_id: str,
    *,
    claimed_by: str,
    log: logging.Logger,
    worker_id: str,
) -> Exception | None:
    """Bind a queue item to a transaction, retrying short-lived SQLite locks."""
    for attempt in range(_LEASE_RENEW_ATTEMPTS):
        try:
            queue.bind_transaction(item_id, transaction_id, claimed_by=claimed_by)
            return None
        except sqlite3.OperationalError as exc:
            if not is_transient_sqlite_lock(exc) or attempt == _LEASE_RENEW_ATTEMPTS - 1:
                return exc
            _sleep_before_sqlite_retry(
                attempt,
                delay_seconds=_LEASE_RETRY_DELAY_SECONDS,
                log=log,
                event="queue_bind_transaction_retry",
                operation="bind_transaction",
                queue_item_id=item_id,
                worker_id=worker_id,
                error=exc,
                max_attempts=_LEASE_RENEW_ATTEMPTS,
            )
        except MemoryError:
            raise
        except Exception as exc:
            return exc
    return None


def _seed_transaction_state_from_payload(
    transaction: Transaction,
    item: QueueItem,
    *,
    worker_id: str,
    log: logging.Logger,
) -> None:
    validate_json_object(item.payload, path="queue item payload")
    state_collisions = sorted(set(transaction.state).intersection(item.payload))
    if state_collisions:
        log.warning(
            "Queue item payload overwrote pre-existing transaction state keys",
            extra={
                "event": "queue_payload_state_collision",
                "queue_item_id": item.id,
                "queue_reference": item.reference,
                "transaction_id": transaction.id,
                "transaction_reference": transaction.reference,
                "state_keys": state_collisions,
                "worker_id": worker_id,
            },
        )
    transaction.state = {**transaction.state, **item.payload}


def _transaction_has_only_business_failures(transaction: Transaction | None) -> bool:
    """Return True when all failed skills ended with business exceptions."""
    if transaction is None:
        return False
    failed = transaction.failed_skills()
    return bool(failed) and all(
        skill.exceptions and isinstance(skill.exceptions[-1], BusinessException)
        for skill in failed
    )


def _strict_transaction_checkpoint(
    *,
    db_path: str,
    item: QueueItem,
    worker_id: str,
    log: logging.Logger,
    summary: QueueRunSummary,
) -> Callable[[Transaction], None]:
    """Return a checkpoint that raises when transaction persistence fails."""

    def checkpoint(transaction: Transaction) -> None:
        error = _save_transaction_with_retries(
            transaction,
            db_path=db_path,
            log=log,
            event="transaction_checkpoint_retry",
            queue_item_id=item.id,
            worker_id=worker_id,
        )
        if error is None:
            return
        summary.persistence_errors += 1
        log.error(
            "Transaction checkpoint failed",
            extra={
                "event": "transaction_checkpoint_error",
                "queue_item_id": item.id,
                "queue_reference": item.reference,
                "transaction_id": transaction.id,
                "transaction_reference": transaction.reference,
                "worker_id": worker_id,
            },
            exc_info=(type(error), error, error.__traceback__),
        )
        raise _CheckpointError(
            error,
            retry=_checkpoint_failure_allows_queue_retry(transaction, db_path=db_path),
        ) from error

    return checkpoint


def _checkpoint_failure_allows_queue_retry(transaction: Transaction, *, db_path: str) -> bool:
    """Return False once durable state proves retrying would duplicate terminal work."""
    try:
        durable = load_transaction(transaction.id, db_path)
    except Exception:
        if transaction.history and transaction.history[-1].event == HistoryEvent.SKILL_FAILED:
            return not _has_business_failed_skill(transaction)
        return True

    if not durable.history:
        return True
    last_event = durable.history[-1].event
    if last_event == HistoryEvent.SKILL_SUCCEEDED:
        return False
    if last_event == HistoryEvent.SKILL_FAILED:
        return not _has_business_failed_skill(durable)
    return True


def _has_business_failed_skill(transaction: Transaction) -> bool:
    return any(
        skill.exceptions and isinstance(skill.exceptions[-1], BusinessException)
        for skill in transaction.failed_skills()
    )


def _save_transaction_with_retries(
    transaction: Transaction,
    *,
    db_path: str,
    log: logging.Logger,
    event: str,
    queue_item_id: str,
    worker_id: str,
) -> Exception | None:
    """Save a transaction, retrying short-lived SQLite lock failures."""
    for attempt in range(_PERSISTENCE_SAVE_ATTEMPTS):
        try:
            save_transaction(transaction, db_path=db_path)
            return None
        except sqlite3.OperationalError as exc:
            if (
                not is_transient_sqlite_lock(exc)
                or attempt == _PERSISTENCE_SAVE_ATTEMPTS - 1
            ):
                return exc
            _sleep_before_sqlite_retry(
                attempt,
                delay_seconds=_PERSISTENCE_RETRY_DELAY_SECONDS,
                log=log,
                event=event,
                operation="save_transaction",
                queue_item_id=queue_item_id,
                worker_id=worker_id,
                error=exc,
                max_attempts=_PERSISTENCE_SAVE_ATTEMPTS,
            )
        except MemoryError:
            raise
        except Exception as exc:
            return exc
    return None


def _delete_transaction_with_retries(
    transaction_id: str,
    *,
    db_path: str,
    log: logging.Logger,
    queue_item_id: str,
    worker_id: str,
) -> Exception | None:
    """Delete a not-yet-bound transaction, retrying short-lived SQLite lock failures."""
    for attempt in range(_PERSISTENCE_SAVE_ATTEMPTS):
        try:
            _delete_unbound_pending_transaction(transaction_id, db_path=db_path)
            return None
        except sqlite3.OperationalError as exc:
            if (
                not is_transient_sqlite_lock(exc)
                or attempt == _PERSISTENCE_SAVE_ATTEMPTS - 1
            ):
                return exc
            _sleep_before_sqlite_retry(
                attempt,
                delay_seconds=_PERSISTENCE_RETRY_DELAY_SECONDS,
                log=log,
                event="transaction_initial_cleanup_retry",
                operation="delete_transaction",
                queue_item_id=queue_item_id,
                worker_id=worker_id,
                error=exc,
                max_attempts=_PERSISTENCE_SAVE_ATTEMPTS,
            )
        except MemoryError:
            raise
        except Exception as exc:
            return exc
    return None
