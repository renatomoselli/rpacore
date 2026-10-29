"""Validate RPA Core from an installed wheel outside the source checkout."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parents[1]
# These scripts must run directly from scripts/ before rpacore is installed.
if str(REPO_ROOT_FOR_IMPORTS) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_FOR_IMPORTS))

from rpacore._validation import (
    PRERELEASE_WHEEL_PATTERN,
    ValidationError,
    example_pytest_target as _example_pytest_target,
    validate_contained_path as _validate_contained_path,
)

_FROZEN_PUBLIC_EXPORTS = (
    "Artifact", "ArtifactReport", "BusinessException", "ConfigField",
    "CredentialNotFoundError", "CredentialProvider", "DefinitionIdentityError",
    "EmailNotifier", "Engine",
    "EnvCredentialProvider", "ExecutionTransition", "ExecutionValidationError",
    "HistoryEntry", "HistoryEvent",
    "KeyringCredentialProvider", "Notifier", "OutcomeCategory", "OutcomeReport",
    "ProcessContext", "ProjectManifest", "QueueAdminEvent", "QueueAttempt",
    "QueueAttemptOutcome", "QueueItem", "QueueLeaseLostError", "QueuePage",
    "QueuePoisonEvent",
    "QueueProvider", "QueueRunSummary", "QueueStatus", "ReportRecord",
    "RetryDisposition", "SqliteQueue", "Status", "Step", "StepReport",
    "SystemException", "TRANSACTION_FORMAT_VERSION", "Transaction", "TransactionFenceError",
    "TransactionPage", "TransactionReport", "TransactionSummary", "WebhookNotifier",
    "atomic_output_path", "bind_log_context", "build_credential_provider", "build_notifiers",
    "configure_logger", "dispatch", "execute_transaction", "find_project_manifest",
    "generate_report", "get_logger", "get_queue_item", "list_transactions", "load_config",
    "load_project_manifest", "load_transaction", "optional_config", "query_queue_attempts",
    "query_queue_items", "query_queue_poison_events", "query_transactions",
    "render_html", "render_json", "render_text", "require_config", "require_section",
    "resolve_config_path", "resolve_config_paths", "resolve_project_entrypoint",
    "resume_transaction", "run_queue_loop", "save_transaction", "serialize_transaction",
    "validate_config",
)


def _run(
    command: list[str],
    *,
    cwd: Path,
    allowed_roots: tuple[Path, ...],
) -> None:
    _validate_contained_path(cwd, allowed_roots=allowed_roots, label="command cwd")
    print(f"+ {' '.join(command)}")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    try:
        subprocess.run(command, cwd=cwd, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        raise ValidationError(
            f"command failed with exit code {exc.returncode}: {' '.join(command)}"
        ) from exc


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_script(venv_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def _latest_wheel(wheelhouse: Path) -> Path:
    wheels = sorted(
        wheelhouse.glob("rpacore-*.whl"),
        key=lambda path: (
            path.stat().st_mtime,
            not PRERELEASE_WHEEL_PATTERN.search(path.name),
            path.name,
        ),
        reverse=True,
    )
    if not wheels:
        raise FileNotFoundError(f"No rpacore wheel found in {wheelhouse}")
    return wheels[0]


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def _onerror(function, failed_path, exc_info) -> None:
        original_error = exc_info[1]
        try:
            os.chmod(failed_path, 0o700)
            function(failed_path)
        except OSError as exc:
            logging.warning(
                "Failed to clean %s after %s: %s",
                failed_path,
                type(original_error).__name__,
                exc,
                exc_info=True,
            )

    shutil.rmtree(path, onerror=_onerror)
    if path.exists():
        raise OSError(f"validation cleanup left directory behind: {path}")


def _smoke_code(repo_root: Path) -> str:
    return f"""
from pathlib import Path
from html.parser import HTMLParser
import io
import importlib
import importlib.metadata
import json
import platform
import sqlite3
import sys

import rpacore
from rpacore import (
    ConfigField,
    Engine,
    ExecutionTransition,
    bind_log_context,
    configure_logger,
    OutcomeCategory,
    OutcomeReport,
    ProcessContext,
    ReportRecord,
    RetryDisposition,
    Step,
    Status,
    SystemException,
    Transaction,
    generate_report,
    query_transactions,
    save_transaction,
    serialize_transaction,
    render_html,
    render_json,
    validate_config,
)

repo_root = Path({str(repo_root.resolve())!r})
checkout_package = repo_root / "rpacore"
module_path = Path(rpacore.__file__).resolve()
if module_path.parent == checkout_package:
    raise SystemExit(f"imported rpacore from checkout: {{module_path}}")
