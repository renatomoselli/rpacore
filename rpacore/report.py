"""Exception reporting for rpacore transactions."""

from __future__ import annotations

import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rpacore.exceptions import BusinessException, SystemException
from rpacore.status import Status

if TYPE_CHECKING:
    from rpacore.skill import Skill
    from rpacore.transaction import Transaction


_ICONS: dict[Status, str] = {
    Status.SUCCESSFUL: "✓",
    Status.FAILED: "✗",
    Status.SKIPPED: "⊘",
    Status.PENDING: "⏸",
    Status.IN_PROGRESS: "⏸",
}


@dataclass
class SkillReport:
    """Reporting view of a single skill's execution outcome."""

    name: str
    execution_order: int
    status: Status
    icon: str
    exceptions: list[BusinessException | SystemException]


@dataclass
class TransactionReport:
    """Reporting view of a completed transaction."""

    transaction_id: str
    reference: str
    status: Status
    retry_count: int
    skills: list[SkillReport]
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def generate_report(transaction: Transaction) -> TransactionReport:
    """Build a TransactionReport from a completed transaction.

    Exception filtering rules:
    - ALL BusinessExceptions are included (expected violations, always relevant).
    - ONLY SystemExceptions whose retry_number equals transaction.retry_count are
      included. Earlier-retry system exceptions are noise once execution has moved on;
      the last-retry system exception is the one that matters for diagnosis.
    """
    skill_reports: list[SkillReport] = []
    for skill in transaction.ordered_skills():
        filtered = [
            exc
            for exc in skill.exceptions
            if isinstance(exc, BusinessException)
            or (
                isinstance(exc, SystemException)
                and exc.retry_number == transaction.retry_count
            )
        ]
        skill_reports.append(
            SkillReport(
                name=skill.name,
                execution_order=skill.execution_order,
                status=skill.status,
                icon=_ICONS.get(skill.status, "?"),
                exceptions=filtered,
            )
        )
    return TransactionReport(
        transaction_id=transaction.id,
        reference=transaction.reference,
        status=transaction.status,
        retry_count=transaction.retry_count,
        skills=skill_reports,
    )


def render_text(report: TransactionReport) -> str:
    """Render a TransactionReport as a plain-text string."""
    lines = [
        f"Transaction: {report.reference} ({report.transaction_id})",
        f"Status:      {report.status}  Retries: {report.retry_count}",
        f"Generated:   {report.generated_at.isoformat()}",
        "",
    ]
    for sr in report.skills:
        lines.append(
            f"  [{sr.icon}] {sr.name} (order {sr.execution_order}) — {sr.status}"
        )
        for exc in sr.exceptions:
            kind = "BIZ" if isinstance(exc, BusinessException) else "SYS"
            lines.append(f"      [{kind}] retry={exc.retry_number}: {exc}")
            if exc.action:
                lines.append(f"             action: {exc.action}")
            if exc.screenshot_path:
                lines.append(f"             screenshot: {exc.screenshot_path}")
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
</style>
</head>
<body>
<h2>$reference</h2>
<p>Status: <strong>$status</strong> &nbsp; Retries: $retry_count &nbsp; Generated: $generated_at</p>
<p>ID: <code>$transaction_id</code></p>
$skills_html
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
    skills_parts: list[str] = []
    for sr in report.skills:
        exc_parts: list[str] = []
        for exc in sr.exceptions:
            kind = "BIZ" if isinstance(exc, BusinessException) else "SYS"
            exc_class = "biz" if isinstance(exc, BusinessException) else "sys"
            action_html = f" &mdash; action: {_esc(exc.action)}" if exc.action else ""
            screenshot_html = (
                f'<br><a href="{_esc(exc.screenshot_path)}">screenshot</a>'
                if exc.screenshot_path
                else ""
            )
            exc_parts.append(
                _EXC_TEMPLATE.substitute(
                    exc_class=exc_class,
                    kind=kind,
                    retry_number=exc.retry_number,
                    message=_esc(str(exc)),
                    action_html=action_html,
                    screenshot_html=screenshot_html,
                )
            )
        css_class = sr.status.replace("_", "-")
        skills_parts.append(
            _SKILL_TEMPLATE.substitute(
                css_class=css_class,
                icon=sr.icon,
                name=_esc(sr.name),
                execution_order=sr.execution_order,
                status=sr.status,
                exceptions_html="\n  ".join(exc_parts),
            )
        )
    return _HTML_TEMPLATE.substitute(
        reference=_esc(report.reference),
        status=report.status,
        retry_count=report.retry_count,
        generated_at=_esc(report.generated_at.isoformat()),
        transaction_id=_esc(report.transaction_id),
        skills_html="\n".join(skills_parts),
    )
