# OREF — Open Robotic Enterprise Framework

A deterministic, stateful RPA framework in Python, designed for code-first enterprise automation.

## What is OREF?

OREF is a Python framework for building reliable, auditable robotic process automations. It provides:

- **Deterministic execution** — predictable behavior, no magic
- **Stateful transactions** — track every skill's status, persist progress
- **Idempotent retries** — resume from failure, only re-execute what failed
- **Exception classification** — distinguish business rule violations from system errors

## Current Status

**Implemented:**
- Exception model — `BusinessException` and `SystemException` with action tracking and UTC timestamps
- Status enum — `Status(StrEnum)` for execution lifecycle: pending, in_progress, successful, failed, skipped
- Skill model — base `Skill` class users subclass to define automation steps
- Transaction model — groups skills into an executable unit with ordering and failure tracking

**Planned:**
- Execution engine — sequential skill execution with status tracking
- Retry logic — automatic retry of failed skills
- SQLite persistence — save/load/resume transactions
- Configuration — TOML-based settings
- Logging — structured per-skill event logging

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/oref.git
cd oref
python -m venv .venv
.venv/Scripts/activate  # Windows
pip install -e ".[dev]"
pytest
```

## Project Structure

```
oref/          # Framework core
skills/        # Your automation skills go here
tests/         # Test suite
main.py        # Entry point
```

## How It Works

1. Define your automation as a **Transaction** containing a list of **Skills**
2. Each Skill is a unit of work with clear input/output
3. The Engine will execute skills in order, tracking status *(planned)*
4. On failure, only failed skills will be retried *(planned)*
5. Progress will be persisted so you can resume from where you left off *(planned)*

## License

Apache 2.0 — see [LICENSE](LICENSE)
