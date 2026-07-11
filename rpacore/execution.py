"""Convenience helpers for explicit transaction execution."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from contextlib import AbstractContextManager
from typing import Callable

from rpacore._sqlite_retry import is_transient_sqlite_lock, sqlite_retry_delay
from rpacore._validation import type_error
from rpacore.context import ProcessContext
from rpacore.credentials import CredentialProvider, EnvCredentialProvider
from rpacore.engine import Engine
from rpacore.persistence import save_transaction
from rpacore.transaction import Transaction

_SQLITE_CHECKPOINT_ATTEMPTS = 3
_SQLITE_CHECKPOINT_RETRY_DELAY_SECONDS = 0.05


def execute_transaction(
    transaction: Transaction,
    *,
    config: dict[str, object] | None = None,
    credentials: CredentialProvider | None = None,
    engine: Engine | None = None,
    checkpoint: Callable[[Transaction], None] | None = None,
    transaction_db_path: str | os.PathLike[str] | None = None,
    resource_scope: AbstractContextManager[dict[str, object] | None] | None = None,
) -> None:
    """Run one transaction with optional strict persistence and runtime resources.

    Pass ``transaction_db_path`` for the common SQLite checkpoint path, or pass
    ``checkpoint`` for custom persistence. Supplying both is rejected so the
    persistence boundary stays explicit. A resource scope may suppress execution
    exceptions by returning a truthy value from ``__exit__``; in that case the
    transaction status still records the engine outcome.
    """
    if checkpoint is not None and transaction_db_path is not None:
        raise ValueError("checkpoint and transaction_db_path are mutually exclusive")

    runner = engine if engine is not None else Engine()
    run_config = {} if config is None else config
    run_credentials = credentials if credentials is not None else EnvCredentialProvider()
    resolved_checkpoint = checkpoint
    if transaction_db_path is not None:
        db_path = str(transaction_db_path)
        resolved_checkpoint = lambda tx: _save_transaction_with_retries(
            tx,
            db_path=db_path,
            logger=runner.logger,
        )

    if resource_scope is None:
        ctx = ProcessContext(
            transaction=transaction,
            config=run_config,
            resources={},
            credentials=run_credentials,
        )
        runner.run(ctx, checkpoint=resolved_checkpoint)
        return

    scope_resources = resource_scope.__enter__()
    try:
        if scope_resources is None:
            resources: dict[str, object] = {}
        elif isinstance(scope_resources, dict):
            resources = dict(scope_resources)
        else:
            raise type_error("resource_scope yield", "dict | None", scope_resources)
        ctx = ProcessContext(
            transaction=transaction,
            config=run_config,
            resources=resources,
            credentials=run_credentials,
        )
        runner.run(ctx, checkpoint=resolved_checkpoint)
    except BaseException as exc:
        exc_info = sys.exc_info()
        try:
            suppress = resource_scope.__exit__(*exc_info)
        except BaseException as cleanup_exc:
            raise cleanup_exc from exc
        if not suppress:
            raise
    else:
        resource_scope.__exit__(None, None, None)


def _save_transaction_with_retries(
    transaction: Transaction,
    *,
    db_path: str,
    logger: logging.Logger,
) -> None:
    for attempt in range(_SQLITE_CHECKPOINT_ATTEMPTS):
        try:
            save_transaction(transaction, db_path=db_path)
            return
        except MemoryError:
            raise
        except sqlite3.OperationalError as exc:
            if (
                not is_transient_sqlite_lock(exc)
                or attempt == _SQLITE_CHECKPOINT_ATTEMPTS - 1
            ):
                raise
            delay_seconds = sqlite_retry_delay(
                attempt,
                base_delay_seconds=_SQLITE_CHECKPOINT_RETRY_DELAY_SECONDS,
            )
            logger.warning(
                "Transaction checkpoint hit transient SQLite lock; retrying",
                extra={
                    "event": "transaction_checkpoint_retry",
                    "transaction_id": transaction.id,
                    "transaction_reference": transaction.reference,
                    "db_path": db_path,
                    "attempt": attempt + 1,
                    "max_attempts": _SQLITE_CHECKPOINT_ATTEMPTS,
                    "delay_seconds": delay_seconds,
                    "error": str(exc),
                },
            )
            time.sleep(delay_seconds)
