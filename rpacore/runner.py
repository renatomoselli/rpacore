"""Runner — queue-driven execution loop."""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable

from rpacore.context import ProcessContext
from rpacore.credentials import CredentialProvider
from rpacore.exceptions import BusinessException
from rpacore.engine import Engine
from rpacore.logger import get_logger
from rpacore.notify import Notifier, dispatch
from rpacore.queue import QueueItem, QueueProvider
from rpacore.report import generate_report
from rpacore.status import Status
from rpacore.transaction import Transaction


@dataclass
class QueueRunSummary:
    """Counts from a completed run_queue_loop() call."""

    processed: int = field(default=0)
    completed: int = field(default=0)
    failed: int = field(default=0)
    callback_errors: int = field(default=0)


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
) -> QueueRunSummary:
    """Drain a queue by running each item through the engine.

    Claims items one at a time until the queue is empty. For each item:
    - Calls build_transaction(item) to construct a Transaction (user responsibility).
    - Builds a ProcessContext with the item's payload in ctx.data.
    - Runs engine.run(ctx).
        - Generates a report and dispatches notifiers after engine.run(ctx).
    - Calls queue.complete() if transaction.status is SUCCESSFUL.
    - Calls queue.fail() with retry=False for business-only transaction failures
      unless retry_business_failures=True.
    - Calls queue.fail() with retry=True for system failures and unexpected errors.
    - Also calls queue.fail() if build_transaction() or any unexpected error raises.
    - Calls after_item(item, transaction, error) once per item regardless of outcome.
            transaction is None if build_transaction() raised; error is None on normal paths
            and set for unexpected processing or post-processing failures.
      Fires before the final queue state transition (complete/fail). If the callback
      raises on a success-path item, the item is still marked complete (the automation
      ran) but the error is counted in QueueRunSummary.callback_errors. If the callback
      raises on a failure-path item, queue.fail() is called as normal. The loop always
      continues regardless.

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

    Returns:
        QueueRunSummary with counts of processed, completed, and failed items.
    """
    log = logger if logger is not None else get_logger()
    if not worker_id:
        worker_id = socket.gethostname()
    _notifiers: list[Notifier] = notifiers if notifiers is not None else []
    summary = QueueRunSummary()

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
        originally_intended_complete = False
        callback_failed = False

        log.info(
            "Processing queue item",
            extra={"event": "queue_item_start", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
        )
        try:
            transaction = build_transaction(item)
            ctx = ProcessContext(
                transaction=transaction,
                config=config,
                data=dict(item.payload),
                credentials=credentials,
            )
            engine.run(ctx)
            originally_intended_complete = ctx.transaction.status == Status.SUCCESSFUL
        except Exception as exc:
            error = exc
            log.exception(
                "Unexpected error processing queue item",
                extra={"event": "queue_item_error", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
            )

        if ctx is not None:
            try:
                report = generate_report(ctx.transaction)
                dispatch(_notifiers, report, logger=log)
            except Exception as exc:
                if error is None:
                    error = exc
                log.exception(
                    "Post-run reporting failed; preserving queue outcome",
                    extra={"event": "queue_item_postprocess_error", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
                )

        if after_item is not None:
            try:
                after_item(item, transaction, error)
            except Exception:
                callback_failed = True
                log.exception(
                    "after_item callback raised during post-processing",
                    extra={"event": "after_item_error", "queue_item_id": item.id, "worker_id": worker_id},
                )

        if originally_intended_complete:
            queue.complete(item.id)
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
            queue.fail(item.id, retry=retry)
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
