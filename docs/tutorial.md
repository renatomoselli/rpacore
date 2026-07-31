# RPA Core Tutorial

This guide walks through a small RPA Core project using PowerShell and the
published package workflow.

## Assumptions

- You have Python 3.11 or newer.
- You want a separate project that installs RPA Core like a user project.
- You are comfortable creating a few files by hand.

## What You Will Build

By the end, you will have a small automation project that:

- installs RPA Core into its own virtual environment
- defines one custom `Skill`
- runs a `Transaction`
- writes output to a file
- checkpoints the transaction to SQLite during execution
- inspects the saved transaction from the CLI

The final project structure will look like this:

```text
hello-rpacore/
  .venv/
  config.toml
  main.py
  skills/
    __init__.py
    greet_user.py
```

## Step 1: Check Python

```powershell
python --version
```

You should see Python 3.11 or newer.

## Step 2: Create The Project

```powershell
mkdir hello-rpacore
cd hello-rpacore
mkdir skills
New-Item -ItemType File skills\__init__.py | Out-Null
```

User automations live outside the framework package. This keeps application code
separate from `rpacore` itself.

## Step 3: Create A Virtual Environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, run this once in the current shell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Step 4: Install RPA Core

```powershell
pip install rpacore
python -c "import rpacore; print(rpacore.__version__)"
```

## Step 5: Create `config.toml`

```toml
max_retries = 1
retry_delay = 0.0
retry_backoff = 1.0
log_level = "INFO"
log_format = "text"
transaction_db_path = "rpacore.db"
screenshot_dir = ""
credential_provider = "env"
```

## Step 6: Create Your First Skill

Create `skills\greet_user.py`:

```python
from __future__ import annotations

from pathlib import Path

from rpacore import BusinessException, ProcessContext, Skill


class WriteGreeting(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        name = self.arguments.get("name")
        if not name or not str(name).strip():
            raise BusinessException(
                message="'name' is required",
                action="WriteGreeting",
            )

        output_path = Path(str(self.arguments.get("output_path", "greeting.txt")))
        output_path.write_text(f"Hello, {name}!\n", encoding="utf-8")
        ctx.state["greeting_path"] = str(output_path)
```

The skill reads its arguments, raises a business exception for missing input,
writes a file, and stores durable state for later inspection.

## Step 7: Create `main.py`

```python
from __future__ import annotations

from rpacore import (
    Engine,
    Transaction,
    configure_logger,
    execute_transaction,
    load_config,
)

from skills.greet_user import WriteGreeting


def main() -> None:
    config = load_config("config.toml")
    configure_logger(level=str(config["log_level"]), fmt=str(config["log_format"]))

    tx = Transaction(
        reference="greet-user",
        definition_identity="greet-user/v1",
    )
    tx.skills = [
        WriteGreeting(
            name="write_greeting",
            execution_order=1,
            arguments={
                "name": "Renato",
                "output_path": "greeting.txt",
            },
        )
    ]

    engine = Engine(
        max_retries=int(config["max_retries"]),
        retry_delay=float(config["retry_delay"]),
        retry_backoff=float(config["retry_backoff"]),
    )
    execute_transaction(
        tx,
        config=config,
        engine=engine,
        transaction_db_path=str(config["transaction_db_path"]),
    )

    print(f"Transaction {tx.id}: {tx.status}")


if __name__ == "__main__":
    main()
```

`execute_transaction()` makes the run crash-durable at engine state boundaries:
the transaction is saved before user skill code starts and after each status
transition. Its definition identity is an application-owned compatibility token,
not the RPA Core package version. Keep it stable across compatible code fixes;
change it when a new automation definition must not resume older in-progress
transactions.

## Step 8: Run The Project

```powershell
python .\main.py
Get-Content .\greeting.txt
```

Expected greeting:

```text
Hello, Renato!
```

The run also creates `rpacore.db`.

## Step 9: Inspect Transactions

```powershell
rpacore transaction list --db .\rpacore.db
rpacore transaction show --db .\rpacore.db <transaction_id> --json
rpacore transaction export --db .\rpacore.db --format json
rpacore transaction export --db .\rpacore.db --format ndjson
```

Use the transaction id printed by `python .\main.py` or listed by
`rpacore transaction list`. The JSON form of `transaction show` includes
`transaction.state.greeting_path`, which was written by the skill and persisted
by the checkpoint callback.

JSON and NDJSON output are intended for tools. Treat exports as sensitive
business data because they can include state, metadata, skill arguments, and
exception messages.

## Step 10: Add A CLI Manifest

Create `rpacore.toml`:

```toml
[project]
entrypoint = "main:main"

[storage]
transaction_db_path = "rpacore.db"
```

Now the project can run through the installed CLI:

```powershell
rpacore run
rpacore transaction list
```

## Next Steps

- Add a second skill with `execution_order=2`.
- Load an existing transaction with `load_transaction()`.
- Resume retryable failures with `resume_transaction()` and the exact persisted
  definition identity.
- Use `ctx.resources` for runtime-only objects that must not be persisted.
- Read [Durability and Storage](durability.md) for recovery details.
- Read [Testing Skills](testing.md) for plain pytest examples.