assert module_path == Path(importlib.metadata.distribution("rpacore").locate_file("rpacore/__init__.py")).resolve()
assert module_path.is_relative_to(Path(sys.prefix).resolve())
print(f"Installed Core: {{module_path}}; OS: {{platform.platform()}}")
if not module_path.with_name("py.typed").is_file():
    raise SystemExit(f"installed package missing py.typed: {{module_path}}")
expected_public_exports = {tuple(_FROZEN_PUBLIC_EXPORTS)!r}
if tuple(rpacore.__all__) != expected_public_exports:
    raise SystemExit(
        "installed package public exports differ from the frozen v0.3 contract: "
        f"{{rpacore.__all__!r}}"
    )
for retired_name in ("Skill", "SkillReport"):
    if hasattr(rpacore, retired_name):
        raise SystemExit(f"installed package exposes retired symbol: {{retired_name}}")
try:
    importlib.import_module("rpacore.skill")
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("installed package exposes retired module: rpacore.skill")

class OkStep(Step):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state["ran"] = True

tx = Transaction(
    reference="installed-wheel-smoke",
    definition_identity="installed-wheel-smoke/v1",
    steps=[OkStep("ok", 1)],
)
ctx = ProcessContext(transaction=tx)
transitions: list[ExecutionTransition] = []
Engine().run(
    ctx,
    transition_sink=transitions.append,
    transition_state_fields=("ran",),
)

assert tx.status is Status.SUCCESSFUL
assert tx.outcome_category is OutcomeCategory.SUCCESSFUL
assert tx.retry_disposition is RetryDisposition.NOT_APPLICABLE
assert tx.steps[0].status is Status.SUCCESSFUL
assert ctx.state == {{"ran": True}}
transaction_record = serialize_transaction(tx)
assert transaction_record["transaction_format_version"] == 3
assert "steps" in transaction_record
assert "skills" not in transaction_record
assert transitions
assert isinstance(transitions[-1], ExecutionTransition)
assert transitions[-1].to_record()["checkpoint_state"] == {{"ran": True}}
assert set(transitions[-1].to_record()) == {{
    "schema_version", "transition_id", "sequence", "occurred_at", "event",
    "transaction_id", "transaction_reference", "definition_identity",
    "transaction_status", "execution_pass", "step_name",
    "step_execution_order", "step_status", "outcome_category",
    "failure_code", "retry_recommended", "checkpoint_state",
}}
report = generate_report(tx)
assert isinstance(report.outcome, OutcomeReport)
assert isinstance(report.record, ReportRecord)
report_payload = json.loads(render_json(report))
assert report_payload["report_format_version"] == 2
assert report_payload["complete"] is True
assert report.outcome.category is OutcomeCategory.SUCCESSFUL
assert report.outcome.retry_disposition is RetryDisposition.NOT_APPLICABLE
default_report = generate_report(Transaction(reference="default-outcome-smoke"))
assert default_report.outcome.category is OutcomeCategory.UNKNOWN
assert default_report.outcome.retry_disposition is RetryDisposition.UNKNOWN
assert default_report.outcome.failure_code == ""

class ScreenshotLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.targets.extend(value for key, value in attrs if key == "href")

for path, target in (
    ("javascript:alert(1)", None),
    ("data:text/html,<script>x</script>", None),
    ("//host/shot.png", None),
    ("shot" + chr(0xDCFF) + ".png", None),
    ("err.png", "err.png"),
    ("shots/a #?%.png", "shots/a%20%23%3F%25.png"),
    ("C:/shots/err.png", "file:///C:/shots/err.png"),
):
    step = Step("screenshot-link-probe", 1)
    step.exceptions = [SystemException("synthetic", screenshot_path=path)]
    screenshot_report = generate_report(Transaction(reference="link-probe", steps=[step]))
    json_before = render_json(screenshot_report)
    links = ScreenshotLinks()
    links.feed(render_html(screenshot_report))
    assert links.targets == ([] if target is None else [target])
    assert render_json(screenshot_report) == json_before
print("Installed screenshot-link policy passed")

assert validate_config(
    {{}},
    (ConfigField("retry_count", int, required=False, default=0, min_value=0),),
) == {{"retry_count": 0}}
save_transaction(Transaction(id="query-smoke", reference="query-smoke"), "query-smoke.db")
query_page = query_transactions("query-smoke.db")
assert [summary.id for summary in query_page.transactions] == ["query-smoke"]
assert query_page.format_version == 1
log_stream = io.StringIO()
log = configure_logger(fmt="json", stream=log_stream)
with bind_log_context(transaction_id="installed-wheel-log"):
    log.info("Installed wheel log proof", extra={{"event": "wheel_log_proof"}})
