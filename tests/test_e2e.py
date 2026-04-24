"""End-to-end workflow contract tests for queue-driven execution."""

from __future__ import annotations

from oref.context import ProcessContext
from oref.credentials import EnvCredentialProvider
from oref.engine import Engine
from oref.exceptions import BusinessException, SystemException
from oref.notify import dispatch
from oref.persistence import list_transactions, save_transaction
from oref.queue import QueueItem, QueueStatus, SqliteQueue
from oref.report import TransactionReport, generate_report
from oref.runner import QueueRunSummary, run_queue_loop
from oref.skill import Skill
from oref.status import Status
from oref.transaction import Transaction


class _RecordingNotifier:
    def __init__(self) -> None:
        self.reports: list[TransactionReport] = []

    def send(self, report: TransactionReport) -> None:
        self.reports.append(report)


class _SuccessSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        pass


class _BusinessFailSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException("invoice validation failed", action=self.name)


class _UnexpectedFailSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise RuntimeError("submit crashed")


def _build_transaction(item: QueueItem) -> Transaction:
    mode = item.payload["mode"]
    if mode == "success":
        skill: Skill = _SuccessSkill("process_invoice", 1)
    elif mode == "business":
        skill = _BusinessFailSkill("validate_invoice", 1)
    elif mode == "unexpected":
        skill = _UnexpectedFailSkill("submit_invoice", 1)
    else:
        raise AssertionError(f"Unexpected mode: {mode!r}")
    return Transaction(reference=item.reference, skills=[skill])


def _run_scenario(tmp_path, *, reference: str, mode: str) -> tuple[
    QueueRunSummary,
    SqliteQueue,
    QueueItem,
    list[Transaction],
    list[TransactionReport],
    list[Exception | None],
]:
    queue = SqliteQueue({"db_path": str(tmp_path / "queue.db"), "max_retries": 0})
    item = QueueItem(reference=reference, payload={"mode": mode})
    queue.add(item)

    tx_db = str(tmp_path / "oref.db")
    notifier = _RecordingNotifier()
    callback_errors: list[Exception | None] = []

    def _after_item(queue_item: QueueItem, transaction: Transaction | None, error: Exception | None) -> None:
        assert queue_item.id == item.id
        assert transaction is not None
        save_transaction(transaction, db_path=tx_db)
        report = generate_report(transaction)
        dispatch([notifier], report)
        callback_errors.append(error)

    summary = run_queue_loop(
        queue=queue,
        engine=Engine(),
        build_transaction=_build_transaction,
        config={},
        credentials=EnvCredentialProvider(),
        worker_id="e2e-worker",
        after_item=_after_item,
    )

    return (
        summary,
        queue,
        item,
        list_transactions(db_path=tx_db),
        notifier.reports,
        callback_errors,
    )


class TestEndToEndQueueWorkflow:
    def test_full_success_path(self, tmp_path) -> None:
        summary, queue, item, transactions, reports, callback_errors = _run_scenario(
            tmp_path,
            reference="invoice-100",
            mode="success",
        )

        assert summary == QueueRunSummary(processed=1, completed=1, failed=0, callback_errors=0)

        stored_item = queue.get_item(item.id)
        assert stored_item is not None
        assert stored_item.status == QueueStatus.SUCCESSFUL

        assert len(transactions) == 1
        transaction = transactions[0]
        assert transaction.reference == "invoice-100"
        assert transaction.status == Status.SUCCESSFUL
        assert transaction.skills[0].status == Status.SUCCESSFUL

        assert callback_errors == [None]
        assert len(reports) == 1
        report = reports[0]
        assert report.reference == "invoice-100"
        assert report.status == Status.SUCCESSFUL
        assert report.skills[0].status == Status.SUCCESSFUL
        assert report.skills[0].exceptions == []

    def test_business_exception_path(self, tmp_path) -> None:
        summary, queue, item, transactions, reports, callback_errors = _run_scenario(
            tmp_path,
            reference="invoice-200",
            mode="business",
        )

        assert summary == QueueRunSummary(processed=1, completed=0, failed=1, callback_errors=0)

        stored_item = queue.get_item(item.id)
        assert stored_item is not None
        assert stored_item.status == QueueStatus.FAILED
        assert stored_item.retry_count == 1

        assert len(transactions) == 1
        transaction = transactions[0]
        assert transaction.reference == "invoice-200"
        assert transaction.status == Status.FAILED
        assert transaction.skills[0].status == Status.FAILED
        assert isinstance(transaction.skills[0].exceptions[0], BusinessException)

        assert callback_errors == [None]
        assert len(reports) == 1
        report = reports[0]
        assert report.reference == "invoice-200"
        assert report.status == Status.FAILED
        assert report.skills[0].status == Status.FAILED
        assert isinstance(report.skills[0].exceptions[0], BusinessException)

    def test_unexpected_error_path_is_wrapped_and_reported(self, tmp_path) -> None:
        summary, queue, item, transactions, reports, callback_errors = _run_scenario(
            tmp_path,
            reference="invoice-300",
            mode="unexpected",
        )

        assert summary == QueueRunSummary(processed=1, completed=0, failed=1, callback_errors=0)

        stored_item = queue.get_item(item.id)
        assert stored_item is not None
        assert stored_item.status == QueueStatus.FAILED
        assert stored_item.retry_count == 1

        assert len(transactions) == 1
        transaction = transactions[0]
        assert transaction.reference == "invoice-300"
        assert transaction.status == Status.FAILED
        assert transaction.skills[0].status == Status.FAILED
        assert isinstance(transaction.skills[0].exceptions[0], SystemException)
        assert str(transaction.skills[0].exceptions[0]) == "submit crashed"
        assert transaction.skills[0].exceptions[0].action == "submit_invoice"

        assert callback_errors == [None]
        assert len(reports) == 1
        report = reports[0]
        assert report.reference == "invoice-300"
        assert report.status == Status.FAILED
        assert report.skills[0].status == Status.FAILED
        assert isinstance(report.skills[0].exceptions[0], SystemException)
        assert str(report.skills[0].exceptions[0]) == "submit crashed"