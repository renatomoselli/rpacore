# OREF - Open Robotic Enterprise Framework

## Purpose

This project is a Python-based, deterministic RPA framework inspired by enterprise RPA patterns, redesigned for modern, code-first automation in Python.

The framework is NOT AI-driven at runtime. It is designed to be:

* deterministic
* stateful
* auditable
* resilient

AI tools are used ONLY to assist development, not to control execution.

---

## Tech Stack

* Python 3.11+
* SQLite (stdlib `sqlite3`) for persistence
* pytest for testing
* tomllib (stdlib 3.11+) for configuration
* No external runtime dependencies (stdlib only)

---

## Project Structure

```
oref/              # Framework core
  __init__.py
  exceptions.py    # BusinessException, SystemException
  status.py        # Status enum
  skill.py         # Skill base class
  transaction.py   # Transaction model
  engine.py        # Execution engine
  persistence.py   # SQLite persistence
  logger.py        # Logging setup
  config.py        # Configuration loader
skills/            # User-defined automation skills
  __init__.py
tests/             # Test suite (mirrors oref/ structure)
  __init__.py
  test_exceptions.py
  test_status.py
  test_skill.py
  test_transaction.py
  test_engine.py
  test_persistence.py
main.py            # Entry point
pyproject.toml     # Dev tooling config
config.toml        # Runtime configuration
README.md
LICENSE
AGENTS.md
```

---

## Design Philosophy

* No abstraction before it is needed
* Incremental evolution over premature generalization
* Flat structures first — add nesting only when demanded by a real use case
* Concrete classes over abstract hierarchies
* Stdlib over third-party when possible

---

## Core Principles

1. Deterministic Execution
   All automation must behave predictably. No runtime AI decision-making.

2. Stateful Transactions
   Each transaction contains:

* list of skills (actions)
* status per skill (pending, in_progress, successful, failed, skipped)
* exception tracking
* persistence

3. Idempotency
   The system must resume from failure and only re-execute failed steps.

4. Separation of Concerns

* Framework core (`oref/`) is independent of automation logic (`skills/`)
* Skills are modular and reusable
* Configs are externalized in `config.toml`

5. Enterprise-Grade Design

* logging
* retry mechanisms
* exception classification (business vs system)
* persistence layer

---

## Architecture Overview

```
Transaction
  ↓
Skill List (deterministic, pre-defined)
  ↓
Execution Engine
  ↓
Status Tracking + Persistence
  ↓
Retry Failed Skills Only
```

---

## Skills

A skill is a unit of work.

Each skill must:

* have a clear input/output
* be idempotent
* handle its own exceptions
* report status

Users create skills by subclassing `Skill` and implementing `execute(context)`.

---

## Exceptions

Two types:

* Business Exception → expected rule violation (does not stop execution by default)
* System Exception → technical failure (stops execution by default)

All exceptions must be logged and persisted.

---

## Naming Conventions

* Files and modules: `snake_case` (e.g., `execution_engine.py`)
* Functions and variables: `snake_case` (e.g., `run_transaction`)
* Classes: `PascalCase` (e.g., `BusinessException`)
* Constants: `UPPER_CASE` (e.g., `MAX_RETRIES`)
* Test files: `test_<module>.py` (e.g., `test_exceptions.py`)

---

## Type Hints

All public functions and class attributes must have type annotations.

```python
def run_transaction(self, transaction: Transaction) -> None: ...
```

---

## Testing

* Framework: pytest
* Test location: `tests/` directory, mirroring `oref/` module structure
* Naming: `test_<module>.py`, test functions prefixed with `test_`
* Run: `pytest` from project root
* Each commit must pass all tests

---

## AI Usage Guidelines

AI is NOT used in execution.

AI can be used for:

* generating code
* improving structure
* debugging

AI must NEVER:

* decide execution flow at runtime
* modify logic without explicit developer approval

---

## Coding Guidelines

* Keep functions small and readable
* Avoid magic behavior
* Prefer explicit over implicit
* Use clear naming
* Prioritize maintainability over cleverness
* No unnecessary abstractions — add complexity only when justified

---

## Development Strategy

Build in small increments.

Each commit should:

* introduce one clear concept
* be easy to understand
* be reversible if needed

---

## Vision

OREF aims to be:

"A deterministic, stateful RPA framework in Python, designed for AI-assisted development."

---

## Notes for AI Agents

When contributing:

* do not introduce unnecessary complexity
* do not add AI dependencies to runtime
* follow existing architecture strictly
* prioritize clarity and consistency
* prefer flat structures — do not add nesting or abstraction layers unless explicitly requested
* all new code must include type hints and corresponding tests
