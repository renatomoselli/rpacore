"""Confirmed queue administration, durable effects and acknowledgement boundaries."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from rpacore import QueueItem, QueueLeaseLostError, SqliteQueue
from rpacore._sqlite import QUEUE_SCHEMA_VERSION
from rpacore.cli import main
import rpacore._queue_admin_cli as handler
import rpacore._queue_cli as selection
from tests.cli_helpers import run_cli


ITEM_ID = " opaque app ID "
REASON = "  reason-secret  "


@pytest.fixture
def storage(tmp_path):
    path = tmp_path / "selected.db"
    queue = SqliteQueue({"db_path": str(path)})
    queue.add(QueueItem(id=ITEM_ID, reference="fixture", payload={"secret": "payload-secret"}, retry_count=2))
    return path, queue


def invoke(command, path, *extra):
    return main(["queue", command, ITEM_ID, "--db", str(path), "--reason", REASON, *extra])


def snapshot(root):
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}


@pytest.mark.parametrize("command", ["complete", "fail"])
def test_refusal_precedes_selection_and_construction(storage, monkeypatch, capsys, command):
    path, _ = storage
    before = snapshot(path.parent)
    def forbidden(*args, **kwargs):
        pytest.fail("unconfirmed command must not select or open storage")
    monkeypatch.setattr(handler, "_queue_db_path", forbidden)
    monkeypatch.setattr(handler, "SqliteQueue", forbidden)
    for target in (path, path.parent / "missing.db"):
        assert invoke(command, target, "--json") == 2
        result = capsys.readouterr()
        assert result.out == "" and "--confirm" in result.err
    assert snapshot(path.parent) == before


@pytest.mark.parametrize("command", ["complete", "fail"])
@pytest.mark.parametrize("arguments", [[], ["id"], ["--reason", "valid"], ["", "--reason", "valid"], ["id", "--reason", " "], [" ", "--reason", "valid"]])
def test_invalid_arguments_precede_storage(command, arguments, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid usage must not select or open storage")
    monkeypatch.setattr(handler, "_queue_db_path", forbidden)
    monkeypatch.setattr(handler, "SqliteQueue", forbidden)
    try:
        result = main(["queue", command, *arguments, "--confirm", "--json"])
    except SystemExit as exc:
        result = exc.code
    assert result == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err


@pytest.mark.parametrize("command", ["complete", "fail"])
@pytest.mark.parametrize("state", ["pending", "claimed", "delayed", "malformed-payload"])
@pytest.mark.parametrize("as_json", [False, True])
def test_override_preserves_data_and_audits_exact_transition(storage, capsys, command, state, as_json):
    path, queue = storage
    claim = queue.next_item("worker") if state == "claimed" else None
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE queue_items SET transaction_id='binding', available_at=?", (due,))
        if state == "malformed-payload":
            conn.execute("UPDATE queue_items SET payload='malformed-payload-secret'")
        original = conn.execute("SELECT payload FROM queue_items").fetchone()[0]
    assert invoke(command, path, "--confirm", *(["--json"] if as_json else [])) == 0
    result = capsys.readouterr()
    assert result.out and not result.err
    assert all(secret not in result.out for secret in ("reason-secret", "payload-secret", "malformed-payload-secret"))
    if claim:
        assert claim.claim_token not in result.out
    if as_json:
        assert json.loads(result.out) == {
            "schema_version": 2, "command": f"queue:{command}", "item_id": ITEM_ID, "applied": True,
        }
    else:
        assert command in result.out and ITEM_ID in result.out
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT status,retry_count,payload,transaction_id,claimed_by,claimed_at,claim_token,available_at FROM queue_items").fetchone()
    expected_status = "successful" if command == "complete" else "failed"
    assert row == (expected_status, 2 + (command == "fail"), original, "binding", "", None, "", None)
    event, = queue.list_admin_events(ITEM_ID)
    assert (event.action, event.reason, event.previous_status.value, event.new_status.value) == (
        f"force_{command}", REASON.strip(), "in_progress" if claim else "pending", expected_status,
    )
    attempts = queue.list_attempts(ITEM_ID)
    if claim:
        assert len(attempts) == 1 and attempts[0].outcome.value == "admin_override"
        assert attempts[0].finished_at is not None
        with pytest.raises(QueueLeaseLostError):
            queue.complete(ITEM_ID, claimed_by=claim.claimed_by, claim_token=claim.claim_token)
    else:
        assert attempts == []
    assert invoke(command, path, "--confirm") == 0
    capsys.readouterr()
    assert len(queue.list_admin_events(ITEM_ID)) == 2
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT retry_count FROM queue_items").fetchone()[0] == 2 + 2 * (command == "fail")


@pytest.mark.parametrize("command", ["complete", "fail"])
def test_db_bypasses_config_and_manifest(storage, monkeypatch, capsys, command):
    path, _ = storage
    monkeypatch.chdir(path.parent)
    def forbidden(*args, **kwargs):
        pytest.fail("explicit --db must bypass config and manifest")
    monkeypatch.setattr(selection, "load_config", forbidden)
    monkeypatch.setattr(selection, "load_project_manifest", forbidden)
    assert invoke(command, path.name, "--config", "absent.toml", "--confirm", "--json") == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True


@pytest.mark.parametrize("command", ["complete", "fail"])
def test_config_and_nearest_manifest_without_entrypoint_or_credentials(storage, monkeypatch, capsys, command):
    path, _ = storage
    root = path.parent
    (root / "config.toml").write_text('[queue]\ndb_path="selected.db"\n', encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    arguments = ["queue", command, ITEM_ID, "--reason", REASON, "--confirm", "--json"]
    assert main(arguments + ["--config", "../config.toml"]) == 0  # No project needed.
    capsys.readouterr()
    (root / "rpacore.toml").write_text('[project]\nentrypoint="admin_sentinel:main"\n[storage]\ntransaction_db_path="transactions.db"\n', encoding="utf-8")
    (root / "admin_sentinel.py").write_text("raise AssertionError('entrypoint imported')\n", encoding="utf-8")
    import rpacore.credentials as credentials
    import rpacore.manifest as manifest
    def forbidden(*args, **kwargs):
        pytest.fail("administration must not access credentials or resolve an entrypoint")
    monkeypatch.setattr(credentials.EnvCredentialProvider, "get", forbidden)
    monkeypatch.setattr(manifest, "resolve_project_entrypoint", forbidden)
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    (nested / "rpacore.toml").write_text('invalid="config-secret"\n', encoding="utf-8")
    assert main(arguments) == 1  # Do not fall through a nearer malformed manifest.
    output = capsys.readouterr()
    assert not output.out and "config-secret" not in output.err


@pytest.mark.parametrize("mode", ["absent-selection", "missing-config", "bad-config", "unreadable-config", "no-queue", "missing-db", "directory"])
def test_selection_failures_create_no_storage(tmp_path, monkeypatch, capsys, mode):
    monkeypatch.chdir(tmp_path)
    args = ["queue", "complete", "id", "--reason", REASON, "--confirm", "--json"]
    config = tmp_path / "config.toml"
    config.write_text('[queue]\ndb_path="absent.db"\n', encoding="utf-8")
    if mode == "missing-config":
        args += ["--config", "missing.toml"]
    elif mode in ("bad-config", "unreadable-config", "no-queue"):
        args += ["--config", str(config)]
        if mode == "bad-config":
            config.write_text('max_retries="config-secret"\n', encoding="utf-8")
        elif mode == "no-queue":
            config.write_text("", encoding="utf-8")
        else:
            def denied(*args, **kwargs):
                raise PermissionError("config-secret")
            monkeypatch.setattr(selection, "load_config", denied)
    elif mode in ("missing-db", "directory"):
        args += ["--db", "absent.db" if mode == "missing-db" else str(tmp_path)]
    before = snapshot(tmp_path)
    assert main(args) == 1
    result = capsys.readouterr()
    assert result.out == "" and result.err and "config-secret" not in result.err
    assert "Could not select queue storage" in result.err
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "queue.db").exists()
    assert not (tmp_path / "absent.db").exists()


@pytest.mark.parametrize("command", ["complete", "fail"])
@pytest.mark.parametrize("mode", ["missing-item", "malformed", "locked", "future", "unknown", "mutation-error"])
def test_storage_and_mutation_errors_have_no_success_output(storage, capsys, monkeypatch, command, mode):
    path, queue = storage
    connection = sqlite3.connect(path)
    try:
        if mode == "missing-item":
            with connection:
                connection.execute("DELETE FROM queue_items")
        elif mode == "future":
            with connection:
                connection.execute("UPDATE rpacore_schema_versions SET version=?", (QUEUE_SCHEMA_VERSION + 1,))
        elif mode == "unknown":
            with connection:
                connection.execute("INSERT INTO rpacore_schema_versions VALUES ('schema-secret',1)")
        elif mode == "locked":
            connection.execute("BEGIN EXCLUSIVE")
        elif mode == "malformed":
            connection.close()
            path.write_bytes(b"malformed-storage-secret")
        elif mode == "mutation-error":
            def denied(*args, **kwargs):
                raise RuntimeError("mutation-secret")
            monkeypatch.setattr(SqliteQueue, f"force_{command}", denied)
        before = snapshot(path.parent)
        assert invoke(command, path, "--confirm", "--json") == 1
        result = capsys.readouterr()
        assert not result.out and result.err
        assert ("Queue item not found" if mode == "missing-item" else "Could not apply queue override") in result.err
        assert all(secret not in result.err for secret in ("schema-secret", "malformed-storage-secret", "mutation-secret", "reason-secret"))
        assert snapshot(path.parent) == before
    finally:
        connection.close()


@pytest.mark.parametrize("missing_item", [False, True])
def test_confirmed_wal_conversion_survives_a_later_missing_item(storage, capsys, missing_item):
    path, queue = storage
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        if missing_item:
            with conn:
                conn.execute("DELETE FROM queue_items")
    assert invoke("complete", path, "--confirm", "--json") == int(missing_item)
    result = capsys.readouterr()
    assert bool(result.err) == missing_item and bool(result.out) != missing_item
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert len(queue.list_admin_events()) == int(not missing_item)


def test_confirmed_command_migrates_supported_old_storage(tmp_path, capsys):
    path = tmp_path / "legacy.db"
    # Published v1 shape; wrapper delegates forward migration to the public API.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE queue_items (id TEXT PRIMARY KEY, reference TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending', retry_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, claimed_by TEXT NOT NULL DEFAULT '', claimed_at TEXT)")
        conn.execute("CREATE TABLE rpacore_schema_versions (component TEXT PRIMARY KEY,version INTEGER NOT NULL)")
        conn.execute("INSERT INTO rpacore_schema_versions VALUES ('queue',1)")
        conn.execute("INSERT INTO queue_items(id,reference,created_at) VALUES (?, 'legacy', ?)", (ITEM_ID, datetime.now(timezone.utc).isoformat()))
    assert invoke("fail", path, "--confirm", "--json") == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version FROM rpacore_schema_versions WHERE component='queue'").fetchone()[0] == QUEUE_SCHEMA_VERSION
    assert SqliteQueue({"db_path": str(path)}).get_item(ITEM_ID).status.value == "failed"


@pytest.mark.parametrize("command", ["complete", "fail"])
def test_output_failure_keeps_one_committed_override(storage, monkeypatch, capsys, command):
    path, queue = storage
    def broken_output(*args, **kwargs):
        raise OSError("output-secret")
    monkeypatch.setattr(handler, "print", broken_output, raising=False)
    assert invoke(command, path, "--confirm", "--json") == 1
    result = capsys.readouterr()
    assert not result.out and "applied" in result.err and "before retrying" in result.err
    assert "output-secret" not in result.err
    assert len(queue.list_admin_events(ITEM_ID)) == 1
    assert queue.get_item(ITEM_ID).retry_count == 2 + (command == "fail")


def test_serialization_failure_precedes_storage(storage, monkeypatch, capsys):
    path, queue = storage
    before = snapshot(path.parent)
    def broken_json(*args, **kwargs):
        raise ValueError("serialization-secret")
    monkeypatch.setattr(handler.json, "dumps", broken_json)
    assert invoke("complete", path, "--confirm", "--json") == 1
    output = capsys.readouterr()
    assert not output.out and "serialization-secret" not in output.err
    assert snapshot(path.parent) == before
    assert queue.list_admin_events(ITEM_ID) == []


@pytest.mark.parametrize("command", ["complete", "fail"])
def test_success_does_not_query_item_or_audit_after_override(storage, monkeypatch, capsys, command):
    path, _ = storage
    def forbidden(*args, **kwargs):
        pytest.fail("an override acknowledgement must not depend on a later item/audit read")
    monkeypatch.setattr(SqliteQueue, "get_item", forbidden)
    monkeypatch.setattr(SqliteQueue, "list_admin_events", forbidden)
    monkeypatch.setattr(selection, "get_queue_item", forbidden)
    assert invoke(command, path, "--confirm", "--json") == 0
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out) == {
        "schema_version": 2, "command": f"queue:{command}", "item_id": ITEM_ID, "applied": True,
    }


@pytest.mark.parametrize("command", ["complete", "fail"])
@pytest.mark.parametrize("as_json", [False, True])
def test_control_characters_cannot_spoof_acknowledgement(storage, capsys, command, as_json):
    path, queue = storage
    item_id = "item\n\x1b[2Jspoofed target"
    queue.add(QueueItem(id=item_id, reference="control-character fixture", payload={}))
    assert main([
        "queue", command, item_id, "--db", str(path), "--reason", "verified",
        "--confirm", *(["--json"] if as_json else []),
    ]) == 0
    output = capsys.readouterr()
    assert not output.err and "\x1b" not in output.out
    assert output.out.count("\n") == 1
    if as_json:
        assert json.loads(output.out)["item_id"] == item_id
    else:
        assert repr(item_id) in output.out and command in output.out
    event, = queue.list_admin_events(item_id)
    assert event.item_id == item_id and event.action == f"force_{command}"


def test_usage_and_help_through_subprocess(tmp_path):
    result = run_cli("queue", "fail", "id", "--reason", "resolved", "--json", cwd=tmp_path)
    assert result.returncode == 2 and result.stdout == "" and "--confirm" in result.stderr
    assert snapshot(tmp_path) == {}
    help_result = run_cli("queue", "--help", cwd=tmp_path)
    assert help_result.returncode == 0 and "administrative overrides" in help_result.stdout
    assert "complete" in help_result.stdout and "fail" in help_result.stdout
    for command in ("complete", "fail"):
        result = run_cli("queue", command, "--help", cwd=tmp_path)
        assert result.returncode == 0 and not result.stderr
        help_text = " ".join(result.stdout.split())
        assert "--reason" in help_text and "--confirm" in help_text
        assert "writable storage initialization" in help_text
