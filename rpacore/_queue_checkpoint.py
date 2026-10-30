"""Private queue claim/revision fencing and checkpoint transaction boundary."""

import sqlite3
from pathlib import Path

import rpacore.persistence as persistence
from rpacore._sqlite import QUEUE_SCHEMA_VERSION, SCHEMA_VERSION_TABLE
from rpacore.exceptions import ExecutionValidationError
from rpacore.persistence import TransactionFenceError
from rpacore.transaction import Transaction


def _qualified_queue_schema_version(conn: sqlite3.Connection, schema: str) -> int:
    table = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master "
        "WHERE type = 'table' AND name = ?",
        (SCHEMA_VERSION_TABLE,),
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute(
        f"SELECT version FROM {schema}.{SCHEMA_VERSION_TABLE} WHERE component = 'queue'"
    ).fetchone()
    return 0 if row is None else int(row["version"])


def _save_queue_transaction_fenced(
    transaction: Transaction,
    *,
    db_path: str,
    queue_db_path: str,
    queue_item_id: str,
    claimed_by: str,
    claim_token: str,
    expected_revision: int,
    initial: bool = False,
) -> int:
    """Save a fenced snapshot, creating its queue binding on initialization."""
    if not claim_token:
        raise TransactionFenceError(
            f"Queue item {queue_item_id!r} has no claim credential"
        )
    if expected_revision < 0:
        raise ValueError(f"expected_revision must be >= 0, got {expected_revision}")
    if initial and expected_revision != 0:
        raise ValueError("initial queue persistence requires revision zero")
    if initial and (not isinstance(transaction.id, str) or not transaction.id.strip()):
        raise ExecutionValidationError("transaction.id must be a nonblank str for initial queue persistence")
    state_json, metadata_json, artifact_rows = persistence._serialized_transaction(transaction)

    transaction_path = Path(db_path).resolve()
    queue_path = Path(queue_db_path).resolve()
    conn = persistence._connect(str(transaction_path))
    transaction_started = False
    try:
        persistence._ensure_schema(conn)
        transaction_journal_mode = str(
            conn.execute("PRAGMA main.journal_mode").fetchone()[0]
        ).lower()
        if transaction_journal_mode != "delete":
            raise RuntimeError(
                "Queue claim fencing requires transaction rollback journal mode; "
                f"got {transaction_journal_mode!r}"
            )
        queue_schema = "main"
        if queue_path != transaction_path:
            conn.execute("ATTACH DATABASE ? AS queue_guard", (str(queue_path),))
            queue_schema = "queue_guard"

        queue_version = _qualified_queue_schema_version(conn, queue_schema)
        if queue_version != QUEUE_SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported queue schema version {queue_version}; "
                f"expected {QUEUE_SCHEMA_VERSION}; migrate queue and transaction databases offline"
            )
        journal_mode = str(
            conn.execute(f"PRAGMA {queue_schema}.journal_mode").fetchone()[0]
        ).lower()
        if journal_mode != "delete":
            raise RuntimeError(
                f"Queue claim fencing requires rollback journal mode; got {journal_mode!r}"
            )

        conn.execute("BEGIN IMMEDIATE")
        transaction_started = True
        claim = conn.execute(
            f"SELECT 1 FROM {queue_schema}.queue_items "
            "WHERE id = ? AND status = 'in_progress' AND claimed_by = ? "
            "AND claim_token = ? AND claim_token != '' "
            "AND (transaction_id = '' OR transaction_id = ?)",
            (queue_item_id, claimed_by, claim_token, transaction.id),
        ).fetchone()
        if claim is None:
            raise TransactionFenceError(
                f"Queue item {queue_item_id!r} claim is stale or bound to another transaction"
            )
        if initial and conn.execute(
            "SELECT 1 FROM transactions WHERE id = ?", (transaction.id,)
        ).fetchone() is not None:
            raise ExecutionValidationError(
                f"Initial queue transaction {transaction.id!r} already exists; use an unused ID"
            )
        new_revision = persistence._write_transaction_rows(
            conn,
            transaction,
            state_json=state_json,
            metadata_json=metadata_json,
            artifact_rows=artifact_rows,
            expected_revision=expected_revision,
            queue_item_id=queue_item_id,
            claim_token=claim_token,
        )
        if initial:
            result = conn.execute(
                f"UPDATE {queue_schema}.queue_items SET transaction_id = ? "
                "WHERE id = ? AND status = 'in_progress' AND claimed_by = ? "
                "AND claim_token = ? AND claim_token != '' "
                "AND (transaction_id = '' OR transaction_id = ?)",
                (transaction.id, queue_item_id, claimed_by, claim_token, transaction.id),
            )
            if result.rowcount != 1:
                raise TransactionFenceError(
                    f"Queue item {queue_item_id!r} claim is stale or bound to another transaction"
                )
        conn.execute("COMMIT")
        transaction_started = False
        return new_revision
    except Exception:
        if transaction_started:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
