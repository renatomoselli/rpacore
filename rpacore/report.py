"""Exception reporting for rpacore transactions."""

from __future__ import annotations

import json
import string
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rpacore.exceptions import BusinessException, SystemException
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.serialization import serialize_transaction
from rpacore.status import Status

if TYPE_CHECKING:
    from rpacore.skill import Skill
    from rpacore.transaction import HistoryEntry, Transaction


_ICONS: dict[Status, str] = {
    Status.SUCCESSFUL: "✓",
    Status.FAILED: "✗",
    Status.SKIPPED: "⊘",
    Status.PENDING: "⏸",
    Status.IN_PROGRESS: "⏸",
}
REPORT_FORMAT_VERSION = 1


@dataclass
class SkillReport:
    """Reporting view of a single skill's execution outcome."""

    name: str
    execution_order: int
    status: Status
    icon: str
    exceptions: list[BusinessException | SystemException]


@dataclass
class ArtifactReport:
    """Reporting view of a transaction artifact."""

    id: str
    name: str
    path: str
    kind: str
    created_at: datetime
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class OutcomeReport:
    """Terminal transaction truth captured by the owning execution boundary.

    This is a direct projection of durable transaction fields. It does not
    infer an outcome or retry decision from lifecycle status, exception prose,
    history, or queue attempts.
    """

    category: OutcomeCategory = OutcomeCategory.UNKNOWN
    retry_disposition: RetryDisposition = RetryDisposition.UNKNOWN
    failure_code: str = ""


@dataclass(frozen=True)
class ReportRecord:
    """Immutable JSON-safe report record.

    ``payload_json`` is the canonical v1 representation. ``to_dict()`` returns
    a fresh mutable view for consumers that need to inspect it.
    """

    format_version: int
    payload_json: str

    def to_dict(self) -> dict[str, object]:
        """Return an isolated decoded view of the record."""
        return json.loads(self.payload_json)


@dataclass
class TransactionReport:
    """Reporting view of a completed transaction."""

    transaction_id: str
    reference: str
    status: Status
    retry_count: int
    skills: list[SkillReport]
    outcome: OutcomeReport = field(default_factory=OutcomeReport)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    artifacts: list[ArtifactReport] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    transaction_record: dict[str, object] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    record: ReportRecord | None = None


def generate_report(transaction: Transaction) -> TransactionReport:
    """Build a TransactionReport from a completed transaction.

    Reports preserve every diagnostic attempt recorded on each skill. Operator
    views should not hide earlier retry failures by default.
    """
    skill_reports: list[SkillReport] = []
    for skill in transaction.ordered_skills():
        skill_reports.append(
            SkillReport(
                name=skill.name,
                execution_order=skill.execution_order,
                status=skill.status,
                icon=_ICONS.get(skill.status, "?"),
                exceptions=[copy(exc) for exc in skill.exceptions],
            )
        )
    artifact_reports = [
        ArtifactReport(
            id=artifact.id,
            name=artifact.name,
            path=artifact.path,
            kind=artifact.kind,
            created_at=artifact.created_at,
            metadata=_snapshot_json_mapping(artifact.metadata),
        )
        for artifact in transaction.artifacts
    ]
    try:
        transaction_record = serialize_transaction(transaction)
    except (TypeError, ValueError):
        transaction_record = {}

    report = TransactionReport(
        transaction_id=transaction.id,
        reference=transaction.reference,
        status=transaction.status,
        retry_count=transaction.retry_count,
        skills=skill_reports,
        outcome=_project_transaction_outcome(transaction),
        created_at=transaction.created_at,
        started_at=transaction.started_at,
        finished_at=transaction.finished_at,
        metadata=_snapshot_json_mapping(transaction.metadata),
        artifacts=artifact_reports,
        history=list(transaction.history),
        transaction_record=transaction_record,
    )
    report.record = _build_report_record(report)
    return report