log_payload = json.loads(log_stream.getvalue())
assert log_payload["log_format_version"] == 3
assert log_payload["event"] == "rpacore.wheel.log.proof"
assert log_payload["attributes"]["transaction_id"] == "installed-wheel-log"
print(rpacore.__version__)
print(f"Python {{platform.python_version()}}; SQLite {{sqlite3.sqlite_version}}")
"""


def _typing_consumer_code() -> str:
    return """\
from rpacore import (
    Engine, ExecutionTransition, ProcessContext, Transaction, optional_config, require_config,
    QueueAttempt, QueueItem, QueuePage, QueuePoisonEvent, get_queue_item,
    query_queue_items, query_queue_attempts, query_queue_poison_events,
)

config: dict[str, object] = {"retries": 2, "label": "ready"}
retries: int = require_config(config, "retries", int)
label: str = optional_config(config, "label", str, "")

transaction = Transaction(reference="typed-consumer")
context = ProcessContext(transaction=transaction, config=config)
config_retries: int = context.require_config("retries", int)
context.state["label"] = label
state_label: str = context.require_state("label", str)
state_retries: int = context.optional_state("retries", int, retries)
transitions: list[ExecutionTransition] = []
Engine().run(
    context,
    transition_sink=transitions.append,
    transition_state_fields=("label",),
)
transition_record: dict[str, object] = transitions[-1].to_record()

assert config_retries == state_retries
assert state_label == label
assert transition_record["schema_version"] == 2

def queue_types(path: str) -> None:
    item: QueueItem | None = get_queue_item("opaque id", path)
    items: QueuePage[QueueItem] = query_queue_items(path)
    attempts: QueuePage[QueueAttempt] = query_queue_attempts(path)
    poison: QueuePage[QueuePoisonEvent] = query_queue_poison_events(path)
    records: tuple[QueueItem, ...] = items.records
    assert item is None or isinstance(item, QueueItem)
    assert isinstance(records, tuple) and attempts.format_version == poison.format_version
"""


def _scaffold_smoke_code() -> str:
    tutorial = (Path(__file__).resolve().parents[1] / "docs" / "tutorial.md").read_text(encoding="utf-8")
    recipe_files = dict(re.findall(r"<!-- recovery-file: ([\w.]+) -->\n```python\n(.*?)\n```", tutorial, re.DOTALL))
    if set(recipe_files) != {"recovery.py", "inject_demo_failure.py", "restore_demo_step.py"}:
        raise ValidationError("Tutorial recovery recipe files are missing")
    return r'''
from pathlib import Path
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tomllib
import zipfile

# Provisioned tooling and --no-isolation avoid build dependency installation.
# PIP_NO_INDEX disables pip index lookups; it does not block arbitrary network I/O.
os.environ["PIP_NO_INDEX"] = "1"
root = Path.cwd()
cli = [sys.executable, "-m", "rpacore.cli"]
assert "PYTHONPATH" not in os.environ
print(f"Native scaffold proof: {platform.system()}; Python {platform.python_version()}; SQLite {sqlite3.sqlite_version}; inherited PYTHONPATH cleared")

def run(command, cwd):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, (command, result.stdout, result.stderr)
    return result

# The installed console script and top-level APIs inspect disposable queue storage.
from contextlib import closing
from rpacore import (
    QueueItem, QueuePage, SqliteQueue, get_queue_item, query_queue_items,
    query_queue_attempts, query_queue_poison_events,
)
queue_path = root / "inspection-queue.db"
queue = SqliteQueue({"db_path": str(queue_path)})
queue.add(QueueItem(id="opaque item", reference="queue-smoke", payload={"secret": "payload-secret"}))
queue.add(QueueItem(id="poison item", reference="queue-poison", payload={}))
with closing(sqlite3.connect(queue_path)) as connection, connection:
    connection.execute("UPDATE queue_items SET payload='not-json' WHERE id='poison item'")
claimed = queue.next_item("wheel-worker")
assert claimed is not None
assert queue.next_item("wheel-worker") is None  # quarantines the fixture's malformed row
with closing(sqlite3.connect(queue_path)) as connection, connection:
    connection.execute("UPDATE queue_items SET payload='{}' WHERE id='poison item'")
assert get_queue_item("opaque item", str(queue_path)).claim_token == claimed.claim_token
assert isinstance(query_queue_items(str(queue_path)), QueuePage)
assert len(query_queue_attempts(str(queue_path)).records) == 1
assert len(query_queue_poison_events(str(queue_path)).records) == 1
before_queue = {p: p.read_bytes() for p in root.glob("inspection-queue.db*")}
console = [str(Path(sys.executable).parent / ("rpacore.exe" if os.name == "nt" else "rpacore"))]
for command in ("list", "show", "attempts", "poison"):
    selector = ["opaque item"] if command == "show" else []
    for json_flag in ([], ["--json"]):
        result = run(console + ["queue", command, *selector, "--db", str(queue_path), *json_flag], root)
        assert result.stdout and result.stderr == "", (command, result)
        assert "payload-secret" not in result.stdout and claimed.claim_token not in result.stdout
        if json_flag:
            payload = json.loads(result.stdout)
            assert payload["schema_version"] == 2 and payload["command"] == "queue:" + command
            assert payload["item"] if command == "show" else payload["records"]
assert {p: p.read_bytes() for p in root.glob("inspection-queue.db*")} == before_queue
for arguments, expected_code in (
    (["show", "absent", "--db", str(queue_path)], 1),
    (["list", "--db", str(root / "missing-queue.db")], 1),
    (["list", "--limit", "1001"], 2),
    (["list", "--db", str(queue_path), "--cursor", "bad"], 1),
):
    result = subprocess.run(console + ["queue", *arguments, "--json"], cwd=root, capture_output=True, text=True)
    assert result.returncode == expected_code and result.stdout == "" and result.stderr, result
print("Installed read-only queue APIs and console commands passed")

# Separate drain-once processes, suitable for an external scheduler. No task is
# registered: the test proves durable timing and bound-transaction recovery.
worker = root / "delayed_worker.py"
worker.write_text("""
import json, sys
from pathlib import Path
from rpacore import (Engine, EnvCredentialProvider, QueueItem, SqliteQueue,
                     Step, SystemException, Transaction, run_queue_loop)
