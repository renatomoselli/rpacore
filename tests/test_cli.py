"""Subprocess tests for the rpacore command line interface."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import stat
import textwrap
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

import rpacore.cli as cli_module
import rpacore._transaction_cli as transaction_cli
import rpacore.doctor as doctor_module
import rpacore._doctor_config as doctor_config_module
import rpacore._schema_structure as schema_structure_module
from rpacore._doctor_errors import input_error_details
from rpacore.doctor import collect_doctor_result
from rpacore.queue import QUEUE_SCHEMA_VERSION
from rpacore.persistence import TRANSACTION_SCHEMA_VERSION
from rpacore import (
    Artifact,
    BusinessException,
    HistoryEvent,
    Step,
    Status,
    Transaction,
    TransactionPage,
    TransactionSummary,
    QueueItem,
    SqliteQueue,
    list_transactions,
    save_transaction,
)
from tests.cli_helpers import run_cli


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


class TestCliDoctor:
    @pytest.mark.parametrize("explicit_config", [False, True])
    def test_missing_discovered_storage_is_unchecked_even_after_removal(self, tmp_path, explicit_config):
        project = write_project(tmp_path, "raise RuntimeError('must not import')\n")
        config = project / "config.toml"
        config.write_text('[queue]\ndb_path = "queue.db"\n', encoding="utf-8")
        args = ("--config", str(config)) if explicit_config else ()
        for previously_created in (False, True):
            if previously_created:
                save_transaction(Transaction(reference="removed"), str(project / "rpacore.db"))
                SqliteQueue({"db_path": str(project / "queue.db")}).add(QueueItem(reference="removed", payload={}))
                (project / "rpacore.db").unlink()
                (project / "queue.db").unlink()
            before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
            result = run_cli("doctor", *args, "--json", cwd=project)
            assert result.returncode == 0 and result.stderr == ""
            payload = json.loads(result.stdout)
            assert set(payload) == {"doctor_format_version", "framework_version", "checks"}
            assert payload["doctor_format_version"] == 1
            checks = {c["id"]: c for c in payload["checks"]}
            for component in ("transactions", "queue"):
                schema = checks[f"{component}.schema"]
                assert schema["status"] == "not_applicable"
                assert schema["details"] == {"reason": "missing_discovered_database"}
                for deeper in ("journal", "quick_check", "foreign_keys"):
                    assert checks[f"{component}.{deeper}"]["status"] == "not_applicable"
                    assert checks[f"{component}.{deeper}"]["details"] == {}
            assert checks["queue.health"]["status"] == "not_applicable"
            assert "absent" in checks["queue.health"]["summary"]
            assert "unchecked" in checks["queue.health"]["summary"]
            human = run_cli("doctor", *args, cwd=project)
            assert human.returncode == 0
            for word in ("unchecked", "removed", "configured location", "recovery/backup", "before running"):
                assert word in human.stdout
            assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before
        for flag in ("--transaction-db", "--queue-db"):
            result = run_cli("doctor", flag, str(project / "missing.db"), "--json", cwd=project)
            assert result.returncode == 1 and result.stderr == ""
            component = "queue" if flag == "--queue-db" else "transactions"
            checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
            assert checks[f"{component}.schema"]["status"] == "fail"
            assert checks[f"{component}.schema"]["details"] == {}

    @pytest.mark.parametrize("component", ["transactions", "queue"])
    @pytest.mark.parametrize("kind", ["directory", "non_sqlite", "old", "future", "malformed"])
    def test_existing_discovered_bad_storage_fails_without_mutation(self, tmp_path, component, kind):
        project = write_project(tmp_path, "raise RuntimeError('must not import')\n")
        (project / "config.toml").write_text('[queue]\ndb_path = "queue.db"\n', encoding="utf-8")
        path = project / ("queue.db" if component == "queue" else "rpacore.db")
        if kind == "directory":
            path.mkdir()
        elif kind == "non_sqlite":
            path.write_bytes(b"not sqlite")
        elif kind == "malformed":
            path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)
        else:
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE rpacore_schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)")
                conn.execute("INSERT INTO rpacore_schema_versions VALUES (?, ?)", (component, 0 if kind == "old" else 99))
        before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
        result = run_cli("doctor", "--json", cwd=project)
        assert result.returncode == 1 and result.stderr == ""
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        assert checks[f"{component}.schema"]["status"] == "fail"
        assert checks[f"{component}.schema"]["details"] == {}
        if component == "queue" and kind == "non_sqlite":
            assert checks["queue.health"] == {
                "id": "queue.health", "status": "not_applicable",
                "summary": "Queue database is not an SQLite database", "details": {},
            }
        assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before

    @pytest.mark.parametrize("target", ["manifest", "config", "database"])
    def test_unreadable_discovered_inputs_are_failures(self, tmp_path, monkeypatch, target):
        project = write_project(tmp_path, "raise RuntimeError('must not import')\n")
        config = project / "config.toml"
        config.write_text("", encoding="utf-8")
        (project / "rpacore.db").write_bytes(b"existing database")
        monkeypatch.chdir(project)
        def denied(*args, **kwargs):
            raise PermissionError("private-marker")
        if target == "manifest":
            monkeypatch.setattr(doctor_module, "load_project_manifest", denied)
        elif target == "config":
            monkeypatch.setattr(doctor_config_module, "_load_config", denied)
        else:
            monkeypatch.setattr(Path, "open", denied)
        result = collect_doctor_result()
        assert result.exit_code == 1
        check_id = "transactions.schema" if target == "database" else f"project.{target}"
        check = next(c for c in result.checks if c.id == check_id)
        assert check.status == "fail"
        if target != "database":
            assert check.details == {"cause": "unreadable"}
        assert "private-marker" not in json.dumps(result.to_dict())

    @pytest.mark.parametrize("exception,cause", [
        (PermissionError("private-discovery-marker"), "unreadable"),
        (RuntimeError("private-discovery-marker"), "unexpected"),
    ])
    def test_manifest_discovery_failures_are_bounded(self, tmp_path, monkeypatch, exception, cause):
        monkeypatch.chdir(tmp_path)
        def fail_discovery():
            raise exception
        monkeypatch.setattr(doctor_module, "find_project_manifest", fail_discovery)
        result = collect_doctor_result()
        checks = [check for check in result.checks if check.id == "project.manifest"]
        assert len(checks) == 1 and checks[0].status == "fail"
        assert checks[0].details == {"cause": cause}
        assert result.exit_code == 1
        assert "private-discovery-marker" not in json.dumps(result.to_dict())
        assert "check the file" in checks[0].summary
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("target", ["manifest", "discovered_config", "explicit_config"])
    def test_directory_inputs_fail_without_storage_creation(self, tmp_path, target):
        project = write_project(tmp_path, "raise RuntimeError('must not import')\n")
        if target == "manifest":
            manifest = project / "rpacore.toml"
            manifest.unlink()
            manifest.mkdir()
            args = ()
        else:
            config = project / "config.toml"
            config.mkdir()
            args = ("--config", str(config)) if target == "explicit_config" else ()
        before = {p.relative_to(project): p.read_bytes() if p.is_file() else None for p in project.rglob("*")}
        result = run_cli("doctor", *args, "--json", cwd=project)
        assert result.returncode == 1 and result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        check = checks["project.manifest" if target == "manifest" else "project.config"]
        assert check["status"] == "fail" and check["details"] == {"cause": "unreadable"}
        assert "check the file" in check["summary"]
        human = run_cli("doctor", *args, cwd=project)
        assert human.returncode == 1 and "unreadable" in human.stdout
        assert "check the file" in human.stdout and human.stderr == ""
        assert {p.relative_to(project): p.read_bytes() if p.is_file() else None for p in project.rglob("*")} == before

    @pytest.mark.parametrize("filename,source,cause,location", [
        ("rpacore.toml", '[project]\nentrypoint = "secret-marker" extra\n', "toml_syntax", "line 2, column 30"),
        ("config.toml", 'value = ["secret-marker",', "toml_syntax", "end of document"),
        ("rpacore.toml", '[project]\n', "invalid_shape", None),
        ("rpacore.toml", 'secret-marker = 1\n', "invalid_shape", None),
        ("rpacore.toml", 'project = "secret-marker"\n', "invalid_type", None),
        ("rpacore.toml", '[project]\nentrypoint = "secret-marker"\n[storage]\ntransaction_db_path = "x.db"\n', "invalid_value", None),
        ("config.toml", 'queue = "secret-marker"\n', "invalid_type", None),
        ("config.toml", 'max_retries = "secret-marker"\n', "invalid_type", None),
        ("config.toml", 'log_format = "secret-marker"\n', "invalid_value", None),
        ("config.toml", '[queue]\ndb_path = ":memory:"\n', "invalid_value", None),
    ])
    def test_expected_input_errors_are_safe_and_actionable(self, tmp_path, filename, source, cause, location):
        if location == "end of document":
            with pytest.raises(tomllib.TOMLDecodeError) as parsed:
                tomllib.loads(source)
            if type(getattr(parsed.value, "lineno", None)) is int and type(getattr(parsed.value, "colno", None)) is int:
                # This named case is one line, with the error immediately after its last byte.
                location = f"line 1, column {len(source) + 1}"
        project = write_project(tmp_path, "raise RuntimeError('must not import')\n")
        (project / filename).write_text(source, encoding="utf-8")
        before = {p: p.read_bytes() for p in project.iterdir() if p.is_file()}
        result = run_cli("doctor", "--json", cwd=project)
        assert result.returncode == 1 and result.stderr == ""
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        check = checks["project.manifest" if filename == "rpacore.toml" else "project.config"]
        expected = {"cause": cause}
        if location is not None:
            expected["location"] = location
        assert check["details"] == expected
        human = run_cli("doctor", cwd=project)
        assert human.returncode == 1 and cause in human.stdout and "check the file" in human.stdout
        if location:
            assert location in human.stdout
        assert "secret-marker" not in result.stdout + human.stdout
        assert {p: p.read_bytes() for p in project.iterdir() if p.is_file()} == before

    def test_parser_location_attributes_and_unknown_suffix_are_bounded(self):
        # Python 3.11/3.12 use suffixes; newer parsers can expose numeric attributes.
        exc = tomllib.TOMLDecodeError("secret-marker (unfamiliar location)")
        assert input_error_details(exc) == {"cause": "toml_syntax"}
        exc.lineno, exc.colno = 4, 8
        assert input_error_details(exc) == {"cause": "toml_syntax", "location": "line 4, column 8"}
        assert input_error_details(RuntimeError("secret-marker")) == {"cause": "unexpected"}

    @pytest.mark.parametrize("path", ["", ":memory:", "invalid\x00path"])
    def test_invalid_discovered_database_paths_are_not_absence(self, tmp_path, monkeypatch, path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(doctor_module, "_discover_manifest", lambda: (None, []))
        monkeypatch.setattr(doctor_module, "discover_config", lambda **kwargs: ({"queue": {"db_path": path}}, []))
        result = collect_doctor_result()
        schema = next(c for c in result.checks if c.id == "queue.schema")
        assert schema.status == "fail" and schema.details == {}

    def test_nonregular_database_is_rejected_before_open(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        path = tmp_path / "pipe.db"
        real_stat = Path.stat
        def nonregular(candidate, **kwargs):
            if candidate == path:
                return SimpleNamespace(st_mode=stat.S_IFIFO)
            return real_stat(candidate, **kwargs)
        def must_not_open(*args, **kwargs):
            raise AssertionError("Nonregular database must not be opened")
        monkeypatch.setattr(Path, "stat", nonregular)
        monkeypatch.setattr(Path, "open", must_not_open)
        with pytest.raises(OSError, match="not a regular file"):
            doctor_module.database_header_journal_mode(str(path), field="test")

    @pytest.mark.parametrize("exception", [PermissionError("private-marker"), RuntimeError("private-marker")])
    def test_config_stat_failures_remain_bounded(self, tmp_path, monkeypatch, exception):
        path = tmp_path / "config.toml"
        real_stat = Path.stat
        def fail_stat(candidate, **kwargs):
            if candidate == path:
                raise exception
            return real_stat(candidate, **kwargs)
        monkeypatch.setattr(Path, "stat", fail_stat)
        config, checks = doctor_config_module.discover_config(config_path=str(path), manifest=None)
        assert config is None and checks[0].status == "fail"
        assert checks[0].details == {"cause": "unreadable" if isinstance(exception, OSError) else "unexpected"}
        assert "private-marker" not in json.dumps(checks[0].to_dict())

    @pytest.mark.parametrize("target", ["manifest", "config"])
    def test_found_input_disappearing_is_not_no_selection(self, tmp_path, monkeypatch, target):
        project = write_project(tmp_path, "raise RuntimeError('must not import')\n")
        (project / "config.toml").write_text("", encoding="utf-8")
        monkeypatch.chdir(project)
        def disappeared(*args, **kwargs):
            raise FileNotFoundError("private-marker")
        monkeypatch.setattr(doctor_module if target == "manifest" else doctor_config_module,
                            "load_project_manifest" if target == "manifest" else "_load_config", disappeared)
        result = collect_doctor_result()
        check = next(c for c in result.checks if c.id == f"project.{target}")
        assert check.status == "fail" and check.details == {"cause": "unreadable"}

    def test_missing_explicit_config_fails(self, tmp_path):
        result = run_cli("doctor", "--config", str(tmp_path / "missing.toml"), "--json", cwd=tmp_path)
        assert result.returncode == 1
        check = next(c for c in json.loads(result.stdout)["checks"] if c["id"] == "project.config")
        assert check["details"] == {"cause": "unreadable"}

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
        assert checks["transactions.schema"]["details"] == {}
        assert checks["queue.schema"]["details"] == {}

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

    @pytest.mark.parametrize("component", ["transactions", "queue"])
    def test_wal_database_sidecars_are_not_changed(self, tmp_path: Path, component: str) -> None:
        db_path = tmp_path / "wal.db"
        if component == "queue":
            SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))
        else:
            save_transaction(Transaction(reference="doctor"), str(db_path))
        connection = sqlite3.connect(db_path)
        try:
            assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
            connection.execute("CREATE TABLE wal_probe (id INTEGER)")
            connection.commit()
            sidecars = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
            assert all(path.exists() for path in sidecars)
            before = {path: path.read_bytes() for path in sidecars}

            if component == "queue":
                result = collect_doctor_result(queue_db_path=str(db_path))
            else:
                result = collect_doctor_result(transaction_db_path=str(db_path))

            assert {path: path.read_bytes() for path in sidecars} == before
        finally:
            connection.close()
        checks = {check.id: check for check in result.checks}
        assert checks[f"{component}.journal"].status == "warning"
        assert checks[f"{component}.schema"].status == "not_applicable"
        assert checks[f"{component}.schema"].details == {}
        assert checks[f"{component}.quick_check"].status == "not_applicable"
        if component == "queue":
            assert checks["queue.health"].status == "not_applicable"
            assert checks["queue.health"].summary == "WAL queue database was not opened"

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

    @pytest.mark.parametrize("damage", [
        pytest.param("ALTER TABLE queue_items DROP COLUMN claim_token", id="claim_token"),
        pytest.param("ALTER TABLE queue_items DROP COLUMN available_at", id="available_at"),
        pytest.param("DROP TABLE queue_poison_events", id="poison_table"),
    ])
    def test_current_queue_missing_schema_object_fails_without_mutation(self, tmp_path: Path, damage: str) -> None:
        db_path = tmp_path / "queue.db"
        SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(damage)
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

        result = run_cli("doctor", "--json", "--queue-db", str(db_path), cwd=tmp_path)

        assert result.returncode == 1
        assert result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["queue.schema"]["status"] == "fail"
        assert checks["queue.schema"]["summary"] == "Queue database schema is structurally incomplete"
        assert checks["queue.schema"]["details"] == {"version": QUEUE_SCHEMA_VERSION, "reason": "missing_schema_objects"}
        for deeper in ("journal", "quick_check", "foreign_keys"):
            assert checks[f"queue.{deeper}"]["status"] == "not_applicable"
        assert checks["queue.health"]["status"] == "not_applicable"
        assert checks["queue.health"]["summary"] == "Queue health requires a current readable schema"
        assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before

    @pytest.mark.parametrize("damage", [
        pytest.param("DROP TABLE transaction_artifacts", id="artifacts_table"),
        pytest.param("ALTER TABLE transactions DROP COLUMN definition_identity", id="definition_identity"),
    ])
    def test_current_transaction_missing_schema_object_fails_without_mutation(
        self, tmp_path: Path, damage: str
    ) -> None:
        db_path = tmp_path / "transactions.db"
        save_transaction(Transaction(reference="doctor"), str(db_path))
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(damage)
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

        result = run_cli("doctor", "--json", "--transaction-db", str(db_path), cwd=tmp_path)

        assert result.returncode == 1
        assert result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["transactions.schema"]["status"] == "fail"
        assert checks["transactions.schema"]["summary"] == "Transaction database schema is structurally incomplete"
        assert checks["transactions.schema"]["details"] == {"version": TRANSACTION_SCHEMA_VERSION, "reason": "missing_schema_objects"}
        for deeper in ("journal", "quick_check", "foreign_keys"):
            assert checks[f"transactions.{deeper}"]["status"] == "not_applicable"
        assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before

    def test_incomplete_queue_structure_skips_health_counts(self, tmp_path: Path, monkeypatch) -> None:
        db_path = tmp_path / "queue.db"
        SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("ALTER TABLE queue_items DROP COLUMN available_at")
        monkeypatch.chdir(tmp_path)

        def health_must_not_run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("queue health must not run after a structural failure")

        monkeypatch.setattr(doctor_module, "_queue_health_check", health_must_not_run)

        result = collect_doctor_result(queue_db_path=str(db_path))

        checks = {check.id: check for check in result.checks}
        assert checks["queue.schema"].status == "fail"
        assert checks["queue.health"].to_dict() == {
            "id": "queue.health",
            "status": "not_applicable",
            "summary": "Queue health requires a current readable schema",
            "details": {},
        }
        assert result.exit_code == 1

    @pytest.mark.parametrize("case", ["empty_queue", "queue_with_item", "transactions", "shared_file"])
    def test_current_framework_databases_pass_structure(self, tmp_path: Path, case: str) -> None:
        db_path = tmp_path / "current.db"
        args: list[str] = []
        components: dict[str, int] = {}
        if case in ("empty_queue", "queue_with_item", "shared_file"):
            queue = SqliteQueue({"db_path": str(db_path)})
            if case != "empty_queue":
                queue.add(QueueItem(reference="doctor", payload={}))
            args += ["--queue-db", str(db_path)]
            components["queue"] = QUEUE_SCHEMA_VERSION
        if case in ("transactions", "shared_file"):
            save_transaction(Transaction(reference="doctor"), str(db_path))
            args += ["--transaction-db", str(db_path)]
            components["transactions"] = TRANSACTION_SCHEMA_VERSION
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

        result = run_cli("doctor", "--json", *args, cwd=tmp_path)

        assert result.returncode == 0
        assert result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        for component, version in components.items():
            assert checks[f"{component}.schema"]["status"] == "pass"
            assert checks[f"{component}.schema"]["details"] == {"version": version}
        assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before

    @pytest.mark.parametrize("component", ["queue", "transactions"])
    def test_extra_user_schema_objects_are_ignored(self, tmp_path: Path, component: str) -> None:
        db_path = tmp_path / "extra.db"
        if component == "queue":
            SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))
        else:
            save_transaction(Transaction(reference="doctor"), str(db_path))
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("CREATE TABLE audit_notes (id INTEGER PRIMARY KEY, note TEXT)")
            if component == "queue":
                connection.execute("ALTER TABLE queue_items ADD COLUMN note TEXT")
        flag = "--queue-db" if component == "queue" else "--transaction-db"

        result = run_cli("doctor", "--json", flag, str(db_path), cwd=tmp_path)

        assert result.returncode == 0
        assert result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks[f"{component}.schema"]["status"] == "pass"
        assert "reason" not in checks[f"{component}.schema"]["details"]

    def test_reference_build_failure_is_an_internal_doctor_failure(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        db_path = tmp_path / "queue.db"
        SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))
        monkeypatch.chdir(tmp_path)

        def broken_reference(_component: str) -> object:
            raise RuntimeError("reference build failure")

        monkeypatch.setattr(schema_structure_module, "_reference_structure", broken_reference)

        result = cli_module.main(["doctor", "--json", "--queue-db", str(db_path)])

        captured = capsys.readouterr()
        assert result == 1
        assert captured.out == ""
        assert captured.err == "rpacore: Could not complete doctor diagnostics\n"

    def test_structure_read_sqlite_error_is_an_incompatible_schema_without_reason(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_path = tmp_path / "queue.db"
        SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))

        def failing_check(*_args: object, **_kwargs: object) -> bool:
            raise sqlite3.OperationalError("structure read failure")

        monkeypatch.setattr(doctor_module, "missing_schema_objects", failing_check)
        monkeypatch.chdir(tmp_path)

        result = collect_doctor_result(queue_db_path=str(db_path))

        checks = {check.id: check for check in result.checks}
        assert checks["queue.schema"].status == "fail"
        assert checks["queue.schema"].summary == "Queue database schema is incompatible"
        assert "reason" not in checks["queue.schema"].details
        for deeper in ("journal", "quick_check", "foreign_keys"):
            assert checks[f"queue.{deeper}"].status == "not_applicable"
        assert result.exit_code == 1

    @pytest.mark.parametrize("component", ["queue", "transactions"])
    def test_structural_failure_human_output(self, tmp_path: Path, component: str) -> None:
        db_path = tmp_path / "damaged.db"
        if component == "queue":
            SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))
            damage, flag, label = "DROP TABLE queue_poison_events", "--queue-db", "Queue"
        else:
            save_transaction(Transaction(reference="doctor"), str(db_path))
            damage, flag, label = "DROP TABLE transaction_artifacts", "--transaction-db", "Transaction"
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute(damage)

        result = run_cli("doctor", flag, str(db_path), cwd=tmp_path)

        assert result.returncode == 1
        assert result.stderr == ""
        assert f"{component}.schema - {label} database schema is structurally incomplete" in result.stdout
        assert not result.stdout.lstrip().startswith("{")

    def test_wal_queue_missing_framework_column_keeps_wal_statuses(self, tmp_path: Path) -> None:
        db_path = tmp_path / "wal.db"
        SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("ALTER TABLE queue_items DROP COLUMN available_at")
            connection.commit()
            assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
            connection.execute("CREATE TABLE wal_probe (id INTEGER)")
            connection.commit()
            sidecars = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
            assert all(path.exists() for path in sidecars)
            before = {path: path.read_bytes() for path in sidecars}

            result = collect_doctor_result(queue_db_path=str(db_path))

            assert {path: path.read_bytes() for path in sidecars} == before
        finally:
            connection.close()
        checks = {check.id: check for check in result.checks}
        assert checks["queue.journal"].status == "warning"
        assert checks["queue.schema"].status == "not_applicable"
        assert checks["queue.schema"].details == {}
        assert checks["queue.quick_check"].status == "not_applicable"
        assert checks["queue.foreign_keys"].status == "not_applicable"
        assert checks["queue.health"].status == "not_applicable"
        assert checks["queue.health"].summary == "WAL queue database was not opened"

    def test_shared_file_missing_only_transaction_table_isolates_the_component(self, tmp_path: Path) -> None:
        db_path = tmp_path / "shared.db"
        SqliteQueue({"db_path": str(db_path)}).add(QueueItem(reference="doctor", payload={}))
        save_transaction(Transaction(reference="doctor"), str(db_path))
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("DROP TABLE transaction_artifacts")
        before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

        result = run_cli(
            "doctor", "--json", "--queue-db", str(db_path), "--transaction-db", str(db_path), cwd=tmp_path
        )

        assert result.returncode == 1
        assert result.stderr == ""
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        assert checks["transactions.schema"]["status"] == "fail"
        assert checks["transactions.schema"]["details"]["reason"] == "missing_schema_objects"
        assert checks["queue.schema"]["status"] == "pass"
        assert checks["queue.health"]["status"] != "not_applicable"
        assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


class TestCliTransaction:
    @pytest.mark.parametrize("command", [("list", "--json"), ("show", "missing", "--json"), ("export", "--format", "json")])
    @pytest.mark.parametrize("manifest", [None, 'value = ["private-marker",'])
    def test_resolution_errors_have_no_empty_database_prefix(self, tmp_path, command, manifest):
        if manifest is not None:
            (tmp_path / "rpacore.toml").write_text(manifest, encoding="utf-8")
        result = run_cli("transaction", *command, cwd=tmp_path)
        assert result.returncode == 1 and result.stdout == ""
        assert "Could not resolve transaction database" in result.stderr
        assert result.stderr.lower().count("could not resolve transaction database") == 1
        assert "pass --db" in result.stderr and "in :" not in result.stderr
        selected = tmp_path / "missing.db"
        result = run_cli("transaction", *command, "--db", str(selected), cwd=tmp_path)
        assert result.returncode == 1 and result.stdout == ""
        assert str(selected) in result.stderr and not selected.exists()

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
        assert payload["schema_version"] == 3
        assert payload["command"] == "transaction:list"
        assert payload["limit"] == 100
        assert payload["transactions"][0]["transaction"]["id"] == tx.id
        assert payload["transactions"][0]["transaction"]["transaction_format_version"] == 3
        assert payload["transactions"][0]["transaction"]["definition_identity"] == ""
        assert payload["transactions"][0]["transaction"]["status"] == "successful"
        assert payload["transactions"][0]["transaction"]["state"] == {"invoice": "001"}
        assert payload["transactions"][0]["transaction"]["metadata"] == {"customer": "acme"}

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
        step = Step("download", 1, arguments={"invoice": "001"})
        step.status = Status.FAILED
        step.exceptions.append(BusinessException("bad invoice", action="download"))
        tx = Transaction(reference="invoice-001", status=Status.FAILED, steps=[step])
        tx.metadata = {"customer": "acme"}
        tx.artifacts = [Artifact(name="invoice", path="invoice.pdf", kind="pdf")]
        tx.append_history(step=step, event=HistoryEvent.STEP_FAILED)
        save_transaction(tx, db_path=str(db_path))

        result = run_cli("transaction", "show", tx.id, "--db", str(db_path), cwd=tmp_path)

        assert result.returncode == 0
        assert f"ID:          {tx.id}" in result.stdout
        assert "Reference:   invoice-001" in result.stdout
        assert "Definition:  (unidentified)" in result.stdout
        assert "1. download: failed" in result.stdout
        assert "- business: bad invoice" in result.stdout
        assert "1. step_failed status=failed retry=0 step=download" in result.stdout
        assert "invoice (pdf): invoice.pdf" in result.stdout
        assert "Artifacts:   1" in result.stdout
        assert result.stderr == ""

    def test_transaction_show_json_contains_loaded_transaction_details(self, tmp_path: Path) -> None:
        db_path = tmp_path / "transactions.db"
        step = Step("download", 1, arguments={"invoice": "001"})
        step.status = Status.FAILED
        step.exceptions.append(BusinessException("bad invoice", action="download"))
        tx = Transaction(
            reference="invoice-001",
            status=Status.FAILED,
            state={"invoice": "001"},
            metadata={"customer": "acme"},
            steps=[step],
            artifacts=[Artifact(name="invoice", path="invoice.pdf", kind="pdf")],
        )
        tx.append_history(step=step, event=HistoryEvent.STEP_FAILED)
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
        assert payload["schema_version"] == 3
        assert payload["command"] == "transaction:show"
        detail = payload["transaction"]
        assert detail["transaction_format_version"] == 3
        assert detail["definition_identity"] == ""
        assert detail["id"] == tx.id
        assert detail["state"] == {"invoice": "001"}
        assert detail["metadata"] == {"customer": "acme"}
        assert detail["steps"][0]["exceptions"][0]["type"] == "business"
        assert detail["history"][0]["event"] == "step_failed"
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
        assert payload["export_format_version"] == 2
        assert payload["framework_version"] == cli_module.__version__
        assert payload["exported_at"].endswith("+00:00")
        assert [tx["reference"] for tx in payload["transactions"]] == ["newer", "older"]
        assert payload["transactions"][1]["transaction_format_version"] == 3
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
            "definition_identity",
            "export_format_version",
            "exported_at",
            "finished_at",
            "framework_version",
            "history",
            "id",
            "metadata",
            "reference",
            "retry_count",
            "steps",
            "started_at",
            "state",
            "status",
            "transaction_format_version",
        }
        assert [record["reference"] for record in records] == ["second", "first"]
        assert {record["export_format_version"] for record in records} == {2}
        assert {record["transaction_format_version"] for record in records} == {3}
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

        monkeypatch.setattr(transaction_cli, "query_transactions", query_page)

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
        assert calls == [{"cursor": None, "limit": 1_000, "statuses": None, "since": None, "until": None, "reference": None, "metadata_filter": None}]

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
        monkeypatch.setattr(transaction_cli, "query_transactions", query_page)
        monkeypatch.setattr(
            transaction_cli,
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
            {"cursor": None, "limit": 1_000, "statuses": None, "since": None, "until": None, "reference": None, "metadata_filter": None},
            {"cursor": "next-page", "limit": 1_000, "statuses": None, "since": None, "until": None, "reference": None, "metadata_filter": None},
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
        serialize_transaction = transaction_cli.serialize_transaction
        checkpoint_saved = False

        def serialize_with_checkpoint(transaction: Transaction) -> dict[str, object]:
            nonlocal checkpoint_saved
            if not checkpoint_saved:
                checkpoint_saved = True
                save_transaction(checkpoint, str(db_path))
            return serialize_transaction(transaction)

        monkeypatch.setattr(
            transaction_cli,
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
        serialize_transaction = transaction_cli.serialize_transaction
        cleanup_complete = False

        def serialize_after_cleanup(transaction: Transaction) -> dict[str, object]:
            nonlocal cleanup_complete
            if not cleanup_complete:
                cleanup_complete = True
                with closing(sqlite3.connect(db_path)) as conn, conn:
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.execute("DELETE FROM transactions WHERE id = ?", (deleted_during_export.id,))
            return serialize_transaction(transaction)

        monkeypatch.setattr(
            transaction_cli,
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
            transaction_cli,
            "query_transactions",
            lambda *_args, **_kwargs: query_page_for(good, bad),
        )
        monkeypatch.setattr(
            transaction_cli,
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
            transaction_cli,
            "query_transactions",
            lambda *_args, **_kwargs: query_page_for(transaction),
        )
        monkeypatch.setattr(
            transaction_cli,
            "load_transaction",
            lambda *_args, **_kwargs: transaction,
        )
        monkeypatch.setattr(
            transaction_cli,
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
            transaction_cli,
            "query_transactions",
            lambda *_args, **_kwargs: query_page_for(tx),
        )
        monkeypatch.setattr(transaction_cli, "load_transaction", lambda *_args, **_kwargs: tx)

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
        assert json.loads(result.stdout)["transactions"][0]["transaction"]["id"] == tx.id

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
