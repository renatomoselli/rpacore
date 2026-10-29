"""Configuration guidance must stay static, nonfatal and privacy bounded."""

import json
import os
import sqlite3
import tomllib
from pathlib import Path

import pytest

from rpacore import load_config, save_transaction, Transaction
from tests.cli_helpers import run_cli


def project_files(tmp_path):
    project = tmp_path / "private-project"
    project.mkdir()
    (project / "rpacore.toml").write_text(
        '[project]\nentrypoint="main:main"\n[storage]\ntransaction_db_path="manifest.db"\n',
        encoding="utf-8",
    )
    (project / "main.py").write_text(
        'from pathlib import Path\nPath("IMPORTED").touch()\nraise RuntimeError("private-value")\n',
        encoding="utf-8",
    )
    return project


def snapshot(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def config_check(result):
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["doctor_format_version"] == 1
    checks = [c for c in payload["checks"] if c["id"] == "project.config"]
    assert len(checks) == 1
    return checks[0]


@pytest.mark.parametrize("source,target,count", [
    ('max_retry=7\n', "max_retries", 1),
    ('max_retrie=7\nmax_retriess=8\nmax_retries=2\n', "max_retries", 2),
    ('[queue]\nlease_timout=300\n', "queue.lease_timeout", 1),
    ('[notification.email]\nattach_screenshot=true\n', "notification.email.attach_screenshots", 1),
    ('[notification.webhook]\ninclude_transacton=true\n', "notification.webhook.include_transaction", 1),
    ('[notificaton]\nmax_retry=7\n', "notification", 1),
    ('[notifications]\nmax_retry=7\n', "notification", 1),
    ('[notification.emails]\nattach_screenshot=true\n', "notification.email", 1),
])
def test_typo_guidance_preserves_config_and_never_echoes_unknowns(tmp_path, source, target, count):
    project = project_files(tmp_path)
    path = project / "config.toml"
    path.write_text(source, encoding="utf-8")
    loaded = load_config(path)
    for key, value in tomllib.loads(source).items():
        assert loaded[key] == value
    before = snapshot(project)
    result = run_cli("doctor", "--json", cwd=project)
    assert result.returncode == 0
    check = config_check(result)
    assert check["status"] == "warning"
    assert check["details"] == {"advisories": [{
        "code": "possible_framework_key_typo", "suggested_key": target, "count": count,
    }]}
    assert target in check["summary"] and str(count) in check["summary"]
    human = run_cli("doctor", cwd=project)
    assert human.returncode == 0 and human.stderr == ""
    assert check["summary"] in human.stdout
    assert str(project) not in result.stdout + human.stdout
    assert snapshot(project) == before


def test_combined_order_counts_privacy_and_separate_failure(tmp_path):
    project = project_files(tmp_path)
    (project / "config.toml").write_text(
        'transaction_db_path="private-value.db"\nmax_retry=71\nmax_retrie=72\n'
        'private_key="private-value"\n[queue]\nlease_timout=300\n'
        '[notification.webhook]\ninclude_transacton=true\n', encoding="utf-8",
    )
    before = snapshot(project)
    expected = [
        {"code": "possible_framework_key_typo", "suggested_key": "max_retries", "count": 2},
        {"code": "possible_framework_key_typo", "suggested_key": "notification.webhook.include_transaction", "count": 1},
        {"code": "possible_framework_key_typo", "suggested_key": "queue.lease_timeout", "count": 1},
        {"code": "transaction_path_mismatch"},
    ]
    for flags, exit_code in [((), 0), (("--transaction-db", str(project / "missing.db")), 1)]:
        result = run_cli("doctor", *flags, "--json", cwd=project)
        assert result.returncode == exit_code
        check = config_check(result)
        assert check["details"] == {"advisories": expected}
        human = run_cli("doctor", *flags, cwd=project)
        assert human.returncode == exit_code and check["summary"] in human.stdout
        assert "manifest" in human.stdout and "library" in human.stdout
        for hidden in ("max_retry", "max_retrie=", "lease_timout", "include_transacton", "private_key", "private-value", str(project)):
            assert hidden not in result.stdout + human.stdout
    assert snapshot(project) == before


def test_known_and_custom_contexts_keep_clean_shape(tmp_path):
    project = project_files(tmp_path)
    (project / "config.toml").write_text(
        'max_retries=2\n[application]\nmax_retry=3\n[queue]\nlog_levl="app"\n'
        'lease_timeout=30\n[notification.email]\nhost="private-value"\n'
        '[notification.webhook]\nurl="https://private.invalid"\n', encoding="utf-8",
    )
    check = config_check(run_cli("doctor", "--json", cwd=project))
    assert check == {"id": "project.config", "status": "pass", "summary": "Configuration file is valid", "details": {}}


@pytest.mark.parametrize("kind", ["different", "equivalent", "absolute", "omitted", "case"])
def test_source_relative_paths_and_inspection_override(tmp_path, kind):
    if kind == "case" and os.name != "nt":
        pytest.skip("Native Windows case equivalence")
    project = project_files(tmp_path)
    nested = project / "nested"
    nested.mkdir()
    config_dir = tmp_path / "config-dir"
    config_dir.mkdir()
    config = config_dir / "settings.toml"
    target = project / "manifest.db"
    value = {"different": "other.db", "equivalent": "../private-project/./manifest.db",
             "absolute": target.as_posix(), "case": target.as_posix().upper(), "omitted": None}[kind]
    config.write_text("" if value is None else f'transaction_db_path="{value}"\n', encoding="utf-8")
    selected = project / "selected.db"
    save_transaction(Transaction(reference="static"), str(selected))
    before = snapshot(tmp_path)
    result = run_cli("doctor", "--config", str(config), "--transaction-db", str(selected), "--json", cwd=nested)
    assert result.returncode == 0
    check = config_check(result)
    assert check["details"] == ({"advisories": [{"code": "transaction_path_mismatch"}]} if kind == "different" else {})
    assert next(c for c in json.loads(result.stdout)["checks"] if c["id"] == "transactions.schema")["status"] == "pass"
    assert snapshot(tmp_path) == before


def test_config_without_valid_manifest_does_not_compare_paths(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('transaction_db_path="absent.db"\n', encoding="utf-8")
    before = snapshot(tmp_path)
    assert config_check(run_cli("doctor", "--config", str(config), "--json", cwd=tmp_path))["details"] == {}
    (tmp_path / "rpacore.toml").write_text('[project]\nentrypoint="invalid"\n', encoding="utf-8")
    result = run_cli("doctor", "--config", str(config), "--json", cwd=tmp_path)
    assert result.returncode == 1 and config_check(result)["details"] == {}
    assert snapshot(tmp_path) == {**before, Path("rpacore.toml"): (tmp_path / "rpacore.toml").read_bytes()}


def test_warning_preserves_live_wal_and_guards_runtime_access(tmp_path, monkeypatch):
    import rpacore.credentials as credentials
    import socket
    from rpacore.doctor import collect_doctor_result

    project = project_files(tmp_path)
    (project / "config.toml").write_text('max_retry=3\ncredential_provider="keyring"\n', encoding="utf-8")
    database = project / "manifest.db"
    save_transaction(Transaction(reference="static"), str(database))
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE advisory_probe(value)")
        connection.commit()
        assert Path(str(database) + "-wal").exists() and Path(str(database) + "-shm").exists()
        before = snapshot(project)
        def forbidden(*args, **kwargs):
            raise AssertionError("Doctor must not access runtime services")
        monkeypatch.setattr(credentials.EnvCredentialProvider, "get", forbidden)
        monkeypatch.setattr(credentials.KeyringCredentialProvider, "get", forbidden)
        monkeypatch.setattr(credentials, "build_credential_provider", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.chdir(project)
        result = collect_doctor_result()
        assert result.exit_code == 0
        assert next(c for c in result.checks if c.id == "project.config").status == "warning"
        assert next(c for c in result.checks if c.id == "transactions.journal").status == "warning"
        assert snapshot(project) == before
    finally:
        connection.close()


@pytest.mark.parametrize("failure", ["typo", "path"])
def test_advisory_defects_are_framework_failures_without_private_output(tmp_path, monkeypatch, capsys, failure):
    import rpacore._doctor_config as config_module
    import rpacore.cli as cli_module
    from rpacore.doctor import collect_doctor_result

    project = project_files(tmp_path)
    (project / "config.toml").write_text('transaction_db_path="different.db"\nmax_retry=3\n', encoding="utf-8")
    before = snapshot(project)
    monkeypatch.chdir(project)
    def defective(*args, **kwargs):
        raise RuntimeError("private-framework-marker")
    if failure == "typo":
        monkeypatch.setattr(config_module, "_typo_advisories", defective)
    else:
        real_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == project / "different.db":
                return defective()
            return real_resolve(path, *args, **kwargs)
        monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(RuntimeError, match="private-framework-marker"):
        collect_doctor_result()
    for args in (["doctor"], ["doctor", "--json"]):
        assert cli_module.main(args) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "rpacore: Could not complete doctor diagnostics\n"
    assert snapshot(project) == before


def test_direct_discovery_projects_combined_advisories_from_one_result(tmp_path):
    from rpacore._doctor_config import discover_config
    from rpacore import load_project_manifest

    project = project_files(tmp_path)
    path = project / "config.toml"
    path.write_text('transaction_db_path="different.db"\nmax_retry=3\nmax_retrie=4\n[queue]\nlease_timout=300\n', encoding="utf-8")
    config, checks = discover_config(config_path=str(path), manifest=load_project_manifest(project))
    assert config["max_retries"] == 0 and config["max_retry"] == 3
    assert len(checks) == 1
    check = checks[0]
    assert check.id == "project.config" and check.status == "warning"
    assert check.details == {"advisories": [
        {"code": "possible_framework_key_typo", "suggested_key": "max_retries", "count": 2},
        {"code": "possible_framework_key_typo", "suggested_key": "queue.lease_timeout", "count": 1},
        {"code": "transaction_path_mismatch"},
    ]}
    assert check.summary == (
        "Inspect 2 unrecognized key(s) near max_retries for a possible typo; "
        "Inspect 1 unrecognized key(s) near queue.lease_timeout for a possible typo; "
        "Explicit config transaction path differs from the manifest; generated/CLI-managed projects use "
        "manifest storage, while library applications choose their storage explicitly"
    )


@pytest.mark.parametrize("context,consumer", [
    ("notification.email", "EmailNotifier"), ("notification.webhook", "WebhookNotifier"),
])
def test_notification_advisory_keys_follow_real_consumer_access(context, consumer):
    import rpacore.notify as notify
    from rpacore._doctor_config import _CONTEXT_KEYS

    accessed = set()
    class Settings(dict):
        def get(self, key, default=None):
            accessed.add(key)
            return super().get(key, default)
    class NoCredentials:
        def get(self, name):
            raise AssertionError("Construction must not read credentials")
    section = Settings(host="smtp.invalid", from_addr="a@invalid", to_addrs=["b@invalid"], url="https://invalid")
    config = {"notification": {context.split(".")[-1]: section}}
    if consumer == "EmailNotifier":
        notify.EmailNotifier(config, NoCredentials())
    else:
        notify.WebhookNotifier(config)
    assert accessed == set(_CONTEXT_KEYS[context])


def test_queue_advisory_keys_follow_real_consumer_access(tmp_path, monkeypatch):
    import rpacore.queue as queue_module
    from rpacore._doctor_config import _CONTEXT_KEYS

    accessed = set()
    original = queue_module.optional_config
    def record(config, field, *args, **kwargs):
        accessed.add(field.removeprefix("queue."))
        return original(config, field, *args, **kwargs)
    monkeypatch.setattr(queue_module, "optional_config", record)

    class Settings(dict):
        def get(self, key, default=None):
            accessed.add(key)
            return super().get(key, default)

    queue_module.SqliteQueue(Settings(db_path=str(tmp_path / "queue.db")))
    assert accessed == set(_CONTEXT_KEYS["queue"])
    assert accessed == {"db_path", "lease_timeout", "max_retries", "retry_delay", "retry_backoff"}