root = Path(__file__).parent
queue = SqliteQueue({'db_path': str(root / 'delayed.db'), 'retry_delay': 10})
class Effect(Step):
    def execute(self, ctx):
        if not ctx.state.get('retried'):
            ctx.state['retried'] = True
            raise SystemException('transient fixture failure')
        ctx.state['done'] = True
def build(item):
    return Transaction(item.reference, definition_identity='wheel-retry/v1',
                       steps=[Effect('effect', 1)])
if sys.argv[1] == 'first':
    queue.add(QueueItem(id='delayed', reference='retry', payload={}))
summary = run_queue_loop(queue, Engine(), build, {}, EnvCredentialProvider(),
                         transaction_db_path=str(root / 'delayed-transactions.db'))
print(json.dumps(vars(summary)))
""", encoding="utf-8")
first = json.loads(run([sys.executable, str(worker), "first"], root).stdout)
assert first["processed"] == first["retry_scheduled"] == 1
delayed = get_queue_item("delayed", str(root / "delayed.db"))
bound_id, due = delayed.transaction_id, delayed.available_at
early = json.loads(run([sys.executable, str(worker), "early"], root).stdout)
assert early["processed"] == 0
from datetime import datetime, timezone
import time
time.sleep(max(0, (due - datetime.now(timezone.utc)).total_seconds()) + .01)
later = json.loads(run([sys.executable, str(worker), "due"], root).stdout)
assert later["completed"] == 1
from rpacore import load_transaction
assert get_queue_item("delayed", str(root / "delayed.db")).transaction_id == bound_id
assert load_transaction(bound_id, str(root / "delayed-transactions.db")).state["done"] is True
print("Installed delayed drain-once restart retained the same transaction")

for name in (
    "my project", ".demo", "demo.", "_demo", "demo_", "-demo", "demo-",
    'my"project', "my'project", "d\u00e9m\u00f8", "\u212aelvin", "demo\n", "demo\t", "demo\x01",
):
    parent = root / "invalid-parent" / "nested"
    result = subprocess.run(cli + ["init", str(parent / name)], capture_output=True, text=True)
    assert result.returncode == 2, (name, result.returncode, result.stderr)
    assert result.stdout == "", (name, result.stdout)
    assert "project basename" in result.stderr and "my-project" in result.stderr
    assert not parent.parent.exists(), name

for target in ("", ".", "..", "missing/.."):
    before = set(root.iterdir())
    result = subprocess.run(cli + ["init", target], capture_output=True, text=True)
    assert result.returncode == 2 and result.stdout == "", (target, result)
    assert set(root.iterdir()) == before

inventory = {
    "pyproject.toml", "rpacore.toml", "config.toml", "main.py", ".gitignore",
    "steps/__init__.py", "steps/greeting.py", "tests/test_greeting_step.py",
}
for name in ("installed_project", "a", "1", "Demo.Project-1", "demo_project", "demo-project", "a..__--b"):
    project = root / "parent with spaces" / "nested" / name
    run(cli + ["init", str(project)], root)
    assert {p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()} == inventory
    assert tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"] == name
    wheelhouse = root / "project-wheels" / name
    run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(wheelhouse)], project)
    wheels = list(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1 and zipfile.is_zipfile(wheels[0]), wheels
    run([sys.executable, "-m", "pytest", "tests/test_greeting_step.py", "-q", "--basetemp", str(project / ".pytest-tmp")], project)
    run(cli + ["run"], project)
    assert (project / "greeting.txt").read_text(encoding="utf-8") == "Hello, Alice\n"
    transactions = json.loads(run(cli + ["transaction", "list", "--json"], project).stdout)["transactions"]
    assert len(transactions) == 1 and transactions[0]["transaction"]["status"] == "successful", transactions
    assert transactions[0]["transaction"]["definition_identity"] == "generated-greeting/v2"
    assert transactions[0]["transaction"]["steps"][0]["status"] == "successful"
    print(f"Installed scaffold build/tests/run/persisted success passed: {name}")

# Static guidance and real configured-provider injection under the installed wheel.
config_project = root / "configuration-journey"
run(cli + ["init", str(config_project)], root)
config_nested = config_project / "nested"
config_nested.mkdir()
(config_project / "config.toml").write_text(
    'max_retry=7\ntransaction_db_path="private-storage.db"\n'
    '[queue]\nlease_timout=300\n', encoding="utf-8",
)
before = {p: p.read_bytes() for p in config_project.rglob("*") if p.is_file()}
for cwd in (config_project, config_nested):
    payload = json.loads(run(cli + ["doctor", "--json"], cwd).stdout)
    assert payload["doctor_format_version"] == 1
    check = next(c for c in payload["checks"] if c["id"] == "project.config")
    assert check["status"] == "warning"
    assert check["details"] == {"advisories": [
        {"code": "possible_framework_key_typo", "suggested_key": "max_retries", "count": 1},
        {"code": "possible_framework_key_typo", "suggested_key": "queue.lease_timeout", "count": 1},
        {"code": "transaction_path_mismatch"},
    ]}
    human = run(cli + ["doctor"], cwd).stdout
    assert check["summary"] in human and "manifest" in human
    assert "max_retry" not in human and "private-storage" not in human
    assert {p: p.read_bytes() for p in config_project.rglob("*") if p.is_file()} == before
provider_probe = """
import runpy, sys
from pathlib import Path
import rpacore
project = Path(sys.argv[1])
expected = getattr(rpacore, sys.argv[2])
def forbidden(*args, **kwargs):
    raise AssertionError("Credentials must not be read")
