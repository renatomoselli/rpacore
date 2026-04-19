"""Runner — queue-driven execution loop."""

from __future__ import annotations

import logging
import socket
from typing import Callable

from oref.context import ProcessContext
from oref.credentials import CredentialProvider
from oref.engine import Engine
from oref.logger import get_logger
from oref.queue import QueueItem, QueueProvider
from oref.status import Status
from oref.transaction import Transaction


def run_queue_loop(
    queue: QueueProvider,
    engine: Engine,
    build_transaction: Callable[[QueueItem], Transaction],
    config: dict[str, object],
    credentials: CredentialProvider,
    *,
    worker_id: str = "",
    logger: logging.Logger | None = None,
) -> None:
    """Drain a queue by running each item through the engine.

    Claims items one at a time until the queue is empty. For each item:
    - Calls build_transaction(item) to construct a Transaction (user responsibility).
    - Builds a ProcessContext with the item's payload in ctx.data.
    - Runs engine.run(ctx).
    - Calls queue.complete() if transaction.status is SUCCESSFUL, queue.fail() otherwise.
    - Also calls queue.fail() if build_transaction() or any unexpected error raises.

    Args:
        queue:             The queue to drain.
        engine:            Configured Engine instance.
        build_transaction: User-supplied callable mapping a QueueItem to a Transaction.
        config:            Framework config dict (passed to ProcessContext).
        credentials:       Credential provider (passed to ProcessContext).
        worker_id:         Worker identifier passed to queue.next_item(). Defaults to hostname.
        logger:            Optional logger. Defaults to the OREF logger.
    """
    log = logger if logger is not None else get_logger()
    if not worker_id:
        worker_id = socket.gethostname()

    while True:
        item = queue.next_item(worker_id)
        if item is None:
            break

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

            if ctx.transaction.status == Status.SUCCESSFUL:
                queue.complete(item.id)
                log.info(
                    "Completed queue item",
                    extra={"event": "queue_item_complete", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
                )
            else:
                queue.fail(item.id)
                log.warning(
                    "Queue item failed (transaction status: %s)",
                    ctx.transaction.status,
                    extra={"event": "queue_item_fail", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
                )
        except Exception:
            queue.fail(item.id)
            log.exception(
                "Unexpected error processing queue item",
                extra={"event": "queue_item_error", "queue_item_id": item.id, "queue_reference": item.reference, "worker_id": worker_id},
            )
