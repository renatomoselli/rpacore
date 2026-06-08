"""Tests for rpacore/report.py and list_transactions() in rpacore/persistence.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rpacore.exceptions import BusinessException, SystemException
from rpacore.persistence import list_transactions, load_transaction, save_transaction
from rpacore.report import (
    SkillReport,
    TransactionReport,
    generate_report,
    render_html,
    render_text,
)
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_skill(name: str, order: int, status: Status = Status.SUCCESSFUL) -> Skill:
    s = Skill(name, order)
    s.status = status
    return s


def biz(
    message: str,
    retry: int = 0,
    action: str = "",
    screenshot: str = "",
    stop: bool = False,
) -> BusinessException:
    return BusinessException(
        message,
        retry_number=retry,
        action=action,
        screenshot_path=screenshot,
        stop=stop,
    )


def sys_(message: str, retry: int = 0, action: str = "", screenshot: str = "") -> SystemException:
    return SystemException(message, retry_number=retry, action=action, screenshot_path=screenshot)


def make_transaction(
    retry_count: int = 0,
    status: Status = Status.SUCCESSFUL,
    skills: list[Skill] | None = None,
) -> Transaction:
    tx = Transaction(reference="ref-test")
    tx.retry_count = retry_count
    tx.status = status
    tx.skills = skills or []
    return tx


# ---------------------------------------------------------------------------
# TestGenerateReport — exception filtering and icon assignment
# ---------------------------------------------------------------------------

class TestGenerateReport:
    def test_all_business_exceptions_included(self):
        skill = make_skill("s1", 1, Status.FAILED)
        skill.exceptions = [biz("err1", retry=0), biz("err2", retry=1), biz("err3", retry=2)]
        tx = make_transaction(retry_count=2, skills=[skill])

        report = generate_report(tx)

        assert len(report.skills[0].exceptions) == 3

    def test_system_exceptions_only_last_retry_included(self):
        skill = make_skill("s1", 1, Status.FAILED)
        skill.exceptions = [
            sys_("early", retry=0),
            sys_("middle", retry=1),
            sys_("last", retry=2),
        ]
        tx = make_transaction(retry_count=2, skills=[skill])

        report = generate_report(tx)

        assert len(report.skills[0].exceptions) == 1
        assert str(report.skills[0].exceptions[0]) == "last"

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

    def test_system_exception_from_earlier_retry_excluded(self):
        skill = make_skill("s1", 1, Status.FAILED)
        skill.exceptions = [sys_("old", retry=0), sys_("current", retry=1)]
        tx = make_transaction(retry_count=1, skills=[skill])

        report = generate_report(tx)
        messages = [str(e) for e in report.skills[0].exceptions]

        assert "old" not in messages
        assert "current" in messages

    def test_mixed_exceptions_filtering(self):
        """All biz exceptions kept; only last-retry system exceptions kept."""
        skill = make_skill("s1", 1, Status.FAILED)
        skill.exceptions = [
            biz("biz0", retry=0),
            sys_("sys0", retry=0),
            biz("biz2", retry=2),
            sys_("sys2", retry=2),
        ]
        tx = make_transaction(retry_count=2, skills=[skill])

        report = generate_report(tx)
        kept = report.skills[0].exceptions

        # Both biz exceptions included, only sys with retry=2
        assert len(kept) == 3
        messages = [str(e) for e in kept]
        assert "biz0" in messages
        assert "biz2" in messages
        assert "sys2" in messages
        assert "sys0" not in messages

    def test_no_exceptions_gives_empty_list(self):
        skill = make_skill("s1", 1)
        tx = make_transaction(skills=[skill])

        report = generate_report(tx)

        assert report.skills[0].exceptions == []

    def test_icon_successful(self):
        skill = make_skill("s1", 1, Status.SUCCESSFUL)
        report = generate_report(make_transaction(skills=[skill]))
        assert report.skills[0].icon == "✓"

    def test_icon_failed(self):
        skill = make_skill("s1", 1, Status.FAILED)
        report = generate_report(make_transaction(skills=[skill]))
        assert report.skills[0].icon == "✗"

    def test_icon_skipped(self):
        skill = make_skill("s1", 1, Status.SKIPPED)
        report = generate_report(make_transaction(skills=[skill]))
        assert report.skills[0].icon == "⊘"

    def test_icon_pending(self):
        skill = make_skill("s1", 1, Status.PENDING)
        report = generate_report(make_transaction(skills=[skill]))
        assert report.skills[0].icon == "⏸"

    def test_skills_in_execution_order(self):
        s3 = make_skill("c", 3)
        s1 = make_skill("a", 1)
        s2 = make_skill("b", 2)
        tx = make_transaction(skills=[s3, s1, s2])

        report = generate_report(tx)

        assert [sr.execution_order for sr in report.skills] == [1, 2, 3]
        assert [sr.name for sr in report.skills] == ["a", "b", "c"]

    def test_report_metadata(self):
        tx = make_transaction(retry_count=3, status=Status.FAILED)
        tx.reference = "inv-42"

        report = generate_report(tx)

        assert report.reference == "inv-42"
        assert report.status == Status.FAILED
        assert report.retry_count == 3
        assert report.transaction_id == tx.id


# ---------------------------------------------------------------------------
# TestRenderText
# ---------------------------------------------------------------------------

class TestRenderText:
    def _report(self) -> TransactionReport:
        skill = make_skill("fetch", 1, Status.FAILED)
        skill.exceptions = [
            biz("invoice missing", retry=0, action="skip row", screenshot="sc.png"),
            sys_("timeout", retry=1),
        ]
        tx = make_transaction(retry_count=1, status=Status.FAILED, skills=[skill])
        return generate_report(tx)

    def test_contains_reference(self):
        report = self._report()
        report.reference = "order-99"
        # Rebuild with correct reference via generate_report sets reference from tx
        skill = make_skill("fetch", 1, Status.FAILED)
        tx = make_transaction(retry_count=1, status=Status.FAILED, skills=[skill])
        tx.reference = "order-99"
        report = generate_report(tx)
        assert "order-99" in render_text(report)

    def test_contains_skill_icon(self):
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
        skill = make_skill("clean", 1, Status.SUCCESSFUL)
        tx = make_transaction(skills=[skill])
        report = generate_report(tx)
        text = render_text(report)
        assert "[BIZ]" not in text
        assert "[SYS]" not in text

    def test_stopping_business_exception_is_labeled(self):
        skill = make_skill("validate", 1, Status.FAILED)
        skill.exceptions = [biz("bad data", retry=0, stop=True)]
        tx = make_transaction(status=Status.FAILED, skills=[skill])

        text = render_text(generate_report(tx))

        assert "stop=true" in text

    def test_skipped_skill_status_is_shown(self):
        skill = make_skill("write_output", 2, Status.SKIPPED)
        tx = make_transaction(status=Status.FAILED, skills=[skill])

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


# ---------------------------------------------------------------------------
# TestRenderHTML
# ---------------------------------------------------------------------------

class TestRenderHTML:
    def _report(self) -> TransactionReport:
        skill = make_skill("parse", 1, Status.FAILED)
        skill.exceptions = [
            biz("bad data", retry=0, action="flag row"),
            sys_("crash", retry=0, screenshot="err.png"),
        ]
        tx = make_transaction(retry_count=0, status=Status.FAILED, skills=[skill])
        tx.reference = "html-ref"
        return generate_report(tx)

    def test_contains_reference_in_title(self):
        html = render_html(self._report())
        assert "html-ref" in html

    def test_contains_failed_css_class(self):
        html = render_html(self._report())
        assert 'class="skill failed"' in html

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
        skill = make_skill("xss", 1, Status.FAILED)
        skill.exceptions = [biz("<script>alert(1)</script>", retry=0)]
        tx = make_transaction(retry_count=0, status=Status.FAILED, skills=[skill])
        html = render_html(generate_report(tx))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_successful_css_class(self):
        skill = make_skill("ok", 1, Status.SUCCESSFUL)
        tx = make_transaction(skills=[skill])
        html = render_html(generate_report(tx))
        assert 'class="skill successful"' in html

    def test_skipped_css_class(self):
        skill = make_skill("skp", 1, Status.SKIPPED)
        tx = make_transaction(skills=[skill])
        html = render_html(generate_report(tx))
        assert 'class="skill skipped"' in html

    def test_stopping_business_exception_is_labeled(self):
        skill = make_skill("validate", 1, Status.FAILED)
        skill.exceptions = [biz("bad data", retry=0, stop=True)]
        tx = make_transaction(status=Status.FAILED, skills=[skill])

        html = render_html(generate_report(tx))

        assert "stop=true" in html

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
