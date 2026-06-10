# RPA Core

Deterministic, stateful RPA in Python for code-first enterprise automation.

Requires Python 3.11+.

## Package Name

The public project name is **RPA Core**. The package, import path, and CLI
command are `rpacore`.

## What Is RPA Core?

RPA Core is a pip-installable Python library for building reliable, auditable
robotic process automations. Define your skills, wire them into a transaction,
and the framework handles execution order, retry logic, persistence, logging,
queues, reports, credentials, and notifications.

The long-term product direction is:

> AI-assisted development, deterministic execution.

That means RPA Core should be friendly to humans and AI coding agents, but the
runtime remains deterministic. There are no runtime AI dependencies.

Core traits:

- **Deterministic execution**: predictable behavior, no hidden runtime magic.
- **Stateful transactions**: every skill's status is tracked and persisted.
- **Idempotent retries**: resume from failure and re-run only failed work.
- **Explicit exceptions**: business rule failures and system failures are
  classified separately.
- **Structured logging**: text or JSON output through stdlib logging.
- **TOML configuration**: externalized settings with simple defaults.
- **SQLite persistence**: local transaction history without a service.
- **Queue processing**: SQLite-backed queue with atomic item claiming.
- **Reports and notifications**: text/HTML reports, SMTP email, and webhooks.

## Installation

```bash
pip install rpacore
```

If you are contributing to the framework itself:

```bash
git clone https://github.com/renatomoselli/rpacore.git
cd rpacore
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

To validate the installed-wheel path used for release gates:

```bash
python scripts/validate_installed_wheel.py
```

## Quick Start

```python
from rpacore import (
    Engine,
    ProcessContext,
    Transaction,
    configure_logger,
    load_config,
    save_transaction,
)

from my_skills import FetchRecord, ProcessRecord, WriteOutput


config = load_config("config.toml")
configure_logger(level=config["log_level"])

tx = Transaction(reference="my-automation")
tx.skills = [
    FetchRecord(
        name="fetch_record",
        execution_order=1,
        arguments={"record_id": "ABC-001"},
    ),
    ProcessRecord(name="process_record", execution_order=2),
    WriteOutput(name="write_output", execution_order=3),
]

ctx = ProcessContext(transaction=tx, config=config)
Engine(
    max_retries=config["max_retries"],
    retry_delay=config["retry_delay"],
    retry_backoff=config["retry_backoff"],
).run(ctx)
save_transaction(tx, db_path=config["transaction_db_path"])
```

## CLI

Create a new project scaffold:

```bash
rpacore init my_project
cd my_project
rpacore run
```

`rpacore init <project_name>` creates a normal Python project with
`pyproject.toml`, `rpacore.toml`, `config.toml`, `main.py`, a `skills/` package,
a pytest skill test, and `.gitignore`.

`rpacore run` discovers `rpacore.toml` from the current directory, resolves the
declared `module:callable` entrypoint, and invokes it. The CLI does not build
skills, transactions, config, or persistence automatically; that wiring remains
in project Python code.

Exit behavior is stable across platforms:

- entrypoint returns `None`: exit `0`
- entrypoint returns an integer from `0` through `255`: propagate it
- entrypoint or framework execution raises: exit `1` with diagnostics on stderr
- CLI usage errors, invalid manifests, entrypoint-resolution errors, or invalid
  return values: exit `2`

`rpacore version` prints the installed framework version.

## Writing a Skill

```python
from rpacore import BusinessException, ProcessContext, Skill


