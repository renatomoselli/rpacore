"""Tests for rpacore/report.py and list_transactions() in rpacore/persistence.py."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

from rpacore.exceptions import BusinessException, SystemException
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.persistence import list_transactions, load_transaction, save_transaction
from rpacore.report import (
    ArtifactReport,
    ReportRecord,
    StepReport,
    TransactionReport,
    generate_report,
    render_html,
    render_json,
    render_text,
)
from rpacore.step import Step
from rpacore.status import Status
from rpacore.transaction import Artifact, HistoryEvent, Transaction


class DomainRejection(BusinessException):
    """Application-level business rejection used by category tests."""


class PortalDown(SystemException):
    """Application-level system failure used by category tests."""


# User classes whose names contradict the category they inherit. Built with
# type() so the misleading names do not shadow the real imports.
NamedBusinessButSystem = type("BusinessException", (SystemException,), {})
NamedSystemButBusiness = type("SystemException", (BusinessException,), {})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_step(name: str, order: int, status: Status = Status.SUCCESSFUL) -> Step:
    s = Step(name, order)
    s.status = status
    return s


def biz(
    message: str,
    retry: int = 0,
    action: str = "",
    screenshot: str = "",
    halts_remaining_steps: bool = False,
) -> BusinessException:
    return BusinessException(
        message,
        retry_number=retry,
        action=action,
        screenshot_path=screenshot,
        halts_remaining_steps=halts_remaining_steps,
    )


def sys_(message: str, retry: int = 0, action: str = "", screenshot: str = "") -> SystemException:
    return SystemException(message, retry_number=retry, action=action, screenshot_path=screenshot)


def make_transaction(
    retry_count: int = 0,
    status: Status = Status.SUCCESSFUL,
    steps: list[Step] | None = None,
) -> Transaction:
    tx = Transaction(reference="ref-test")
    tx.retry_count = retry_count
    tx.status = status
    tx.steps = steps or []
    return tx


class ReportHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.text = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs))

    def handle_data(self, data):
        self.text.append(data)


@pytest.mark.parametrize("exception_type", [BusinessException, SystemException])
@pytest.mark.parametrize("path", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>x</script>",
    "vbscript:msgbox(1)", "https://example.com/shot.png", "http://example.com/x",
    "custom:shot.png", "mailto:x@example.com", "file:///tmp/shot.png", "FILE:///C:/shot.png",
    "file://host/shot.png", "java\nscript:alert(1)", "java\tscript:alert(1)",
    "java\rscript:alert(1)", "\x00javascript:alert(1)", "shot\x7f.png", "shot\x1f.png",
    " javascript:alert(1)", "err.png ", "\terr.png", "\u00a0err.png",
    "C:shot.png", r"\shot.png", "//host/shot.png", r"\\host\shot.png",
    r"/\host/shot.png", r"\/host/shot.png", r"\\?\C:\shot.png", r"\\.\device",
    "shot\udcff.png", "C:/shot\udcff.png",
    'javascript:alert(1)\"><script>x</script><a href="x',
])
def test_unsafe_screenshot_paths_are_visible_without_links(exception_type, path):
    step = make_step("unsafe", 1, Status.FAILED)
    step.exceptions = [exception_type("synthetic", screenshot_path=path)]
    parsed = ReportHTML()
    parsed.feed(render_html(generate_report(make_transaction(steps=[step]))))

    assert parsed.links == []
    assert path in "".join(parsed.text)
    assert "script" not in parsed.tags


@pytest.mark.parametrize(("path", "target"), [
    ("err.png", "err.png"), ("./shots/err.png", "./shots/err.png"),
    ("../shots/err.png", "../shots/err.png"), (r"shots\err.png", "shots/err.png"),
    ("/tmp/err.png", "/tmp/err.png"),
    (r"C:\shots\err.png", "file:///C:/shots/err.png"),
    ("C:/shots/err.png", "file:///C:/shots/err.png"),
    ("z:/shots/a #?%.png", "file:///z:/shots/a%20%23%3F%25.png"),
    ('shots/a #?%&\"<>\'.png', "shots/a%20%23%3F%25%26%22%3C%3E%27.png"),
    ("shots/a:b.png", "shots/a%3Ab.png"),
    ("shots/ação.png", "shots/a%C3%A7%C3%A3o.png"),
    ("", None),
])
def test_local_screenshot_paths_have_exact_targets(path, target):
    step = make_step("local", 1)
    step.exceptions = [sys_("synthetic", screenshot=path)]
    html = render_html(generate_report(make_transaction(steps=[step])))
    parsed = ReportHTML()
    parsed.feed(html)

    assert parsed.links == ([] if target is None else [{"href": target}])
    assert "script" not in parsed.tags
    if target is None:
        assert "screenshot" not in html


def test_loaded_screenshot_rendering_preserves_evidence_without_io(tmp_path, monkeypatch):
    step = make_step("persisted", 1, Status.FAILED)
    paths = ["javascript:alert(1)", "err.png", "data:text/html,<script>x</script>"]
    step.exceptions = [
        biz("first", screenshot=paths[0]), sys_("retry", retry=1, screenshot=paths[1]),
        sys_("last", retry=2, screenshot=paths[2]),
    ]
    tx = make_transaction(steps=[step])
    tx.definition_identity = "report-link-test/v1"
    db = str(tmp_path / "report.db")
    save_transaction(tx, db)
    loaded = load_transaction(tx.id, db)
    report = generate_report(loaded)
    json_before, text_before = render_json(report), render_text(report)

    def unexpected_io(*args, **kwargs):
        pytest.fail("HTML rendering performed I/O")

    with monkeypatch.context() as guard:
        guard.setattr("builtins.open", unexpected_io)
        guard.setattr(Path, "exists", unexpected_io)
        guard.setattr(Path, "resolve", unexpected_io)
        guard.setattr("socket.socket", unexpected_io)
        parsed = ReportHTML()
        parsed.feed(render_html(report))

    assert parsed.links == [{"href": "err.png"}]
    assert paths[0] in "".join(parsed.text) and paths[2] in "".join(parsed.text)
    assert [exc.screenshot_path for exc in loaded.steps[0].exceptions] == paths
    assert render_json(report) == json_before
    assert render_text(report) == text_before


# ---------------------------------------------------------------------------
# TestGenerateReport — exception filtering and icon assignment
# ---------------------------------------------------------------------------

class TestGenerateReport:
    def test_all_business_exceptions_included(self):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [biz("err1", retry=0), biz("err2", retry=1), biz("err3", retry=2)]
        tx = make_transaction(retry_count=2, steps=[step])

        report = generate_report(tx)

        assert len(report.steps[0].exceptions) == 3

    def test_all_system_exceptions_included(self):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [
            sys_("early", retry=0),
            sys_("middle", retry=1),
            sys_("last", retry=2),
        ]
        tx = make_transaction(retry_count=2, steps=[step])

        report = generate_report(tx)

        assert [str(exc) for exc in report.steps[0].exceptions] == [
            "early",
            "middle",
            "last",
        ]

    def test_report_includes_timestamps_and_history(self):
        tx = make_transaction()
        tx.started_at = tx.created_at
        tx.finished_at = tx.created_at
        tx.append_history(HistoryEvent.TRANSACTION_STARTED)

        report = generate_report(tx)

        assert report.created_at == tx.created_at
        assert report.started_at == tx.started_at
        assert report.finished_at == tx.finished_at
        assert report.history == tx.history

    def test_report_history_is_a_defensive_copy(self):
        tx = make_transaction()
        tx.append_history(HistoryEvent.TRANSACTION_STARTED)

        report = generate_report(tx)
        report.history.clear()

        assert len(tx.history) == 1

    def test_report_includes_metadata_as_defensive_copy(self):
        tx = make_transaction()
        tx.metadata = {"customer": "acme", "nested": {"b": 2, "a": 1}}

        report = generate_report(tx)
        report.metadata.clear()

        assert tx.metadata == {"customer": "acme", "nested": {"b": 2, "a": 1}}

    def test_report_nested_data_and_exceptions_are_snapshot_isolated(self):
        error = biz("invalid", action="original")
        step = make_step("validate", 1, Status.FAILED)
        step.exceptions = [error]
        tx = make_transaction(status=Status.FAILED, steps=[step])
        tx.metadata = {"nested": {"values": ["original"]}}
        tx.artifacts = [
            Artifact(
                name="audit",
                path="audit.json",
                metadata={"nested": {"values": ["original"]}},
            )
        ]

        report = generate_report(tx)
        report.metadata["nested"]["values"].append("report")
        report.artifacts[0].metadata["nested"]["values"].append("report")
        report.steps[0].exceptions[0].action = "report"

        assert tx.metadata == {"nested": {"values": ["original"]}}
        assert tx.artifacts[0].metadata == {
            "nested": {"values": ["original"]}
        }
        assert error.action == "original"

    def test_report_includes_canonical_transaction_record(self):
        tx = make_transaction()

        report = generate_report(tx)

        assert report.transaction_record["transaction_format_version"] == 3
        assert report.transaction_record["id"] == tx.id

    def test_report_v2_record_is_immutable_and_json_safe(self):
        tx = make_transaction()
        tx.metadata = {"nested": {"values": ["original"]}}

        report = generate_report(tx)

        assert isinstance(report.record, ReportRecord)
        payload = report.record.to_dict()
        assert payload["report_format_version"] == 3
        assert payload["complete"] is True
        payload["transaction"]["metadata"]["nested"]["values"].append("view")
        assert report.record.to_dict()["transaction"]["metadata"] == {
            "nested": {"values": ["original"]}
        }
        assert render_json(report) == report.record.payload_json

    def test_report_v2_record_exposes_transaction_serialization_failure(self):
        tx = make_transaction()
        tx.state = {"runtime": object()}

        report = generate_report(tx)
        payload = report.record.to_dict()

        assert report.transaction_record == {}
        assert payload["complete"] is False
        assert payload["errors"] == [
            {
                "code": "rpacore.report.transaction_serialization_failed",
                "scope": "transaction_record",
            }
        ]
        assert payload["transaction_record"] is None
        assert json.loads(render_json(report))["complete"] is False

    def test_report_v2_record_exposes_json_encoding_failure(self):
        tx = make_transaction()
        tx.metadata = {"ratio": float("nan")}

        payload = generate_report(tx).record.to_dict()

        assert payload["complete"] is False
        assert payload["errors"] == [
            {
                "code": "rpacore.report.record_serialization_failed",
                "type": "ValueError",
            }
        ]

    def test_render_json_builds_record_for_manually_constructed_report(self):
        report = TransactionReport(
            transaction_id="manual-001",
            reference="manual",
            status=Status.SUCCESSFUL,
            retry_count=0,
            steps=[],
        )

        payload = json.loads(render_json(report))

        assert payload["report_format_version"] == 3
        assert payload["transaction"]["id"] == "manual-001"

    def test_text_renderer_uses_recorded_transaction_truth(self):
        tx = make_transaction(status=Status.SUCCESSFUL)
        report = generate_report(tx)
        report.reference = "mutated-after-generation"
        report.status = Status.FAILED

        text = render_text(report)

        assert f"Transaction: {tx.reference} ({tx.id})" in text
        assert "Status:      successful" in text

    def test_html_renderer_uses_recorded_transaction_truth(self):
        tx = make_transaction(status=Status.SUCCESSFUL)
        report = generate_report(tx)
        report.reference = "mutated-after-generation"
        report.status = Status.FAILED

        html = render_html(report)

        assert tx.reference in html
        assert "mutated-after-generation" not in html

    def test_renderers_use_recorded_detail_truth(self):
        step = make_step("validate", 1, Status.FAILED)
        step.exceptions = [biz("original failure", action="correct input")]
        tx = make_transaction(status=Status.FAILED, steps=[step])
        tx.metadata = {"customer": "original"}
        tx.artifacts = [Artifact(name="audit", path="original.json")]
        tx.append_history(HistoryEvent.TRANSACTION_STARTED)
        report = generate_report(tx)

        report.steps.clear()
        report.metadata["customer"] = "mutated"
        report.artifacts.clear()
        report.history.clear()

        text = render_text(report)
        html = render_html(report)

        for rendered in (text, html):
            assert "validate" in rendered
            assert "original failure" in rendered
            assert "original" in rendered
            assert "original.json" in rendered
            assert "mutated" not in rendered

    def test_incomplete_record_remains_renderable(self):
        tx = make_transaction()
        tx.metadata = {"ratio": float("nan")}

        report = generate_report(tx)

        text = render_text(report)
        html = render_html(report)

        assert "Transaction:" in text
        assert "rpacore.report.record_serialization_failed" in text
        assert tx.reference in html
        assert "rpacore.report.record_serialization_failed" in html

    def test_report_projects_captured_outcome_without_reclassifying_history(self):
        step = make_step("save", 1, Status.FAILED)
        step.exceptions = [sys_("transport failure")]
        tx = make_transaction(status=Status.FAILED, steps=[step])
        tx.outcome_category = OutcomeCategory.BUSINESS_FAILED
        tx.retry_disposition = RetryDisposition.NOT_REQUESTED
        tx.failure_code = "acme.invoice.duplicate"

        report = generate_report(tx)

        assert report.outcome.category is OutcomeCategory.BUSINESS_FAILED
        assert report.outcome.retry_disposition is RetryDisposition.NOT_REQUESTED
        assert report.outcome.failure_code == "acme.invoice.duplicate"
        assert report.transaction_record["transaction_format_version"] == 3
        assert "outcome_category" not in report.transaction_record

    def test_report_defaults_legacy_outcome_to_unknown(self):
        report = generate_report(make_transaction())

        assert report.outcome.category is OutcomeCategory.UNKNOWN
        assert report.outcome.retry_disposition is RetryDisposition.UNKNOWN
        assert report.outcome.failure_code == ""

    def test_report_omits_transaction_record_when_serialization_fails(self):
        tx = make_transaction()
        tx.state = {"runtime": object()}

        report = generate_report(tx)

        assert report.transaction_record == {}

    def test_report_includes_artifacts_as_defensive_copy(self):
        tx = make_transaction()
        tx.artifacts = [
            Artifact(
                id="artifact-001",
                name="invoice",
                path="/missing/invoice.pdf",
                kind="pdf",
                metadata={"invoice_id": 42},
            )
        ]

        report = generate_report(tx)
        report.artifacts[0].metadata.clear()

        assert isinstance(report.artifacts[0], ArtifactReport)
        assert tx.artifacts[0].metadata == {"invoice_id": 42}

    def test_artifact_report_fields_match_artifact_model(self):
        artifact_fields = {field.name for field in dataclasses.fields(Artifact)}
        report_fields = {field.name for field in dataclasses.fields(ArtifactReport)}

        assert report_fields == artifact_fields

    def test_system_exception_from_earlier_retry_preserved(self):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [sys_("old", retry=0), sys_("current", retry=1)]
        tx = make_transaction(retry_count=1, steps=[step])

        report = generate_report(tx)
        messages = [str(e) for e in report.steps[0].exceptions]

        assert "old" in messages
        assert "current" in messages

    def test_mixed_exceptions_preserve_recorded_order(self):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [
            biz("biz0", retry=0),
            sys_("sys0", retry=0),
            biz("biz2", retry=2),
            sys_("sys2", retry=2),
        ]
        tx = make_transaction(retry_count=2, steps=[step])

        report = generate_report(tx)
        kept = report.steps[0].exceptions

        assert [str(e) for e in kept] == ["biz0", "sys0", "biz2", "sys2"]

    def test_no_exceptions_gives_empty_list(self):
        step = make_step("s1", 1)
        tx = make_transaction(steps=[step])

        report = generate_report(tx)

        assert report.steps[0].exceptions == []

    def test_icon_successful(self):
        step = make_step("s1", 1, Status.SUCCESSFUL)
        report = generate_report(make_transaction(steps=[step]))
        assert report.steps[0].icon == "✓"

    def test_icon_failed(self):
        step = make_step("s1", 1, Status.FAILED)
        report = generate_report(make_transaction(steps=[step]))
        assert report.steps[0].icon == "✗"

    def test_icon_skipped(self):
        step = make_step("s1", 1, Status.SKIPPED)
        report = generate_report(make_transaction(steps=[step]))
        assert report.steps[0].icon == "⊘"

    def test_icon_pending(self):
        step = make_step("s1", 1, Status.PENDING)
        report = generate_report(make_transaction(steps=[step]))
        assert report.steps[0].icon == "⏸"

    def test_steps_in_execution_order(self):
        s3 = make_step("c", 3)
        s1 = make_step("a", 1)
        s2 = make_step("b", 2)
        tx = make_transaction(steps=[s3, s1, s2])

        report = generate_report(tx)

        assert [sr.execution_order for sr in report.steps] == [1, 2, 3]
        assert [sr.name for sr in report.steps] == ["a", "b", "c"]

    def test_report_metadata(self):
        tx = make_transaction(retry_count=3, status=Status.FAILED)
        tx.reference = "inv-42"

        report = generate_report(tx)

        assert report.reference == "inv-42"
        assert report.status == Status.FAILED
        assert report.retry_count == 3
        assert report.transaction_id == tx.id


# ---------------------------------------------------------------------------
# TestExceptionCategory — business/system labels come from isinstance, not names
# ---------------------------------------------------------------------------

class TestExceptionCategory:
    @pytest.mark.parametrize(
        ("exc", "category", "type_name"),
        [
            pytest.param(DomainRejection("bad row"), "business", "DomainRejection", id="business_subclass"),
            pytest.param(BusinessException("x"), "business", "BusinessException", id="business_base"),
            pytest.param(PortalDown("portal"), "system", "PortalDown", id="system_subclass"),
            pytest.param(SystemException("x"), "system", "SystemException", id="system_base"),
        ],
    )
    def test_record_exception_category_and_type(self, exc, category, type_name):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [exc]

        report = generate_report(make_transaction(status=Status.FAILED, steps=[step]))

        entry = report.record.to_dict()["steps"][0]["exceptions"][0]
        assert set(entry) == {
            "action", "category", "halts_remaining_steps", "message",
            "retry_number", "screenshot_path", "type",
        }
        assert entry["category"] == category
        assert entry["type"] == type_name

    @pytest.mark.parametrize(
        "case",
        ["complete", "transaction_serialization_failed", "record_serialization_failed"],
    )
    def test_report_record_format_version_is_3(self, case):
        tx = make_transaction()
        if case == "transaction_serialization_failed":
            tx.state = {"runtime": object()}
        elif case == "record_serialization_failed":
            tx.metadata = {"ratio": float("nan")}

        report = generate_report(tx)
        payload = report.record.to_dict()

        assert report.record.format_version == 3
        assert payload["report_format_version"] == 3
        if case == "complete":
            assert payload["errors"] == []
        else:
            assert payload["errors"][0]["code"] == f"rpacore.report.{case}"

    def test_text_labels_exceptions_by_category(self):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [DomainRejection("bad row"), PortalDown("portal")]

        report = generate_report(make_transaction(status=Status.FAILED, steps=[step]))

        lines = render_text(report).splitlines()
        assert "      [BIZ] retry=0: bad row" in lines
        assert "      [SYS] retry=0 halts_remaining_steps=true: portal" in lines
        assert not any("[SYS] retry=0: bad row" in line for line in lines)

    def test_html_labels_exceptions_by_category(self):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [DomainRejection("bad row"), PortalDown("portal")]

        report = generate_report(make_transaction(status=Status.FAILED, steps=[step]))

        html = render_html(report)
        assert '<div class="exc biz"><strong>[BIZ]</strong> retry=0: bad row' in html
        assert '<div class="exc sys"><strong>[SYS]</strong> retry=0 halts_remaining_steps=true: portal' in html
        assert '<div class="exc sys"><strong>[SYS]</strong> retry=0: bad row' not in html

    @pytest.mark.parametrize(
        ("exc_cls", "category", "text_line", "html_prefix"),
        [
            pytest.param(
                NamedBusinessButSystem, "system",
                "      [SYS] retry=0 halts_remaining_steps=true: misleading",
                '<div class="exc sys"><strong>[SYS]</strong>',
                id="named_business_is_system",
            ),
            pytest.param(
                NamedSystemButBusiness, "business",
                "      [BIZ] retry=0: misleading",
                '<div class="exc biz"><strong>[BIZ]</strong>',
                id="named_system_is_business",
            ),
        ],
    )
    def test_class_name_does_not_decide_label(
        self, exc_cls, category, text_line, html_prefix,
    ):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [exc_cls("misleading")]

        report = generate_report(make_transaction(status=Status.FAILED, steps=[step]))

        entry = report.record.to_dict()["steps"][0]["exceptions"][0]
        assert entry["category"] == category
        assert entry["type"] == exc_cls.__name__
        assert text_line in render_text(report).splitlines()
        assert html_prefix in render_html(report)

    def test_reloaded_report_keeps_exception_category(self, tmp_path):
        step = make_step("s1", 1, Status.FAILED)
        step.exceptions = [DomainRejection("bad row"), PortalDown("portal")]
        tx = make_transaction(status=Status.FAILED, steps=[step])
        tx.definition_identity = "report-category-test/v1"
        db = str(tmp_path / "report.db")
        save_transaction(tx, db)

        report = generate_report(load_transaction(tx.id, db))

        entries = report.record.to_dict()["steps"][0]["exceptions"]
        assert [entry["category"] for entry in entries] == ["business", "system"]
        assert [entry["type"] for entry in entries] == [
            "BusinessException", "SystemException",
        ]
        text = render_text(report)
        assert "[BIZ] retry=0: bad row" in text
        assert "[SYS] retry=0 halts_remaining_steps=true: portal" in text


# ---------------------------------------------------------------------------
# TestRenderText
# ---------------------------------------------------------------------------

class TestRenderText:
    def _report(self) -> TransactionReport:
        step = make_step("fetch", 1, Status.FAILED)
        step.exceptions = [
            biz("invoice missing", retry=0, action="skip row", screenshot="sc.png"),
            sys_("timeout", retry=1),
        ]
        tx = make_transaction(retry_count=1, status=Status.FAILED, steps=[step])
        return generate_report(tx)

    def test_contains_reference(self):
        report = self._report()
        report.reference = "order-99"
        # Rebuild with correct reference via generate_report sets reference from tx
        step = make_step("fetch", 1, Status.FAILED)
        tx = make_transaction(retry_count=1, status=Status.FAILED, steps=[step])
        tx.reference = "order-99"
        report = generate_report(tx)
        assert "order-99" in render_text(report)

    def test_contains_captured_outcome_and_failure_code(self):
        tx = make_transaction(status=Status.FAILED)
        tx.outcome_category = OutcomeCategory.SYSTEM_FAILED
        tx.retry_disposition = RetryDisposition.RETRY_EXHAUSTED
        tx.failure_code = "rpacore.system.unexpected"

        text = render_text(generate_report(tx))

        assert "Outcome:     system_failed  Retry disposition: retry_exhausted" in text
        assert "Failure code: rpacore.system.unexpected" in text

    def test_omits_empty_failure_code(self):
        assert "Failure code:" not in render_text(self._report())

    def test_contains_step_icon(self):
        text = render_text(self._report())
        assert "[✗]" in text

    def test_contains_biz_exception_kind(self):
        text = render_text(self._report())
        assert "[BIZ]" in text

    def test_contains_sys_exception_kind(self):
        text = render_text(self._report())
        assert "[SYS]" in text

    def test_action_shown(self):
        text = render_text(self._report())
        assert "skip row" in text

    def test_screenshot_path_shown(self):
        text = render_text(self._report())
        assert "sc.png" in text

    def test_no_exceptions_no_exc_lines(self):
        step = make_step("clean", 1, Status.SUCCESSFUL)
        tx = make_transaction(steps=[step])
        report = generate_report(tx)
        text = render_text(report)
        assert "[BIZ]" not in text
        assert "[SYS]" not in text

    def test_stopping_business_exception_is_labeled(self):
        step = make_step("validate", 1, Status.FAILED)
        step.exceptions = [
            biz("bad data", retry=0, halts_remaining_steps=True)
        ]
        tx = make_transaction(status=Status.FAILED, steps=[step])

        text = render_text(generate_report(tx))

        assert "halts_remaining_steps=true" in text

    def test_skipped_step_status_is_shown(self):
        step = make_step("write_output", 2, Status.SKIPPED)
        tx = make_transaction(status=Status.FAILED, steps=[step])

        text = render_text(generate_report(tx))

        assert "write_output" in text
        assert "skipped" in text

    def test_timestamps_and_history_are_shown(self):
        tx = make_transaction()
        tx.started_at = tx.created_at
        tx.finished_at = tx.created_at
        tx.append_history(HistoryEvent.TRANSACTION_STARTED)

        text = render_text(generate_report(tx))

        assert "Created:" in text
        assert "Started:" in text
        assert "Finished:" in text
        assert "History:" in text
        assert "transaction_started" in text

    def test_empty_history_is_not_shown(self):
        tx = make_transaction()

        text = render_text(generate_report(tx))

        assert "History:" not in text

    def test_unknown_timestamp_is_shown(self):
        tx = make_transaction()
        tx.created_at = None

        text = render_text(generate_report(tx))

        assert "Created:     unknown" in text

    def test_metadata_is_shown(self):
        tx = make_transaction()
        tx.metadata = {"customer": "acme", "nested": {"b": 2, "a": 1}}

        text = render_text(generate_report(tx))

        assert "Metadata:" in text
        assert '  customer: "acme"' in text
        assert '  nested: {"a": 1, "b": 2}' in text

    def test_artifacts_are_shown_without_requiring_files(self):
        tx = make_transaction()
        tx.artifacts = [
            Artifact(
                name="invoice",
                path="/missing/invoice.pdf",
                kind="pdf",
                metadata={"invoice_id": 42},
            )
        ]

        text = render_text(generate_report(tx))

        assert "Artifacts:" in text
        assert "invoice kind=pdf path=/missing/invoice.pdf" in text
        assert "    invoice_id: 42" in text


# ---------------------------------------------------------------------------
# TestRenderHTML
# ---------------------------------------------------------------------------

class TestRenderHTML:
    def _report(self) -> TransactionReport:
        step = make_step("parse", 1, Status.FAILED)
        step.exceptions = [
            biz("bad data", retry=0, action="flag row"),
            sys_("crash", retry=0, screenshot="err.png"),
        ]
        tx = make_transaction(retry_count=0, status=Status.FAILED, steps=[step])
        tx.reference = "html-ref"
        return generate_report(tx)

    def test_contains_reference_in_title(self):
        html = render_html(self._report())
        assert "html-ref" in html

    def test_contains_captured_outcome_and_failure_code(self):
        tx = make_transaction(status=Status.FAILED)
        tx.outcome_category = OutcomeCategory.SYSTEM_FAILED
        tx.retry_disposition = RetryDisposition.RETRY_EXHAUSTED
        tx.failure_code = "rpacore.system.unexpected"

        html = render_html(generate_report(tx))

        assert "Outcome: <strong>system_failed</strong>" in html
        assert "Retry disposition: retry_exhausted" in html
        assert "<code>rpacore.system.unexpected</code>" in html

    def test_omits_empty_failure_code_without_blank_html_line(self):
        html = render_html(self._report())

        assert "Failure code:" not in html
        assert "Retry disposition: unknown</p>\n<p>Created:" in html

    def test_contains_failed_css_class(self):
        html = render_html(self._report())
        assert 'class="step failed"' in html

    def test_contains_biz_kind_label(self):
        html = render_html(self._report())
        assert "[BIZ]" in html

    def test_contains_sys_kind_label(self):
        html = render_html(self._report())
        assert "[SYS]" in html

    def test_screenshot_link_rendered(self):
        html = render_html(self._report())
        assert 'href="err.png"' in html

    def test_html_escaped(self):
        step = make_step("xss", 1, Status.FAILED)
        step.exceptions = [biz("<script>alert(1)</script>", retry=0)]
        tx = make_transaction(retry_count=0, status=Status.FAILED, steps=[step])
        html = render_html(generate_report(tx))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_successful_css_class(self):
        step = make_step("ok", 1, Status.SUCCESSFUL)
        tx = make_transaction(steps=[step])
        html = render_html(generate_report(tx))
        assert 'class="step successful"' in html

    def test_skipped_css_class(self):
        step = make_step("skp", 1, Status.SKIPPED)
        tx = make_transaction(steps=[step])
        html = render_html(generate_report(tx))
        assert 'class="step skipped"' in html

    def test_stopping_business_exception_is_labeled(self):
        step = make_step("validate", 1, Status.FAILED)
        step.exceptions = [
            biz("bad data", retry=0, halts_remaining_steps=True)
        ]
        tx = make_transaction(status=Status.FAILED, steps=[step])

        html = render_html(generate_report(tx))

        assert "halts_remaining_steps=true" in html

    def test_timestamps_and_history_are_shown(self):
        tx = make_transaction()
        tx.started_at = tx.created_at
        tx.finished_at = tx.created_at
        tx.append_history(HistoryEvent.TRANSACTION_STARTED)

        html = render_html(generate_report(tx))

        assert "Created:" in html
        assert "Started:" in html
        assert "Finished:" in html
        assert "History" in html
        assert "transaction_started" in html

    def test_empty_history_is_not_shown(self):
        tx = make_transaction()

        html = render_html(generate_report(tx))

        assert 'class="history"' not in html

    def test_unknown_timestamp_is_shown(self):
        tx = make_transaction()
        tx.finished_at = None

        html = render_html(generate_report(tx))

        assert "Finished: unknown" in html

    def test_metadata_is_shown_and_escaped(self):
        tx = make_transaction()
        tx.metadata = {"customer": "<acme>", "nested": {"b": 2, "a": 1}}

        html = render_html(generate_report(tx))

        assert "<h3>Metadata</h3>" in html
        assert "&lt;acme&gt;" in html
        assert "{&quot;a&quot;: 1, &quot;b&quot;: 2}" in html

    def test_artifacts_are_shown_and_escaped(self):
        tx = make_transaction()
        tx.artifacts = [
            Artifact(
                name="<invoice>",
                path="/missing/invoice.pdf",
                kind="pdf",
                metadata={"source": "<step>"},
            )
        ]

        html = render_html(generate_report(tx))

        assert "<h3>Artifacts</h3>" in html
        assert "&lt;invoice&gt;" in html
        assert "/missing/invoice.pdf" in html
        assert "&lt;step&gt;" in html


@pytest.mark.parametrize(
    ("renderer", "outcome_line"),
    [
        (render_text, "Outcome:     unknown  Retry disposition: unknown"),
        (render_html, "Outcome: <strong>unknown</strong>"),
    ],
)
def test_renderers_preserve_unknown_outcome_with_failure_code(renderer, outcome_line):
    tx = make_transaction(status=Status.FAILED)
    tx.outcome_category = OutcomeCategory.UNKNOWN
    tx.failure_code = "acme.legacy.incomplete"

    rendered = renderer(generate_report(tx))

    assert outcome_line in rendered
    assert "Failure code:" in rendered
    assert "acme.legacy.incomplete" in rendered


# ---------------------------------------------------------------------------
# TestListTransactions
# ---------------------------------------------------------------------------

class TestListTransactions:
    def _save(self, tmp_path, reference: str, status: Status = Status.SUCCESSFUL) -> Transaction:
        tx = Transaction(reference=reference)
        tx.status = status
        save_transaction(tx, str(tmp_path / "rpacore.db"))
        return tx

    def test_empty_returns_empty(self, tmp_path):
        result = list_transactions(str(tmp_path / "rpacore.db"))
        assert result == []

    def test_returns_saved_transactions(self, tmp_path):
        tx = self._save(tmp_path, "ref-a")
        result = list_transactions(str(tmp_path / "rpacore.db"))
        assert len(result) == 1
        assert result[0].id == tx.id

    def test_filter_by_status_match(self, tmp_path):
        self._save(tmp_path, "ok", Status.SUCCESSFUL)
        self._save(tmp_path, "fail", Status.FAILED)
        result = list_transactions(str(tmp_path / "rpacore.db"), status=Status.FAILED)
        assert len(result) == 1
        assert result[0].reference == "fail"

    def test_filter_by_status_no_match(self, tmp_path):
        self._save(tmp_path, "ok", Status.SUCCESSFUL)
        result = list_transactions(str(tmp_path / "rpacore.db"), status=Status.FAILED)
        assert result == []

    def test_filter_by_since(self, tmp_path):
        import sqlite3 as _sqlite3
        db = str(tmp_path / "rpacore.db")
        tx_old = Transaction(reference="old")
        tx_old.status = Status.SUCCESSFUL
        save_transaction(tx_old, db)
        with _sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE transactions SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (tx_old.id,),
            )

        cutoff = datetime.now(timezone.utc)

        tx_new = Transaction(reference="new")
        tx_new.status = Status.SUCCESSFUL
        save_transaction(tx_new, db)

        result = list_transactions(db, since=cutoff)
        references = [tx.reference for tx in result]
        assert "new" in references
        assert "old" not in references

    def test_limit(self, tmp_path):
        db = str(tmp_path / "rpacore.db")
        for i in range(5):
            tx = Transaction(reference=f"ref-{i}")
            tx.status = Status.SUCCESSFUL
            save_transaction(tx, db)
        result = list_transactions(db, limit=3)
        assert len(result) == 3

    def test_multiple_filters_combined(self, tmp_path):
        import sqlite3 as _sqlite3
        db = str(tmp_path / "rpacore.db")
        tx1 = Transaction(reference="before-fail")
        tx1.status = Status.FAILED
        save_transaction(tx1, db)
        # Force tx1 into the past so the cutoff is reliably after it.
        conn = _sqlite3.connect(db)
        conn.execute("UPDATE transactions SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (tx1.id,))
        conn.commit()
        conn.close()

        cutoff = datetime.now(timezone.utc)

        tx2 = Transaction(reference="after-success")
        tx2.status = Status.SUCCESSFUL
        save_transaction(tx2, db)
        tx3 = Transaction(reference="after-fail")
        tx3.status = Status.FAILED
        save_transaction(tx3, db)

        result = list_transactions(db, status=Status.FAILED, since=cutoff)
        assert len(result) == 1
        assert result[0].reference == "after-fail"

    def test_legacy_row_with_unknown_created_at_is_not_backfilled(self, tmp_path):
        """Rows migrated from a pre-created_at schema keep unknown created_at."""
        import sqlite3

        db = str(tmp_path / "rpacore.db")

        # Simulate a legacy database: create the transactions table without created_at,
        # insert a row manually, then add the column as the migration would.
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE transactions ("
            "id TEXT PRIMARY KEY, reference TEXT NOT NULL, "
            "status TEXT NOT NULL, retry_count INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE skills ("
            "id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL, "
            "name TEXT NOT NULL, execution_order INTEGER NOT NULL, "
            "status TEXT NOT NULL, arguments TEXT NOT NULL DEFAULT '{}', "
            "UNIQUE (transaction_id, name, execution_order), "
            "FOREIGN KEY (transaction_id) REFERENCES transactions(id))"
        )
        conn.execute(
            "CREATE TABLE exceptions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT NOT NULL, "
            "exception_type TEXT NOT NULL, message TEXT NOT NULL, action TEXT NOT NULL, "
            "retry_number INTEGER NOT NULL, datetime_occurred TEXT NOT NULL, "
            "screenshot_path TEXT NOT NULL DEFAULT '', "
            "FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE)"
        )
        legacy_id = "legacy-id-001"
        conn.execute(
            "INSERT INTO transactions (id, reference, status, retry_count) VALUES (?, ?, ?, ?)",
            (legacy_id, "legacy-ref", "successful", 0),
        )
        conn.commit()
        conn.execute("ALTER TABLE transactions ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        conn.commit()
        conn.close()

        # Now call save_transaction() — this should backfill created_at.
        cutoff = datetime.now(timezone.utc)
        tx = load_transaction(legacy_id, db)
        assert tx.created_at is None
        save_transaction(tx, db)

        # The row should now be findable via since= filter.
        result = list_transactions(db, since=cutoff)
        assert all(t.id != legacy_id for t in result)
