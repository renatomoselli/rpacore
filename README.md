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
- Execution engine — sequential execution with business/system exception semantics
- Retry logic — immediate retry of retryable system failures in the same run
- SQLite persistence — save/load transactions with crash recovery for interrupted runs
- Logging — structured stdlib logging with text or JSON output

**Planned:**
- Configuration — TOML-based settings
- Integration wiring in `main.py`
- Example skills and expanded documentation

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

```text
oref/          # Framework core
skills/        # Your automation skills go here
tests/         # Test suite
main.py        # Entry point
```

## How It Works

1. Define your automation as a **Transaction** containing a list of **Skills**.
2. Each Skill is a unit of work with clear input/output.
3. The **Engine** executes skills in order, tracking status.
4. On failure, retryable technical failures can be retried immediately.
5. Progress can be persisted so you can resume from where you left off.

## License

Apache 2.0 — see [LICENSE](LICENSE)
