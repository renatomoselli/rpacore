# OREF — Open Robotic Enterprise Framework

A deterministic, stateful RPA framework in Python, designed for code-first enterprise automation.

## What is OREF?

OREF is a Python template for building reliable, auditable robotic process automations. Clone it, replace the example skills with your own, and you have a production-ready automation with structured logging, SQLite persistence, retry logic, and configuration management — all from the Python standard library.

- **Deterministic execution** — predictable behavior, no runtime magic
- **Stateful transactions** — every skill's status is tracked and persisted
- **Idempotent retries** — resume from failure, only re-execute what failed
- **Exception classification** — distinguish business rule violations from system errors
- **Structured logging** — human-readable or JSON output via stdlib logging
- **TOML configuration** — externalized settings with validated defaults

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/oref.git
cd oref
python -m venv .venv
.venv\Scripts\activate     # Windows
# source .venv/bin/activate  # Linux / macOS
pip install -e ".[dev]"
pytest
python main.py
```

## Project Structure

```text
oref/              # Framework core — do not edit
  __init__.py      #   public API re-exports
  exceptions.py    #   BusinessException, SystemException
  status.py        #   Status enum
  skill.py         #   Skill base class
  transaction.py   #   Transaction model
  engine.py        #   Execution engine
  persistence.py   #   SQLite persistence
  logger.py        #   Logging helpers
  config.py        #   Configuration loader
skills/            # Your automation skills go here
  __init__.py
  greet_user.py    #   Example: ValidateInput, WriteGreeting, ConfirmOutput
tests/             # Test suite (mirrors oref/ structure)
main.py            # Entry point — wire your skills here
config.toml        # Runtime configuration
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  main.py  (your wiring layer)                       │
│                                                     │
│  load_config()  ──▶  configure_logger()             │
│       │                                             │
│       ▼                                             │
│  Transaction                                        │
│    └── [Skill, Skill, Skill, ...]  (your skills/)   │
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
└─────────────────────────────────────────────────────┘
```

**Transaction lifecycle:**

```
PENDING ──▶ IN_PROGRESS ──▶ SUCCESSFUL
                       └──▶ FAILED
```

**Skill lifecycle (per skill, per run):**

```
PENDING ──▶ IN_PROGRESS ──▶ SUCCESSFUL
                       └──▶ FAILED  (BusinessException → engine continues)
                       └──▶ FAILED  (SystemException   → engine stops, retryable)
```

## Writing a Skill

Subclass `Skill` and implement `execute(context)`:

```python
from oref import Skill, BusinessException, SystemException

class FetchRecord(Skill):
    def execute(self, context: dict) -> None:
        record_id = self.arguments.get("record_id")
        if not record_id:
            raise BusinessException(
                message="record_id is required",
                action="FetchRecord",
            )
        # do the work; write results into context for downstream skills
        context["record"] = fetch_from_source(record_id)
```

Wire it in `main.py`:

```python
from skills.my_skills import FetchRecord, ProcessRecord, WriteOutput
from oref import Transaction, Engine, load_config, configure_logger, save_transaction

config = load_config("config.toml")
configure_logger(level=config["log_level"])

tx = Transaction(reference="my-automation")
tx.skills = [
    FetchRecord(name="fetch_record", execution_order=1, arguments={"record_id": "ABC-001"}),
    ProcessRecord(name="process_record", execution_order=2),
    WriteOutput(name="write_output", execution_order=3),
]

Engine(max_retries=config["max_retries"]).run(tx)
save_transaction(tx, db_path=config["db_path"])
```

## Configuration

Edit `config.toml` to override defaults:

```toml
max_retries = 2        # retry failed system skills up to this many times
log_level   = "INFO"   # DEBUG, INFO, WARNING, ERROR
db_path     = "oref.db"
```

## Exception Model

| Exception | Meaning | Engine behavior |
|-----------|---------|-----------------|
| `BusinessException` | Expected rule violation (e.g. missing field) | Skill fails, execution continues |
| `SystemException` | Technical failure (e.g. network error) | Skill fails, execution stops, retryable |
| Any other exception | Unhandled — wrapped as `SystemException` | Same as SystemException |

## License

Apache 2.0 — see [LICENSE](LICENSE)