rpacore.EnvCredentialProvider.get = forbidden
rpacore.KeyringCredentialProvider.get = forbidden
sys.modules["keyring"] = None
sys.path.insert(0, str(project))
from steps.greeting import WriteGreeting
original = WriteGreeting.execute
seen = []
def inspect(self, ctx):
    assert isinstance(ctx.credentials, expected)
    seen.append(type(ctx.credentials))
    original(self, ctx)
WriteGreeting.execute = inspect
application = runpy.run_path(str(project / "main.py"))
assert application["main"]() == 0
assert seen == [expected]
assert sys.modules["keyring"] is None
"""
for setting, class_name in (("env", "EnvCredentialProvider"), ("keyring", "KeyringCredentialProvider")):
    (config_project / "config.toml").write_text(f'credential_provider="{setting}"\n', encoding="utf-8")
    run([sys.executable, "-c", provider_probe, str(config_project), class_name], config_nested)
assert (config_project / "greeting.txt").read_bytes() == b"Hello, Alice\n"
print("Installed configuration advisories and env/keyring context injection passed without credential reads")

# Receipt publication under this exact installed Core wheel; source pytest owns
# the logging, failure-category, and initial/final checkpoint scenario matrix.
project = root / "parent with spaces" / "receipt-journey"
run(cli + ["init", str(project)], root)
before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
doctor = json.loads(run(cli + ["doctor", "--json"], project).stdout)
schema = next(c for c in doctor["checks"] if c["id"] == "transactions.schema")
assert schema["status"] == "not_applicable"
assert schema["details"] == {"reason": "missing_discovered_database"}
assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before
nested = project / "nested"
nested.mkdir()
result = run(cli + ["run"], nested)
assert result.stdout.strip() and ": successful" in result.stdout, result.stdout
transactions = json.loads(run(cli + ["transaction", "list", "--json"], nested).stdout)["transactions"]
saved = transactions[0]["transaction"]
destination = project / "greeting.txt"
assert saved["steps"][0]["arguments"]["output_path"] == str(destination)
assert saved["state"]["greeting_path"] == str(destination)
assert saved["artifacts"][0]["path"] == str(destination)
assert Path(saved["artifacts"][0]["path"]).read_bytes() == b"Hello, Alice\n"
assert not (nested / "greeting.txt").exists()

# Execute the public tutorial's exact application and temporary injection scripts.
replay_project = root / "parent with spaces" / "replay-journey"
run(cli + ["init", str(replay_project)], root)
recipe_files = __RECOVERY_RECIPE_FILES__
for name, source in recipe_files.items():
    (replay_project / name).write_text(source, encoding="utf-8")
run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(root / "recovery-wheels")], replay_project)
run([sys.executable, "-m", "pytest", "tests/test_greeting_step.py", "-q", "--basetemp", str(replay_project / ".pytest-tmp")], replay_project)
replay_db = replay_project / "rpacore.db"
doctor = json.loads(run(cli + ["doctor", "--json"], replay_project).stdout)
assert not replay_db.exists()
assert next(c for c in doctor["checks"] if c["id"] == "transactions.schema")["status"] == "not_applicable"
application = [sys.executable, str(replay_project / "recovery.py"), str(replay_project), str(replay_db)]
run(application, root)  # Unrelated success in the same database.
original_step = (replay_project / "steps" / "greeting.py").read_bytes()
run([sys.executable, str(replay_project / "inject_demo_failure.py")], root)
failed = subprocess.run(application, cwd=root, capture_output=True, text=True)
assert failed.returncode == 1 and ": failed" in failed.stdout, failed
selection = cli + ["transaction", "list", "--db", str(replay_db), "--run-id", "tutorial-run", "--status", "failed", "--json"]
payload = json.loads(run(selection, root).stdout)
assert payload["schema_version"] == 3 and not payload["has_more"]
assert len(payload["transactions"]) == 1
entry = payload["transactions"][0]
saved = entry["transaction"]
assert entry["outcome"]["category"] == "system_failed"
assert saved["definition_identity"] == "tutorial-greeting/v1"
assert [step["status"] for step in saved["steps"]] == ["successful", "failed"]
assert saved["state"] == {"preparation_count": 1} and saved["artifacts"] == []
assert saved["steps"][1]["exceptions"][-1]["message"] == "controlled-post-publication-failure"
replay_destination = replay_project / "greeting.txt"
published = replay_destination.read_bytes()
assert published == b"Hello, Alice\n"
run([sys.executable, str(replay_project / "restore_demo_step.py")], root)
assert (replay_project / "steps" / "greeting.py").read_bytes() == original_step
assert not (replay_project / "steps" / "greeting.py.backup").exists()
# A wrong identity cannot mutate a closed current database.
before_mismatch = replay_db.read_bytes()
mismatch = """
import sys
from pathlib import Path
from rpacore import DefinitionIdentityError, resume_transaction
project = Path(sys.argv[1])
sys.path.insert(0, str(project))
from recovery import steps
try:
    resume_transaction(sys.argv[2], steps(project, resuming=True), db_path=str(project / "rpacore.db"), definition_identity="incompatible/v1")
