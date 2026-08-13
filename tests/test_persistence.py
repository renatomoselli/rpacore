"""Tests for rpacore.persistence."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import rpacore.persistence as persistence_module
from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import (
    BusinessException,
    DefinitionIdentityError,
    ExecutionValidationError,
    SystemException,
)
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.persistence import (
    TransactionFenceError,
    TransactionPage,
    _delete_unbound_pending_transaction,
    _save_queue_transaction_fenced,
    iter_transactions,
    list_transactions,
    load_transaction,
    query_transactions,
    save_transaction,
)
from rpacore.recovery import resume_transaction
from rpacore.step import Step
from rpacore.status import Status
from rpacore.transaction import Artifact, HistoryEvent, Transaction


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def make_transaction(**kwargs) -> Transaction:
    kwargs.setdefault("definition_identity", "tests.persistence/v1")
    return Transaction(reference="REF-001", **kwargs)


def seed_transaction_query_rows(db_path: str, count: int) -> datetime:
    """Create lightweight rows for deterministic transaction-query tests."""
    save_transaction(make_transaction(), db_path)
    base = datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)
    rows = [
        (
            f"scale-{index:04d}",
            "scale-reference",
            "successful",
            (base + timedelta(seconds=index)).isoformat(),
        )
        for index in range(count)
    ]
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO transactions "
            "(id, reference, status, retry_count, created_at, created_at_utc, state) "
            "VALUES (?, ?, ?, 0, ?, ?, '{}')",
            [
                (transaction_id, reference, status, timestamp, timestamp)
                for transaction_id, reference, status, timestamp in rows
            ],
        )
        conn.executemany(
            "INSERT INTO transaction_metadata (transaction_id, key, value_json) "
            "VALUES (?, 'run_id', ?)",
            [(transaction_id, json.dumps("scale")) for transaction_id, *_ in rows],
        )
        conn.commit()
    finally:
        conn.close()
    return base


def lock_database(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=1)
    conn.execute("BEGIN EXCLUSIVE")
    return conn


def transaction_storage_snapshot(db_path: str, transaction_id: str) -> dict[str, list[tuple]]:
    """Return every persisted row owned by one transaction."""
    conn = sqlite3.connect(db_path)
    try:
        step_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM steps WHERE transaction_id = ? ORDER BY id",
                (transaction_id,),
            ).fetchall()
        ]
        placeholders = ", ".join("?" for _ in step_ids)
        exceptions = (
            conn.execute(
                f"SELECT * FROM exceptions WHERE step_id IN ({placeholders}) ORDER BY id",
                step_ids,
            ).fetchall()
            if step_ids
            else []
        )
        return {
            "transactions": conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
            ).fetchall(),
            "steps": conn.execute(
                "SELECT * FROM steps WHERE transaction_id = ? ORDER BY id",
                (transaction_id,),
            ).fetchall(),
            "exceptions": exceptions,
            "history": conn.execute(
                "SELECT * FROM transaction_history WHERE transaction_id = ? ORDER BY sequence",
                (transaction_id,),
            ).fetchall(),
            "metadata": conn.execute(
                "SELECT * FROM transaction_metadata WHERE transaction_id = ? ORDER BY key",
                (transaction_id,),
            ).fetchall(),
            "artifacts": conn.execute(
                "SELECT * FROM transaction_artifacts WHERE transaction_id = ? ORDER BY sequence",
                (transaction_id,),
            ).fetchall(),
        }
    finally:
        conn.close()


class TestPersistencePaths:
    @pytest.mark.parametrize("db_path", ["", "   ", ":memory:", " :memory: "])
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


def downgrade_current_db_to_v9(db_path: str) -> None:
    """Translate a disposable current database back to the released v9 schema."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("ALTER TABLE steps RENAME TO skills")
        conn.execute("ALTER TABLE exceptions RENAME COLUMN step_id TO skill_id")
        conn.execute(
            "ALTER TABLE exceptions RENAME COLUMN occurred_at TO datetime_occurred"
        )
        conn.execute(
            "ALTER TABLE exceptions RENAME COLUMN halts_remaining_steps TO stops_execution"
        )
        conn.execute(
            """
            CREATE TABLE transaction_history_v9 (
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
            "INSERT INTO transaction_history_v9 "
            "SELECT transaction_id, sequence, timestamp, "
            "CASE event "
            "WHEN 'step_started' THEN 'skill_started' "
            "WHEN 'step_succeeded' THEN 'skill_succeeded' "
            "WHEN 'step_failed' THEN 'skill_failed' "
            "WHEN 'step_skipped' THEN 'skill_skipped' "
            "WHEN 'step_interrupted' THEN 'skill_interrupted' "
            "ELSE event END, status, retry_number, step_name, step_execution_order "
            "FROM transaction_history"
        )
        conn.execute("DROP TABLE transaction_history")
        conn.execute("ALTER TABLE transaction_history_v9 RENAME TO transaction_history")
        conn.execute(
            "UPDATE rpacore_schema_versions SET version = 9 WHERE component = 'transactions'"
        )
        conn.commit()
    finally:
        conn.close()


def create_versioned_db(db_path: str, version: int) -> str:
    """Create the exact historical transaction schema requested for migration proof."""
    early_factories = {
        1: create_v1_db,
        2: create_v2_db,
        3: create_v3_db,
        4: create_v4_db,
    }
    if version in early_factories:
        return early_factories[version](db_path)
    transaction_id = create_v4_db(db_path)
    conn = persistence_module._connect(db_path)
    try:
        with conn:
            migrations = (
                persistence_module._migrate_transactions_to_v5,
                persistence_module._migrate_transactions_to_v6,
                persistence_module._migrate_transactions_to_v7,
                persistence_module._migrate_transactions_to_v8,
                persistence_module._migrate_transactions_to_v9,
            )
            for migration_version, migration in enumerate(migrations, start=5):
                if migration_version > version:
                    break
                migration(conn)
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
        assert loaded.steps == []

    def test_roundtrip_preserves_definition_identity(self, db_path) -> None:
        transaction = make_transaction(
            definition_identity="invoice-processing/v3",
        )

        save_transaction(transaction, db_path)
        loaded = load_transaction(transaction.id, db_path)

        assert loaded.definition_identity == "invoice-processing/v3"

    def test_save_rejects_definition_identity_change_without_mutation(
        self,
        db_path,
    ) -> None:
        transaction = make_transaction()
        save_transaction(transaction, db_path)
        before = transaction_storage_snapshot(db_path, transaction.id)
        transaction.definition_identity = "tests.persistence/v2"
        transaction.reference = "must-not-persist"

        with pytest.raises(DefinitionIdentityError, match="immutable"):
            save_transaction(transaction, db_path)

        assert transaction_storage_snapshot(db_path, transaction.id) == before

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

    def test_roundtrip_preserves_outcome_and_failure_codes(self, db_path) -> None:
        step = Step("validate", 1)
        step.status = Status.FAILED
        step.exceptions.append(
            BusinessException("missing invoice", code="acme.invoice.missing_number")
        )
        tx = make_transaction(
            status=Status.FAILED,
            outcome_category=OutcomeCategory.BUSINESS_FAILED,
            retry_disposition=RetryDisposition.NOT_REQUESTED,
            failure_code="acme.invoice.missing_number",
            steps=[step],
        )

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        assert loaded.outcome_category is OutcomeCategory.BUSINESS_FAILED
        assert loaded.retry_disposition is RetryDisposition.NOT_REQUESTED
        assert loaded.failure_code == "acme.invoice.missing_number"
        assert loaded.steps[0].exceptions[0].code == "acme.invoice.missing_number"

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

    def test_save_rejects_tuple_step_arguments_before_database_creation(
        self,
        tmp_path,
    ) -> None:
        db_path = tmp_path / "invalid-arguments.db"
        transaction = Transaction(
            reference="tuple-arguments",
            steps=[Step("tuple", 1, arguments={"value": (1, 2)})],
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
    def test_load_rejects_corrupt_step_arguments(
        self,
        db_path,
        stored_arguments: str,
    ) -> None:
        transaction = make_transaction(steps=[Step("submit", 1)])
        save_transaction(transaction, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE steps SET arguments = ? WHERE transaction_id = ?",
                (stored_arguments, transaction.id),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemException) as exc_info:
            load_transaction(transaction.id, db_path)

        assert "Persisted step arguments are invalid" in str(exc_info.value)
        assert transaction.id in str(exc_info.value)
        assert "submit" in str(exc_info.value)
        assert (
            exc_info.value.action
            == "repair step arguments in the persistence database"
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
                "(transaction_id, sequence, timestamp, event, status, retry_number, step_name, step_execution_order) "
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
                    "(transaction_id, sequence, timestamp, event, status, retry_number, step_name, step_execution_order) "
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
                    "(transaction_id, sequence, timestamp, event, status, retry_number, step_name, step_execution_order) "
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

    def test_roundtrip_preserves_steps(self, db_path) -> None:
        s1 = Step("login", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Step("fetch", 2)
        s2.status = Status.FAILED
        tx = make_transaction(steps=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.steps) == 2
        assert loaded.steps[0].name == "login"
        assert loaded.steps[0].status is Status.SUCCESSFUL
        assert loaded.steps[1].name == "fetch"
        assert loaded.steps[1].status is Status.FAILED

    def test_roundtrip_preserves_step_execution_order(self, db_path) -> None:
        s1 = Step("a", 3)
        s2 = Step("b", 1)
        tx = make_transaction(steps=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.steps[0].name == "b"
        assert loaded.steps[0].execution_order == 1
        assert loaded.steps[1].name == "a"
        assert loaded.steps[1].execution_order == 3

    def test_roundtrip_preserves_business_exception(self, db_path) -> None:
        step = Step("validate", 1)
        exc = BusinessException("bad data", action="validate", retry_number=0)
        step.exceptions.append(exc)
        step.status = Status.FAILED
        tx = make_transaction(steps=[step])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.steps[0].exceptions) == 1
        loaded_exc = loaded.steps[0].exceptions[0]
        assert isinstance(loaded_exc, BusinessException)
        assert str(loaded_exc) == "bad data"
        assert loaded_exc.action == "validate"
        assert loaded_exc.retry_number == 0

    def test_roundtrip_preserves_stopping_business_exception(self, db_path) -> None:
        step = Step("validate", 1)
        exc = BusinessException(
            "bad data",
            action="validate",
            retry_number=0,
            halts_remaining_steps=True,
        )
        step.exceptions.append(exc)
        step.status = Status.FAILED
        tx = make_transaction(steps=[step])

        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)

        loaded_exc = loaded.steps[0].exceptions[0]
        assert isinstance(loaded_exc, BusinessException)
        assert loaded_exc.halts_remaining_steps is True
        assert loaded_exc.halts_remaining_steps is True

    def test_roundtrip_preserves_system_exception(self, db_path) -> None:
        step = Step("connect", 1)
        exc = SystemException("timeout", action="connect", retry_number=1)
        step.exceptions.append(exc)
        step.status = Status.FAILED
        tx = make_transaction(steps=[step])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        loaded_exc = loaded.steps[0].exceptions[0]
        assert isinstance(loaded_exc, SystemException)
        assert str(loaded_exc) == "timeout"
        assert loaded_exc.retry_number == 1

    def test_roundtrip_preserves_multiple_exceptions(self, db_path) -> None:
        step = Step("connect", 1)
        step.exceptions.append(SystemException("timeout", action="connect", retry_number=0))
        step.exceptions.append(SystemException("timeout", action="connect", retry_number=1))
        step.status = Status.FAILED
        tx = make_transaction(steps=[step])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.steps[0].exceptions) == 2
        assert loaded.steps[0].exceptions[0].retry_number == 0
        assert loaded.steps[0].exceptions[1].retry_number == 1

    def test_roundtrip_preserves_screenshot_path(self, db_path) -> None:
        step = Step("capture", 1)
        step.status = Status.FAILED
        exc = SystemException("crash", action="capture", screenshot_path="/tmp/shot.png")
        step.exceptions.append(exc)
        tx = make_transaction(steps=[step])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        loaded_exc = loaded.steps[0].exceptions[0]
        assert loaded_exc.screenshot_path == "/tmp/shot.png"

    def test_roundtrip_preserves_empty_screenshot_path(self, db_path) -> None:
        step = Step("noscreenshot", 1)
        step.status = Status.FAILED
        exc = BusinessException("bad data", action="noscreenshot")
        step.exceptions.append(exc)
        tx = make_transaction(steps=[step])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        loaded_exc = loaded.steps[0].exceptions[0]
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

    def test_removed_step_deleted_on_resave(self, db_path) -> None:
        s1 = Step("a", 1)
        s2 = Step("b", 2)
        tx = make_transaction(steps=[s1, s2])
        save_transaction(tx, db_path)
        tx.steps.remove(s2)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.steps) == 1
        assert loaded.steps[0].name == "a"

    def test_roundtrip_preserves_arguments(self, db_path) -> None:
        step = Step("login", 1, arguments={"user": "admin", "timeout": 30})
        tx = make_transaction(steps=[step])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.steps[0].arguments == {"user": "admin", "timeout": 30}

    def test_duplicate_step_name_and_order_raises(self, db_path) -> None:
        s1 = Step("dup", 1)
        s2 = Step("dup", 1)
        tx = make_transaction(steps=[s1, s2])
        with pytest.raises(ExecutionValidationError, match="step.name must be unique"):
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


class TestQueueClaimFencing:
    def test_stale_same_label_checkpoint_changes_no_transaction_rows(self, tmp_path) -> None:
        from rpacore.queue import QueueItem, SqliteQueue

        queue_db = str(tmp_path / "queue.db")
        transaction_db = str(tmp_path / "transactions.db")
        queue = SqliteQueue({"db_path": queue_db, "lease_timeout": 1})
        queue.add(QueueItem(reference="fenced", payload={}))
        stale = queue.next_item("shared-worker")
        assert stale is not None
        transaction = Transaction(
            reference="fenced",
            metadata={"source": "initial"},
            artifacts=[Artifact(name="initial", path="initial.txt")],
            steps=[Step("step", 1)],
        )
        revision = _save_queue_transaction_fenced(
            transaction,
            db_path=transaction_db,
            queue_db_path=queue_db,
            queue_item_id=stale.id,
            claimed_by=stale.claimed_by,
            claim_token=stale.claim_token,
            expected_revision=0,
        )
        assert revision == 1
        queue.bind_transaction(
            stale.id,
            transaction.id,
            claimed_by=stale.claimed_by,
            claim_token=stale.claim_token,
        )

        conn = sqlite3.connect(queue_db)
        try:
            conn.execute(
                "UPDATE queue_items SET claimed_at = datetime('now', '-10 seconds') "
                "WHERE id = ?",
                (stale.id,),
            )
            conn.commit()
        finally:
            conn.close()
        current = queue.next_item("shared-worker")
        assert current is not None
        assert current.claim_token != stale.claim_token

        transaction.state["stale"] = True
        transaction.metadata = {"source": "stale"}
        transaction.artifacts = [Artifact(name="stale", path="stale.txt")]
        transaction.steps[0].status = Status.SUCCESSFUL
        transaction.append_history(HistoryEvent.STEP_SUCCEEDED, step=transaction.steps[0])
        before = transaction_storage_snapshot(transaction_db, transaction.id)

        with pytest.raises(TransactionFenceError, match="claim is stale"):
            _save_queue_transaction_fenced(
                transaction,
                db_path=transaction_db,
                queue_db_path=queue_db,
                queue_item_id=stale.id,
                claimed_by=stale.claimed_by,
                claim_token=stale.claim_token,
                expected_revision=revision,
            )

        assert transaction_storage_snapshot(transaction_db, transaction.id) == before
        assert _save_queue_transaction_fenced(
            transaction,
            db_path=transaction_db,
            queue_db_path=queue_db,
            queue_item_id=current.id,
            claimed_by=current.claimed_by,
            claim_token=current.claim_token,
            expected_revision=revision,
        ) == 2

    def test_manual_save_advances_revision_and_fences_older_queue_snapshot(self, tmp_path) -> None:
        from rpacore.queue import QueueItem, SqliteQueue

        queue_db = str(tmp_path / "queue.db")
        transaction_db = str(tmp_path / "transactions.db")
        queue = SqliteQueue({"db_path": queue_db})
        queue.add(QueueItem(reference="revision", payload={}))
        claim = queue.next_item("worker")
        assert claim is not None
        transaction = Transaction(reference="revision", steps=[Step("step", 1)])
        revision = _save_queue_transaction_fenced(
            transaction,
            db_path=transaction_db,
            queue_db_path=queue_db,
            queue_item_id=claim.id,
            claimed_by=claim.claimed_by,
            claim_token=claim.claim_token,
            expected_revision=0,
        )
        queue.bind_transaction(
            claim.id,
            transaction.id,
            claimed_by=claim.claimed_by,
            claim_token=claim.claim_token,
        )

        transaction.state["manual"] = True
        save_transaction(transaction, transaction_db)
        before = transaction_storage_snapshot(transaction_db, transaction.id)
        transaction.state["stale"] = True

        with pytest.raises(TransactionFenceError, match="revision 1 is stale"):
            _save_queue_transaction_fenced(
                transaction,
                db_path=transaction_db,
                queue_db_path=queue_db,
                queue_item_id=claim.id,
                claimed_by=claim.claimed_by,
                claim_token=claim.claim_token,
                expected_revision=revision,
            )

        assert transaction_storage_snapshot(transaction_db, transaction.id) == before

    def test_child_write_failure_rolls_back_header_and_children(self, monkeypatch, tmp_path) -> None:
        from rpacore.queue import QueueItem, SqliteQueue

        queue_db = str(tmp_path / "queue.db")
        transaction_db = str(tmp_path / "transactions.db")
        queue = SqliteQueue({"db_path": queue_db})
        queue.add(QueueItem(reference="rollback", payload={}))
        claim = queue.next_item("worker")
        assert claim is not None
        transaction = Transaction(reference="rollback", steps=[Step("step", 1)])
        revision = _save_queue_transaction_fenced(
            transaction,
            db_path=transaction_db,
            queue_db_path=queue_db,
            queue_item_id=claim.id,
            claimed_by=claim.claimed_by,
            claim_token=claim.claim_token,
            expected_revision=0,
        )
        before = transaction_storage_snapshot(transaction_db, transaction.id)
        transaction.state["not-durable"] = True
        real_write = persistence_module._write_transaction_rows

        def write_then_fail(*args, **kwargs):
            real_write(*args, **kwargs)
            raise sqlite3.OperationalError("injected child write failure")

        monkeypatch.setattr(persistence_module, "_write_transaction_rows", write_then_fail)
        with pytest.raises(sqlite3.OperationalError, match="injected child write failure"):
            _save_queue_transaction_fenced(
                transaction,
                db_path=transaction_db,
                queue_db_path=queue_db,
                queue_item_id=claim.id,
                claimed_by=claim.claimed_by,
                claim_token=claim.claim_token,
                expected_revision=revision,
            )

        assert transaction_storage_snapshot(transaction_db, transaction.id) == before


class TestCrashRecovery:
    def test_in_progress_step_loaded_faithfully(self, db_path) -> None:
        step = Step("process", 1)
        step.status = Status.IN_PROGRESS
        tx = make_transaction(steps=[step])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.steps[0].status is Status.IN_PROGRESS

    def test_in_progress_transaction_loaded_faithfully(self, db_path) -> None:
        tx = make_transaction(status=Status.IN_PROGRESS)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.status is Status.IN_PROGRESS

    def test_successful_steps_not_changed_by_faithful_load(self, db_path) -> None:
        s1 = Step("login", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Step("process", 2)
        s2.status = Status.IN_PROGRESS
        tx = make_transaction(steps=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.steps[0].status is Status.SUCCESSFUL
        assert loaded.steps[1].status is Status.IN_PROGRESS


class TestResumeScenario:
    def test_resume_skips_successful_steps(self, db_path) -> None:
        from rpacore.engine import Engine

        # Save a transaction where step "a" already succeeded and "b" is pending.
        s1 = Step("a", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Step("b", 2)
        s2.status = Status.PENDING

        tx = make_transaction(steps=[s1, s2])
        save_transaction(tx, db_path)

        # Reload and run with a concrete subclass wired to the same names.
        counts: dict[str, int] = {"a": 0, "b": 0}

        class TrackStep(Step):
            def execute(self, ctx: ProcessContext) -> None:
                counts[self.name] += 1

        loaded = load_transaction(tx.id, db_path)
        # Replace plain Step instances with executable ones, preserving status.
        for i, step in enumerate(loaded.steps):
            track = TrackStep(step.name, step.execution_order)
            track.status = step.status
            track.exceptions = step.exceptions
            loaded.steps[i] = track

        from rpacore.engine import Engine
        Engine().run(ProcessContext(transaction=loaded))

        assert counts["a"] == 0  # already successful, engine skips it
        assert counts["b"] == 1
        assert loaded.status is Status.SUCCESSFUL


class TestTransactionQuery:
    def test_save_derives_utc_query_key_from_an_aware_timestamp(self, db_path) -> None:
        transaction = Transaction(
            id="offset-save",
            reference="offset-save",
            created_at=datetime(
                2026,
                7,
                16,
                6,
                0,
                tzinfo=timezone(timedelta(hours=-3)),
            ),
        )

        save_transaction(transaction, db_path)

        conn = sqlite3.connect(db_path)
        try:
            stored = conn.execute(
                "SELECT created_at, created_at_utc FROM transactions WHERE id = ?",
                (transaction.id,),
            ).fetchone()
        finally:
            conn.close()
        assert stored == ("2026-07-16T09:00:00+00:00", "2026-07-16T09:00:00+00:00")
        page = query_transactions(db_path)
        assert page.transactions[0].created_at == datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)

    def test_page_is_complete_and_cursor_is_bound_to_its_filters(self, db_path) -> None:
        transactions = [
            Transaction(
                id="tx-1",
                reference="invoice",
                status=Status.SUCCESSFUL,
                created_at=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
                metadata={"run_id": "run-1"},
            ),
            Transaction(
                id="tx-2",
                reference="invoice",
                status=Status.SUCCESSFUL,
                created_at=datetime(2026, 7, 16, 11, 0, tzinfo=timezone.utc),
                metadata={"run_id": "run-1"},
            ),
            Transaction(
                id="tx-3",
                reference="other",
                status=Status.FAILED,
                created_at=datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
                metadata={"run_id": "run-1"},
            ),
            Transaction(
                id="tx-4",
                reference="invoice",
                status=Status.SUCCESSFUL,
                created_at=datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc),
                metadata={"run_id": "run-2"},
            ),
        ]
        for transaction in transactions:
            save_transaction(transaction, db_path)

        first_page = query_transactions(
            db_path,
            statuses=[Status.SUCCESSFUL],
            reference="invoice",
            metadata_filter={"run_id": "run-1"},
            limit=1,
        )

        assert isinstance(first_page, TransactionPage)
        assert first_page.format_version == 1
        assert first_page.has_more is True
        assert [summary.id for summary in first_page.transactions] == ["tx-2"]
        assert first_page.next_cursor is not None

        second_page = query_transactions(
            db_path,
            statuses=[Status.SUCCESSFUL],
            reference="invoice",
            metadata_filter={"run_id": "run-1"},
            cursor=first_page.next_cursor,
            limit=1,
        )

        assert second_page.has_more is False
        assert second_page.next_cursor is None
        assert [summary.id for summary in second_page.transactions] == ["tx-1"]
        with pytest.raises(ValueError, match="does not match"):
            query_transactions(
                db_path,
                statuses=[Status.SUCCESSFUL],
                reference="other",
                metadata_filter={"run_id": "run-1"},
                cursor=first_page.next_cursor,
            )

    def test_query_normalizes_new_and_legacy_offset_timestamps(self, db_path) -> None:
        offset = timezone.utc
        transaction = Transaction(
            id="offset",
            reference="offset",
            created_at=datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc),
        )
        save_transaction(transaction, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE transactions SET created_at = ?, created_at_utc = ? WHERE id = ?",
                ("2026-07-16T06:00:00-03:00", "", transaction.id),
            )
            conn.execute(
                "UPDATE rpacore_schema_versions SET version = 6 WHERE component = 'transactions'"
            )
            conn.commit()
        finally:
            conn.close()

        page = query_transactions(
            db_path,
            since=datetime(2026, 7, 16, 9, 0, tzinfo=offset),
            readonly=False,
        )

        assert [summary.id for summary in page.transactions] == [transaction.id]
        assert page.transactions[0].created_at == datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)
        conn = sqlite3.connect(db_path)
        try:
            stored = conn.execute(
                "SELECT created_at_utc FROM transactions WHERE id = ?", (transaction.id,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert stored == "2026-07-16T09:00:00+00:00"

    def test_query_is_readonly_by_default_and_rejects_ambiguous_boundaries(self, db_path) -> None:
        transaction = make_transaction()
        save_transaction(transaction, db_path)
        before = Path(db_path).read_bytes()

        page = query_transactions(db_path)

        assert [summary.id for summary in page.transactions] == [transaction.id]
        assert Path(db_path).read_bytes() == before
        with pytest.raises(ValueError, match="timezone-aware"):
            query_transactions(db_path, since=datetime(2026, 7, 16, 9, 0))
        with pytest.raises(ValueError, match="valid transaction query cursor"):
            query_transactions(db_path, cursor="not-a-cursor")

    def test_query_rejects_invalid_limits_and_time_windows(self, db_path) -> None:
        for limit in (0, 1_001):
            with pytest.raises(ValueError, match="1 through 1000"):
                query_transactions(db_path, limit=limit)
        with pytest.raises(TypeError, match="1 through 1000"):
            query_transactions(db_path, limit=True)
        with pytest.raises(ValueError, match="earlier than or equal"):
            query_transactions(
                db_path,
                since=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
                until=datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc),
            )

    def test_query_supports_independent_reference_metadata_and_until_filters(self, db_path) -> None:
        older = Transaction(
            id="older",
            reference="invoice",
            created_at=datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc),
            metadata={"kind": "invoice", "run_id": "run-1"},
        )
        newer = Transaction(
            id="newer",
            reference="invoice",
            created_at=datetime(2026, 7, 16, 11, 0, tzinfo=timezone.utc),
            metadata={"kind": "invoice", "run_id": "run-2"},
        )
        other = Transaction(
            id="other",
            reference="other",
            created_at=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
            metadata={"kind": "other", "run_id": "run-1"},
        )
        for transaction in (older, newer, other):
            save_transaction(transaction, db_path)

        assert [summary.id for summary in query_transactions(db_path, reference="invoice").transactions] == [
            newer.id,
            older.id,
        ]
        assert [
            summary.id
            for summary in query_transactions(db_path, metadata_filter={"run_id": "run-1"}).transactions
        ] == [other.id, older.id]
        assert [
            summary.id
            for summary in query_transactions(
                db_path,
                metadata_filter={"kind": "invoice", "run_id": "run-1"},
            ).transactions
        ] == [older.id]
        assert [
            summary.id
            for summary in query_transactions(
                db_path,
                since=datetime(2026, 7, 16, 9, 30, tzinfo=timezone.utc),
                until=datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc),
            ).transactions
        ] == [other.id]
        empty = query_transactions(db_path, reference="missing")
        assert empty.transactions == ()
        assert empty.has_more is False
        assert empty.next_cursor is None

    @pytest.mark.parametrize("count", [10, 100, 1_000])
    def test_query_pages_are_complete_at_supported_scale(self, db_path, count: int) -> None:
        seed_transaction_query_rows(db_path, count)

        transaction_ids: list[str] = []
        cursor = None
        while True:
            page = query_transactions(
                db_path,
                metadata_filter={"run_id": "scale"},
                cursor=cursor,
                limit=37,
            )
            transaction_ids.extend(summary.id for summary in page.transactions)
            if not page.has_more:
                assert page.next_cursor is None
                break
            assert page.next_cursor is not None
            cursor = page.next_cursor

        assert transaction_ids == [f"scale-{index:04d}" for index in reversed(range(count))]

    def test_query_cursor_excludes_newer_inserts_and_can_include_later_inserts(self, db_path) -> None:
        initial = [
            Transaction(
                id=f"initial-{hour}",
                reference="cursor",
                created_at=datetime(2026, 7, 17, hour, 0, tzinfo=timezone.utc),
            )
            for hour in (8, 9, 10)
        ]
        for transaction in initial:
            save_transaction(transaction, db_path)

        first_page = query_transactions(db_path, limit=1)
        save_transaction(
            Transaction(
                id="newer",
                reference="cursor",
                created_at=datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc),
            ),
            db_path,
        )
        save_transaction(
            Transaction(
                id="later",
                reference="cursor",
                created_at=datetime(2026, 7, 17, 7, 0, tzinfo=timezone.utc),
            ),
            db_path,
        )

        later_page = query_transactions(db_path, cursor=first_page.next_cursor, limit=10)

        assert [summary.id for summary in first_page.transactions] == ["initial-10"]
        assert [summary.id for summary in later_page.transactions] == [
            "initial-9",
            "initial-8",
            "later",
        ]

    def test_query_plan_uses_status_time_and_metadata_indexes(self, db_path) -> None:
        base = seed_transaction_query_rows(db_path, 1_000)

        conn = sqlite3.connect(db_path)
        try:
            details = [
                row[3]
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT id, reference, status, retry_count, created_at_utc FROM transactions "
                    "JOIN transaction_metadata tm_0 ON tm_0.transaction_id = transactions.id "
                    "AND tm_0.key = ? AND tm_0.value_json = ? "
                    "WHERE status IN (?) AND created_at_utc != '' AND created_at_utc >= ? "
                    "ORDER BY created_at_utc DESC, id ASC LIMIT ?",
                    ("run_id", json.dumps("scale"), "successful", base.isoformat(), 101),
                ).fetchall()
            ]
        finally:
            conn.close()

        assert any("idx_transactions_status_created_at_utc_id" in detail for detail in details)
        assert any("idx_transaction_metadata_key_value_transaction" in detail for detail in details)

    def test_query_does_not_assign_a_utc_instant_to_naive_legacy_timestamps(self, db_path) -> None:
        transaction = Transaction(
            id="naive-legacy",
            reference="naive-legacy",
            created_at=datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc),
        )
        save_transaction(transaction, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE transactions SET created_at = ?, created_at_utc = ? WHERE id = ?",
                ("2026-07-16T09:00:00", "", transaction.id),
            )
            conn.execute(
                "UPDATE rpacore_schema_versions SET version = 6 WHERE component = 'transactions'"
            )
            conn.commit()
        finally:
            conn.close()

        all_rows = query_transactions(db_path, readonly=False)
        windowed_rows = query_transactions(
            db_path,
            since=datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc),
        )

        assert [(summary.id, summary.created_at) for summary in all_rows.transactions] == [
            (transaction.id, None)
        ]
        assert windowed_rows.transactions == ()

    def test_loaded_legacy_naive_timestamps_can_be_checkpointed_without_utc_coercion(
        self, db_path
    ) -> None:
        transaction = Transaction(
            id="legacy-resume",
            reference="legacy-resume",
            created_at=datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc),
            started_at=datetime(2026, 7, 16, 9, 1, tzinfo=timezone.utc),
        )
        save_transaction(transaction, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE transactions SET created_at = ?, started_at = ?, created_at_utc = ? WHERE id = ?",
                ("2026-07-16T09:00:00", "2026-07-16T09:01:00", "", transaction.id),
            )
            conn.execute(
                "UPDATE rpacore_schema_versions SET version = 6 WHERE component = 'transactions'"
            )
            conn.commit()
        finally:
            conn.close()

        loaded = load_transaction(transaction.id, db_path)
        save_transaction(loaded, db_path)

        conn = sqlite3.connect(db_path)
        try:
            stored = conn.execute(
                "SELECT created_at, started_at, created_at_utc FROM transactions WHERE id = ?",
                (transaction.id,),
            ).fetchone()
        finally:
            conn.close()
        assert stored == ("2026-07-16T09:00:00", "2026-07-16T09:01:00", "")

    def test_query_migration_adds_measured_indexes(self, db_path) -> None:
        save_transaction(make_transaction(), db_path)

        conn = sqlite3.connect(db_path)
        try:
            indexes = {
                row[1]
                for row in conn.execute("SELECT * FROM sqlite_master WHERE type = 'index'").fetchall()
            }
        finally:
            conn.close()

        assert {
            "idx_transactions_created_at_utc_id",
            "idx_transactions_status_created_at_utc_id",
            "idx_transaction_metadata_key_value_transaction",
        } <= indexes

    def test_query_migration_backfills_more_than_one_batch_with_irregular_text_ids(
        self, db_path
    ) -> None:
        save_transaction(make_transaction(), db_path)
        transaction_ids = ["!first", "2", "10", "a", "a-100", "a-2", "~last"]
        transaction_ids.extend(f"batch-{index}" for index in range(501))
        conn = sqlite3.connect(db_path)
        try:
            conn.executemany(
                "INSERT INTO transactions (id, reference, status, retry_count, created_at, created_at_utc) "
                "VALUES (?, ?, 'successful', 0, ?, '')",
                [
                    (
                        transaction_id,
                        "batch",
                        "2026-07-16T09:00:00+00:00",
                    )
                    for transaction_id in transaction_ids
                ],
            )
            conn.execute(
                "UPDATE rpacore_schema_versions SET version = 6 WHERE component = 'transactions'"
            )
            conn.commit()
        finally:
            conn.close()

        query_transactions(db_path, readonly=False)

        conn = sqlite3.connect(db_path)
        try:
            unresolved = conn.execute(
                "SELECT count(*) FROM transactions WHERE created_at_utc = ''"
            ).fetchone()[0]
        finally:
            conn.close()
        assert unresolved == 0

    def test_query_migration_rolls_back_ddl_and_schema_marker_on_failure(
        self, db_path, monkeypatch
    ) -> None:
        save_transaction(make_transaction(), db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP INDEX idx_transactions_created_at_utc_id")
            conn.execute("DROP INDEX idx_transactions_status_created_at_utc_id")
            conn.execute("DROP INDEX idx_transaction_metadata_key_value_transaction")
            conn.execute("ALTER TABLE transactions DROP COLUMN created_at_utc")
            conn.execute(
                "UPDATE rpacore_schema_versions SET version = 6 WHERE component = 'transactions'"
            )
            conn.commit()
        finally:
            conn.close()

        migrate_to_v7 = persistence_module._migrate_transactions_to_v7

        def fail_after_v7_migration(conn: sqlite3.Connection) -> None:
            migrate_to_v7(conn)
            raise RuntimeError("forced migration failure")

        monkeypatch.setattr(
            persistence_module,
            "_migrate_transactions_to_v7",
            fail_after_v7_migration,
        )

        with pytest.raises(RuntimeError, match="forced migration failure"):
            query_transactions(db_path, readonly=False)

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
            indexes = {
                row[1]
                for row in conn.execute("SELECT * FROM sqlite_master WHERE type = 'index'")
            }
            version = conn.execute(
                "SELECT version FROM rpacore_schema_versions WHERE component = 'transactions'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "created_at_utc" not in columns
        assert {
            "idx_transactions_created_at_utc_id",
            "idx_transactions_status_created_at_utc_id",
            "idx_transaction_metadata_key_value_transaction",
        }.isdisjoint(indexes)
        assert version == 6


class TestSchemaMigration:
    @pytest.mark.parametrize("source_version", range(1, 10))
    def test_every_supported_transaction_schema_migrates_to_v10(
        self, db_path, source_version: int
    ) -> None:
        transaction_id = create_versioned_db(db_path, source_version)

        loaded = load_transaction(transaction_id, db_path)

        conn = sqlite3.connect(db_path)
        try:
            version = conn.execute(
                "SELECT version FROM rpacore_schema_versions WHERE component = 'transactions'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert loaded.id == transaction_id
        assert version == 10
        assert "steps" in tables
        assert "skills" not in tables

    def test_v9_to_v10_migration_preserves_rows_and_translates_vocabulary(
        self, db_path
    ) -> None:
        occurred_at = datetime(2026, 7, 20, 10, 2, tzinfo=timezone.utc)
        step = Step("validate", 2, arguments={"invoice": 42})
        step.status = Status.FAILED
        step.exceptions.append(
            BusinessException(
                "missing field",
                action="validate",
                retry_number=2,
                occurred_at=occurred_at,
                screenshot_path="evidence/shot.png",
                halts_remaining_steps=True,
                code="tests.invoice.missing_field",
            )
        )
        transaction = Transaction(
            id="v9-complete-record",
            reference="invoice-42",
            definition_identity="tests.persistence/v9",
            status=Status.FAILED,
            retry_count=2,
            created_at=datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),
            started_at=datetime(2026, 7, 20, 10, 1, tzinfo=timezone.utc),
            finished_at=datetime(2026, 7, 20, 10, 3, tzinfo=timezone.utc),
            state={"invoice": 42},
            metadata={"customer": "acme"},
            artifacts=[
                Artifact(
                    id="artifact-42",
                    name="invoice",
                    path="output/invoice.pdf",
                    kind="pdf",
                    created_at=datetime(2026, 7, 20, 10, 2, tzinfo=timezone.utc),
                    metadata={"pages": 1},
                )
            ],
            steps=[step],
            outcome_category=OutcomeCategory.BUSINESS_FAILED,
            retry_disposition=RetryDisposition.NOT_REQUESTED,
            failure_code="tests.invoice.missing_field",
        )
        transaction.append_history(
            HistoryEvent.STEP_STARTED,
            status=Status.IN_PROGRESS,
            retry_number=2,
            step=step,
            timestamp=datetime(2026, 7, 20, 10, 1, tzinfo=timezone.utc),
        )
        transaction.append_history(
            HistoryEvent.STEP_FAILED,
            status=Status.FAILED,
            retry_number=2,
            step=step,
            timestamp=occurred_at,
        )
        save_transaction(transaction, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE transactions SET revision = 7, queue_item_id = ?, claim_token = ? "
                "WHERE id = ?",
                ("queue-item-42", "claim-token-42", transaction.id),
            )
            conn.commit()
        finally:
            conn.close()
        downgrade_current_db_to_v9(db_path)

        conn = sqlite3.connect(db_path)
        try:
            transaction_before = conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (transaction.id,)
            ).fetchone()
            step_before = conn.execute(
                "SELECT id, transaction_id, name, execution_order, status, arguments "
                "FROM skills"
            ).fetchone()
            exception_before = conn.execute(
                "SELECT id, skill_id, exception_type, message, action, retry_number, "
                "datetime_occurred, screenshot_path, stops_execution, code FROM exceptions"
            ).fetchone()
            history_before = conn.execute(
                "SELECT transaction_id, sequence, timestamp, event, status, retry_number, "
                "skill_name, skill_execution_order FROM transaction_history ORDER BY sequence"
            ).fetchall()
            metadata_before = conn.execute(
                "SELECT * FROM transaction_metadata ORDER BY key"
            ).fetchall()
            artifacts_before = conn.execute(
                "SELECT * FROM transaction_artifacts ORDER BY sequence"
            ).fetchall()
        finally:
            conn.close()

        loaded = load_transaction(transaction.id, db_path)

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute(
                "SELECT * FROM transactions WHERE id = ?", (transaction.id,)
            ).fetchone() == transaction_before
            assert conn.execute(
                "SELECT id, transaction_id, name, execution_order, status, arguments "
                "FROM steps"
            ).fetchone() == step_before
            assert conn.execute(
                "SELECT id, step_id, exception_type, message, action, retry_number, "
                "occurred_at, screenshot_path, halts_remaining_steps, code FROM exceptions"
            ).fetchone() == exception_before
            translated_history = [
                (*row[:3], row[3].replace("skill_", "step_"), *row[4:])
                for row in history_before
            ]
            assert conn.execute(
                "SELECT transaction_id, sequence, timestamp, event, status, retry_number, "
                "step_name, step_execution_order FROM transaction_history ORDER BY sequence"
            ).fetchall() == translated_history
            assert conn.execute(
                "SELECT * FROM transaction_metadata ORDER BY key"
            ).fetchall() == metadata_before
            assert conn.execute(
                "SELECT * FROM transaction_artifacts ORDER BY sequence"
            ).fetchall() == artifacts_before
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            version = conn.execute(
                "SELECT version FROM rpacore_schema_versions WHERE component = 'transactions'"
            ).fetchone()[0]
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            conn.close()

        assert "steps" in tables
        assert "skills" not in tables
        assert version == 10
        assert violations == []
        assert loaded.definition_identity == "tests.persistence/v9"
        assert loaded.steps[0].arguments == {"invoice": 42}
        assert loaded.steps[0].exceptions[0].occurred_at == occurred_at
        assert loaded.steps[0].exceptions[0].halts_remaining_steps is True
        assert loaded.steps[0].exceptions[0].code == "tests.invoice.missing_field"

    def test_v9_to_v10_migration_rolls_back_schema_rows_and_marker(
        self, db_path, monkeypatch
    ) -> None:
        transaction = make_transaction(steps=[Step("validate", 1)])
        save_transaction(transaction, db_path)
        downgrade_current_db_to_v9(db_path)

        conn = sqlite3.connect(db_path)
        try:
            schema_before = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            rows_before = conn.execute("SELECT * FROM skills").fetchall()
        finally:
            conn.close()

        record_version = persistence_module._record_component_schema_version

        def fail_before_v10_marker(
            conn: sqlite3.Connection, component: str, version: int
        ) -> None:
            if component == "transactions" and version == 10:
                raise RuntimeError("forced v10 migration failure")
            record_version(conn, component, version)

        monkeypatch.setattr(
            persistence_module,
            "_record_component_schema_version",
            fail_before_v10_marker,
        )

        with pytest.raises(RuntimeError, match="forced v10 migration failure"):
            load_transaction(transaction.id, db_path)

        conn = sqlite3.connect(db_path)
        try:
            schema_after = conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            rows_after = conn.execute("SELECT * FROM skills").fetchall()
            version = conn.execute(
                "SELECT version FROM rpacore_schema_versions WHERE component = 'transactions'"
            ).fetchone()[0]
            has_steps = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'steps'"
            ).fetchone()
        finally:
            conn.close()

        assert schema_after == schema_before
        assert rows_after == rows_before
        assert version == 9
        assert has_steps is None

    def test_migrated_interrupted_transaction_resumes_with_equivalent_steps(
        self, db_path
    ) -> None:
        executions: dict[str, int] = {}

        class TrackingStep(Step):
            def execute(self, ctx: ProcessContext) -> None:
                executions[self.name] = executions.get(self.name, 0) + 1

        completed = Step("completed", 1)
        completed.status = Status.SUCCESSFUL
        interrupted = Step("interrupted", 2)
        interrupted.status = Status.IN_PROGRESS
        transaction = make_transaction(
            status=Status.IN_PROGRESS,
            state={"completed": True},
            steps=[completed, interrupted],
        )
        save_transaction(transaction, db_path)
        downgrade_current_db_to_v9(db_path)

        resumed = resume_transaction(
            transaction.id,
            [
                TrackingStep("completed", 1),
                TrackingStep("interrupted", 2),
            ],
            definition_identity=transaction.definition_identity,
            db_path=db_path,
        )
        Engine().run(ProcessContext(transaction=resumed))

        assert executions == {"interrupted": 1}
        assert resumed.state == {"completed": True}
        assert resumed.status is Status.SUCCESSFUL
        assert [step.status for step in resumed.steps] == [
            Status.SUCCESSFUL,
            Status.SUCCESSFUL,
        ]

    def test_legacy_schema_loads_existing_transaction_step_and_exception(self, db_path) -> None:
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
        assert len(loaded.steps) == 1
        assert loaded.steps[0].name == "validate"
        assert loaded.steps[0].status is Status.FAILED
        assert loaded.steps[0].arguments == {"invoice": 42}
        assert len(loaded.steps[0].exceptions) == 1
        assert isinstance(loaded.steps[0].exceptions[0], BusinessException)
        assert str(loaded.steps[0].exceptions[0]) == "missing field"
        assert loaded.steps[0].exceptions[0].screenshot_path == "shot.png"
        assert loaded.steps[0].exceptions[0].halts_remaining_steps is False
        assert loaded.outcome_category is OutcomeCategory.UNKNOWN
        assert loaded.retry_disposition is RetryDisposition.UNKNOWN
        assert loaded.failure_code == ""
        assert loaded.steps[0].exceptions[0].code == ""

    def test_legacy_schema_gets_created_at_column(self, db_path) -> None:
        create_legacy_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
        finally:
            conn.close()
        assert "created_at" in columns

    def test_legacy_schema_gets_exception_halts_remaining_steps_column(self, db_path) -> None:
        create_legacy_db(db_path)

        list_transactions(db_path)

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(exceptions)")}
        finally:
            conn.close()
        assert "halts_remaining_steps" in columns

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

    def test_v7_schema_migrates_outcome_and_failure_code_defaults(self, db_path) -> None:
        transaction = make_transaction()
        save_transaction(transaction, db_path)

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("ALTER TABLE exceptions DROP COLUMN code")
            conn.execute("ALTER TABLE transactions DROP COLUMN outcome_category")
            conn.execute("ALTER TABLE transactions DROP COLUMN retry_disposition")
            conn.execute("ALTER TABLE transactions DROP COLUMN failure_code")
            conn.execute(
                "UPDATE rpacore_schema_versions SET version = 7 WHERE component = 'transactions'"
            )
            conn.commit()
        finally:
            conn.close()

        loaded = load_transaction(transaction.id, db_path)

        assert loaded.outcome_category is OutcomeCategory.UNKNOWN
        assert loaded.retry_disposition is RetryDisposition.UNKNOWN
        assert loaded.failure_code == ""
        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
            exception_columns = {row[1] for row in conn.execute("PRAGMA table_info(exceptions)")}
        finally:
            conn.close()
        assert {"outcome_category", "retry_disposition", "failure_code"}.issubset(columns)
        assert "code" in exception_columns

    def test_v8_schema_migrates_unidentified_definition_identity(
        self,
        db_path,
    ) -> None:
        transaction = make_transaction()
        save_transaction(transaction, db_path)

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("ALTER TABLE transactions DROP COLUMN definition_identity")
            conn.execute(
                "UPDATE rpacore_schema_versions SET version = 8 "
                "WHERE component = 'transactions'"
            )
            conn.commit()
        finally:
            conn.close()

        loaded = load_transaction(transaction.id, db_path)

        assert loaded.definition_identity == ""
        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
            version = conn.execute(
                "SELECT version FROM rpacore_schema_versions "
                "WHERE component = 'transactions'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "definition_identity" in columns
        assert version == 10

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
        assert version == 10

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

        assert rows == [("queue", 4), ("transactions", 10)]

    def test_unsupported_transaction_schema_version_raises(self, db_path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE rpacore_schema_versions ("
                "component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT INTO rpacore_schema_versions (component, version) VALUES (?, ?)",
                ("transactions", 11),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(RuntimeError, match="Unsupported transaction schema version 11"):
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
