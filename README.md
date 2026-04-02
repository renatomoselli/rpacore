# OREF — Open Robotic Enterprise Framework

A deterministic, stateful RPA framework in Python, designed for code-first enterprise automation.

## What is OREF?

OREF is a Python framework for building reliable, auditable robotic process automations. It provides:

- **Deterministic execution** — predictable behavior, no magic
- **Stateful transactions** — track every skill's status, persist progress
- **Idempotent retries** — resume from failure, only re-execute what failed
- **Exception classification** — distinguish business rule violations from system errors

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
config.toml    # Configuration (created later)
```

## How It Works

1. Define your automation as a **Transaction** containing a list of **Skills**
2. Each Skill is a unit of work with clear input/output
3. The **Engine** executes skills in order, tracking status
4. On failure, only failed skills are retried
5. Everything is persisted so you can resume from where you left off

## License

Apache 2.0 — see [LICENSE](LICENSE)