class FetchRecord(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        record_id = self.arguments.get("record_id")
        if not record_id:
            raise BusinessException(
                message="record_id is required",
                action="FetchRecord",
            )

        # Fetch and store durable state for later skills.
        ctx.state["record"] = {"id": record_id}
```

## Project Structure

```text
rpacore/              # Framework core
  __init__.py      # Public API re-exports
  exceptions.py    # BusinessException, SystemException
  status.py        # Status enum
  skill.py         # Skill base class
  transaction.py   # Transaction model
  engine.py        # Execution engine
  persistence.py   # SQLite persistence
  logger.py        # Logging helpers
  config.py        # Configuration loader
  context.py       # ProcessContext
  credentials.py   # Credential providers
  queue.py         # SqliteQueue, QueueProvider
  runner.py        # run_queue_loop
  report.py        # Report generation and rendering
  notify.py        # Email and webhook notifications
```

User automations should live outside `rpacore/`, usually in their own repository
with a `skills/` package and a small `main.py` wiring layer.

## Execution Model

```text
main.py
  load_config()
  configure_logger()
  create Transaction
  attach ordered Skills
  create ProcessContext
  Engine.run(ctx)
  save_transaction()
  generate report / dispatch notifications
```

Transaction lifecycle:

```text
PENDING -> IN_PROGRESS -> SUCCESSFUL
                       -> FAILED
```

`Engine.run()` validates transaction wiring before any skill runs. Transaction
references and skill names must be non-empty, skill names must be unique within
the transaction, and skill execution orders must be unique positive integers.
Malformed wiring raises `ExecutionValidationError`, marks the transaction
`FAILED`, and should be treated as a permanent configuration or code issue, not
as a retryable runtime failure. The same validation applies to loaded
transactions before resume, so persisted malformed skill wiring must be fixed
rather than silently re-run.

Persistence is normally written by user wiring or the queue runner after
`Engine.run()` finishes. Loading a persisted transaction resets any
`IN_PROGRESS` transaction or skill to `FAILED`, but ordinary in-process
execution is not yet crash-durable at each successful skill boundary.

## Configuration

Create a `rpacore.toml` in your project to declare the Python entrypoint and
transaction storage:

```toml
[project]
entrypoint = "main:main"

[storage]
transaction_db_path = "rpacore.db"
```

`rpacore.toml` is intentionally small. Skill construction and transaction wiring
stay in Python; the manifest does not define pipelines or automatic skill
discovery. See `docs/project-manifest.md` for the full schema.

When both `rpacore.toml` and `config.toml` are present, the value passed to the
runner or storage layer by user wiring still decides which transaction database
is used. Queue settings, retry settings, credentials, screenshots, and
notifications remain `config.toml` settings; they are not part of the project
manifest schema.

Create a `config.toml` in your project:

```toml
max_retries = 2
retry_delay = 0.0
retry_backoff = 1.0
log_level = "INFO"
transaction_db_path = "rpacore.db"
screenshot_dir = ""
credential_provider = "env"

[queue]
db_path = "queue.db"
lease_timeout = 30
max_retries = 3

# [notification.email]
# host = "smtp.example.com"
# port = 587
# from_addr = "rpacore@example.com"
# to_addrs = ["admin@example.com"]

# [notification.webhook]
# url = "https://hooks.example.com/rpacore"
```

## Exception Model

| Exception | Meaning | Engine behavior |
|---|---|---|
| `BusinessException` | Expected rule violation, such as invalid input data. | Skill fails, execution continues. |
| `SystemException` | Technical failure, such as network or file errors. | Skill fails, execution stops, retryable. |
| Any other exception | Unhandled Python exception. | Wrapped as `SystemException`. |

Use `stop=True` for a business failure that should stop downstream work:

```python
raise BusinessException("bad row", action=self.name, stop=True)
```

## Timeouts and Deadlines

RPA Core does not provide a generic per-skill timeout. Python threads cannot be
safely stopped, so an in-process timeout can mark a skill failed while the timed
out code keeps running and mutating external systems.

Configure I/O timeouts in the library that performs the work, such as the HTTP,
SMTP, browser, database, or desktop automation client used by a skill. If an
automation needs a hard deadline with termination, run it behind an external
worker-process or orchestrator boundary and record the outcome back into RPA
Core. RPA Core v0.1.0 intentionally rejects Pebble or similar process-timeout
dependencies because process termination cannot make arbitrary external side
effects reversible.

## Optional Dependencies

```bash
pip install "rpacore[screenshots]"   # mss: auto-capture screenshots on exception
pip install "rpacore[keyring]"       # keyring: OS credential store integration
```

## Examples

This repo keeps a minimal in-repo automation under `examples/` to support
integration-style tests for the framework itself:

- `examples/sample_skill.py`
- `examples/sample_main.py`

For a step-by-step beginner guide, see [docs/tutorial.md](docs/tutorial.md).

For persistence, migrations, and crash-behavior details, see
[docs/durability.md](docs/durability.md).

For fuller showcase automations, see the examples repository:

- examples repository: `rpacore-examples`

## Local-First Direction

RPA Core Cloud is a future optional orchestrator/control plane. The framework
itself must stay useful without it:

- projects remain normal Python repos
- runs persist locally
- logs, reports, queues, transactions, and artifacts stay readable
- future worker/orchestrator contracts should be documented and exportable

See `book/notes/future-rpacore-cloud-thesis.md` for the private planning note.

## Compatibility Baseline

`v0.1.0` is the first supported public compatibility baseline. Breaking changes
made before that release are tracked for maintainers in
[docs/pre-v0.1.0-api-migration.md](docs/pre-v0.1.0-api-migration.md), not hidden
behind compatibility aliases.

## License

Apache 2.0. See [LICENSE](LICENSE).
