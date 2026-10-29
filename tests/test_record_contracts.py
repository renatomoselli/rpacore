"""Checked compatibility examples for versioned machine-readable records."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import rpacore.cli as cli_module
import rpacore._transaction_cli as transaction_cli
from rpacore import (
    ExecutionTransition,
    QueueItem,
    QueueStatus,
    SqliteQueue,
    Status,
    Transaction,
    generate_report,
    query_transactions,
    save_transaction,
    serialize_transaction,
)
from rpacore.doctor import DoctorCheck, DoctorResult
from rpacore.logger import JsonFormatter


_FIXTURES = Path(__file__).parent / "fixtures" / "record-contracts"
_DOCTOR_CHECK_IDS = (
    "runtime.python",
    "runtime.sqlite",
    "project.manifest",
    "project.config",
    "transactions.schema",
    "transactions.journal",
    "transactions.quick_check",
    "transactions.foreign_keys",
    "queue.schema",
    "queue.journal",
    "queue.quick_check",
    "queue.foreign_keys",
    "queue.health",
)


def _json_fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_transaction(*, transaction_id: str = "transaction-v3", hour: int = 12) -> Transaction:
    return Transaction(
        id=transaction_id,
        reference="fixture",
        definition_identity="fixture/v3",
        status=Status.SUCCESSFUL,
        created_at=datetime(2026, 7, 27, hour, 0, tzinfo=timezone.utc),
        state={"invoice": "001"},
        metadata={"customer": "acme"},
    )


def _normalize_export(payload: dict[str, object]) -> dict[str, object]:
    payload = dict(payload)
    payload["framework_version"] = "fixture-framework"
    payload["exported_at"] = "fixture-exported-at"
    return payload


def test_checked_examples_are_json_and_have_the_frozen_framework_envelopes() -> None:
    transaction_v3 = _json_fixture("transaction-v3.json")
    assert tuple(transaction_v3) == (
        "transaction_format_version", "id", "reference", "definition_identity",
        "status", "retry_count", "created_at", "started_at", "finished_at",
        "state", "metadata", "steps", "history", "artifacts",
    )
    assert transaction_v3["transaction_format_version"] == 3

    # CLI-v2 fixtures remain historical contracts; live CLI assertions use v3 below.
    for name, version_key, version in (
        ("cli-transaction-list-v2.json", "schema_version", 2),
        ("cli-transaction-show-v2.json", "schema_version", 2),
        ("export-json-v2.json", "export_format_version", 2),
        ("report-v2-complete.json", "report_format_version", 2),
        ("report-v2-incomplete.json", "report_format_version", 2),
        ("transition-v2.json", "schema_version", 2),
        ("log-v3.json", "log_format_version", 3),
        ("doctor-v1.json", "doctor_format_version", 1),
    ):
        assert _json_fixture(name)[version_key] == version

    ndjson = json.loads((_FIXTURES / "export-ndjson-v2.ndjson").read_text(encoding="utf-8"))
    assert ndjson["export_format_version"] == 2
    assert ndjson["transaction_format_version"] == 3
    assert tuple(check["id"] for check in _json_fixture("doctor-v1.json")["checks"]) == _DOCTOR_CHECK_IDS  # type: ignore[index]


def test_transaction_v3_fixture_matches_the_live_serializer() -> None:
    assert serialize_transaction(_fixture_transaction()) == _json_fixture("transaction-v3.json")


def test_query_cursor_v1_fixture_remains_accepted_and_rejects_future_versions(tmp_path: Path) -> None:
    db_path = tmp_path / "transactions.db"
    save_transaction(_fixture_transaction(transaction_id="transaction-v1"), str(db_path))
    save_transaction(_fixture_transaction(transaction_id="older", hour=11), str(db_path))
    cursor = (_FIXTURES / "query-cursor-v1.txt").read_text(encoding="utf-8").strip()

    page = query_transactions(str(db_path), cursor=cursor)
    assert [summary.id for summary in page.transactions] == ["older"]

    decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    decoded["version"] = 2
    future_cursor = base64.urlsafe_b64encode(
        json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    with pytest.raises(ValueError, match="unsupported transaction query version"):
        query_transactions(str(db_path), cursor=future_cursor)


def test_cli_list_and_show_v3_fixtures_match_live_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "transactions.db"
    transaction = _fixture_transaction()
    save_transaction(transaction, str(db_path))

    assert cli_module.main(["transaction", "list", "--db", str(db_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _json_fixture("cli-transaction-list-v3.json")

    assert cli_module.main(["transaction", "show", transaction.id, "--db", str(db_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _json_fixture("cli-transaction-show-v3.json")


def test_queue_v2_fixtures_match_all_live_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "queue.db"
    stamp = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    SqliteQueue({"db_path": str(db_path)}).add(QueueItem(
        id="queue-fixture", reference="fixture", payload={"secret": "payload-secret"},
        status=QueueStatus.IN_PROGRESS, retry_count=1, created_at=stamp,
        claimed_at=stamp, claimed_by="fixture-worker", claim_token="token-secret",
        transaction_id="transaction-fixture",
    ))
    SqliteQueue({"db_path": str(db_path)}).add(QueueItem(
        id="queue-delayed", reference="fixture-delayed", payload={},
        created_at=stamp + timedelta(seconds=1),
        available_at=stamp + timedelta(seconds=2),
    ))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO queue_attempts (item_id, claim_token, claimed_by, started_at) VALUES (?, ?, ?, ?)",
            ("queue-fixture", "token-secret", "fixture-worker", stamp.isoformat()),
        )
        conn.execute(
            "INSERT INTO queue_poison_events (item_id, error_type, created_at) VALUES (?, ?, ?)",
            ("queue-fixture", "JSONDecodeError", stamp.isoformat()),
        )
    before = db_path.read_bytes()
    for command in ("list", "show", "attempts", "poison"):
        selector = ["queue-delayed"] if command == "show" else []
        assert cli_module.main(["queue", command, *selector, "--db", str(db_path), "--json"]) == 0
        output = capsys.readouterr()
        assert not output.err
        assert json.loads(output.out) == _json_fixture(f"cli-queue-{command}-v2.json")
        historical = _json_fixture(f"cli-queue-{command}-v1.json")
        assert historical["schema_version"] == 1
        old_items = [historical["item"]] if command == "show" else historical.get("records", [])
        assert all("available_at" not in item for item in old_items)
        assert db_path.read_bytes() == before


def test_export_v2_fixtures_match_live_output(capsys: pytest.CaptureFixture[str]) -> None:
    transaction = _fixture_transaction()

    transaction_cli._write_transaction_export([transaction], export_format="json")
    assert _normalize_export(json.loads(capsys.readouterr().out)) == _json_fixture("export-json-v2.json")

    transaction_cli._write_transaction_export([transaction], export_format="ndjson")
    ndjson = json.loads(capsys.readouterr().out)
    assert _normalize_export(ndjson) == json.loads(
        (_FIXTURES / "export-ndjson-v2.ndjson").read_text(encoding="utf-8")
    )


def test_report_v2_complete_and_incomplete_fixtures_match_live_output() -> None:
    complete = generate_report(_fixture_transaction()).record
    assert complete is not None
    complete_payload = complete.to_dict()
    complete_payload["generated_at"] = "fixture-generated-at"
    assert complete_payload == _json_fixture("report-v2-complete.json")

    incomplete_transaction = _fixture_transaction()
    incomplete_transaction.state = {"not_json": object()}
    incomplete = generate_report(incomplete_transaction).record
    assert incomplete is not None
    incomplete_payload = incomplete.to_dict()
    incomplete_payload["generated_at"] = "fixture-generated-at"
    assert incomplete_payload == _json_fixture("report-v2-incomplete.json")


def test_json_log_v3_fixture_matches_live_output() -> None:
    record = logging.LogRecord(
        "rpacore.fixture", logging.INFO, __file__, 1, "fixture message", (), None
    )
    record.created = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc).timestamp()
    record.event = "fixture.event"  # type: ignore[attr-defined]
    assert json.loads(JsonFormatter().format(record)) == _json_fixture("log-v3.json")


def test_transition_v2_fixture_matches_live_record() -> None:
    transition = ExecutionTransition(
        schema_version=2,
        transition_id="transition-001",
        sequence=3,
        occurred_at="2026-07-27T12:00:01+00:00",
        event="step_succeeded",
        transaction_id="transaction-v3",
        transaction_reference="fixture",
        definition_identity="fixture/v3",
        transaction_status="in_progress",
        execution_pass=1,
        step_name="fixture-step",
        step_execution_order=1,
        step_status="successful",
        outcome_category="unknown",
        failure_code="",
        retry_recommended=None,
        checkpoint_state={"invoice": "001"},
    )

    assert transition.to_record() == _json_fixture("transition-v2.json")


def test_doctor_v1_fixture_matches_the_record_producer() -> None:
    fixture = _json_fixture("doctor-v1.json")
    checks = tuple(
        DoctorCheck(
            id=check["id"], status=check["status"], summary=check["summary"], details=check["details"]
        )
        for check in fixture["checks"]  # type: ignore[index]
    )
    payload = DoctorResult(checks).to_dict()
    payload["framework_version"] = "fixture-framework"
    assert payload == fixture
