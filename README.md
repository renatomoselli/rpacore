# OREF — Open Robotic Enterprise Framework

A deterministic, stateful RPA framework in Python, designed for code-first enterprise automation.

Requires Python 3.11+.

## What is OREF?

OREF is a pip-installable Python library for building reliable, auditable robotic process automations. Define your skills, wire them into a transaction, and the framework handles execution order, retry logic, persistence, structured logging, and notifications — all from the Python standard library.

- **Deterministic execution** — predictable behavior, no runtime magic
- **Stateful transactions** — every skill's status is tracked and persisted
- **Idempotent retries** — resume from failure, only re-execute what failed
- **Exception classification** — distinguish business rule violations from system errors
- **Structured logging** — human-readable or JSON output via stdlib logging
- **TOML configuration** — externalized settings with validated defaults
- **Queue processing** — multi-worker safe SQLite queue with atomic claiming
- **Notifications** — SMTP email and webhook dispatch after each transaction

## Installation

```bash
pip install oref
```

If you are contributing to OREF itself:

```bash
git clone https://github.com/renatomoselli/oref.git
cd oref
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Quick Start

```python
from oref import (
    Engine, Transaction, ProcessContext,
    load_config, configure_logger, save_transaction,
)
from my_skills import FetchRecord, ProcessRecord, WriteOutput

config = load_config("config.toml")
configure_logger(level=config["log_level"])

tx = Transaction(reference="my-automation")
tx.skills = [
    FetchRecord(name="fetch_record", execution_order=1, arguments={"record_id": "ABC-001"}),
    ProcessRecord(name="process_record", execution_order=2),
    WriteOutput(name="write_output", execution_order=3),
]

ctx = ProcessContext(transaction=tx, config=config)
Engine(max_retries=config["max_retries"]).run(ctx)
save_transaction(tx, db_path=config["db_path"])
```

## Writing a Skill

```python
from oref import Skill, ProcessContext, BusinessException, SystemException

class FetchRecord(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        record_id = self.arguments.get("record_id")
        if not record_id:
            raise BusinessException(message="record_id is required", action="FetchRecord")
        # ... fetch logic ...
```

## Project Structure

```text
oref/              # Framework core
  __init__.py      #   public API re-exports
  exceptions.py    #   BusinessException, SystemException
  status.py        #   Status enum
  skill.py         #   Skill base class
  transaction.py   #   Transaction model
  engine.py        #   Execution engine
  persistence.py   #   SQLite persistence
  logger.py        #   Logging helpers
  config.py        #   Configuration loader
  context.py       #   ProcessContext
  credentials.py   #   CredentialProvider, EnvCredentialProvider
  queue.py         #   SqliteQueue, QueueProvider
  runner.py        #   run_queue_loop
  report.py        #   generate_report, render_html, render_text
  notify.py        #   EmailNotifier, WebhookNotifier, dispatch
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  your entry point                                   │
│                                                     │
│  load_config()  ──▶  configure_logger()             │
│       │                                             │
│       ▼                                             │
│  Transaction                                        │
│    └── [Skill, Skill, Skill, ...]                   │
│            │                                        │
│            ▼                                        │
│        Engine.run()                                 │
│            │                                        │
│     ┌──────┴──────┐                                 │
│     │  execute()  │  ◀── per skill, in order        │
│     │  + status   │                                 │
│     │  + retry    │  ◀── SystemException only       │
│     └──────┬──────┘                                 │
│            │                                        │
│            ▼                                        │
│  save_transaction()  ──▶  SQLite (oref.db)          │
│  dispatch(notifiers, report)                        │
└─────────────────────────────────────────────────────┘
```

**Transaction lifecycle:**

```
PENDING ──▶ IN_PROGRESS ──▶ SUCCESSFUL
                       └──▶ FAILED
```

## Configuration

Create a `config.toml` in your project:

```toml
max_retries = 2
log_level   = "INFO"
db_path     = "oref.db"
screenshot_dir = ""
credential_provider = "env"

[queue]
db_path = "queue.db"
claim_timeout = 30
max_retries = 3

# [notification.email]
# host = "smtp.example.com"
# port = 587
# from_addr = "oref@example.com"
# to_addrs = ["admin@example.com"]

# [notification.webhook]
# url = "https://hooks.example.com/oref"
```

## Exception Model

| Exception | Meaning | Engine behavior |
|-----------|---------|-----------------|
| `BusinessException` | Expected rule violation (e.g. missing field) | Skill fails, execution continues |
| `SystemException` | Technical failure (e.g. network error) | Skill fails, execution stops, retryable |
| Any other exception | Unhandled — wrapped as `SystemException` | Same as SystemException |

## Optional Dependencies

```bash
pip install "oref[screenshots]"   # mss — auto-capture screenshots on exception
pip install "oref[keyring]"       # keyring — OS credential store integration
```

## Examples

This repo keeps a minimal in-repo automation under `examples/` to support integration-style tests for the framework itself:

- `examples/sample_skill.py`
- `examples/sample_main.py`

These are not the recommended place to look for user-facing examples. For fuller real-world automations, see [oref-examples](https://github.com/renatomoselli/oref-examples).

## License

Apache 2.0 — see [LICENSE](LICENSE)
