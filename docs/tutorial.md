# RPA Core Tutorial

This guide walks through creating a brand-new `rpacore-examples` style project on
Windows using PowerShell.

## Assumptions

- You are new to Python.
- The framework repo exists at `d:\repos\rpacore`.
- You want a separate project that installs the framework like a real user
  would.

## What You Will Build

By the end of this tutorial, you will have a small automation project that:

- installs RPA Core into its own virtual environment
- defines one custom `Skill`
- runs a `Transaction`
- writes output to a file
- saves the transaction to SQLite

The final project structure will look like this:

```text
rpacore-examples/
  .venv/
  config.toml
  main.py
  skills/
    __init__.py
    greet_user.py
```

## Step 1: Make Sure Python Is Installed

Open PowerShell and run:

```powershell
python --version
```

You should see Python 3.11 or newer.

If you get an error, install Python first and make sure `python` is available
in PowerShell.

## Step 2: Build a Fresh rpacore Wheel

From the framework repo, create a fresh installable package:

```powershell
cd d:\repos\rpacore
.venv\Scripts\python.exe -m build
```

This creates files in:

```text
D:\repos\rpacore\dist\
```

You will usually see two files:

- `rpacore-<version>.tar.gz`
- `rpacore-<version>-py3-none-any.whl`

For the examples project, use the `.whl` file.

## Step 3: Create the New Examples Project Folder

Create a separate project directory outside the framework repo:

```powershell
cd d:\repos
mkdir rpacore-examples
cd rpacore-examples
mkdir skills
New-Item -ItemType File skills\__init__.py | Out-Null
```

Why this matters:

- the framework stays in `d:\repos\rpacore`
- `rpacore-examples` becomes user code
- this avoids accidentally importing local source files from the framework
  checkout

## Step 4: Create a Virtual Environment

A virtual environment is an isolated Python installation just for this project.

Run:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

If activation works, your prompt usually changes and starts with `(.venv)`.

If PowerShell blocks activation, run this once in the current shell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Step 5: Install rpacore Into the Examples Project

Install the wheel you built earlier:

```powershell
pip install d:\repos\rpacore\dist\rpacore-0.1.0-py3-none-any.whl
```

If the filename changes in the future, replace it with the newest wheel in
`d:\repos\rpacore\dist`.

Check that installation worked:

```powershell
python -c "import rpacore; print(rpacore.__version__)"
```

You should see the installed version number, for example:

```text
0.1.0
```

## Step 6: Create `config.toml`

Create `config.toml` in the root of `rpacore-examples`:

```toml
max_retries = 1
retry_delay = 0.0
retry_backoff = 1.0
log_level = "INFO"
db_path = "rpacore.db"
screenshot_dir = ""
credential_provider = "env"
```

This is enough to get started.

## Step 7: Create Your First Skill

Create `skills\greet_user.py` with this content:

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
```

What this skill does:

- reads `name` from the skill arguments
- raises a `BusinessException` if the name is missing
- writes `Hello, <name>!` to a file

## Step 8: Create `main.py`

Create `main.py` in the root of `rpacore-examples`:

```python
from __future__ import annotations

from rpacore import (
    Engine,
    ProcessContext,
    Transaction,
    configure_logger,
    load_config,
    save_transaction,
)

from skills.greet_user import WriteGreeting


def main() -> None:
    config = load_config("config.toml")
    configure_logger(level=str(config["log_level"]))

    tx = Transaction(reference="greet-user")
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

    ctx = ProcessContext(transaction=tx, config=config)
    Engine(
        max_retries=int(config["max_retries"]),
        retry_delay=float(config["retry_delay"]),
        retry_backoff=float(config["retry_backoff"]),
    ).run(ctx)
    save_transaction(tx, db_path=str(config["db_path"]))

    print(f"Transaction {tx.id}: {tx.status}")


if __name__ == "__main__":
    main()
```

What this file does:

- loads configuration from `config.toml`
- configures the logger
- creates a `Transaction`
- adds your skill to the transaction
- runs the engine
- saves the finished transaction to SQLite

The engine validates transaction wiring before any skill runs. The transaction
reference and skill names must be non-empty, skill names must be unique, and
execution orders must be unique positive integers. This example saves
persistence after `Engine.run()` finishes; normal execution is not yet
crash-durable at each skill boundary.

## Step 9: Run the Example

From the `rpacore-examples` folder, run:

```powershell
python .\main.py
```

If everything works, you should see a final status printed to the console, and
these files should appear:

- `greeting.txt`
- `rpacore.db`

Check the greeting file:

```powershell
Get-Content .\greeting.txt
```

Expected output:

```text
Hello, Renato!
```

## Step 10: Understand What Happened

When you ran `main.py`, rpacore did this:

1. Loaded your config.
2. Created one transaction.
3. Ran the `WriteGreeting` skill.
4. Updated the transaction and skill statuses.
5. Saved the transaction to `rpacore.db`.

That is the basic rpacore workflow.

## Common Beginner Mistakes

### Running From The Wrong Folder

Run `python .\main.py` from the `rpacore-examples` root, not from inside
`skills\`.

Good:

```powershell
cd d:\repos\rpacore-examples
python .\main.py
```

Bad:

```powershell
cd d:\repos\rpacore-examples\skills
python ..\main.py
```

### Forgetting To Activate The Virtual Environment

If `import rpacore` fails, activate `.venv` first:

```powershell
.\.venv\Scripts\Activate.ps1
```

### Expecting A Wheel Install To Update Automatically

Installing from a wheel is a snapshot.

If you change code in `d:\repos\rpacore`, the examples project will not see those
changes until you rebuild and reinstall.

## How To Reinstall rpacore After Framework Changes

If you make new changes in the framework repo:

1. Rebuild the wheel.
2. Reinstall it in `rpacore-examples`.

Rebuild:

```powershell
cd d:\repos\rpacore
.venv\Scripts\python.exe -m build
```

Reinstall in the examples project:

```powershell
cd d:\repos\rpacore-examples
.\.venv\Scripts\Activate.ps1
pip install --force-reinstall d:\repos\rpacore\dist\rpacore-0.1.0-py3-none-any.whl
```

## When To Use Editable Install Instead

A wheel is the best choice when you want to test the real install experience.

An editable install is useful only if you are actively changing the framework
and the examples project at the same time.

Editable install command:

```powershell
pip uninstall -y rpacore
pip install -e d:\repos\rpacore
```

Use editable mode only while developing. Before release checks, switch back to
the wheel.

## Next Things To Try

After the basic example works, the next useful additions are:

1. Add a second skill.
2. Read a credential with `ctx.credentials.get(...)`.
3. Save and resume a failed transaction.
4. Use `SqliteQueue` and `run_queue_loop()`.
5. Add email or webhook notifications.

## Minimal Checklist

If you want the shortest working path, this is it:

```powershell
cd d:\repos\rpacore
.venv\Scripts\python.exe -m build

cd d:\repos
mkdir rpacore-examples
cd rpacore-examples
mkdir skills
New-Item -ItemType File skills\__init__.py | Out-Null
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install d:\repos\rpacore\dist\rpacore-0.1.0-py3-none-any.whl
```

Then create:

- `config.toml`
- `skills\greet_user.py`
- `main.py`

And run:

```powershell
python .\main.py
```
