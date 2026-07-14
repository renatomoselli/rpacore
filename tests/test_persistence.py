"""Tests for rpacore.persistence."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import rpacore.persistence as persistence_module
from rpacore.context import ProcessContext
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
from rpacore.persistence import (
    _delete_unbound_pending_transaction,
    iter_transactions,
    list_transactions,
    load_transaction,
    save_transaction,
)
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Artifact, HistoryEvent, Transaction


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def make_transaction(**kwargs) -> Transaction:
    return Transaction(reference="REF-001", **kwargs)


def lock_database(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=1)
    conn.execute("BEGIN EXCLUSIVE")
    return conn


class TestPersistencePaths:
    @pytest.mark.parametrize("db_path", ["", "   ", ":memory:"])
    def test_transient_durable_path_rejected(self, db_path: str) -> None:
        with pytest.raises(ValueError, match="transaction_db_path"):
            save_transaction(make_transaction(), db_path)

    def test_readonly_missing_path_does_not_create_database(self, tmp_path) -> None:
        db_path = tmp_path / "missing.db"

        with pytest.raises(FileNotFoundError, match="SQLite database not found"):
            list_transactions(str(db_path), readonly=True)

        assert not db_path.exists()


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


def create_v2_db(db_path: str) -> str:
    transaction_id = create_v1_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN state TEXT NOT NULL DEFAULT '{}'")
        conn.execute(
            "UPDATE rpacore_schema_versions SET version = ? WHERE component = ?",
            (2, "transactions"),
        )
        conn.commit()
    finally:
        conn.close()
    return transaction_id


def create_v3_db(db_path: str) -> str:
    transaction_id = create_v2_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE transactions ADD COLUMN started_at TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE transactions ADD COLUMN finished_at TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE transaction_history (
                transaction_id          TEXT NOT NULL,
                sequence                INTEGER NOT NULL,
                timestamp               TEXT NOT NULL,
                event                   TEXT NOT NULL CHECK (
                    event IN (
                        'transaction_started',
                        'skill_started',
                        'skill_succeeded',
                        'skill_failed',
                        'skill_skipped',
                        'skill_interrupted',
                        'retry_scheduled',
                        'transaction_resumed',
                        'transaction_completed'
                    )
                ),
                status                  TEXT NOT NULL CHECK (
                    status IN ('pending', 'in_progress', 'successful', 'failed', 'skipped')
                ),
                retry_number            INTEGER NOT NULL,
                skill_name              TEXT NOT NULL DEFAULT '',
                skill_execution_order   INTEGER,
                PRIMARY KEY (transaction_id, sequence),
                FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "UPDATE rpacore_schema_versions SET version = ? WHERE component = ?",
            (3, "transactions"),
        )
        conn.commit()
    finally:
        conn.close()
    return transaction_id


def create_v4_db(db_path: str) -> str:
    transaction_id = create_v3_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE transaction_metadata (
                transaction_id TEXT NOT NULL,
                key            TEXT NOT NULL,
                value_json     TEXT NOT NULL,
                PRIMARY KEY (transaction_id, key),
                FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "UPDATE rpacore_schema_versions SET version = ? WHERE component = ?",
            (4, "transactions"),
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

    def test_roundtrip_preserves_transaction_metadata(self, db_path) -> None:
        tx = make_transaction(
            metadata={
                "customer": "acme",
                "priority": 3,
                "flags": ["manual-review", "vip"],
                "nested": {"b": 2, "a": 1},
                "active": True,
                "closed_at": None,
            }
        )

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert loaded.metadata == tx.metadata

    def test_roundtrip_preserves_empty_transaction_metadata(self, db_path) -> None:
        tx = make_transaction()

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert loaded.metadata == {}

    def test_metadata_values_are_stored_as_canonical_json(self, db_path) -> None:
        tx = make_transaction(metadata={"nested": {"b": 2, "a": 1}})

        save_transaction(tx, db_path)

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT value_json FROM transaction_metadata WHERE transaction_id = ? AND key = ?",
                (tx.id, "nested"),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == '{"a":1,"b":2}'

    def test_roundtrip_preserves_transaction_artifacts(self, db_path) -> None:
        artifact = Artifact(
            id="artifact-001",
            name="invoice pdf",
            path="/missing/invoice.pdf",
            kind="pdf",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            metadata={"invoice_id": 42, "tags": ["generated"]},
        )
        tx = make_transaction(artifacts=[artifact])

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert len(loaded.artifacts) == 1
        loaded_artifact = loaded.artifacts[0]
        assert loaded_artifact.id == "artifact-001"
        assert loaded_artifact.name == "invoice pdf"
        assert loaded_artifact.path == "/missing/invoice.pdf"
        assert loaded_artifact.kind == "pdf"
        assert loaded_artifact.created_at == artifact.created_at
        assert loaded_artifact.metadata == {"invoice_id": 42, "tags": ["generated"]}

    def test_roundtrip_preserves_artifact_order(self, db_path) -> None:
        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        tx = make_transaction(
            artifacts=[
                Artifact(id="artifact-b", name="second-sort", path="b.txt", created_at=created_at),
                Artifact(id="artifact-a", name="first-sort", path="a.txt", created_at=created_at),
            ]
        )

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert [artifact.id for artifact in loaded.artifacts] == [
            "artifact-b",
            "artifact-a",
        ]

    def test_roundtrip_preserves_empty_transaction_artifacts(self, db_path) -> None:
        tx = make_transaction()

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert loaded.artifacts == []

    def test_artifact_metadata_is_stored_as_canonical_json(self, db_path) -> None:
        tx = make_transaction(
            artifacts=[
                Artifact(
                    id="artifact-001",
                    name="invoice",
                    path="/missing/invoice.pdf",
                    metadata={"nested": {"b": 2, "a": 1}},
                )
            ]
        )

        save_transaction(tx, db_path)

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT metadata FROM transaction_artifacts WHERE transaction_id = ? AND id = ?",
                (tx.id, "artifact-001"),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == '{"nested":{"a":1,"b":2}}'

    def test_roundtrip_preserves_transaction_timestamps(self, db_path) -> None:
        tx = make_transaction()
        tx.started_at = tx.created_at
        tx.finished_at = tx.created_at

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert loaded.created_at == tx.created_at
        assert loaded.started_at == tx.started_at
        assert loaded.finished_at == tx.finished_at

    def test_roundtrip_preserves_history_order(self, db_path) -> None:
        tx = make_transaction()
        tx.append_history(HistoryEvent.TRANSACTION_STARTED)
        tx.append_history(HistoryEvent.TRANSACTION_COMPLETED)

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert [entry.sequence for entry in loaded.history] == [1, 2]
        assert [entry.event for entry in loaded.history] == [
            HistoryEvent.TRANSACTION_STARTED,
            HistoryEvent.TRANSACTION_COMPLETED,
        ]

    def test_repeated_saves_do_not_duplicate_history(self, db_path) -> None:
        tx = make_transaction()
        tx.append_history(HistoryEvent.TRANSACTION_STARTED)

        save_transaction(tx, db_path)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert [entry.sequence for entry in loaded.history] == [1]

    def test_save_rejects_non_json_safe_transaction_state(self, db_path) -> None:
        tx = make_transaction()
        tx.state["client"] = object()

        with pytest.raises(TypeError) as exc_info:
            save_transaction(tx, db_path)

        message = str(exc_info.value)
        assert "transaction.state['client'] expected JSON value" in message
        assert "ctx.resources" in message

    def test_save_rejects_invalid_wiring_before_database_creation(self, tmp_path) -> None:
        db_path = tmp_path / "invalid-wiring.db"
        transaction = Transaction(reference="")

        with pytest.raises(ExecutionValidationError, match="transaction.reference"):
            save_transaction(transaction, str(db_path))

        assert not db_path.exists()

    def test_save_rejects_tuple_skill_arguments_before_database_creation(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "invalid-arguments.db"
        transaction = Transaction(
            reference="tuple-arguments",
            skills=[Skill("tuple", 1, arguments={"value": (1, 2)})],
        )

        with pytest.raises(TypeError, match=r"arguments\['value'\]"):
            save_transaction(transaction, str(db_path))

        assert not db_path.exists()

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

    def test_save_rejects_non_json_safe_transaction_metadata(self, db_path) -> None:
        tx = make_transaction(metadata={"invoice": {"client": object()}})

        with pytest.raises(TypeError) as exc_info:
            save_transaction(tx, db_path)

        assert "transaction.metadata['invoice']['client'] expected JSON value" in str(exc_info.value)

    def test_save_rejects_non_object_transaction_metadata(self, db_path) -> None:
        tx = make_transaction()
        tx.metadata = ["invoice"]  # type: ignore[assignment]

        with pytest.raises(TypeError) as exc_info:
            save_transaction(tx, db_path)

        assert "transaction.metadata expected JSON object" in str(exc_info.value)

    def test_list_rejects_non_json_safe_metadata_filter(self, db_path) -> None:
        with pytest.raises(TypeError) as exc_info:
            list_transactions(db_path, metadata_filter={"client": object()})

        assert "metadata_filter['client'] expected JSON value" in str(exc_info.value)

    def test_save_rejects_non_json_safe_artifact_metadata(self, db_path) -> None:
        tx = make_transaction(
            artifacts=[
                Artifact(
                    name="invoice",
                    path="/missing/invoice.pdf",
                    metadata={"client": object()},
                )
            ]
        )

        with pytest.raises(TypeError) as exc_info:
            save_transaction(tx, db_path)

        assert "transaction.artifacts[0].metadata['client'] expected JSON value" in str(
            exc_info.value
        )

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

    @pytest.mark.parametrize("stored_arguments", ["not-json", "[]"])
    def test_load_rejects_corrupt_skill_arguments(
        self,
        db_path,
        stored_arguments: str,
    ) -> None:
        transaction = make_transaction(skills=[Skill("submit", 1)])
        save_transaction(transaction, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE skills SET arguments = ? WHERE transaction_id = ?",
                (stored_arguments, transaction.id),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(transaction.id, db_path)

        assert "Persisted skill arguments are invalid" in str(exc_info.value)
        assert transaction.id in str(exc_info.value)
        assert "submit" in str(exc_info.value)
        assert (
            exc_info.value.action
            == "repair skill arguments in the persistence database"
        )

    def test_load_rejects_corrupt_transaction_timestamp(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE transactions SET started_at = ? WHERE id = ?", ("not-a-date", tx.id))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(tx.id, db_path)

        assert "Persisted transaction timestamp is invalid" in str(exc_info.value)
        assert exc_info.value.action == "repair transaction timestamps in the persistence database"

    def test_load_rejects_corrupt_transaction_history(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA ignore_check_constraints = ON")
            conn.execute(
                "INSERT INTO transaction_history "
                "(transaction_id, sequence, timestamp, event, status, retry_number, skill_name, skill_execution_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tx.id, 1, "not-a-date", "transaction_started", "pending", 0, "", None),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(tx.id, db_path)

        assert "Persisted transaction history is invalid" in str(exc_info.value)
        assert exc_info.value.action == "repair transaction history in the persistence database"

    def test_load_rejects_corrupt_transaction_metadata(self, db_path) -> None:
        tx = make_transaction(metadata={"customer": "acme"})
        save_transaction(tx, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE transaction_metadata SET value_json = ? WHERE transaction_id = ? AND key = ?",
                ("not-json", tx.id, "customer"),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(tx.id, db_path)

        assert f"Persisted transaction metadata is invalid for transaction {tx.id!r}" in str(
            exc_info.value
        )
        assert exc_info.value.action == "repair transaction metadata in the persistence database"

    def test_load_rejects_corrupt_transaction_artifact_metadata(self, db_path) -> None:
        tx = make_transaction(
            artifacts=[Artifact(id="artifact-001", name="invoice", path="/missing/invoice.pdf")]
        )
        save_transaction(tx, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE transaction_artifacts SET metadata = ? WHERE transaction_id = ? AND id = ?",
                ("not-json", tx.id, "artifact-001"),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(tx.id, db_path)

        assert "Persisted transaction artifact metadata is invalid" in str(exc_info.value)
        assert (
            exc_info.value.action
            == "repair transaction artifact metadata in the persistence database"
        )

    def test_load_rejects_corrupt_transaction_artifact_timestamp(self, db_path) -> None:
        tx = make_transaction(
            artifacts=[Artifact(id="artifact-001", name="invoice", path="/missing/invoice.pdf")]
        )
        save_transaction(tx, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE transaction_artifacts SET created_at = ? WHERE transaction_id = ? AND id = ?",
                ("not-a-date", tx.id, "artifact-001"),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(tx.id, db_path)

        assert "Persisted transaction artifact timestamp is invalid" in str(exc_info.value)
        assert exc_info.value.action == "repair transaction artifacts in the persistence database"

    def test_history_event_and_status_are_schema_constrained(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        conn = sqlite3.connect(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO transaction_history "
                    "(transaction_id, sequence, timestamp, event, status, retry_number, skill_name, skill_execution_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tx.id,
                        1,
                        tx.created_at.isoformat() if tx.created_at is not None else "",
                        "bad_event",
                        "pending",
                        0,
                        "",
                        None,
                    ),
                )
            conn.rollback()
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO transaction_history "
                    "(transaction_id, sequence, timestamp, event, status, retry_number, skill_name, skill_execution_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tx.id,
                        1,
                        tx.created_at.isoformat() if tx.created_at is not None else "",
                        "transaction_started",
                        "bad_status",
                        0,
                        "",
                        None,
                    ),
                )
        finally:
            conn.close()

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
        with pytest.raises(ExecutionValidationError, match="skill.name must be unique"):
            save_transaction(tx, db_path)

        assert not Path(db_path).exists()

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

    def test_list_filters_by_top_level_metadata_exact_match(self, db_path) -> None:
        matching = make_transaction(metadata={"customer": "acme", "priority": 3})
        wrong_value = make_transaction(metadata={"customer": "acme", "priority": 2})
        missing_key = make_transaction(metadata={"customer": "acme"})
        nested_match = make_transaction(metadata={"customer": {"id": 42, "name": "acme"}})
        for tx in [matching, wrong_value, missing_key, nested_match]:
            save_transaction(tx, db_path)

        assert {
            tx.id
            for tx in list_transactions(
                db_path,
                metadata_filter={"customer": "acme", "priority": 3},
            )
        } == {matching.id}
        assert {
            tx.id
            for tx in list_transactions(
                db_path,
                metadata_filter={"customer": {"name": "acme", "id": 42}},
            )
        } == {nested_match.id}

    def test_list_empty_metadata_filter_matches_all_transactions(self, db_path) -> None:
        tx = make_transaction(metadata={"customer": "acme"})
        save_transaction(tx, db_path)

        assert [item.id for item in list_transactions(db_path, metadata_filter={})] == [tx.id]

    def test_list_skips_transaction_deleted_after_identifier_selection(
        self,
        db_path,
        monkeypatch,
    ) -> None:
        deleted_during_list = Transaction(
            reference="deleted-during-list",
            created_at=datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc),
        )
        retained = Transaction(
            reference="retained",
            created_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )
        save_transaction(deleted_during_list, db_path)
        save_transaction(retained, db_path)
        load_transaction = persistence_module.load_transaction

        def load_after_cleanup(
            transaction_id: str,
            database_path: str,
            *,
            readonly: bool = False,
        ) -> Transaction:
            if transaction_id == deleted_during_list.id:
                _delete_unbound_pending_transaction(
                    transaction_id,
                    db_path=database_path,
                )
            return load_transaction(
                transaction_id,
                database_path,
                readonly=readonly,
            )

        monkeypatch.setattr(
            persistence_module,
            "load_transaction",
            load_after_cleanup,
        )

        assert [
            transaction.id
            for transaction in list_transactions(db_path, readonly=True)
        ] == [retained.id]

    def test_iterator_is_not_limited_to_list_default(self, db_path) -> None:
        created_at = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
        for index in range(101):
            save_transaction(
                Transaction(
                    id=f"transaction-{index:03d}",
                    reference=f"transaction-{index:03d}",
                    created_at=created_at,
                ),
                db_path,
            )

        assert len(list_transactions(db_path, readonly=True)) == 100
        assert len(list(iter_transactions(db_path, readonly=True))) == 101

    @pytest.mark.parametrize("reader", ["list", "iterator"])
    def test_snapshot_readers_propagate_non_missing_load_failures(
        self,
        db_path,
        monkeypatch,
        reader: str,
    ) -> None:
        save_transaction(Transaction(reference="corrupt"), db_path)

        def fail_load(
            _transaction_id: str,
            _database_path: str,
            *,
            readonly: bool = False,
        ) -> Transaction:
            del readonly
            raise ValueError("corrupt transaction record")

        monkeypatch.setattr(persistence_module, "load_transaction", fail_load)

        with pytest.raises(ValueError, match="corrupt transaction record"):
            if reader == "list":
                list_transactions(db_path, readonly=True)
            else:
                list(iter_transactions(db_path, readonly=True))

    def test_paused_iterator_does_not_block_checkpoint_and_keeps_membership_snapshot(
        self,
        db_path,
    ) -> None:
        older = Transaction(
            reference="older",
            created_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )
        newer = Transaction(
            reference="newer",
            created_at=datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc),
        )
        save_transaction(older, db_path)
        save_transaction(newer, db_path)
        transactions = iter_transactions(db_path, readonly=True)

        assert next(transactions).id == newer.id
        older.reference = "updated-after-snapshot"
        save_transaction(older, db_path)
        inserted_later = Transaction(
            reference="inserted-later",
            created_at=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
        )
        save_transaction(inserted_later, db_path)

        remaining = list(transactions)
        assert [transaction.id for transaction in remaining] == [older.id]
        assert remaining[0].reference == "updated-after-snapshot"

    def test_closing_partially_consumed_iterator_releases_resources(
        self,
        db_path,
    ) -> None:
        older = Transaction(
            reference="older",
            created_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )
        newer = Transaction(
            reference="newer",
            created_at=datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc),
        )
        save_transaction(older, db_path)
        save_transaction(newer, db_path)
        transactions = iter_transactions(db_path, readonly=True)

        assert next(transactions).id == newer.id
        transactions.close()

        save_transaction(Transaction(reference="after-close"), db_path)

    def test_iterator_skips_transaction_deleted_after_membership_snapshot(
        self,
        db_path,
    ) -> None:
        deleted_during_iteration = Transaction(
            reference="deleted-during-iteration",
            created_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        )
        first = Transaction(
            reference="first",
            created_at=datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc),
        )
        save_transaction(deleted_during_iteration, db_path)
        save_transaction(first, db_path)
        transactions = iter_transactions(db_path, readonly=True)

        assert next(transactions).id == first.id
        _delete_unbound_pending_transaction(
            deleted_during_iteration.id,
            db_path=db_path,
        )

        assert list(transactions) == []

    def test_iterator_yields_nothing_when_every_selected_transaction_is_deleted(
        self,
        db_path,
        monkeypatch,
    ) -> None:
        selected = [
            Transaction(reference="first"),
            Transaction(reference="second"),
        ]
        for transaction in selected:
            save_transaction(transaction, db_path)
        load_transaction = persistence_module.load_transaction

        def load_after_cleanup(
            transaction_id: str,
            database_path: str,
            *,
            readonly: bool = False,
        ) -> Transaction:
            _delete_unbound_pending_transaction(
                transaction_id,
                db_path=database_path,
            )
            return load_transaction(
                transaction_id,
                database_path,
                readonly=readonly,
            )

        monkeypatch.setattr(
            persistence_module,
            "load_transaction",
            load_after_cleanup,
        )

        assert list(iter_transactions(db_path, readonly=True)) == []

    def test_iterator_preserves_order_and_filters(self, db_path) -> None:
        matching_older = Transaction(
            reference="matching-older",
            status=Status.SUCCESSFUL,
            created_at=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
            metadata={"customer": "acme"},
        )
        matching_newer = Transaction(
            reference="matching-newer",
            status=Status.SUCCESSFUL,
            created_at=datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc),
            metadata={"customer": "acme"},
        )
        wrong_status = Transaction(
            reference="wrong-status",
            status=Status.FAILED,
            created_at=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
            metadata={"customer": "acme"},
        )
        wrong_metadata = Transaction(
            reference="wrong-metadata",
            status=Status.SUCCESSFUL,
            created_at=datetime(2026, 7, 14, 13, 0, tzinfo=timezone.utc),
            metadata={"customer": "other"},
        )
        before_cutoff = Transaction(
            reference="before-cutoff",
            status=Status.SUCCESSFUL,
            created_at=datetime(2026, 7, 13, 23, 59, tzinfo=timezone.utc),
            metadata={"customer": "acme"},
        )
        for transaction in (
            matching_older,
            matching_newer,
            wrong_status,
            wrong_metadata,
            before_cutoff,
        ):
            save_transaction(transaction, db_path)

        transactions = iter_transactions(
            db_path,
            status=Status.SUCCESSFUL,
            since=datetime(2026, 7, 14, tzinfo=timezone.utc),
            metadata_filter={"customer": "acme"},
            readonly=True,
        )

        assert [transaction.id for transaction in transactions] == [
            matching_newer.id,
            matching_older.id,
        ]


class TestCrashRecovery:
    def test_in_progress_skill_loaded_faithfully(self, db_path) -> None:
        skill = Skill("process", 1)
        skill.status = Status.IN_PROGRESS
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].status is Status.IN_PROGRESS

    def test_in_progress_transaction_loaded_faithfully(self, db_path) -> None:
        tx = make_transaction(status=Status.IN_PROGRESS)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.status is Status.IN_PROGRESS

    def test_successful_skills_not_changed_by_faithful_load(self, db_path) -> None:
        s1 = Skill("login", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Skill("process", 2)
        s2.status = Status.IN_PROGRESS
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].status is Status.SUCCESSFUL
        assert loaded.skills[1].status is Status.IN_PROGRESS


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
        assert loaded.created_at is None
        assert loaded.started_at is None
        assert loaded.finished_at is None
        assert loaded.state == {}
        assert loaded.history == []
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

    def test_v1_schema_gets_timestamp_columns_and_history_table(self, db_path) -> None:
        create_v1_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
            history_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transaction_history'"
            ).fetchone()
        finally:
            conn.close()
        assert {"started_at", "finished_at"}.issubset(columns)
        assert history_exists is not None

    def test_v3_schema_gets_metadata_table(self, db_path) -> None:
        create_v3_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            metadata_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transaction_metadata'"
            ).fetchone()
        finally:
            conn.close()
        assert metadata_exists is not None

    def test_v3_schema_metadata_filter_migrates_and_returns_no_matches(self, db_path) -> None:
        create_v3_db(db_path)

        result = list_transactions(db_path, metadata_filter={"customer": "acme"})

        assert result == []

    def test_v4_schema_gets_artifact_table(self, db_path) -> None:
        create_v4_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            artifact_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transaction_artifacts'"
            ).fetchone()
        finally:
            conn.close()
        assert artifact_exists is not None

    def test_v1_schema_loads_with_empty_state(self, db_path) -> None:
        transaction_id = create_v1_db(db_path)

        loaded = load_transaction(transaction_id, db_path)

        assert loaded.state == {}

    def test_legacy_schema_unknown_created_at_is_not_invented_for_since_filter(self, db_path) -> None:
        create_legacy_db(db_path)
        cutoff = datetime.now(timezone.utc)

        result = list_transactions(db_path, since=cutoff)

        assert result == []

    def test_legacy_schema_migration_is_idempotent(self, db_path) -> None:
        transaction_id = create_legacy_db(db_path)

        first = list_transactions(db_path)
        second = list_transactions(db_path)

        assert [tx.id for tx in first] == [transaction_id]
        assert [tx.id for tx in second] == [transaction_id]

    def test_v2_schema_migration_to_latest_is_idempotent(self, db_path) -> None:
        transaction_id = create_v2_db(db_path)

        first = list_transactions(db_path)
        second = list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            transaction_columns = [
                row[1] for row in conn.execute("PRAGMA table_info(transactions)")
            ]
            history_tables = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'transaction_history'"
            ).fetchone()[0]
            metadata_tables = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'transaction_metadata'"
            ).fetchone()[0]
            artifact_tables = conn.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'transaction_artifacts'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert [tx.id for tx in first] == [transaction_id]
        assert [tx.id for tx in second] == [transaction_id]
        assert transaction_columns.count("started_at") == 1
        assert transaction_columns.count("finished_at") == 1
        assert history_tables == 1
        assert metadata_tables == 1
        assert artifact_tables == 1

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
        assert version == 5

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

        assert rows == [("queue", 2), ("transactions", 5)]

    def test_unsupported_transaction_schema_version_raises(self, db_path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE rpacore_schema_versions ("
                "component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO rpacore_schema_versions (component, version) VALUES (?, ?)",
                ("transactions", 6),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(RuntimeError, match="Unsupported transaction schema version 6"):
            list_transactions(db_path)

    @pytest.mark.parametrize("readonly", [False, True])
    def test_future_transaction_schema_is_rejected_without_mutation(
        self,
        db_path,
        readonly: bool,
    ) -> None:
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
        with open(db_path, "rb") as database_file:
            before = database_file.read()

        with pytest.raises(RuntimeError, match="Unsupported transaction schema version 99"):
            list_transactions(db_path, readonly=readonly)

        with open(db_path, "rb") as database_file:
            assert database_file.read() == before

    def test_future_queue_schema_blocks_transaction_mutation(self, db_path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE rpacore_schema_versions ("
                "component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO rpacore_schema_versions VALUES ('queue', 99)"
            )
            conn.commit()
        finally:
            conn.close()
        with open(db_path, "rb") as database_file:
            before = database_file.read()

        with pytest.raises(RuntimeError, match="Unsupported queue schema version 99"):
            save_transaction(make_transaction(), db_path)

        with open(db_path, "rb") as database_file:
            assert database_file.read() == before

    def test_delete_unbound_pending_transaction_is_persistence_owned(self, db_path) -> None:
        transaction = make_transaction()
        save_transaction(transaction, db_path)

        _delete_unbound_pending_transaction(transaction.id, db_path=db_path)

        with pytest.raises(KeyError, match="Transaction not found"):
            load_transaction(transaction.id, db_path)

    def test_delete_unbound_pending_transaction_rejects_started_work(self, db_path) -> None:
        transaction = make_transaction(status=Status.IN_PROGRESS)
        transaction.append_history(HistoryEvent.TRANSACTION_STARTED)
        save_transaction(transaction, db_path)

        with pytest.raises(RuntimeError, match="Refusing to delete transaction"):
            _delete_unbound_pending_transaction(transaction.id, db_path=db_path)

        assert load_transaction(transaction.id, db_path).status is Status.IN_PROGRESS

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
