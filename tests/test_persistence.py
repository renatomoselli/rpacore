"""Tests for rpacore.persistence."""

import sqlite3
from datetime import datetime, timezone

import pytest

from rpacore.context import ProcessContext
from rpacore.exceptions import BusinessException, SystemException
from rpacore.persistence import list_transactions, load_transaction, save_transaction
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def make_transaction(**kwargs) -> Transaction:
    return Transaction(reference="REF-001", **kwargs)


def lock_database(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=1)
    conn.execute("BEGIN EXCLUSIVE")
    return conn


def create_legacy_db(db_path: str) -> str:
    transaction_id = "legacy-tx-001"
    skill_id = f"{transaction_id}:validate:1"
    conn = sqlite3.connect(db_path)
    try:
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
        conn.execute(
            "INSERT INTO transactions (id, reference, status, retry_count) VALUES (?, ?, ?, ?)",
            (transaction_id, "legacy-ref", "failed", 2),
        )
        conn.execute(
            "INSERT INTO skills "
            "(id, transaction_id, name, execution_order, status, arguments) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (skill_id, transaction_id, "validate", 1, "failed", '{"invoice": 42}'),
        )
        conn.execute(
            "INSERT INTO exceptions "
            "(skill_id, exception_type, message, action, retry_number, datetime_occurred, screenshot_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                skill_id,
                "business",
                "missing field",
                "validate",
                2,
                "2026-01-01T00:00:00+00:00",
                "shot.png",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return transaction_id


def create_v1_db(db_path: str) -> str:
    transaction_id = "v1-tx-001"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE rpacore_schema_versions ("
            "component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO rpacore_schema_versions (component, version) VALUES (?, ?)",
            ("transactions", 1),
        )
        conn.execute(
            "CREATE TABLE transactions ("
            "id TEXT PRIMARY KEY, reference TEXT NOT NULL, status TEXT NOT NULL, "
            "retry_count INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT '')"
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
            "screenshot_path TEXT NOT NULL DEFAULT '', stops_execution INTEGER NOT NULL DEFAULT 0, "
            "FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE)"
        )
        conn.execute(
            "INSERT INTO transactions (id, reference, status, retry_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (transaction_id, "v1-ref", "pending", 0, "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return transaction_id


class TestSaveAndLoad:
    def test_roundtrip_empty_transaction(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.id == tx.id
        assert loaded.reference == tx.reference
        assert loaded.status is Status.PENDING
        assert loaded.retry_count == 0
        assert loaded.skills == []

    def test_roundtrip_preserves_status(self, db_path) -> None:
        tx = make_transaction(status=Status.SUCCESSFUL)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.status is Status.SUCCESSFUL

    def test_roundtrip_preserves_retry_count(self, db_path) -> None:
        tx = make_transaction(retry_count=3)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.retry_count == 3

    def test_roundtrip_preserves_transaction_state(self, db_path) -> None:
        tx = make_transaction(state={"invoice": {"id": 42}, "tags": ["new", "vip"]})
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.state == {"invoice": {"id": 42}, "tags": ["new", "vip"]}

    def test_save_rejects_non_json_safe_transaction_state(self, db_path) -> None:
        tx = make_transaction()
        tx.state["client"] = object()

        with pytest.raises(TypeError) as exc_info:
            save_transaction(tx, db_path)

        message = str(exc_info.value)
        assert "transaction.state['client'] expected JSON value" in message
        assert "ctx.resources" in message

    def test_save_rejects_circular_transaction_state(self, db_path) -> None:
        tx = make_transaction()
        tx.state["self"] = tx.state

        with pytest.raises(TypeError) as exc_info:
            save_transaction(tx, db_path)

        assert "transaction.state['self'] expected acyclic JSON value" in str(exc_info.value)

    def test_save_rejects_non_object_transaction_state(self, db_path) -> None:
        tx = make_transaction()
        tx.state = ["invoice"]  # type: ignore[assignment]

        with pytest.raises(TypeError) as exc_info:
            save_transaction(tx, db_path)

        assert "transaction.state expected JSON object" in str(exc_info.value)

    def test_save_rejects_nested_non_json_safe_transaction_state(self, db_path) -> None:
        tx = make_transaction(state={"invoice": {"ids": [1, object()]}})

        with pytest.raises(TypeError) as exc_info:
            save_transaction(tx, db_path)

        assert "transaction.state['invoice']['ids'][1] expected JSON value" in str(exc_info.value)

    def test_load_rejects_corrupt_transaction_state_json(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE transactions SET state = ? WHERE id = ?", ("not-json", tx.id))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(tx.id, db_path)

        assert f"Persisted transaction state is invalid for transaction {tx.id!r}" in str(exc_info.value)
        assert exc_info.value.action == "repair transaction state in the persistence database"

    def test_load_rejects_non_object_transaction_state_json(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE transactions SET state = ? WHERE id = ?", ("[]", tx.id))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(tx.id, db_path)

        assert "transaction.state expected JSON object" in str(exc_info.value)

    def test_roundtrip_preserves_skills(self, db_path) -> None:
        s1 = Skill("login", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Skill("fetch", 2)
        s2.status = Status.FAILED
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.skills) == 2
        assert loaded.skills[0].name == "login"
        assert loaded.skills[0].status is Status.SUCCESSFUL
        assert loaded.skills[1].name == "fetch"
        assert loaded.skills[1].status is Status.FAILED

    def test_roundtrip_preserves_skill_execution_order(self, db_path) -> None:
        s1 = Skill("a", 3)
        s2 = Skill("b", 1)
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].name == "b"
        assert loaded.skills[0].execution_order == 1
        assert loaded.skills[1].name == "a"
        assert loaded.skills[1].execution_order == 3

    def test_roundtrip_preserves_business_exception(self, db_path) -> None:
        skill = Skill("validate", 1)
        exc = BusinessException("bad data", action="validate", retry_number=0)
        skill.exceptions.append(exc)
        skill.status = Status.FAILED
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.skills[0].exceptions) == 1
        loaded_exc = loaded.skills[0].exceptions[0]
        assert isinstance(loaded_exc, BusinessException)
        assert str(loaded_exc) == "bad data"
        assert loaded_exc.action == "validate"
        assert loaded_exc.retry_number == 0

    def test_roundtrip_preserves_stopping_business_exception(self, db_path) -> None:
        skill = Skill("validate", 1)
        exc = BusinessException("bad data", action="validate", retry_number=0, stop=True)
        skill.exceptions.append(exc)
        skill.status = Status.FAILED
        tx = make_transaction(skills=[skill])

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        loaded_exc = loaded.skills[0].exceptions[0]
        assert isinstance(loaded_exc, BusinessException)
        assert loaded_exc.stop is True
        assert loaded_exc.stops_execution is True

    def test_roundtrip_preserves_system_exception(self, db_path) -> None:
        skill = Skill("connect", 1)
        exc = SystemException("timeout", action="connect", retry_number=1)
        skill.exceptions.append(exc)
        skill.status = Status.FAILED
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        loaded_exc = loaded.skills[0].exceptions[0]
        assert isinstance(loaded_exc, SystemException)
        assert str(loaded_exc) == "timeout"
        assert loaded_exc.retry_number == 1

    def test_roundtrip_preserves_multiple_exceptions(self, db_path) -> None:
        skill = Skill("connect", 1)
        skill.exceptions.append(SystemException("timeout", action="connect", retry_number=0))
        skill.exceptions.append(SystemException("timeout", action="connect", retry_number=1))
        skill.status = Status.FAILED
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.skills[0].exceptions) == 2
        assert loaded.skills[0].exceptions[0].retry_number == 0
        assert loaded.skills[0].exceptions[1].retry_number == 1

    def test_roundtrip_preserves_screenshot_path(self, db_path) -> None:
        skill = Skill("capture", 1)
        skill.status = Status.FAILED
        exc = SystemException("crash", action="capture", screenshot_path="/tmp/shot.png")
        skill.exceptions.append(exc)
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        loaded_exc = loaded.skills[0].exceptions[0]
        assert loaded_exc.screenshot_path == "/tmp/shot.png"

    def test_roundtrip_preserves_empty_screenshot_path(self, db_path) -> None:
        skill = Skill("noscreenshot", 1)
        skill.status = Status.FAILED
        exc = BusinessException("bad data", action="noscreenshot")
        skill.exceptions.append(exc)
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        loaded_exc = loaded.skills[0].exceptions[0]
        assert loaded_exc.screenshot_path == ""

    def test_not_found_raises_key_error(self, db_path) -> None:
        with pytest.raises(KeyError, match="not-a-real-id"):
            load_transaction("not-a-real-id", db_path)

    def test_save_is_idempotent(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.id == tx.id

    def test_save_updates_existing_transaction(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        tx.status = Status.SUCCESSFUL
        tx.retry_count = 2
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.status is Status.SUCCESSFUL
        assert loaded.retry_count == 2

    def test_removed_skill_deleted_on_resave(self, db_path) -> None:
        s1 = Skill("a", 1)
        s2 = Skill("b", 2)
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        tx.skills.remove(s2)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.skills) == 1
        assert loaded.skills[0].name == "a"

    def test_roundtrip_preserves_arguments(self, db_path) -> None:
        skill = Skill("login", 1, arguments={"user": "admin", "timeout": 30})
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].arguments == {"user": "admin", "timeout": 30}

    def test_duplicate_skill_name_and_order_raises(self, db_path) -> None:
        s1 = Skill("dup", 1)
        s2 = Skill("dup", 1)
        tx = make_transaction(skills=[s1, s2])
        with pytest.raises(sqlite3.IntegrityError):
            save_transaction(tx, db_path)

    def test_save_locked_database_exposes_sqlite_lock_error(self, db_path) -> None:
        save_transaction(make_transaction(), db_path)
        lock_conn = lock_database(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                save_transaction(make_transaction(), db_path)
        finally:
            lock_conn.rollback()
            lock_conn.close()

    def test_load_locked_database_exposes_sqlite_lock_error(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        lock_conn = lock_database(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                load_transaction(tx.id, db_path)
        finally:
            lock_conn.rollback()
            lock_conn.close()

    def test_list_locked_database_exposes_sqlite_lock_error(self, db_path) -> None:
        save_transaction(make_transaction(), db_path)
        lock_conn = lock_database(db_path)
        try:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                list_transactions(db_path)
        finally:
            lock_conn.rollback()
            lock_conn.close()


class TestCrashRecovery:
    def test_in_progress_skill_loaded_as_failed(self, db_path) -> None:
        skill = Skill("process", 1)
        skill.status = Status.IN_PROGRESS
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].status is Status.FAILED

    def test_in_progress_transaction_loaded_as_failed(self, db_path) -> None:
        tx = make_transaction(status=Status.IN_PROGRESS)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.status is Status.FAILED

    def test_successful_skills_not_affected_by_crash_recovery(self, db_path) -> None:
        s1 = Skill("login", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Skill("process", 2)
        s2.status = Status.IN_PROGRESS
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].status is Status.SUCCESSFUL
        assert loaded.skills[1].status is Status.FAILED


class TestResumeScenario:
    def test_resume_skips_successful_skills(self, db_path) -> None:
        from rpacore.engine import Engine

        # Save a transaction where skill "a" already succeeded and "b" is pending.
        s1 = Skill("a", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Skill("b", 2)
        s2.status = Status.PENDING

        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)

        # Reload and run with a concrete subclass wired to the same names.
        counts: dict[str, int] = {"a": 0, "b": 0}

        class TrackSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts[self.name] += 1

        loaded = load_transaction(tx.id, db_path)
        # Replace plain Skill instances with executable ones, preserving status.
        for i, skill in enumerate(loaded.skills):
            track = TrackSkill(skill.name, skill.execution_order)
            track.status = skill.status
            track.exceptions = skill.exceptions
            loaded.skills[i] = track

        from rpacore.engine import Engine
        Engine().run(ProcessContext(transaction=loaded))

        assert counts["a"] == 0  # already successful, engine skips it
        assert counts["b"] == 1
        assert loaded.status is Status.SUCCESSFUL


class TestSchemaMigration:
    def test_legacy_schema_loads_existing_transaction_skill_and_exception(self, db_path) -> None:
        transaction_id = create_legacy_db(db_path)

        loaded = load_transaction(transaction_id, db_path)

        assert loaded.id == transaction_id
        assert loaded.reference == "legacy-ref"
        assert loaded.status is Status.FAILED
        assert loaded.retry_count == 2
        assert loaded.state == {}
        assert len(loaded.skills) == 1
        assert loaded.skills[0].name == "validate"
        assert loaded.skills[0].status is Status.FAILED
        assert loaded.skills[0].arguments == {"invoice": 42}
        assert len(loaded.skills[0].exceptions) == 1
        assert isinstance(loaded.skills[0].exceptions[0], BusinessException)
        assert str(loaded.skills[0].exceptions[0]) == "missing field"
        assert loaded.skills[0].exceptions[0].screenshot_path == "shot.png"
        assert loaded.skills[0].exceptions[0].stops_execution is False

    def test_legacy_schema_gets_created_at_column(self, db_path) -> None:
        create_legacy_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
        finally:
            conn.close()
        assert "created_at" in columns

    def test_legacy_schema_gets_exception_stops_execution_column(self, db_path) -> None:
        create_legacy_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(exceptions)")}
        finally:
            conn.close()
        assert "stops_execution" in columns

    def test_v1_schema_gets_state_column(self, db_path) -> None:
        create_v1_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
        finally:
            conn.close()
        assert "state" in columns

    def test_v1_schema_loads_with_empty_state(self, db_path) -> None:
        transaction_id = create_v1_db(db_path)

        loaded = load_transaction(transaction_id, db_path)

        assert loaded.state == {}

    def test_legacy_schema_backfills_created_at_for_since_filter(self, db_path) -> None:
        transaction_id = create_legacy_db(db_path)
        cutoff = datetime.now(timezone.utc)

        result = list_transactions(db_path, since=cutoff)

        assert [tx.id for tx in result] == [transaction_id]

    def test_legacy_schema_migration_is_idempotent(self, db_path) -> None:
        transaction_id = create_legacy_db(db_path)

        first = list_transactions(db_path)
        second = list_transactions(db_path)

        assert [tx.id for tx in first] == [transaction_id]
        assert [tx.id for tx in second] == [transaction_id]

    def test_component_schema_version_is_recorded_after_migration(self, db_path) -> None:
        create_legacy_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            version = conn.execute(
                "SELECT version FROM rpacore_schema_versions WHERE component = 'transactions'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert version == 2

    def test_transaction_and_queue_schema_versions_can_share_database(self, db_path) -> None:
        from rpacore.queue import QueueItem, SqliteQueue

        save_transaction(make_transaction(), db_path)
        queue = SqliteQueue({"db_path": db_path})
        queue.add(QueueItem(reference="queue-ref", payload={}))

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT component, version FROM rpacore_schema_versions ORDER BY component"
            ).fetchall()
        finally:
            conn.close()

        assert rows == [("queue", 1), ("transactions", 2)]

    def test_unsupported_transaction_schema_version_raises(self, db_path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE rpacore_schema_versions ("
                "component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO rpacore_schema_versions (component, version) VALUES (?, ?)",
                ("transactions", 3),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(RuntimeError, match="Unsupported transaction schema version 3"):
            list_transactions(db_path)

    def test_unrelated_schema_errors_are_not_swallowed(self, db_path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE VIEW transactions AS "
                "SELECT 'tx-view' AS id, 'view-ref' AS reference, 'pending' AS status, 0 AS retry_count"
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(sqlite3.OperationalError):
            save_transaction(make_transaction(), db_path)
