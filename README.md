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
Engine(max_retries=config["max_retries"]).run(ctx)
save_transaction(tx, db_path=config["db_path"])
```

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

        # Fetch and store data for later skills.
        ctx.data["record"] = {"id": record_id}
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

## Configuration

Create a `config.toml` in your project:

```toml
max_retries = 2
log_level = "INFO"
db_path = "rpacore.db"
screenshot_dir = ""
credential_provider = "env"

[queue]
db_path = "queue.db"
claim_timeout = 30
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

Phase 5 plans to add explicit business-stop behavior:

```python
raise BusinessException("bad row", action=self.name, stop=True)
```

That API is planned, not yet implemented in the current package.

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

## License

Apache 2.0. See [LICENSE](LICENSE).
