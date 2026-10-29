"""Operator selection, privacy, exit codes and buffered queue output."""

import json
import sqlite3

import pytest

from rpacore import QueueItem, SqliteQueue
from rpacore._sqlite import QUEUE_SCHEMA_VERSION
from rpacore.cli import main
import rpacore._queue_cli as handler


@pytest.fixture
def storage(tmp_path):
    path = tmp_path / "queue.db"
    queue = SqliteQueue({"db_path": str(path)})
    for name in ("opaque app ID", "eac938cf-390a-488f-8a57-4f6c408f30cd", "f" * 32):
        queue.add(QueueItem(id=name, reference="fixture", payload={"secret": "payload-secret"}))
    item = queue.next_item("worker")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE queue_attempts SET claim_token='token-secret'")
        conn.execute("INSERT INTO queue_poison_events (item_id,error_type,created_at) VALUES (?, 'JSONDecodeError', ?)", (item.id, item.created_at.isoformat()))
    return path


def test_empty_item_list_in_human_and_json_output(tmp_path, capsys):
    path = tmp_path / "empty.db"
    SqliteQueue({"db_path": str(path)})
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    for json_flag in ([], ["--json"]):
        assert main(["queue", "list", "--db", str(path), *json_flag]) == 0
        result = capsys.readouterr()
        assert not result.err
        if json_flag:
            assert json.loads(result.out) == {
                "schema_version": 2, "command": "queue:list", "limit": 100,
                "records": [], "has_more": False, "next_cursor": None,
            }
        else:
            assert "No queue items found." in result.out and "More records" not in result.out
        assert {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("command", [["list"], ["show", "opaque app ID"], ["attempts"], ["poison"]])
@pytest.mark.parametrize("as_json", [False, True])
def test_allowlisted_output_and_nonmutation(storage, capsys, command, as_json):
    before = {p: p.read_bytes() for p in storage.parent.iterdir() if p.is_file()}
    assert main(["queue", *command, "--db", str(storage), *(["--json"] if as_json else [])]) == 0
    result = capsys.readouterr()
    assert result.out and not result.err
    assert "payload-secret" not in result.out and "token-secret" not in result.out
    assert "claim_token" not in result.out and "payload" not in result.out
    assert {p: p.read_bytes() for p in storage.parent.iterdir() if p.is_file()} == before
    if as_json:
        data = json.loads(result.out)
        assert data["schema_version"] == 2 and data["command"] == f"queue:{command[0]}"
        records = [data["item"]] if command[0] == "show" else data["records"]
        assert records  # In particular, an omitted --status means all items.


def test_truncation_union_and_continuation(storage, capsys):
    assert main(["queue", "list", "--db", str(storage), "--limit", "1"]) == 0
    assert "More records" in capsys.readouterr().out
    first = ["queue", "list", "--db", str(storage), "--limit", "1", "--status", "pending", "--status", "pending", "--json"]
    assert main(first) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["has_more"] and len(page["records"]) == 1
    assert main(first + ["--cursor", page["next_cursor"]]) == 0
    second = json.loads(capsys.readouterr().out)
    assert not second["has_more"] and second["next_cursor"] is None
    assert second["records"][0]["id"] != page["records"][0]["id"]


def test_db_bypasses_config_and_manifest(storage, monkeypatch, capsys):
    monkeypatch.chdir(storage.parent)
    def forbidden(*args, **kwargs):
        pytest.fail("explicit --db must bypass all config and manifest loading")
    monkeypatch.setattr(handler, "load_config", forbidden)
    monkeypatch.setattr(handler, "load_project_manifest", forbidden)
    assert main(["queue", "list", "--db", storage.name, "--config", "absent.toml", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["records"]) == 3


def test_config_without_project_and_upward_nearest_manifest(storage, monkeypatch, capsys):
    root = storage.parent
    config = root / "selected.toml"
    config.write_text('[queue]\ndb_path="queue.db"\n', encoding="utf-8")
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert main(["queue", "list", "--config", "../selected.toml", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["records"]) == 3
    (root / "config.toml").write_bytes(config.read_bytes())
    (root / "rpacore.toml").write_text('[project]\nentrypoint="sentinel:main"\n[storage]\ntransaction_db_path="transactions.db"\n', encoding="utf-8")
    (root / "sentinel.py").write_text("raise AssertionError('entrypoint imported')\n", encoding="utf-8")
    import rpacore.credentials as credentials
    def forbidden(*args, **kwargs):
        pytest.fail("inspection must not read credentials")
    monkeypatch.setattr(credentials.EnvCredentialProvider, "get", forbidden)
    assert main(["queue", "list", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["records"]) == 3
    # The nearest malformed manifest must fail rather than fall through to its parent.
    (nested / "rpacore.toml").write_text('private-key="secret"\n', encoding="utf-8")
    assert main(["queue", "list"]) == 1
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["no-manifest", "missing-config", "bad-config", "unreadable-config", "no-queue", "missing-storage"])
def test_selection_failures_have_empty_stdout_and_bounded_errors(tmp_path, monkeypatch, capsys, mode):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.toml"
    config.write_text('[queue]\ndb_path="absent.db"\n', encoding="utf-8")
    args = ["queue", "list", "--json"]
    if mode != "no-manifest":
        args.extend(["--config", str(config)])
    if mode == "missing-config":
        config.unlink()
    elif mode == "bad-config":
        config.write_text('max_retries="config-secret"\n', encoding="utf-8")
    elif mode == "no-queue":
        config.write_text("", encoding="utf-8")
    elif mode == "unreadable-config":
        def denied(*args, **kwargs):
            raise PermissionError("config-secret")
        monkeypatch.setattr(handler, "load_config", denied)
    assert main(args) == 1
    result = capsys.readouterr()
    assert result.out == "" and result.err
    assert "config-secret" not in result.err
    assert not (tmp_path / "absent.db").exists()


@pytest.mark.parametrize("arguments", [["list", "--status", "invalid-secret"], ["list", "--limit", "1001"], ["show", "  "], ["attempts", " "], ["poison", ""]])
def test_invalid_usage_precedes_storage(arguments, monkeypatch, capsys):
    def forbidden(*args):
        pytest.fail("usage error must precede storage selection")
    monkeypatch.setattr(handler, "_queue_db_path", forbidden)
    assert main(["queue", *arguments]) == 2
    result = capsys.readouterr()
    assert not result.out and result.err and "invalid-secret" not in result.err


def test_missing_item_and_empty_event_queries(storage, capsys):
    assert main(["queue", "show", "absent", "--db", str(storage)]) == 1
    assert capsys.readouterr().out == ""
    for command in ("attempts", "poison"):
        assert main(["queue", command, "absent", "--db", str(storage), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["records"] == []
    assert main(["queue", "list", "--db", str(storage), "--cursor", "bad-secret"]) == 1
    result = capsys.readouterr()
    assert result.out == "" and result.err and "bad-secret" not in result.err


@pytest.mark.parametrize("mode", ["old", "future", "unknown", "wal", "locked", "malformed"])
def test_storage_failures_preserve_first_invocation_bytes(storage, capsys, mode):
    conn = sqlite3.connect(storage)
    try:
        if mode in ("old", "future"):
            version = QUEUE_SCHEMA_VERSION - 1 if mode == "old" else QUEUE_SCHEMA_VERSION + 1
            conn.execute("UPDATE rpacore_schema_versions SET version=?", (version,))
            conn.commit()
        elif mode == "unknown":
            conn.execute("INSERT INTO rpacore_schema_versions VALUES ('schema-secret', 1)")
            conn.commit()
        elif mode == "wal":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("UPDATE queue_items SET retry_count=1")
            conn.commit()
        elif mode == "locked":
            conn.execute("BEGIN EXCLUSIVE")
        else:
            conn.close()
            storage.write_bytes(b"malformed-storage-secret")
        before = {p: p.read_bytes() for p in storage.parent.iterdir() if p.is_file()}
        for command in ("list", "show", "attempts", "poison"):
            selector = ["opaque app ID"] if command == "show" else []
            assert main(["queue", command, *selector, "--db", str(storage), "--json"]) == 1
            result = capsys.readouterr()
            assert not result.out and result.err
            assert "schema-secret" not in result.err and "malformed-storage-secret" not in result.err
            assert {p: p.read_bytes() for p in storage.parent.iterdir() if p.is_file()} == before
    finally:
        conn.close()


@pytest.mark.parametrize("as_json", [False, True])
def test_later_decode_and_serialization_failures_are_buffered(storage, capsys, monkeypatch, as_json):
    args = ["queue", "list", "--db", str(storage), *(["--json"] if as_json else [])]
    with sqlite3.connect(storage) as conn:
        conn.execute("UPDATE queue_items SET payload='[\"payload-secret\"]' WHERE id=?", ("f" * 32,))
    before = storage.read_bytes()
    assert main(args) == 1
    result = capsys.readouterr()
    assert not result.out and result.err and "payload-secret" not in result.err
    assert storage.read_bytes() == before
    for command in ("attempts", "poison"):
        assert main(["queue", command, "--db", str(storage)]) == 0
        capsys.readouterr()
    with sqlite3.connect(storage) as conn:
        conn.execute("UPDATE queue_items SET payload='{}'")
    original = handler._item_projection
    count = 0
    def projection(item):
        nonlocal count
        count += 1
        if count == 2:
            raise ValueError("serialization-secret")
        return original(item)
    monkeypatch.setattr(handler, "_item_projection", projection)
    assert main(args) == 1
    result = capsys.readouterr()
    assert not result.out and result.err and "serialization-secret" not in result.err
