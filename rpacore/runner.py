"""Runner — queue-driven execution loop."""

from __future__ import annotations

import logging
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from rpacore._validation import type_error
from rpacore.context import ProcessContext
from rpacore.credentials import CredentialProvider
from rpacore.exceptions import BusinessException
from rpacore.engine import Engine
from rpacore.logger import get_logger
from rpacore.notify import Notifier, dispatch
from rpacore.persistence import save_transaction
from rpacore.queue import QueueItem, QueueProvider
from rpacore.report import generate_report
from rpacore.status import Status
from rpacore.transaction import Transaction


_PERSISTENCE_SAVE_ATTEMPTS = 3
_PERSISTENCE_RETRY_DELAY_SECONDS = 0.05


@dataclass
class QueueRunSummary:
    """Counts from a completed run_queue_loop() call."""

    processed: int = field(default=0)
    completed: int = field(default=0)
    failed: int = field(default=0)
    callback_errors: int = field(default=0)
    persistence_errors: int = field(default=0)
    lifecycle_errors: int = field(default=0)


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
    on_start: Callable[[dict[str, object]], dict[str, object] | None] | None = None,
    on_finish: Callable[[QueueRunSummary], None] | None = None,
) -> QueueRunSummary:
    """Drain a queue by running each item through the engine.

    Claims items one at a time until the queue is empty. For each item:
    - Calls build_transaction(item) to construct a Transaction (user responsibility).
    - Builds a ProcessContext with the item's payload in ctx.data.
    - Runs engine.run(ctx).
        - Saves the transaction when transaction_db_path is set.
        - Generates a report and dispatches notifiers after engine.run(ctx).
    - Calls queue.complete() if transaction.status is SUCCESSFUL.
    - Calls queue.fail() with retry=False for business-only transaction failures
      unless retry_business_failures=True.
    - Calls queue.fail() with retry=True for system failures and unexpected errors.
    - Also calls queue.fail() if build_transaction() or any unexpected error raises.
    - Calls after_item(item, transaction, error) once per item regardless of outcome.
            transaction is None if build_transaction() raised; error is None on normal paths
            and set for unexpected processing, reporting, or transaction persistence failures.
      Fires before the final queue state transition (complete/fail). If the callback
      raises on a success-path item, the item is still marked complete (the automation
      ran) but the error is counted in QueueRunSummary.callback_errors. If the callback
      raises on a failure-path item, queue.fail() is called as normal. The loop always
      continues regardless.
    - Calls on_start(config) before claiming any items. Returned data is shallow-copied
      into every item context, with item payload values taking precedence. Top-level
      keys are isolated per item; nested values and resources retain shared identity.
    - Calls on_finish(summary) exactly once after the loop exits. Exceptions are logged
      and swallowed so cleanup cannot change the run outcome.

    Args:
        queue:             The queue to drain.
        engine:            Configured Engine instance.
        build_transaction: User-supplied callable mapping a QueueItem to a Transaction.
        config:            Framework config dict (passed to ProcessContext).
        credentials:       Credential provider (passed to ProcessContext).
        worker_id:         Worker identifier passed to queue.next_item(). Defaults to hostname.
        notifiers:         Optional list of notifiers to call after each transaction. Defaults to [].
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
                           Optional SQLite transaction database path. When set, each
                           transaction is saved after engine.run(ctx) completes and before
                           callbacks run. Persistence is best-effort: if the process exits
                           between engine completion and save, the queue remains the source
                           of truth and may retry the item.
        on_start:          Optional callback fired before claiming items. Receives config
                           and may return shared data shallow-copied into every item
                           context. Nested values and resources remain shared.
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
        shared_data: dict[str, object] = {}
        if on_start is not None:
            start_data = on_start(config)
            if start_data is not None:
                if not isinstance(start_data, dict):
                    raise type_error("on_start return", "dict | None", start_data)
                shared_data = dict(start_data)

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
            shared_data=shared_data,
            summary=summary,
        )
    finally:
        if on_finish is not None:
            try:
                on_finish(summary)
            except MemoryError:
                raise
            except Exception:
                summary.lifecycle_errors += 1
                log.exception(
                    "on_finish callback raised during lifecycle cleanup",
                    extra={"event": "on_finish_error", "worker_id": worker_id},
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
    shared_data: dict[str, object],
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
        transaction: Transaction | None = None
        ctx: ProcessContext | None = None
        error: Exception | None = None
        persistence_error: Exception | None = None
        originally_intended_complete = False
        callback_failed = False

        log.info(
            "Processing queue item",
            extra={"event": "queue_item_start", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
        )
        try:
            transaction = build_transaction(item)
            item_data = dict(shared_data)
            item_data.update(item.payload)
            ctx = ProcessContext(
                transaction=transaction,
                config=config,
                data=item_data,
                credentials=credentials,
            )
            engine.run(ctx)
            originally_intended_complete = ctx.transaction.status == Status.SUCCESSFUL
            if transaction_db_path is not None:
                persistence_error = _save_transaction_with_retries(
                    ctx.transaction,
                    db_path=transaction_db_path,
                )
                if persistence_error is not None:
                    summary.persistence_errors += 1
                    log.error(
                        "Transaction persistence failed; preserving queue outcome",
                        extra={
                            "event": "transaction_persistence_error",
                            "queue_item_id": item.id,
                            "queue_reference": item.reference,
                            "transaction_id": ctx.transaction.id,
                            "transaction_reference": ctx.transaction.reference,
                            "worker_id": worker_id,
                        },
                        exc_info=(type(persistence_error), persistence_error, persistence_error.__traceback__),
                    )
        except MemoryError:
            raise
        except Exception as exc:
            error = exc
            log.exception(
                "Unexpected error processing queue item",
                extra={"event": "queue_item_error", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
            )

        if ctx is not None:
            try:
                report = generate_report(ctx.transaction)
                dispatch(notifiers, report, logger=log)
            except MemoryError:
                raise
            except Exception as exc:
                if error is None:
                    error = exc
                log.exception(
                    "Post-run reporting failed; preserving queue outcome",
                    extra={"event": "queue_item_postprocess_error", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
                )

        if after_item is not None:
            try:
                after_item(item, transaction, error if error is not None else persistence_error)
            except MemoryError:
                raise
            except Exception:
                callback_failed = True
                log.exception(
                    "after_item callback raised during post-processing",
                    extra={"event": "after_item_error", "queue_item_id": item.id, "worker_id": worker_id},
                )

        if originally_intended_complete:
            queue.complete(item.id, claimed_by=item.claimed_by)
            summary.completed += 1
            if callback_failed:
                summary.callback_errors += 1
            log.info(
                "Completed queue item",
                extra={"event": "queue_item_complete", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
            )
        else:
            retry = (
                retry_business_failures
                or error is not None
                or not _transaction_has_only_business_failures(transaction)
            )
            queue.fail(item.id, retry=retry, claimed_by=item.claimed_by)
            summary.failed += 1
            if not error and not callback_failed and ctx is not None:
                log.warning(
                    "Queue item failed (transaction status: %s)",
                    ctx.transaction.status,
                    extra={"event": "queue_item_fail", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
                )

    return summary


def _transaction_has_only_business_failures(transaction: Transaction | None) -> bool:
    """Return True when all failed skills ended with business exceptions."""
    if transaction is None:
        return False
    failed = transaction.failed_skills()
    return bool(failed) and all(
        skill.exceptions and isinstance(skill.exceptions[-1], BusinessException)
        for skill in failed
    )


def _save_transaction_with_retries(transaction: Transaction, *, db_path: str) -> Exception | None:
    """Save a transaction, retrying short-lived SQLite lock failures."""
    for attempt in range(_PERSISTENCE_SAVE_ATTEMPTS):
        try:
            save_transaction(transaction, db_path=db_path)
            return None
        except sqlite3.OperationalError as exc:
            if attempt == _PERSISTENCE_SAVE_ATTEMPTS - 1:
                return exc
            time.sleep(_PERSISTENCE_RETRY_DELAY_SECONDS * (2 ** attempt))
        except MemoryError:
            raise
        except Exception as exc:
            return exc
    return None
