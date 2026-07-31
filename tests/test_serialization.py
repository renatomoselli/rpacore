"""Tests for canonical transaction serialization."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from rpacore import (
    Artifact,
    BusinessException,
    HistoryEvent,
    ProcessContext,
    Skill,
    Status,
    SystemException,
    Transaction,
    serialize_transaction,
)
from rpacore._json_state import JsonStateError


def test_serialize_transaction_returns_canonical_json_safe_record() -> None:
    skill = Skill("download", 1, arguments={"invoice": "001"})
    skill.status = Status.FAILED
    skill.exceptions.append(BusinessException("bad invoice", action="download"))
    skill.exceptions.append(SystemException("timeout", action="download", retry_number=1))
    tx = Transaction(
        id="tx-001",
        reference="invoice-001",
        status=Status.FAILED,
        retry_count=1,
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        started_at=datetime(2026, 6, 10, 12, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 6, 10, 12, 2, tzinfo=timezone.utc),
        state={"invoice": "001"},
        metadata={"customer": "acme"},
        definition_identity="invoice-processing/v1",
        skills=[skill],
        artifacts=[
            Artifact(
                id="artifact-001",
                name="invoice",
                path="invoice.pdf",
                kind="pdf",
                created_at=datetime(2026, 6, 10, 12, 3, tzinfo=timezone.utc),
                metadata={"pages": 1},
            )
        ],
    )
    tx.append_history(
        HistoryEvent.SKILL_FAILED,
        skill=skill,
        timestamp=datetime(2026, 6, 10, 12, 4, tzinfo=timezone.utc),
    )

    record = serialize_transaction(tx)

    assert record == {
        "transaction_format_version": 2,
        "id": "tx-001",
        "reference": "invoice-001",
        "definition_identity": "invoice-processing/v1",
        "status": "failed",
        "retry_count": 1,
        "created_at": "2026-06-10T12:00:00+00:00",
        "started_at": "2026-06-10T12:01:00+00:00",
        "finished_at": "2026-06-10T12:02:00+00:00",
        "state": {"invoice": "001"},
        "metadata": {"customer": "acme"},
        "skills": [
            {
                "name": "download",
                "execution_order": 1,
                "status": "failed",
                "arguments": {"invoice": "001"},
                "exceptions": [
                    {
                        "type": "business",
                        "message": "bad invoice",
                        "action": "download",
                        "retry_number": 0,
                        "datetime_occurred": skill.exceptions[0].datetime_occurred.isoformat(),
                        "screenshot_path": "",
                        "stops_execution": False,
                    },
                    {
                        "type": "system",
                        "message": "timeout",
                        "action": "download",
                        "retry_number": 1,
                        "datetime_occurred": skill.exceptions[1].datetime_occurred.isoformat(),
                        "screenshot_path": "",
                        "stops_execution": True,
                    },
                ],
            }
        ],
        "history": [
            {
                "sequence": 1,
                "timestamp": "2026-06-10T12:04:00+00:00",
                "event": "skill_failed",
                "status": "failed",
                "retry_number": 1,
                "skill_name": "download",
                "skill_execution_order": 1,
            }
        ],
        "artifacts": [
            {
                "id": "artifact-001",
                "name": "invoice",
                "path": "invoice.pdf",
                "kind": "pdf",
                "created_at": "2026-06-10T12:03:00+00:00",
                "metadata": {"pages": 1},
            }
        ],
    }
    assert json.loads(json.dumps(record, sort_keys=True)) == record


def test_serializer_excludes_resources_config_credentials_and_artifact_contents(tmp_path) -> None:
    artifact_path = tmp_path / "invoice.txt"
    artifact_path.write_text("secret artifact body", encoding="utf-8")
    tx = Transaction(
        reference="invoice",
        artifacts=[Artifact(name="invoice", path=str(artifact_path))],
    )
    ctx = ProcessContext(
        transaction=tx,
        config={"token": "secret"},
        resources={"browser": object()},
    )

    record = serialize_transaction(ctx.transaction)

    assert "resources" not in record
    assert "config" not in record
    assert "credentials" not in record
    assert record["artifacts"][0]["path"] == str(artifact_path)  # type: ignore[index]
    assert "secret artifact body" not in json.dumps(record)


def test_serializer_returns_snapshot_of_mutable_json_fields() -> None:
    skill = Skill("download", 1, arguments={"items": [{"id": "001"}]})
    tx = Transaction(
        reference="invoice",
        state={"items": [{"id": "001"}]},
        metadata={"labels": ["urgent"]},
        skills=[skill],
        artifacts=[
            Artifact(
                name="invoice",
                path="invoice.pdf",
                metadata={"labels": ["pdf"]},
            )
        ],
    )

    record = serialize_transaction(tx)

    tx.state["items"][0]["id"] = "mutated"  # type: ignore[index]
    tx.metadata["labels"].append("mutated")  # type: ignore[union-attr]
    skill.arguments["items"][0]["id"] = "mutated"  # type: ignore[index]
    tx.artifacts[0].metadata["labels"].append("mutated")  # type: ignore[union-attr]

    assert record["state"] == {"items": [{"id": "001"}]}
    assert record["metadata"] == {"labels": ["urgent"]}
    assert record["skills"][0]["arguments"] == {  # type: ignore[index]
        "items": [{"id": "001"}],
    }
    assert record["artifacts"][0]["metadata"] == {  # type: ignore[index]
        "labels": ["pdf"],
    }


def test_serializer_normalizes_naive_datetime_as_utc() -> None:
    tx = Transaction(
        reference="invoice",
        created_at=datetime(2026, 6, 10, 12, 0),
    )

    record = serialize_transaction(tx)

    assert record["created_at"] == "2026-06-10T12:00:00+00:00"


def test_serializer_rejects_non_json_safe_skill_arguments_with_path() -> None:
    skill = Skill("download", 1, arguments={"invoice": object()})
    tx = Transaction(reference="invoice", skills=[skill])

    with pytest.raises(JsonStateError, match=r"transaction\.skills\['download'\]\.arguments\['invoice'\]"):
        serialize_transaction(tx)


def test_serializer_rejects_tuple_skill_arguments_without_coercion() -> None:
    tx = Transaction(
        reference="invoice",
        skills=[Skill("download", 1, arguments={"ids": (1, 2)})],
    )

    with pytest.raises(
        JsonStateError,
        match=r"transaction\.skills\['download'\]\.arguments\['ids'\]",
    ):
        serialize_transaction(tx)


@pytest.mark.parametrize("state", [None, "string", []])
def test_serializer_rejects_non_object_transaction_state_with_path(state: object) -> None:
    tx = Transaction(reference="invoice")
    tx.state = state  # type: ignore[assignment]

    with pytest.raises(JsonStateError, match=r"transaction\.state"):
        serialize_transaction(tx)


def test_serializer_rejects_non_json_safe_transaction_metadata_with_path() -> None:
    tx = Transaction(reference="invoice")
    tx.metadata = {"raw": object()}

    with pytest.raises(JsonStateError, match=r"transaction\.metadata\['raw'\]"):
        serialize_transaction(tx)


def test_serializer_rejects_non_object_transaction_metadata_with_path() -> None:
    tx = Transaction(reference="invoice")
    tx.metadata = object()  # type: ignore[assignment]

    with pytest.raises(JsonStateError, match=r"transaction\.metadata"):
        serialize_transaction(tx)


def test_serializer_rejects_non_json_safe_artifact_metadata_with_path() -> None:
    tx = Transaction(
        reference="invoice",
        artifacts=[Artifact(name="invoice", path="invoice.pdf", metadata={"raw": object()})],
    )

    with pytest.raises(JsonStateError, match=r"transaction\.artifacts\[0\]\.metadata\['raw'\]"):
        serialize_transaction(tx)
