"""Canonical transaction serialization for machine-readable surfaces."""

from __future__ import annotations

from datetime import datetime, timezone

from rpacore.exceptions import BusinessException
from rpacore.step import Step
from rpacore.transaction import Artifact, HistoryEntry, Transaction


TRANSACTION_FORMAT_VERSION = 3


def serialize_transaction(transaction: Transaction) -> dict[str, object]:
    """Return a canonical record after durable-data, but not wiring, validation."""
    transaction.validate_durable_data()
    return {
        "transaction_format_version": TRANSACTION_FORMAT_VERSION,
        "id": transaction.id,
        "reference": transaction.reference,
        "definition_identity": transaction.definition_identity,
        "status": str(transaction.status),
        "retry_count": transaction.retry_count,
        "created_at": _timestamp(transaction.created_at, path="transaction.created_at"),
        "started_at": _timestamp(transaction.started_at, path="transaction.started_at"),
        "finished_at": _timestamp(transaction.finished_at, path="transaction.finished_at"),
        "state": _copy_json_value(transaction.state),
        "metadata": _copy_json_value(transaction.metadata),
        "steps": [_step_record(step) for step in transaction.ordered_steps()],
        "history": [_history_record(entry) for entry in transaction.history],
        "artifacts": [
            _artifact_record(artifact, index)
            for index, artifact in enumerate(transaction.artifacts)
        ],
    }


def _step_record(step: Step) -> dict[str, object]:
    return {
        "name": step.name,
        "execution_order": step.execution_order,
        "status": str(step.status),
        "arguments": _copy_json_value(step.arguments),
        "exceptions": [_exception_record(exc, step) for exc in step.exceptions],
    }


def _exception_record(exc: BaseException, step: Step) -> dict[str, object]:
    return {
        "type": "business" if isinstance(exc, BusinessException) else "system",
        "message": str(exc),
        "action": str(getattr(exc, "action", "")),
        "retry_number": int(getattr(exc, "retry_number", 0)),
        "occurred_at": _timestamp(
            getattr(exc, "occurred_at", None),
            path=f"transaction.steps[{step.name!r}].exceptions.occurred_at",
        ),
        "screenshot_path": str(getattr(exc, "screenshot_path", "")),
        "halts_remaining_steps": bool(getattr(exc, "halts_remaining_steps", True)),
    }


def _history_record(entry: HistoryEntry) -> dict[str, object]:
    return {
        "sequence": entry.sequence,
        "timestamp": _timestamp(entry.timestamp, path=f"transaction.history[{entry.sequence}].timestamp"),
        "event": str(entry.event),
        "status": str(entry.status),
        "retry_number": entry.retry_number,
        "step_name": entry.step_name,
        "step_execution_order": entry.step_execution_order,
    }


def _artifact_record(artifact: Artifact, index: int) -> dict[str, object]:
    return {
        "id": artifact.id,
        "name": artifact.name,
        "path": artifact.path,
        "kind": artifact.kind,
        "created_at": _timestamp(
            artifact.created_at,
            path=f"transaction.artifacts[{index}].created_at",
        ),
        "metadata": _copy_json_value(artifact.metadata),
    }


def _copy_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _copy_json_value(item) for key, item in value.items()}
    raise TypeError(
        f"expected validated JSON value; got {type(value).__name__} value={value!r}"
    )


def _timestamp(value: object, *, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{path} expected datetime | None; got {type(value).__name__} value={value!r}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