def _build_report_record(report: TransactionReport) -> ReportRecord:
    """Build the immutable report-v1 record without changing work truth."""
    serialization_error = ""
    transaction_record: dict[str, object] | None = report.transaction_record
    if not transaction_record:
        serialization_error = "rpacore.report.transaction_serialization_failed"
        transaction_record = None
    payload: dict[str, object] = {
        "report_format_version": REPORT_FORMAT_VERSION,
        "generated_at": report.generated_at.isoformat(),
        "complete": serialization_error == "",
        "errors": (
            []
            if not serialization_error
            else [{"code": serialization_error, "scope": "transaction_record"}]
        ),
        "transaction": {
            "id": report.transaction_id,
            "reference": report.reference,
            "status": str(report.status),
            "retry_count": report.retry_count,
            "created_at": _format_dt(report.created_at),
            "started_at": _format_dt(report.started_at),
            "finished_at": _format_dt(report.finished_at),
            "metadata": _snapshot_json_mapping(report.metadata),
            "outcome": {
                "category": str(report.outcome.category),
                "retry_disposition": str(report.outcome.retry_disposition),
                "failure_code": report.outcome.failure_code,
            },
        },
        "skills": [
            {
                "name": skill.name,
                "execution_order": skill.execution_order,
                "status": str(skill.status),
                "exceptions": [
                    {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "retry_number": exc.retry_number,
                        "action": exc.action,
                        "screenshot_path": exc.screenshot_path,
                        "stops_execution": exc.stops_execution,
                    }
                    for exc in skill.exceptions
                ],
            }
            for skill in report.skills
        ],
        "artifacts": [
            {
                "id": artifact.id,
                "name": artifact.name,
                "path": artifact.path,
                "kind": artifact.kind,
                "created_at": artifact.created_at.isoformat(),
                "metadata": _snapshot_json_mapping(artifact.metadata),
            }
            for artifact in report.artifacts
        ],
        "history": [
            {
                "sequence": entry.sequence,
                "timestamp": entry.timestamp.isoformat(),
                "event": str(entry.event),
                "status": str(entry.status),
                "retry_number": entry.retry_number,
                "skill_name": entry.skill_name,
                "skill_execution_order": entry.skill_execution_order,
            }
            for entry in report.history
        ],
        "transaction_record": transaction_record,
    }
    try:
        payload_json = json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        payload_json = json.dumps(
            {
                "report_format_version": REPORT_FORMAT_VERSION,
                "generated_at": report.generated_at.isoformat(),
                "complete": False,
                "errors": [
                    {
                        "code": "rpacore.report.record_serialization_failed",
                        "type": type(exc).__name__,
                    }
                ],
                "transaction": {
                    "id": report.transaction_id,
                    "reference": report.reference,
                    "status": str(report.status),
                    "retry_count": report.retry_count,
                    "created_at": _format_dt(report.created_at),
                    "started_at": _format_dt(report.started_at),
                    "finished_at": _format_dt(report.finished_at),
                    "metadata": {},
                    "outcome": {
                        "category": str(report.outcome.category),
                        "retry_disposition": str(report.outcome.retry_disposition),
                        "failure_code": report.outcome.failure_code,
                    },
                },
                "skills": [],
                "artifacts": [],
                "history": [],
                "transaction_record": None,
            },
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return ReportRecord(REPORT_FORMAT_VERSION, payload_json)


def render_json(report: TransactionReport) -> str:
    """Render the immutable report-v1 record as canonical JSON."""
    if report.record is None:
        return _build_report_record(report).payload_json
    return report.record.payload_json


def _record_payload(report: TransactionReport) -> dict[str, object]:
    """Return the immutable report-v1 payload used by renderers."""
    return json.loads(render_json(report))


def _project_transaction_outcome(transaction: Transaction) -> OutcomeReport:
    """Project captured transaction truth without reconstructing absent evidence."""
    return OutcomeReport(
        category=transaction.outcome_category,
        retry_disposition=transaction.retry_disposition,
        failure_code=transaction.failure_code,
    )


def _snapshot_report(report: TransactionReport) -> TransactionReport:
    """Return an observer-local copy of a report's mutable diagnostic data."""
    return TransactionReport(
        transaction_id=report.transaction_id,
        reference=report.reference,
        status=report.status,
        retry_count=report.retry_count,
        skills=[
            SkillReport(
                name=skill.name,
                execution_order=skill.execution_order,
                status=skill.status,
                icon=skill.icon,
                exceptions=[copy(exc) for exc in skill.exceptions],
            )
            for skill in report.skills
        ],
        outcome=report.outcome,
        created_at=report.created_at,
        started_at=report.started_at,
        finished_at=report.finished_at,
        metadata=_snapshot_json_mapping(report.metadata),
        artifacts=[
            ArtifactReport(
                id=artifact.id,
                name=artifact.name,
                path=artifact.path,
                kind=artifact.kind,
                created_at=artifact.created_at,
                metadata=_snapshot_json_mapping(artifact.metadata),
            )
            for artifact in report.artifacts
        ],
        history=list(report.history),
        transaction_record=_snapshot_json_mapping(report.transaction_record),
        generated_at=report.generated_at,
        record=report.record,
    )


def _snapshot_json_mapping(value: dict[str, object]) -> dict[str, object]:
    """Copy nested JSON containers while leaving unsupported objects opaque."""
    return {key: _snapshot_json_value(item) for key, item in value.items()}


def _snapshot_json_value(value: object) -> object:
    if isinstance(value, list):
        return [_snapshot_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _snapshot_json_value(item)
            for key, item in value.items()
        }
    return value


def _format_dt(value: datetime | None) -> str:
    return "unknown" if value is None else value.isoformat()


def _format_json_value(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def render_text(report: TransactionReport) -> str:
    """Render a TransactionReport as a plain-text string."""
    payload = _record_payload(report)
    transaction = payload["transaction"]
    assert isinstance(transaction, dict)
    outcome = transaction["outcome"]
    assert isinstance(outcome, dict)
    lines = [
        f"Transaction: {transaction['reference']} ({transaction['id']})",
        f"Status:      {transaction['status']}  Retries: {transaction['retry_count']}",
        "Outcome:     "
        f"{outcome['category']}  Retry disposition: {outcome['retry_disposition']}",
    ]
    if outcome["failure_code"]:
        lines.append(f"Failure code: {outcome['failure_code']}")
    if not payload["complete"]:
        errors = payload["errors"]
        assert isinstance(errors, list)
        error_codes = [
            str(error["code"])
            for error in errors
            if isinstance(error, dict) and error.get("code")
        ]
        lines.append(
            "Report record incomplete: " + ", ".join(error_codes or ["unknown"])
        )
    lines.extend([
        f"Created:     {transaction['created_at']}",
        f"Started:     {transaction['started_at']}",
        f"Finished:    {transaction['finished_at']}",
        f"Generated:   {payload['generated_at']}",
        "",
    ])
    skills = payload["skills"]
    assert isinstance(skills, list)
    for skill in skills:
        assert isinstance(skill, dict)
        status = str(skill["status"])
        try:
            icon = _ICONS.get(Status(status), "?")
        except ValueError:
            icon = "?"
        lines.append(
            f"  [{icon}] {skill['name']} (order {skill['execution_order']}) — {status}"
        )
        exceptions = skill["exceptions"]
        assert isinstance(exceptions, list)
        for exc in exceptions:
            assert isinstance(exc, dict)
            kind = "BIZ" if exc["type"] == "BusinessException" else "SYS"
            stop_text = " stop=true" if exc["stops_execution"] else ""
            lines.append(
                f"      [{kind}] retry={exc['retry_number']}{stop_text}: {exc['message']}"
            )
            if exc["action"]:
                lines.append(f"             action: {exc['action']}")
            screenshot_path = exc.get("screenshot_path")
            if isinstance(screenshot_path, str) and screenshot_path:
                lines.append(f"             screenshot: {screenshot_path}")
    metadata = transaction["metadata"]
    assert isinstance(metadata, dict)
    if metadata:
        lines.extend(["", "Metadata:"])
        for key in sorted(metadata):
            lines.append(f"  {key}: {_format_json_value(metadata[key])}")
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    if artifacts:
        lines.extend(["", "Artifacts:"])
        for artifact in artifacts:
            assert isinstance(artifact, dict)
            kind = f" kind={artifact['kind']}" if artifact["kind"] else ""
            lines.append(
                f"  {artifact['name']}{kind} path={artifact['path']} "
                f"created={artifact['created_at']}"
            )
            artifact_metadata = artifact["metadata"]
            assert isinstance(artifact_metadata, dict)
            for key in sorted(artifact_metadata):
                lines.append(f"    {key}: {_format_json_value(artifact_metadata[key])}")
    history = payload["history"]
    assert isinstance(history, list)
    if history:
        lines.extend(["", "History:"])
        for entry in history:
            assert isinstance(entry, dict)
            skill = ""
            if entry["skill_name"]:
                skill = f" skill={entry['skill_name']} order={entry['skill_execution_order']}"
            lines.append(
                f"  #{entry['sequence']} {entry['timestamp']} "
                f"{entry['event']} status={entry['status']} retry={entry['retry_number']}{skill}"
            )
    return "\n".join(lines)


_HTML_TEMPLATE = string.Template(
    """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>rpacore Report &mdash; $reference</title>
<style>
body{font-family:monospace;padding:1rem;max-width:900px;margin:auto}
h2{margin-bottom:.25rem}
.skill{margin:.5rem 0;padding:.5rem;border:1px solid #ccc;border-radius:4px}
.successful{border-color:#4caf50}.failed{border-color:#f44336}
.skipped{border-color:#9e9e9e}.pending{border-color:#ff9800}
.in-progress{border-color:#2196f3}
.exc{margin:.25rem 0 .25rem 1.5rem;font-size:.9em}
.biz{color:#e65100}.sys{color:#b71c1c}
.metadata{margin-top:1rem}.metadata li{margin:.25rem 0}
.artifacts{margin-top:1rem}.artifacts li{margin:.25rem 0}
.history{margin-top:1rem}.history li{margin:.25rem 0}
</style>
</head>
<body>
<h2>$reference</h2>
<p>Status: <strong>$status</strong> &nbsp; Retries: $retry_count &nbsp; Generated: $generated_at</p>
<p>Outcome: <strong>$outcome_category</strong> &nbsp; Retry disposition: $retry_disposition</p>$failure_code_html$report_error_html
<p>Created: $created_at &nbsp; Started: $started_at &nbsp; Finished: $finished_at</p>
<p>ID: <code>$transaction_id</code></p>
$skills_html
$metadata_html
$artifacts_html
$history_html
</body>
</html>"""
)

_SKILL_TEMPLATE = string.Template(
    """\
<div class="skill $css_class">
  <strong>$icon $name</strong> (order $execution_order) &mdash; $status
  $exceptions_html
</div>"""
)

_EXC_TEMPLATE = string.Template(
    """\
<div class="exc $exc_class"><strong>[$kind]</strong> retry=$retry_number: $message$action_html$screenshot_html</div>"""
)


_HISTORY_TEMPLATE = string.Template(
    """\
<section class="history">
  <h3>History</h3>
  <ol>
$history_items
  </ol>
</section>"""
)

_HISTORY_ITEM_TEMPLATE = string.Template(
    """\
    <li><code>#$sequence</code> $timestamp $event status=$status retry=$retry_number$skill</li>"""
)


_METADATA_TEMPLATE = string.Template(
    """\
<section class="metadata">
  <h3>Metadata</h3>
  <ul>
$metadata_items
  </ul>
</section>"""
)


_METADATA_ITEM_TEMPLATE = string.Template(
    """\
    <li><code>$key</code>: $value</li>"""
)


_ARTIFACTS_TEMPLATE = string.Template(
    """\
<section class="artifacts">
  <h3>Artifacts</h3>
  <ul>
$artifact_items
  </ul>
</section>"""
)


_ARTIFACT_ITEM_TEMPLATE = string.Template(
    """\
    <li><strong>$name</strong>$kind path=<code>$path</code> created=$created_at$metadata</li>"""
)


def _esc(text: str) -> str:
    """Minimal HTML entity escaping."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_html(report: TransactionReport) -> str:
    """Render a TransactionReport as an HTML string."""
    payload = _record_payload(report)
    transaction = payload["transaction"]
    assert isinstance(transaction, dict)
    outcome = transaction["outcome"]
    assert isinstance(outcome, dict)
    errors = payload["errors"]
    assert isinstance(errors, list)
    error_codes = [
        str(error["code"])
        for error in errors
        if isinstance(error, dict) and error.get("code")
    ]
    skills_parts: list[str] = []
    skills = payload["skills"]
    assert isinstance(skills, list)
    for skill in skills:
        assert isinstance(skill, dict)
        exc_parts: list[str] = []
        exceptions = skill["exceptions"]
        assert isinstance(exceptions, list)
        for exc in exceptions:
            assert isinstance(exc, dict)
            kind = "BIZ" if exc["type"] == "BusinessException" else "SYS"
            exc_class = "biz" if kind == "BIZ" else "sys"
            stop_html = " stop=true" if exc["stops_execution"] else ""
            action_html = (
                f" &mdash; action: {_esc(str(exc['action']))}" if exc["action"] else ""
            )
            screenshot_path = exc.get("screenshot_path")
            screenshot_html = (
                f'<br><a href="{_esc(screenshot_path)}">screenshot</a>'
                if isinstance(screenshot_path, str) and screenshot_path
                else ""
            )
            exc_parts.append(
                _EXC_TEMPLATE.substitute(
                    exc_class=exc_class,
                    kind=kind,
                    retry_number=f"{exc['retry_number']}{stop_html}",
                    message=_esc(str(exc["message"])),
                    action_html=action_html,
                    screenshot_html=screenshot_html,
                )
            )
        status = str(skill["status"])
        try:
            icon = _ICONS.get(Status(status), "?")
        except ValueError:
            icon = "?"
        css_class = status.replace("_", "-")
        skills_parts.append(
            _SKILL_TEMPLATE.substitute(
                css_class=css_class,
                icon=icon,
                name=_esc(str(skill["name"])),
                execution_order=skill["execution_order"],
                status=status,
                exceptions_html="\n  ".join(exc_parts),
            )
        )
    history_html = ""
    history = payload["history"]
    assert isinstance(history, list)
    if history:
        items: list[str] = []
        for entry in history:
            assert isinstance(entry, dict)
            skill = ""
            if entry["skill_name"]:
                skill = (
                    f" skill={_esc(str(entry['skill_name']))}"
                    f" order={entry['skill_execution_order']}"
                )
            items.append(
                _HISTORY_ITEM_TEMPLATE.substitute(
                    sequence=entry["sequence"],
                    timestamp=_esc(str(entry["timestamp"])),
                    event=_esc(str(entry["event"])),
                    status=_esc(str(entry["status"])),
                    retry_number=entry["retry_number"],
                    skill=skill,
                )
            )
        history_html = _HISTORY_TEMPLATE.substitute(history_items="\n".join(items))
    metadata_html = ""
    metadata = transaction["metadata"]
    assert isinstance(metadata, dict)
    if metadata:
        items = [
            _METADATA_ITEM_TEMPLATE.substitute(
                key=_esc(key),
                value=_esc(_format_json_value(metadata[key])),
            )
            for key in sorted(metadata)
        ]
        metadata_html = _METADATA_TEMPLATE.substitute(metadata_items="\n".join(items))
    artifacts_html = ""
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    if artifacts:
        items = []
        for artifact in artifacts:
            assert isinstance(artifact, dict)
            kind = f" kind={_esc(str(artifact['kind']))}" if artifact["kind"] else ""
            metadata = ""
            artifact_metadata = artifact["metadata"]
            assert isinstance(artifact_metadata, dict)
            if artifact_metadata:
                metadata_items = [
                    f"{_esc(key)}={_esc(_format_json_value(artifact_metadata[key]))}"
                    for key in sorted(artifact_metadata)
                ]
                metadata = f" metadata={'; '.join(metadata_items)}"
            items.append(
                _ARTIFACT_ITEM_TEMPLATE.substitute(
                    name=_esc(str(artifact["name"])),
                    kind=kind,
                    path=_esc(str(artifact["path"])),
                    created_at=_esc(str(artifact["created_at"])),
                    metadata=metadata,
                )
            )
        artifacts_html = _ARTIFACTS_TEMPLATE.substitute(artifact_items="\n".join(items))
    return _HTML_TEMPLATE.substitute(
        reference=_esc(str(transaction["reference"])),
        status=transaction["status"],
        retry_count=transaction["retry_count"],
        outcome_category=_esc(str(outcome["category"])),
        retry_disposition=_esc(str(outcome["retry_disposition"])),
        failure_code_html=(
            f"\n<p>Failure code: <code>{_esc(str(outcome['failure_code']))}</code></p>"
            if outcome["failure_code"]
            else ""
        ),
        report_error_html=(
            "\n<p>Report record incomplete: <code>"
            + _esc(", ".join(error_codes or ["unknown"]))
            + "</code></p>"
            if not payload["complete"]
            else ""
        ),
        generated_at=_esc(str(payload["generated_at"])),
        created_at=_esc(str(transaction["created_at"])),
        started_at=_esc(str(transaction["started_at"])),
        finished_at=_esc(str(transaction["finished_at"])),
        transaction_id=_esc(str(transaction["id"])),
        skills_html="\n".join(skills_parts),
        metadata_html=metadata_html,
        artifacts_html=artifacts_html,
        history_html=history_html,
    )
