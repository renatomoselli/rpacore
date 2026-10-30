"""Private transaction row capture and decoding, with no connection ownership."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from rpacore._json_state import JsonStateError, validate_json_object
from rpacore.exceptions import BusinessException, SystemException
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.step import Step
from rpacore.status import Status
from rpacore.transaction import Artifact, HistoryEntry, HistoryEvent, Transaction


@dataclass(frozen=True)
class _TransactionRows:
    header: sqlite3.Row
    steps: list[tuple[sqlite3.Row, list[sqlite3.Row]]]
    history: list[sqlite3.Row]
    metadata: list[sqlite3.Row]
    artifacts: list[sqlite3.Row]


def _read_transaction_rows(conn: sqlite3.Connection, transaction_id: str) -> _TransactionRows:
    """Capture raw rows on the caller's read snapshot; do not decode under lock."""
    row = conn.execute(
        "SELECT id, reference, status, retry_count, outcome_category, retry_disposition, "
        "failure_code, created_at, started_at, finished_at, state, definition_identity, revision "
        "FROM transactions WHERE id = ?",
        (transaction_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Transaction not found: {transaction_id!r}")
    step_rows = conn.execute(
        "SELECT id, name, execution_order, status, arguments FROM steps "
        "WHERE transaction_id = ? ORDER BY execution_order",
        (transaction_id,),
    ).fetchall()
    steps = []
    for step in step_rows:
        exceptions = conn.execute(
            "SELECT exception_type, message, action, retry_number, occurred_at, "
            "screenshot_path, halts_remaining_steps, code FROM exceptions WHERE step_id = ? ORDER BY id",
            (step["id"],),
        ).fetchall()
        steps.append((step, exceptions))
    history = conn.execute(
        "SELECT sequence, timestamp, event, status, retry_number, "
        "step_name, step_execution_order FROM transaction_history "
        "WHERE transaction_id = ? ORDER BY sequence",
        (transaction_id,),
    ).fetchall()
    metadata = conn.execute(
        "SELECT key, value_json FROM transaction_metadata "
        "WHERE transaction_id = ? ORDER BY key",
        (transaction_id,),
    ).fetchall()
    artifacts = conn.execute(
        "SELECT id, name, path, kind, created_at, metadata FROM transaction_artifacts "
        "WHERE transaction_id = ? ORDER BY sequence",
        (transaction_id,),
    ).fetchall()
    return _TransactionRows(row, steps, history, metadata, artifacts)


def _transaction_from_rows(rows: _TransactionRows) -> Transaction:
    """Decode a captured record after its connection and snapshot have closed."""
    row = rows.header
    transaction_id = row["id"]
    transaction_state = _load_transaction_state(row)

    steps: list[Step] = []
    for sr, exc_rows in rows.steps:
        step = Step(
            sr["name"],
            sr["execution_order"],
            arguments=_load_step_arguments(transaction_id, sr),
        )
        step.status = Status(sr["status"])

        for er in exc_rows:
            dt = datetime.fromisoformat(er["occurred_at"])
            screenshot = er["screenshot_path"]
            if er["exception_type"] == "business":
                exc: BusinessException | SystemException = BusinessException(
                    er["message"],
                    action=er["action"],
                    retry_number=er["retry_number"],
                    occurred_at=dt,
                    screenshot_path=screenshot,
                    halts_remaining_steps=bool(er["halts_remaining_steps"]),
                    code=er["code"],
                )
            else:
                exc = SystemException(
                    er["message"],
                    action=er["action"],
                    retry_number=er["retry_number"],
                    occurred_at=dt,
                    screenshot_path=screenshot,
                    code=er["code"],
                )
            step.exceptions.append(exc)
        steps.append(step)

    tx_status = Status(row["status"])
    history = _load_history(transaction_id, rows.history)
    metadata = _load_metadata(transaction_id, rows.metadata)
    artifacts = _load_artifacts(transaction_id, rows.artifacts)
    return Transaction(
        reference=row["reference"],
        id=row["id"],
        status=tx_status,
        retry_count=row["retry_count"],
        outcome_category=OutcomeCategory(row["outcome_category"]),
        retry_disposition=RetryDisposition(row["retry_disposition"]),
        failure_code=row["failure_code"],
        created_at=_timestamp_from_storage(
            row["created_at"], path="transactions.created_at", transaction_id=transaction_id
        ),
        started_at=_timestamp_from_storage(
            row["started_at"], path="transactions.started_at", transaction_id=transaction_id
        ),
        finished_at=_timestamp_from_storage(
            row["finished_at"], path="transactions.finished_at", transaction_id=transaction_id
        ),
        state=transaction_state,
        metadata=metadata,
        artifacts=artifacts,
        steps=steps,
        history=history,
        definition_identity=row["definition_identity"],
    )


def _load_transaction_state(row: sqlite3.Row) -> dict[str, object]:
    try:
        state = json.loads(row["state"])
        validate_json_object(state, path="transaction.state")
    except (json.JSONDecodeError, JsonStateError) as exc:
        raise SystemException(
            f"Persisted transaction state is invalid for transaction {row['id']!r}: {exc}",
            action="repair transaction state in the persistence database",
        ) from exc
    return state


def _load_step_arguments(
    transaction_id: str,
    row: sqlite3.Row,
) -> dict[str, object]:
    """Load one Step argument mapping or raise actionable repair guidance."""
    try:
        arguments = json.loads(row["arguments"])
        validate_json_object(
            arguments,
            path=f"transaction.steps[{row['name']!r}].arguments",
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise SystemException(
            f"Persisted step arguments are invalid for transaction "
            f"{transaction_id!r} step {row['name']!r}: {exc}",
            action="repair step arguments in the persistence database",
        ) from exc
    return arguments


def _load_metadata(transaction_id: str, rows: list[sqlite3.Row]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for row in rows:
        key = row["key"]
        try:
            value = json.loads(row["value_json"])
            validate_json_object({key: value}, path="transaction.metadata")
        except (json.JSONDecodeError, JsonStateError) as exc:
            raise SystemException(
                f"Persisted transaction metadata is invalid for transaction {transaction_id!r} "
                f"at key {key!r}: {exc}",
                action="repair transaction metadata in the persistence database",
            ) from exc
        metadata[key] = value
    return metadata


def _load_artifact_metadata(
    transaction_id: str,
    artifact_id: str,
    raw_metadata: str,
) -> dict[str, object]:
    try:
        metadata = json.loads(raw_metadata)
        validate_json_object(metadata, path="artifact.metadata")
    except (json.JSONDecodeError, JsonStateError) as exc:
        raise SystemException(
            f"Persisted transaction artifact metadata is invalid for transaction "
            f"{transaction_id!r} artifact {artifact_id!r}: {exc}",
            action="repair transaction artifact metadata in the persistence database",
        ) from exc
    return metadata


def _load_artifacts(transaction_id: str, rows: list[sqlite3.Row]) -> list[Artifact]:
    artifacts: list[Artifact] = []
    for row in rows:
        artifact_id = row["id"]
        try:
            created_at = datetime.fromisoformat(row["created_at"])
        except ValueError as exc:
            raise SystemException(
                f"Persisted transaction artifact timestamp is invalid for transaction "
                f"{transaction_id!r} artifact {artifact_id!r}: {row['created_at']!r}",
                action="repair transaction artifacts in the persistence database",
            ) from exc
        artifacts.append(
            Artifact(
                id=artifact_id,
                name=row["name"],
                path=row["path"],
                kind=row["kind"],
                created_at=created_at,
                metadata=_load_artifact_metadata(
                    transaction_id,
                    artifact_id,
                    row["metadata"],
                ),
            )
        )
    return artifacts


def _timestamp_from_storage(value: str | None, *, path: str, transaction_id: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemException(
            f"Persisted transaction timestamp is invalid for transaction {transaction_id!r} "
            f"at {path}: {value!r}",
            action="repair transaction timestamps in the persistence database",
        ) from exc


def _load_history(transaction_id: str, rows: list[sqlite3.Row]) -> list[HistoryEntry]:
    history: list[HistoryEntry] = []
    for row in rows:
        try:
            history.append(
                HistoryEntry(
                    sequence=row["sequence"],
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    event=HistoryEvent(row["event"]),
                    status=Status(row["status"]),
                    retry_number=row["retry_number"],
                    step_name=row["step_name"],
                    step_execution_order=row["step_execution_order"],
                )
            )
        except (TypeError, ValueError) as exc:
            raise SystemException(
                f"Persisted transaction history is invalid for transaction {transaction_id!r} "
                f"at sequence {row['sequence']!r}: {exc}",
                action="repair transaction history in the persistence database",
            ) from exc
    return history