except DefinitionIdentityError:
    pass
else:
    raise AssertionError("identity mismatch accepted")
"""
run([sys.executable, "-c", mismatch, str(replay_project), saved["id"]], root)
assert replay_db.read_bytes() == before_mismatch
run(application + ["--resume", saved["id"]], root)
final_payload = json.loads(run(cli + ["transaction", "show", saved["id"], "--db", str(replay_db), "--json"], root).stdout)
final = final_payload["transaction"]
assert final["id"] == saved["id"] and final["status"] == "successful"
assert final_payload["outcome"]["category"] == "successful"
assert [step["status"] for step in final["steps"]] == ["successful", "successful"]
assert final["state"]["preparation_count"] == 1
assert final["steps"][1]["arguments"] == {"name": "Alice", "output_path": str(replay_destination)}
started = [event["step_name"] for event in final["history"] if event["event"] == "step_started"]
assert started.count("prepare_greeting") == 1 and started.count("write_greeting") == 2, started
assert any(event["event"] == "transaction_resumed" for event in final["history"])
assert final["state"]["greeting_path"] == str(replay_destination)
assert final["artifacts"][0]["path"] == str(replay_destination)
assert replay_destination.read_bytes() == published
assert not (replay_project / "wrong.txt").exists()
assert not list(replay_project.glob(".greeting.txt.*.tmp"))
report_path = replay_project / f"report-{saved['id']}.json"
report = json.loads(report_path.read_text(encoding="utf-8"))
assert report["report_format_version"] == 2 and report["transaction"]["id"] == saved["id"]
assert report["transaction"]["outcome"]["category"] == "successful"
for format in ("json", "ndjson"):
    result = run(cli + ["transaction", "export", "--db", str(replay_db), "--transaction-id", saved["id"], "--format", format], root)
    exported = json.loads(result.stdout)
    records = exported["transactions"] if format == "json" else [exported]
    assert len(records) == 1 and records[0]["id"] == saved["id"]
    assert exported["export_format_version"] == 2
    assert records[0]["transaction_format_version"] == 3
    if format == "json":
        assert records[0] == final
    else:
        assert {k: v for k, v in records[0].items() if k not in {"export_format_version", "framework_version", "exported_at"}} == final
all_records = json.loads(run(cli + ["transaction", "list", "--db", str(replay_db), "--run-id", "tutorial-run", "--json"], root).stdout)
assert len(all_records["transactions"]) == 2
before_noop = replay_db.read_bytes()
run(application + ["--resume", saved["id"]], root)
assert replay_db.read_bytes() == before_noop
assert replay_destination.read_bytes() == published
print(f"Installed tutorial journey passed; recovered ID: {saved['id']}; database: {replay_db}; output: {replay_destination}; report: {report_path}")

# Fail the final checkpoint after real SQLite writes and successful step work.
# This custom callback bypasses Core's transient-lock retry wrapper.
# Mirrors the final-boundary probe in tests/test_generated_project.py.
main_path = project / "main.py"
injection = (
    "from rpacore import save_transaction\n"
    "real_execute_transaction = execute_transaction\n"
    "def execute_transaction(transaction, **kwargs):\n"
    "    db_path = kwargs.pop('transaction_db_path')\n"
    "    def checkpoint(tx):\n"
    "        if tx.status is Status.SUCCESSFUL:\n"
    "            raise RuntimeError('controlled-final-checkpoint-failure')\n"
    "        save_transaction(tx, db_path=db_path)\n"
    "    real_execute_transaction(transaction, checkpoint=checkpoint, **kwargs)\n\n"
)
source = main_path.read_text(encoding="utf-8")
needle = "def main() -> int:"
assert needle in source
main_path.write_text(source.replace(needle, injection + needle), encoding="utf-8")
result = subprocess.run(cli + ["run"], cwd=project, capture_output=True, text=True)
assert result.returncode == 1 and result.stdout == "", result
assert "durable outcome is unconfirmed" in result.stderr, result.stderr
assert "Project execution failed: controlled-final-checkpoint-failure" in result.stderr
print("Installed receipt smoke passed: successful run and final-checkpoint abort")
print("Limitation: custom checkpoint injection bypasses transient SQLite retries")
'''.replace("__RECOVERY_RECIPE_FILES__", repr(recipe_files))


def validate_installed_wheel(
    *,
    repo_root: Path,
    work_dir: Path,
    wheel_dir: Path | None,
    examples_repo: Path | None,
    examples_pytest: list[str],
) -> None:
    generated_wheelhouse = work_dir / "wheelhouse"
    wheelhouse = wheel_dir if wheel_dir is not None else generated_wheelhouse
    venv_dir = work_dir / "venv"
    outside_dir = work_dir / "outside"
    generated_dirs = (venv_dir, outside_dir) if wheel_dir is not None else (generated_wheelhouse, venv_dir, outside_dir)

    # An attempted invocation must not retain an earlier passing safety report.
    for name in ("sqlite-runtime.json", "sqlite-runtime.xml"):
        (work_dir / "sqlite-runtime" / name).unlink(missing_ok=True)
    if examples_repo is not None and not examples_pytest:
        raise ValidationError("--examples-pytest is required when --examples-repo is provided")
    if examples_pytest and examples_repo is None:
        raise ValidationError("--examples-repo is required when --examples-pytest is used")
    allowed_run_roots = (repo_root, work_dir)

    try:
        wheelhouse.mkdir(parents=True, exist_ok=True)
        outside_dir.mkdir(parents=True, exist_ok=True)

        if wheel_dir is None:
            # Build the wheel from the freshly produced sdist. A direct wheel
            # build can reuse build/lib and accidentally package a source
            # module that was deleted during an API rename.
            _run(
                [sys.executable, "-m", "build", "--outdir", str(wheelhouse)],
                cwd=repo_root,
                allowed_roots=allowed_run_roots,
            )
        wheel = _latest_wheel(wheelhouse)
        print(f"Core wheel: {wheel.name}; SHA-256: {hashlib.sha256(wheel.read_bytes()).hexdigest()}")

        _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=outside_dir, allowed_roots=allowed_run_roots)
        python = _venv_python(venv_dir)
        _run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], cwd=outside_dir, allowed_roots=allowed_run_roots)
        _run([str(python), "-c", _smoke_code(repo_root)], cwd=outside_dir, allowed_roots=allowed_run_roots)
        _run(
            [str(python), "-m", "pip", "install", "mypy==2.1.0", "build>=1.0", "setuptools>=77.0.0", "wheel>=0.46.2", "pytest>=8.1.1"],
            cwd=outside_dir,
            allowed_roots=allowed_run_roots,
        )
        typing_consumer = outside_dir / "typing_consumer.py"
        typing_consumer.write_text(_typing_consumer_code(), encoding="utf-8")
        _run(
            [str(python), "-m", "mypy", "--no-incremental", typing_consumer.name],
            cwd=outside_dir,
            allowed_roots=allowed_run_roots,
        )
        rpacore_cli = _venv_script(venv_dir, "rpacore")
        _run([str(rpacore_cli), "version"], cwd=outside_dir, allowed_roots=allowed_run_roots)
        _run([str(python), "-c", _scaffold_smoke_code()], cwd=outside_dir, allowed_roots=allowed_run_roots)
        generated_project = outside_dir / "installed_project"
        if generated_project.exists():
            _remove_tree(generated_project)
        _run([str(rpacore_cli), "init", "installed_project"], cwd=outside_dir, allowed_roots=allowed_run_roots)
        _run(
            [
                str(python),
                "-c",
                "from pathlib import Path; "
                "assert Path('steps/greeting.py').is_file(); "
                "assert Path('tests/test_greeting_step.py').is_file(); "
                "assert not Path('skills').exists()",
            ],
            cwd=generated_project,
            allowed_roots=allowed_run_roots,
        )
        _run([str(rpacore_cli), "run"], cwd=generated_project, allowed_roots=allowed_run_roots)
        _run([str(rpacore_cli), "transaction", "list"], cwd=generated_project, allowed_roots=allowed_run_roots)
        _run([str(rpacore_cli), "transaction", "list", "--json"], cwd=generated_project, allowed_roots=allowed_run_roots)
        _run([str(rpacore_cli), "doctor", "--json"], cwd=generated_project, allowed_roots=allowed_run_roots)
        _run(
            [str(python), str(repo_root / "scripts" / "validate_sqlite_runtime.py"),
             "--repo-root", str(repo_root), "--wheel", str(wheel),
             "--output-dir", str(work_dir / "sqlite-runtime")],
            cwd=outside_dir,
            allowed_roots=allowed_run_roots,
        )

        if examples_pytest:
            _run([str(python), "-m", "pip", "install", "pytest"], cwd=outside_dir, allowed_roots=allowed_run_roots)
            for test_path in examples_pytest:
                target = _example_pytest_target(test_path, examples_root=examples_repo)
                _run(
                    [str(python), "-m", "pytest", target.pytest_path, "-q"],
                    cwd=target.project_dir,
                    allowed_roots=(examples_repo,),
                )
    except BaseException:
        # Keep generated-directory cleanup deterministic, then re-raise interrupts unchanged.
        cleanup_failures: list[Path] = []
        for generated_dir in generated_dirs:
            try:
                _remove_tree(generated_dir)
            except OSError:
                logging.warning("Failed to clean generated directory %s", generated_dir, exc_info=True)
                cleanup_failures.append(generated_dir)
        for generated_dir in generated_dirs:
            if generated_dir.exists() and generated_dir not in cleanup_failures:
                cleanup_failures.append(generated_dir)
        if cleanup_failures:
            logging.warning(
                "Installed-wheel cleanup left generated directories behind: %s",
                ", ".join(str(path) for path in cleanup_failures),
            )
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build RPA Core, install the wheel in a clean venv, and run smoke checks."
    )
    default_repo_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--repo-root", type=Path, default=default_repo_root)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument(
        "--wheel-dir",
        type=Path,
        default=None,
        help="Directory containing a prebuilt rpacore wheel. Defaults to building one into --work-dir.",
    )
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument("--examples-repo", type=Path, default=None)
    parser.add_argument(
        "--examples-pytest",
        action="append",
        default=[],
        help="Optional pytest path to run from --examples-repo using the installed-wheel venv.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    owns_work_dir = args.work_dir is None
    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="rpacore-installed-wheel-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        validate_installed_wheel(
            repo_root=args.repo_root.resolve(),
            work_dir=work_dir.resolve(),
            wheel_dir=args.wheel_dir.resolve() if args.wheel_dir else None,
            examples_repo=args.examples_repo.resolve() if args.examples_repo else None,
            examples_pytest=list(args.examples_pytest),
        )
    finally:
        if owns_work_dir and not args.keep_work_dir:
            try:
                _remove_tree(work_dir)
            except OSError:
                logging.warning("Failed to clean owned work directory %s", work_dir, exc_info=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
