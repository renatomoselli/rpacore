"""Canonical transaction serialization for machine-readable surfaces."""

from __future__ import annotations

from datetime import datetime, timezone

from rpacore._json_state import validate_json_object
from rpacore.exceptions import BusinessException
from rpacore.skill import Skill
from rpacore.transaction import Artifact, HistoryEntry, Transaction


TRANSACTION_FORMAT_VERSION = 1


def serialize_transaction(transaction: Transaction) -> dict[str, object]:
    """Return the canonical JSON-safe transaction record."""
    validate_json_object(transaction.state, path="transaction.state")
    validate_json_object(transaction.metadata, path="transaction.metadata")
    return {
        "transaction_format_version": TRANSACTION_FORMAT_VERSION,
        "id": transaction.id,
        "reference": transaction.reference,
        "status": str(transaction.status),
        "retry_count": transaction.retry_count,
        "created_at": _timestamp(transaction.created_at, path="transaction.created_at"),
        "started_at": _timestamp(transaction.started_at, path="transaction.started_at"),
        "finished_at": _timestamp(transaction.finished_at, path="transaction.finished_at"),
        "state": _copy_json_value(transaction.state),
        "metadata": _copy_json_value(transaction.metadata),
        "skills": [_skill_record(skill) for skill in transaction.ordered_skills()],
        "history": [_history_record(entry) for entry in transaction.history],
        "artifacts": [
            _artifact_record(artifact, index)
            for index, artifact in enumerate(transaction.artifacts)
        ],
    }


def _skill_record(skill: Skill) -> dict[str, object]:
    validate_json_object(skill.arguments, path=f"transaction.skills[{skill.name!r}].arguments")
    return {
        "name": skill.name,
        "execution_order": skill.execution_order,
        "status": str(skill.status),
        "arguments": _copy_json_value(skill.arguments),
        "exceptions": [_exception_record(exc, skill) for exc in skill.exceptions],
    }


def _exception_record(exc: BaseException, skill: Skill) -> dict[str, object]:
    return {
        "type": "business" if isinstance(exc, BusinessException) else "system",
        "message": str(exc),
        "action": str(getattr(exc, "action", "")),
        "retry_number": int(getattr(exc, "retry_number", 0)),
        "datetime_occurred": _timestamp(
            getattr(exc, "datetime_occurred", None),
            path=f"transaction.skills[{skill.name!r}].exceptions.datetime_occurred",
        ),
        "screenshot_path": str(getattr(exc, "screenshot_path", "")),
        "stops_execution": bool(getattr(exc, "stops_execution", True)),
    }


def _history_record(entry: HistoryEntry) -> dict[str, object]:
    return {
        "sequence": entry.sequence,
        "timestamp": _timestamp(entry.timestamp, path=f"transaction.history[{entry.sequence}].timestamp"),
        "event": str(entry.event),
        "status": str(entry.status),
        "retry_number": entry.retry_number,
        "skill_name": entry.skill_name,
        "skill_execution_order": entry.skill_execution_order,
    }


def _artifact_record(artifact: Artifact, index: int) -> dict[str, object]:
    validate_json_object(
        artifact.metadata,
        path=f"transaction.artifacts[{index}].metadata",
    )
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
