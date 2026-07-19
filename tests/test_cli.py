"""Subprocess tests for the rpacore command line interface."""

from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import rpacore.cli as cli_module
from rpacore.doctor import collect_doctor_result
from rpacore import (
    Artifact,
    BusinessException,
    HistoryEvent,
    Skill,
    Status,
    Transaction,
    TransactionPage,
    TransactionSummary,
    QueueItem,
    SqliteQueue,
    list_transactions,
    save_transaction,
)
from rpacore.persistence import _delete_unbound_pending_transaction


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing_python_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT)
        if existing_python_path is None
        else str(REPO_ROOT) + os.pathsep + existing_python_path
    )
    return subprocess.run(
        [sys.executable, "-m", "rpacore.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def query_page_for(*transactions: Transaction) -> TransactionPage:
    """Return one complete summary page for CLI query-boundary tests."""
    return TransactionPage(
        transactions=tuple(
            TransactionSummary(
                id=transaction.id,
                reference=transaction.reference,
                status=transaction.status,
                retry_count=transaction.retry_count,
                created_at=transaction.created_at,
            )
            for transaction in transactions
        ),
        has_more=False,
        next_cursor=None,
    )


def write_project(tmp_path: Path, main_source: str, *, manifest: str | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "rpacore.toml").write_text(
        manifest
        if manifest is not None
        else textwrap.dedent("""\
            [project]
            entrypoint = "main:main"

            [storage]
            transaction_db_path = "rpacore.db"
            """),
        encoding="utf-8",
    )
    (project / "main.py").write_text(textwrap.dedent(main_source), encoding="utf-8")
    return project


class TestCliRun:
    def test_none_entrypoint_result_exits_zero(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                return None
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 0
        assert result.stderr == ""

    def test_integer_entrypoint_result_is_process_exit_code(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                return 7
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 7
        assert result.stderr == ""

    def test_entrypoint_can_import_project_modules_during_execution(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                from local_module import exit_code
                return exit_code()
            """,
        )
        (project / "local_module.py").write_text(
            "def exit_code():\n    return 3\n",
            encoding="utf-8",
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 3
        assert result.stderr == ""

    def test_entrypoint_exception_exits_one_with_stderr(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                raise OSError("database open failed")
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 1
        assert "Project execution failed" in result.stderr
        assert "database open failed" in result.stderr

    def test_invalid_manifest_exits_two_with_stderr(self, tmp_path: Path) -> None:
        project = write_project(tmp_path, "def main():\n    return 0\n", manifest="[project]\n")

        result = run_cli("run", cwd=project)

        assert result.returncode == 2
        assert "Project manifest or entrypoint is invalid" in result.stderr

    def test_entrypoint_resolution_failure_exits_two_with_stderr(self, tmp_path: Path) -> None:
        project = write_project(tmp_path, "def other():\n    return 0\n")

        result = run_cli("run", cwd=project)

        assert result.returncode == 2
        assert "Project manifest or entrypoint is invalid" in result.stderr

    def test_invalid_entrypoint_return_value_exits_two(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                return "ok"
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 2
        assert "must return None or an integer exit code" in result.stderr

    def test_out_of_range_entrypoint_return_value_exits_two(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                return 256
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 2
        assert "out-of-range exit code" in result.stderr

    def test_usage_error_exits_two(self, tmp_path: Path) -> None:
        result = run_cli(cwd=tmp_path)

        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_unknown_command_exits_two(self, tmp_path: Path) -> None:
        result = run_cli("bogus", cwd=tmp_path)

        assert result.returncode == 2
        assert "invalid choice" in result.stderr

    def test_empty_pythonpath_is_preserved_for_subprocess(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PYTHONPATH", "")

        result = run_cli("version", cwd=tmp_path)

        assert result.returncode == 0
        assert result.stderr == ""


class TestCliInit:
    def test_init_creates_project_that_runs_without_edits(self, tmp_path: Path) -> None:
        result = run_cli("init", "demo_project", cwd=tmp_path)

        assert result.returncode == 0
        assert "Created RPA Core project: demo_project" in result.stdout
        project = tmp_path / "demo_project"
        assert (project / "pyproject.toml").exists()
        assert (project / "rpacore.toml").exists()
        assert (project / "config.toml").exists()
        assert (project / "main.py").exists()
        assert (project / "skills" / "__init__.py").exists()
        assert (project / "skills" / "greeting.py").exists()
        assert (project / "tests" / "test_greeting_skill.py").exists()
        assert (project / ".gitignore").exists()
        assert not (project / "LICENSE").exists()
        assert not (project / "NOTICE").exists()

        run_result = run_cli("run", cwd=project)

        assert run_result.returncode == 0
        assert run_result.stderr == ""
        assert (project / "greeting.txt").read_text(encoding="utf-8") == "Hello, Alice\n"
        assert (project / "rpacore.db").exists()
        assert "execute_transaction(" in (
            project / "main.py"
        ).read_text(encoding="utf-8")
        transactions = list_transactions(str(project / "rpacore.db"))
        assert len(transactions) == 1
        assert transactions[0].status is Status.SUCCESSFUL
        assert HistoryEvent.SKILL_SUCCEEDED in [
            entry.event for entry in transactions[0].history
        ]

    def test_generated_project_skill_test_passes(self, tmp_path: Path) -> None:
        init_result = run_cli("init", "demo_project", cwd=tmp_path)
        assert init_result.returncode == 0
        project = tmp_path / "demo_project"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + str(project)

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_greeting_skill.py"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0

    def test_init_existing_path_exits_two(self, tmp_path: Path) -> None:
        (tmp_path / "demo_project").mkdir()

        result = run_cli("init", "demo_project", cwd=tmp_path)

        assert result.returncode == 2
        assert "Project path already exists" in result.stderr

    def test_init_write_failure_exits_one_and_reports_cleanup(
        self,
        tmp_path: Path,
        capsys,
        monkeypatch,
    ) -> None:
        def fail_write(project_dir: Path) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(cli_module, "_write_project_files", fail_write)

        result = cli_module.main(["init", str(tmp_path / "demo_project")])

        captured = capsys.readouterr()
        assert result == 1
        assert "Could not create project" in captured.err
        assert "disk full" in captured.err
        assert not (tmp_path / "demo_project").exists()


class TestCliDoctor:
    def test_unexpected_collection_error_is_bounded(self, monkeypatch, capsys) -> None:
        def raise_unexpected_error(**_kwargs: object) -> object:
            raise RuntimeError("unexpected diagnostic failure")

        monkeypatch.setattr(cli_module, "collect_doctor_result", raise_unexpected_error)

        result = cli_module.main(["doctor", "--json"])

        captured = capsys.readouterr()
        assert result == 1
        assert captured.out == ""
        assert captured.err == "rpacore: Could not complete doctor diagnostics\n"

    def test_json_without_project_is_parseable_and_clean(self, tmp_path: Path) -> None:
        result = run_cli("doctor", "--json", cwd=tmp_path)

        assert result.returncode == 0
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["doctor_format_version"] == 1
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks["runtime.python"]["status"] == "pass"
        assert checks["project.manifest"]["status"] == "not_applicable"

    def test_transaction_database_is_inspected_without_mutation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        save_transaction(Transaction(reference="doctor"), str(db_path))
        before = db_path.read_bytes()

        result = run_cli("doctor", "--transaction-db", str(db_path), "--json", cwd=tmp_path)

        assert result.returncode == 0
        assert result.stderr == ""
        assert db_path.read_bytes() == before
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["transactions.schema"]["status"] == "pass"
        assert checks["transactions.quick_check"]["status"] == "pass"
        assert checks["transactions.foreign_keys"]["status"] == "pass"

    def test_missing_explicit_database_is_structured_failure_without_creation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "missing.db"

        result = run_cli("doctor", "--transaction-db", str(db_path), "--json", cwd=tmp_path)

        assert result.returncode == 1
        assert result.stderr == ""
        assert not db_path.exists()
        payload = json.loads(result.stdout)
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks["transactions.schema"]["status"] == "fail"
        assert str(db_path) not in result.stdout

    def test_non_sqlite_database_is_a_structured_failure(self, tmp_path: Path) -> None:
        db_path = tmp_path / "not-a-database.db"
        db_path.write_bytes(b"not a SQLite database")

        result = run_cli("doctor", "--transaction-db", str(db_path), "--json", cwd=tmp_path)

        assert result.returncode == 1
        assert result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["transactions.schema"] == {
            "id": "transactions.schema",
            "status": "fail",
            "summary": "Transaction database is not an SQLite database",
            "details": {},
        }

    def test_future_database_is_structured_failure_without_mutation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "future.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE rpacore_schema_versions "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO rpacore_schema_versions VALUES ('transactions', 99)")
            conn.commit()
        finally:
            conn.close()
        before = db_path.read_bytes()

        result = run_cli("doctor", "--transaction-db", str(db_path), "--json", cwd=tmp_path)

        assert result.returncode == 1
        assert db_path.read_bytes() == before
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["transactions.schema"]["status"] == "fail"

    def test_queue_health_reports_counts_without_reading_payloads(self, tmp_path: Path) -> None:
        db_path = tmp_path / "queue.db"
        queue = SqliteQueue({"db_path": str(db_path)})
        queue.add(QueueItem(reference="private-reference", payload={"secret": "value"}))
        before = db_path.read_bytes()

        result = run_cli("doctor", "--queue-db", str(db_path), "--json", cwd=tmp_path)

        assert result.returncode == 0
        assert db_path.read_bytes() == before
        assert "private-reference" not in result.stdout
        assert "secret" not in result.stdout
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["queue.health"] == {
            "id": "queue.health",
            "status": "pass",
            "summary": "Queue items have complete claim bindings",
            "details": {
                "bound_items": 0,
                "pending_items": 1,
                "in_progress_items": 0,
                "successful_items": 0,
                "failed_items": 0,
                "unknown_status_items": 0,
                "oldest_age_seconds": 0,
            },
        }

    def test_project_discovery_never_imports_entrypoint(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            from pathlib import Path
            Path('entrypoint-imported.txt').write_text('unexpected', encoding='utf-8')

            def main():
                return 0
            """,
        )
        db_path = project / "rpacore.db"
        save_transaction(Transaction(reference="doctor"), str(db_path))

        result = run_cli("doctor", "--json", cwd=project)

        assert result.returncode == 0
        assert not (project / "entrypoint-imported.txt").exists()
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["project.manifest"]["status"] == "pass"
        assert checks["transactions.schema"]["status"] == "pass"

    def test_configured_queue_database_is_discovered_readonly(self, tmp_path: Path) -> None:
        project = write_project(tmp_path, "def main():\n    return 0\n")
        (project / "config.toml").write_text(
            "[queue]\ndb_path = 'queue.db'\n",
            encoding="utf-8",
        )
        save_transaction(Transaction(reference="doctor"), str(project / "rpacore.db"))
        queue_path = project / "queue.db"
        SqliteQueue({"db_path": str(queue_path)}).add(
            QueueItem(reference="doctor", payload={})
        )
        before = queue_path.read_bytes()

        result = run_cli("doctor", "--json", cwd=project)

        assert result.returncode == 0
        assert queue_path.read_bytes() == before
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["project.config"]["status"] == "pass"
        assert checks["queue.schema"]["status"] == "pass"
        assert checks["queue.health"]["details"]["pending_items"] == 1

    def test_wal_database_sidecars_are_not_changed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "wal.db"
        save_transaction(Transaction(reference="doctor"), str(db_path))
        connection = sqlite3.connect(db_path)
        try:
            assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
            connection.execute("CREATE TABLE wal_probe (id INTEGER)")
            connection.commit()
            sidecars = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
            assert all(path.exists() for path in sidecars)
            before = {path: path.read_bytes() for path in sidecars}

            result = collect_doctor_result(transaction_db_path=str(db_path))

            assert {path: path.read_bytes() for path in sidecars} == before
        finally:
            connection.close()
        checks = {check.id: check for check in result.checks}
        assert checks["transactions.journal"].status == "warning"
        assert checks["transactions.schema"].status == "not_applicable"
        assert checks["transactions.quick_check"].status == "not_applicable"

    def test_malformed_foreign_key_is_a_structured_json_failure(self, tmp_path: Path) -> None:
        db_path = tmp_path / "foreign-key-mismatch.db"
        save_transaction(Transaction(reference="doctor"), str(db_path))
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("CREATE TABLE parent (id INTEGER)")
            connection.execute(
                "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))"
            )
            connection.commit()
        finally:
            connection.close()

        result = run_cli("doctor", "--transaction-db", str(db_path), "--json", cwd=tmp_path)

        assert result.returncode == 1
        assert result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["transactions.foreign_keys"]["status"] == "fail"

    def test_malformed_queue_config_is_a_structured_failure(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("queue = 42\n", encoding="utf-8")

        result = run_cli("doctor", "--config", str(config_path), "--json", cwd=tmp_path)

        assert result.returncode == 1
        assert result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["project.config"]["status"] == "fail"

    def test_queue_item_without_claimed_at_is_unhealthy(self, tmp_path: Path) -> None:
        db_path = tmp_path / "queue.db"
        queue = SqliteQueue({"db_path": str(db_path)})
        item = QueueItem(reference="doctor", payload={})
        queue.add(item)
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE queue_items SET status = 'in_progress', claimed_by = 'worker', "
                "claim_token = 'claim-token', claimed_at = NULL WHERE id = ?",
                (item.id,),
            )
            connection.commit()
        finally:
            connection.close()

        result = run_cli("doctor", "--queue-db", str(db_path), "--json", cwd=tmp_path)

        assert result.returncode == 1
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["queue.health"]["status"] == "fail"

    def test_queue_health_counts_unknown_statuses_without_disclosure(self, tmp_path: Path) -> None:
        db_path = tmp_path / "queue.db"
        queue = SqliteQueue({"db_path": str(db_path)})
        item = QueueItem(reference="doctor", payload={})
        queue.add(item)
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE queue_items SET status = 'unknown-internal-state' WHERE id = ?",
                (item.id,),
            )
            connection.commit()
        finally:
            connection.close()

        result = run_cli("doctor", "--queue-db", str(db_path), "--json", cwd=tmp_path)

        assert result.returncode == 0
        assert "unknown-internal-state" not in result.stdout
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["queue.health"]["details"]["unknown_status_items"] == 1


class TestCliTransaction:
    def test_transaction_resume_is_not_a_cli_command(self, tmp_path: Path) -> None:
        result = run_cli("transaction", "resume", cwd=tmp_path)

        assert result.returncode == 2
        assert "invalid choice" in result.stderr

    def test_transaction_list_missing_database_fails_without_creating_it(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"

        result = run_cli("transaction", "list", "--db", str(db_path), cwd=tmp_path)

        assert result.returncode == 1
        assert result.stdout == ""
        assert "SQLite database not found" in result.stderr
        assert not db_path.exists()

    def test_transaction_list_rejects_blank_database_path(self, tmp_path: Path) -> None:
        result = run_cli("transaction", "list", "--db", "", cwd=tmp_path)

        assert result.returncode == 1
        assert result.stdout == ""
        assert "transaction_db_path expected non-empty SQLite file path" in result.stderr
        assert list(tmp_path.iterdir()) == []

    def test_transaction_list_rejects_future_schema_without_mutation(self, tmp_path: Path) -> None:
        db_path = tmp_path / "future.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE rpacore_schema_versions ("
                "component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO rpacore_schema_versions VALUES ('transactions', 99)"
            )
            conn.commit()
        finally:
            conn.close()
        before = db_path.read_bytes()

        result = run_cli("transaction", "list", "--db", str(db_path), cwd=tmp_path)

        assert result.returncode == 1
        assert "Unsupported transaction schema version 99" in result.stderr
        assert db_path.read_bytes() == before

    def test_transaction_list_does_not_change_journal_mode(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        save_transaction(Transaction(reference="journal"), str(db_path))
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        finally:
            conn.close()

        result = run_cli("transaction", "list", "--db", str(db_path), cwd=tmp_path)

        assert result.returncode == 0
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()

    def test_transaction_list_populated_database_human_output(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        tx = Transaction(reference="invoice-001", status=Status.SUCCESSFUL)
        save_transaction(tx, db_path=str(db_path))

        result = run_cli("transaction", "list", "--db", str(db_path), cwd=tmp_path)

        assert result.returncode == 0
        assert tx.id in result.stdout
        assert "successful" in result.stdout
        assert "invoice-001" in result.stdout
        assert result.stderr == ""

    def test_transaction_list_json_stdout_is_parseable_and_clean(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        tx = Transaction(
            reference="invoice-001",
            status=Status.SUCCESSFUL,
            state={"invoice": "001"},
            metadata={"customer": "acme"},
        )
        save_transaction(tx, db_path=str(db_path))

        result = run_cli(
            "transaction",
            "list",
            "--db",
            str(db_path),
            "--json",
            cwd=tmp_path,
        )

        assert result.returncode == 0
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == 1
        assert payload["command"] == "transaction:list"
        assert payload["limit"] == 100
        assert payload["transactions"][0]["id"] == tx.id
        assert payload["transactions"][0]["transaction_format_version"] == 1
        assert payload["transactions"][0]["status"] == "successful"
        assert payload["transactions"][0]["state"] == {"invoice": "001"}
        assert payload["transactions"][0]["metadata"] == {"customer": "acme"}

    def test_transaction_list_limit_is_visible_and_applied(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        first = Transaction(reference="first", status=Status.SUCCESSFUL)
        second = Transaction(reference="second", status=Status.SUCCESSFUL)
        save_transaction(first, db_path=str(db_path))
        save_transaction(second, db_path=str(db_path))

        human = run_cli(
            "transaction",
            "list",
            "--db",
            str(db_path),
            "--limit",
            "1",
            cwd=tmp_path,
        )
        json_result = run_cli(
            "transaction",
            "list",
            "--db",
            str(db_path),
            "--limit",
            "1",
            "--json",
            cwd=tmp_path,
        )

        assert human.returncode == 0
        assert "Showing up to 1 transactions." in human.stdout
        payload = json.loads(json_result.stdout)
        assert payload["limit"] == 1
        assert len(payload["transactions"]) == 1

    def test_transaction_list_invalid_limit_exits_one(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"

        result = run_cli(
            "transaction",
            "list",
            "--db",
            str(db_path),
            "--limit",
            "0",
            cwd=tmp_path,
        )

        assert result.returncode == 2
        assert result.stdout == ""
        assert "must be an integer >= 1: 0" in result.stderr

    def test_transaction_show_human_output(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        skill = Skill("download", 1, arguments={"invoice": "001"})
        skill.status = Status.FAILED
        skill.exceptions.append(BusinessException("bad invoice", action="download"))
        tx = Transaction(reference="invoice-001", status=Status.FAILED, skills=[skill])
        tx.metadata = {"customer": "acme"}
        tx.artifacts = [Artifact(name="invoice", path="invoice.pdf", kind="pdf")]
        tx.append_history(skill=skill, event=HistoryEvent.SKILL_FAILED)
        save_transaction(tx, db_path=str(db_path))

        result = run_cli("transaction", "show", tx.id, "--db", str(db_path), cwd=tmp_path)

        assert result.returncode == 0
        assert f"ID:          {tx.id}" in result.stdout
        assert "Reference:   invoice-001" in result.stdout
        assert "1. download: failed" in result.stdout
        assert "- business: bad invoice" in result.stdout
        assert "1. skill_failed status=failed retry=0 skill=download" in result.stdout
        assert "invoice (pdf): invoice.pdf" in result.stdout
        assert "Artifacts:   1" in result.stdout
        assert result.stderr == ""

    def test_transaction_show_json_contains_loaded_transaction_details(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        skill = Skill("download", 1, arguments={"invoice": "001"})
        skill.status = Status.FAILED
        skill.exceptions.append(BusinessException("bad invoice", action="download"))
        tx = Transaction(
            reference="invoice-001",
            status=Status.FAILED,
            state={"invoice": "001"},
            metadata={"customer": "acme"},
            skills=[skill],
            artifacts=[Artifact(name="invoice", path="invoice.pdf", kind="pdf")],
        )
        tx.append_history(skill=skill, event=HistoryEvent.SKILL_FAILED)
        save_transaction(tx, db_path=str(db_path))

        result = run_cli(
            "transaction",
            "show",
            tx.id,
            "--db",
            str(db_path),
            "--json",
            cwd=tmp_path,
        )

        assert result.returncode == 0
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == 1
        assert payload["command"] == "transaction:show"
        detail = payload["transaction"]
        assert detail["transaction_format_version"] == 1
        assert detail["id"] == tx.id
        assert detail["state"] == {"invoice": "001"}
        assert detail["metadata"] == {"customer": "acme"}
        assert detail["skills"][0]["exceptions"][0]["type"] == "business"
        assert detail["history"][0]["event"] == "skill_failed"
        assert detail["artifacts"][0]["path"] == "invoice.pdf"

    def test_transaction_export_json_stdout_is_versioned_and_clean(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        artifact_path = tmp_path / "invoice.txt"
        artifact_path.write_text("secret artifact body", encoding="utf-8")
        older = Transaction(
            reference="older",
            status=Status.SUCCESSFUL,
            created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
            state={"invoice": "001"},
            metadata={"customer": "acme"},
            artifacts=[Artifact(name="invoice", path=str(artifact_path), kind="txt")],
        )
        newer = Transaction(
            reference="newer",
            status=Status.FAILED,
            created_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
        )
        save_transaction(older, db_path=str(db_path))
        save_transaction(newer, db_path=str(db_path))

        result = run_cli(
            "transaction",
            "export",
            "--db",
            str(db_path),
            "--format",
            "json",
            cwd=tmp_path,
        )

        assert result.returncode == 0
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert set(payload) == {
            "export_format_version",
            "exported_at",
            "framework_version",
            "transactions",
        }
        assert payload["export_format_version"] == 1
        assert payload["framework_version"] == cli_module.__version__
        assert payload["exported_at"].endswith("+00:00")
        assert [tx["reference"] for tx in payload["transactions"]] == ["newer", "older"]
        assert payload["transactions"][1]["transaction_format_version"] == 1
        assert payload["transactions"][1]["state"] == {"invoice": "001"}
        assert "secret artifact body" not in result.stdout

    def test_transaction_export_ndjson_stdout_is_parseable_and_clean(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        first = Transaction(
            reference="first",
            status=Status.SUCCESSFUL,
            created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        )
        second = Transaction(
            reference="second",
            status=Status.SUCCESSFUL,
            created_at=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc),
        )
        save_transaction(first, db_path=str(db_path))
        save_transaction(second, db_path=str(db_path))

        result = run_cli(
            "transaction",
            "export",
            "--db",
            str(db_path),
            "--format",
            "ndjson",
            cwd=tmp_path,
        )

        assert result.returncode == 0
        assert result.stderr == ""
        lines = result.stdout.splitlines()
        assert len(lines) == 2
        records = [json.loads(line) for line in lines]
        assert set(records[0]) == {
            "artifacts",
            "created_at",
            "export_format_version",
            "exported_at",
            "finished_at",
            "framework_version",
            "history",
            "id",
            "metadata",
            "reference",
            "retry_count",
            "skills",
            "started_at",
            "state",
            "status",
            "transaction_format_version",
        }
        assert [record["reference"] for record in records] == ["second", "first"]
        assert {record["export_format_version"] for record in records} == {1}
        assert {record["transaction_format_version"] for record in records} == {1}
        assert {record["framework_version"] for record in records} == {
            cli_module.__version__,
        }
        assert all(record["exported_at"].endswith("+00:00") for record in records)

    def test_transaction_export_json_uses_manifest_storage_path_by_default(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        db_path = project / "data" / "transactions.db"
        db_path.parent.mkdir()
        (project / "rpacore.toml").write_text(
            "[project]\nentrypoint = \"main:main\"\n\n"
            "[storage]\ntransaction_db_path = \"data/transactions.db\"\n",
            encoding="utf-8",
        )
        tx = Transaction(reference="manifest-db", status=Status.SUCCESSFUL)
        save_transaction(tx, db_path=str(db_path))

        result = run_cli("transaction", "export", "--format", "json", cwd=project)

        assert result.returncode == 0
        assert result.stderr == ""
        payload = json.loads(result.stdout)
        assert payload["transactions"][0]["id"] == tx.id

    def test_transaction_export_uses_query_pages(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        calls: list[dict[str, object]] = []

        def query_page(*_args, **kwargs) -> TransactionPage:
            calls.append(kwargs)
            return query_page_for()

        monkeypatch.setattr(cli_module, "query_transactions", query_page)

        result = cli_module.main(
            [
                "transaction",
                "export",
                "--db",
                str(tmp_path / "transactions.db"),
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        assert result == 0
        assert captured.err == ""
        assert json.loads(captured.out)["transactions"] == []
        assert calls == [{"cursor": None, "limit": 1_000}]

    def test_transaction_export_follows_query_page_cursors(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        first = Transaction(reference="first")
        second = Transaction(reference="second")
        pages = {
            None: TransactionPage(query_page_for(first).transactions, True, "next-page"),
            "next-page": query_page_for(second),
        }
        calls: list[dict[str, object]] = []

        def query_page(*_args, **kwargs) -> TransactionPage:
            calls.append(kwargs)
            return pages[kwargs["cursor"]]

        transactions = {first.id: first, second.id: second}
        monkeypatch.setattr(cli_module, "query_transactions", query_page)
        monkeypatch.setattr(
            cli_module,
            "load_transaction",
            lambda transaction_id, *_args, **_kwargs: transactions[transaction_id],
        )

        result = cli_module.main(
            [
                "transaction",
                "export",
                "--db",
                str(tmp_path / "transactions.db"),
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        assert result == 0
        assert captured.err == ""
        assert [
            transaction["id"] for transaction in json.loads(captured.out)["transactions"]
        ] == [first.id, second.id]
        assert calls == [
            {"cursor": None, "limit": 1_000},
            {"cursor": "next-page", "limit": 1_000},
        ]

    def test_transaction_export_does_not_block_concurrent_checkpoint(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        db_path = tmp_path / "transactions.db"
        older = Transaction(
            reference="older",
            created_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )
        newer = Transaction(
            reference="newer",
            created_at=datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc),
        )
        save_transaction(older, str(db_path))
        save_transaction(newer, str(db_path))
        checkpoint = Transaction(
            reference="checkpoint-during-export",
            created_at=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        )
        serialize_transaction = cli_module.serialize_transaction
        checkpoint_saved = False

        def serialize_with_checkpoint(transaction: Transaction) -> dict[str, object]:
            nonlocal checkpoint_saved
            if not checkpoint_saved:
                checkpoint_saved = True
                save_transaction(checkpoint, str(db_path))
            return serialize_transaction(transaction)

        monkeypatch.setattr(
            cli_module,
            "serialize_transaction",
            serialize_with_checkpoint,
        )

        result = cli_module.main(
            [
                "transaction",
                "export",
                "--db",
                str(db_path),
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        assert result == 0
        assert captured.err == ""
        exported = json.loads(captured.out)["transactions"]
        exported_references = [transaction["reference"] for transaction in exported]
        assert checkpoint.reference not in exported_references
        assert exported_references == [
            "newer",
            "older",
        ]
        assert {transaction.id for transaction in list_transactions(str(db_path))} == {
            older.id,
            newer.id,
            checkpoint.id,
        }

    def test_transaction_export_omits_transaction_deleted_during_export(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        db_path = tmp_path / "transactions.db"
        deleted_during_export = Transaction(
            reference="deleted-during-export",
            created_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )
        first = Transaction(
            reference="first",
            created_at=datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc),
        )
        save_transaction(deleted_during_export, str(db_path))
        save_transaction(first, str(db_path))
        serialize_transaction = cli_module.serialize_transaction
        cleanup_complete = False

        def serialize_after_cleanup(transaction: Transaction) -> dict[str, object]:
            nonlocal cleanup_complete
            if not cleanup_complete:
                cleanup_complete = True
                _delete_unbound_pending_transaction(
                    deleted_during_export.id,
                    db_path=str(db_path),
                )
            return serialize_transaction(transaction)

        monkeypatch.setattr(
            cli_module,
            "serialize_transaction",
            serialize_after_cleanup,
        )

        result = cli_module.main(
            [
                "transaction",
                "export",
                "--db",
                str(db_path),
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        assert result == 0
        assert captured.err == ""
        exported = json.loads(captured.out)["transactions"]
        assert [transaction["id"] for transaction in exported] == [first.id]

    def test_transaction_export_ndjson_serialization_error_writes_no_stdout(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        good = Transaction(reference="good")
        bad = Transaction(reference="bad", state={"runtime": object()})
        transactions = {good.id: good, bad.id: bad}
        monkeypatch.setattr(
            cli_module,
            "query_transactions",
            lambda *_args, **_kwargs: query_page_for(good, bad),
        )
        monkeypatch.setattr(
            cli_module,
            "load_transaction",
            lambda transaction_id, *_args, **_kwargs: transactions[transaction_id],
        )

        result = cli_module.main(
            [
                "transaction",
                "export",
                "--db",
                str(tmp_path / "transactions.db"),
                "--format",
                "ndjson",
            ]
        )

        captured = capsys.readouterr()
        assert result == 1
        assert captured.out == ""
        assert "transaction.state['runtime'] expected JSON value" in captured.err

    def test_transaction_export_ndjson_rejects_export_key_collision(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        transaction = Transaction(reference="collision")
        monkeypatch.setattr(
            cli_module,
            "query_transactions",
            lambda *_args, **_kwargs: query_page_for(transaction),
        )
        monkeypatch.setattr(
            cli_module,
            "load_transaction",
            lambda *_args, **_kwargs: transaction,
        )
        monkeypatch.setattr(
            cli_module,
            "serialize_transaction",
            lambda _transaction: {"export_format_version": 999},
        )

        result = cli_module.main(
            [
                "transaction",
                "export",
                "--db",
                str(tmp_path / "transactions.db"),
                "--format",
                "ndjson",
            ]
        )

        captured = capsys.readouterr()
        assert result == 1
        assert captured.out == ""
        assert "transaction export record key collision: export_format_version" in captured.err

    def test_transaction_list_json_serialization_type_error_exits_one(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ) -> None:
        tx = Transaction(reference="invoice", state={"runtime": object()})
        monkeypatch.setattr(
            cli_module,
            "query_transactions",
            lambda *_args, **_kwargs: query_page_for(tx),
        )
        monkeypatch.setattr(cli_module, "load_transaction", lambda *_args, **_kwargs: tx)

        result = cli_module.main(
            ["transaction", "list", "--db", str(tmp_path / "transactions.db"), "--json"]
        )

        captured = capsys.readouterr()
        assert result == 1
        assert captured.out == ""
        assert "transaction.state['runtime'] expected JSON value" in captured.err

    def test_transaction_list_uses_manifest_storage_path_by_default(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        db_path = project / "data" / "transactions.db"
        db_path.parent.mkdir()
        (project / "rpacore.toml").write_text(
            "[project]\nentrypoint = \"main:main\"\n\n"
            "[storage]\ntransaction_db_path = \"data/transactions.db\"\n",
            encoding="utf-8",
        )
        tx = Transaction(reference="manifest-db", status=Status.SUCCESSFUL)
        save_transaction(tx, db_path=str(db_path))

        result = run_cli("transaction", "list", "--json", cwd=project)

        assert result.returncode == 0
        assert result.stderr == ""
        assert json.loads(result.stdout)["transactions"][0]["id"] == tx.id

    def test_transaction_show_json_uses_manifest_storage_path_by_default(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        db_path = project / "data" / "transactions.db"
        db_path.parent.mkdir()
        (project / "rpacore.toml").write_text(
            "[project]\nentrypoint = \"main:main\"\n\n"
            "[storage]\ntransaction_db_path = \"data/transactions.db\"\n",
            encoding="utf-8",
        )
        tx = Transaction(reference="manifest-db", status=Status.SUCCESSFUL)
        save_transaction(tx, db_path=str(db_path))

        result = run_cli("transaction", "show", tx.id, "--json", cwd=project)

        assert result.returncode == 0
        assert result.stderr == ""
        assert json.loads(result.stdout)["transaction"]["id"] == tx.id

    def test_transaction_show_missing_transaction_exits_one(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        save_transaction(Transaction(reference="existing"), str(db_path))

        result = run_cli(
            "transaction",
            "show",
            "missing",
            "--db",
            str(db_path),
            cwd=tmp_path,
        )

        assert result.returncode == 1
        assert result.stdout == ""
        assert "Transaction not found" in result.stderr

    def test_transaction_show_json_missing_transaction_exits_one(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        save_transaction(Transaction(reference="existing"), str(db_path))

        result = run_cli(
            "transaction",
            "show",
            "missing",
            "--db",
            str(db_path),
            "--json",
            cwd=tmp_path,
        )

        assert result.returncode == 1
        assert result.stdout == ""
        assert "Transaction not found" in result.stderr

    def test_transaction_list_invalid_database_path_exits_one(self, tmp_path: Path) -> None:
        db_path = tmp_path / "missing" / "transactions.db"

        result = run_cli("transaction", "list", "--db", str(db_path), cwd=tmp_path)

        assert result.returncode == 1
        assert result.stdout == ""
        assert "Could not inspect transactions" in result.stderr
        assert str(db_path) in result.stderr

    def test_transaction_show_json_invalid_database_path_exits_one(self, tmp_path: Path) -> None:
        db_path = tmp_path / "missing" / "transactions.db"

        result = run_cli(
            "transaction",
            "show",
            "tx-1",
            "--db",
            str(db_path),
            "--json",
            cwd=tmp_path,
        )

        assert result.returncode == 1
        assert result.stdout == ""
        assert "Could not inspect transactions" in result.stderr
        assert str(db_path) in result.stderr

    def test_transaction_list_without_manifest_or_db_exits_one(self, tmp_path: Path) -> None:
        result = run_cli("transaction", "list", cwd=tmp_path)

        assert result.returncode == 1
        assert result.stdout == ""
        assert "pass --db" in result.stderr
