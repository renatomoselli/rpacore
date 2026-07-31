"""Checked compatibility examples for versioned machine-readable records."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

import rpacore.cli as cli_module
from rpacore import Status, Transaction, generate_report, query_transactions, save_transaction, serialize_transaction
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


def _fixture_transaction(*, transaction_id: str = "transaction-v2", hour: int = 12) -> Transaction:
    return Transaction(
        id=transaction_id,
        reference="fixture",
        definition_identity="fixture/v2",
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
    transaction_v1 = _json_fixture("transaction-v1.json")
    assert tuple(transaction_v1) == (
        "transaction_format_version", "id", "reference", "status", "retry_count",
        "created_at", "started_at", "finished_at", "state", "metadata", "skills",
        "history", "artifacts",
    )
    assert transaction_v1["transaction_format_version"] == 1
    transaction_v2 = _json_fixture("transaction-v2.json")
    assert tuple(transaction_v2) == (
        "transaction_format_version", "id", "reference", "definition_identity",
        "status", "retry_count", "created_at", "started_at", "finished_at",
        "state", "metadata", "skills", "history", "artifacts",
    )
    assert transaction_v2["transaction_format_version"] == 2

    for name, version_key, version in (
        ("cli-transaction-list-v1.json", "schema_version", 1),
        ("cli-transaction-show-v1.json", "schema_version", 1),
        ("export-json-v1.json", "export_format_version", 1),
        ("report-v1-complete.json", "report_format_version", 1),
        ("report-v1-incomplete.json", "report_format_version", 1),
        ("log-v1.json", "log_format_version", 1),
        ("log-v2.json", "log_format_version", 2),
        ("doctor-v1.json", "doctor_format_version", 1),
    ):
        assert _json_fixture(name)[version_key] == version

    ndjson = json.loads((_FIXTURES / "export-ndjson-v1.ndjson").read_text(encoding="utf-8"))
    assert ndjson["export_format_version"] == 1
    assert ndjson["transaction_format_version"] == 2
    assert tuple(check["id"] for check in _json_fixture("doctor-v1.json")["checks"]) == _DOCTOR_CHECK_IDS  # type: ignore[index]


def test_transaction_v2_fixture_matches_the_live_serializer() -> None:
    assert serialize_transaction(_fixture_transaction()) == _json_fixture("transaction-v2.json")


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


def test_cli_list_and_show_v1_fixtures_match_live_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "transactions.db"
    transaction = _fixture_transaction()
    save_transaction(transaction, str(db_path))

    assert cli_module.main(["transaction", "list", "--db", str(db_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _json_fixture("cli-transaction-list-v1.json")

    assert cli_module.main(["transaction", "show", transaction.id, "--db", str(db_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == _json_fixture("cli-transaction-show-v1.json")


def test_export_v1_fixtures_match_live_output(capsys: pytest.CaptureFixture[str]) -> None:
    transaction = _fixture_transaction()

    cli_module._write_transaction_export([transaction], export_format="json")
    assert _normalize_export(json.loads(capsys.readouterr().out)) == _json_fixture("export-json-v1.json")

    cli_module._write_transaction_export([transaction], export_format="ndjson")
    ndjson = json.loads(capsys.readouterr().out)
    assert _normalize_export(ndjson) == json.loads(
        (_FIXTURES / "export-ndjson-v1.ndjson").read_text(encoding="utf-8")
    )


def test_report_v1_complete_and_incomplete_fixtures_match_live_output() -> None:
    complete = generate_report(_fixture_transaction(transaction_id="transaction-v1")).record
    assert complete is not None
    complete_payload = complete.to_dict()
    complete_payload["generated_at"] = "fixture-generated-at"
    assert complete_payload == _json_fixture("report-v1-complete.json")

    incomplete_transaction = _fixture_transaction(transaction_id="transaction-v1")
    incomplete_transaction.state = {"not_json": object()}
    incomplete = generate_report(incomplete_transaction).record
    assert incomplete is not None
    incomplete_payload = incomplete.to_dict()
    incomplete_payload["generated_at"] = "fixture-generated-at"
    assert incomplete_payload == _json_fixture("report-v1-incomplete.json")


def test_json_log_v1_and_v2_fixtures_match_live_output() -> None:
    record = logging.LogRecord(
        "rpacore.fixture", logging.INFO, __file__, 1, "fixture message", (), None
    )
    record.created = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc).timestamp()
    record.event = "fixture_event"  # type: ignore[attr-defined]

    assert json.loads(JsonFormatter().format(record)) == _json_fixture("log-v1.json")

    record.event = "fixture.event"  # type: ignore[attr-defined]
    assert json.loads(JsonFormatter(version=2).format(record)) == _json_fixture("log-v2.json")


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
