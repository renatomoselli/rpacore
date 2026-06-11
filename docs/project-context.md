# RPA Core Project Context

This document holds the broader project context that was previously mixed into
`AGENTS.md`. It is useful for humans and AI tools that need more than the
minimal repo rules.

## Purpose

RPA Core is a Python-based, deterministic RPA framework inspired by enterprise RPA
patterns and adapted for modern, code-first automation.

The framework is not AI-driven at runtime. It is designed to be:

- deterministic
- stateful
- auditable
- resilient

AI tools may assist development, but they do not control execution.

## Tech Stack

- Python 3.11+
- SQLite via stdlib `sqlite3`
- pytest
- `tomllib` from the standard library
- No external runtime dependencies by default
- Optional extras: `mss` for screenshots, `keyring` for OS credential storage
- Runtime dependency decisions are recorded in `docs/runtime-dependencies.md`.

## Project Structure

```text
rpacore/              # Framework core
  __init__.py
  exceptions.py    # BusinessException, SystemException
  status.py        # Status enum
  skill.py         # Skill base class
  transaction.py   # Transaction model
  engine.py        # Execution engine
  persistence.py   # SQLite persistence
  logger.py        # Logging setup
  config.py        # Configuration loader
  context.py       # ProcessContext
  credentials.py   # CredentialProvider implementations
  queue.py         # SqliteQueue and QueueProvider
  runner.py        # Queue-driven execution loop
  report.py        # Transaction reporting and HTML/text rendering
  notify.py        # Email/webhook notifications
examples/          # Minimal repo-side sample automation for integration tests
tests/             # Test suite (mirrors rpacore/ structure)
pyproject.toml     # Packaging and dev tooling
config.toml        # Runtime configuration
README.md
AGENTS.md
docs/project-context.md
```

## Design Philosophy

- No abstraction before it is needed
- Incremental evolution over premature generalization
- Flat structures first; add nesting only when a real use case demands it
- Concrete classes over abstract hierarchies
- Stdlib over third-party when possible

## Core Principles

### Deterministic Execution

All automation must behave predictably. No runtime AI decision-making.

### Stateful Transactions

Each transaction carries:

- a list of skills
- status per skill
- exception tracking
- persistence

### Idempotency

The system must resume from failure and only re-execute failed steps.

### Separation of Concerns

- Framework core in `rpacore/` stays independent from user automation code
- Sample automation in `examples/` exists for repo-side integration coverage,
  not as part of the installed package
- Skills are modular and reusable
- Configuration is externalized in `config.toml`

### Enterprise-Oriented Design

- structured logging
- retry handling
- exception classification
- persistence
- optional queue processing
- optional notification dispatch

## Architecture Overview

```text
load_config/config.toml
  ->
Transaction + Skill list
  ->
ProcessContext
  ->
Engine
  ->
Status tracking + retry policy
  ->
Persistence (rpacore.db)
  ->
Reporting + optional notifications

Optional queue path:
SqliteQueue/QueueProvider
  ->
run_queue_loop()
  ->
build_transaction(item)
  ->
Engine / Persistence / Reporting / Notifications
```

## Skills

A skill is a unit of work.

Each skill should:

- have a clear input/output
- be idempotent where possible
- handle its own domain logic cleanly
- expose status through the framework lifecycle

Users create skills by subclassing `Skill` and implementing
`execute(ctx: ProcessContext)`.

The repo includes `examples/sample_skill.py` and `examples/sample_main.py` as
small integration fixtures for test coverage. User-facing, fuller automation
examples belong in the separate `rpacore-examples` repository.

## Exceptions

There are two exception categories:

- `BusinessException`: expected rule violation; does not stop execution by
  default
- `SystemException`: technical failure; stops execution by default

All exceptions should be logged and persisted.

## Coding Conventions

- modules, functions, variables: `snake_case`
- classes: `PascalCase`
- constants: `UPPER_CASE`
- tests: `test_<module>.py`
- public functions and attributes should have type hints

## Testing Expectations

- Tests live under `tests/`
- The test layout mirrors `rpacore/` where practical
- Run `pytest` from the project root
- Each commit should pass the full test suite
- Skill testing guidance lives in `docs/testing.md`; v0.1.0 uses normal pytest
  tests instead of a custom `SkillTestCase` base class.
- Core validation migration decisions live in `docs/core-validation-audit.md`.

## Development Strategy

Build in small increments.

Each commit should:

- introduce one clear concept
- be easy to understand
- be reversible if needed

For release-oriented work, validate not just tests but also packaging:

- build the wheel/sdist
- install the wheel into a clean virtualenv
- run `twine check` before release actions

Release gates use a reproducible installed-wheel smoke script:

```powershell
python scripts/validate_installed_wheel.py
```

The script builds a wheel, installs it into a clean virtual environment outside
the checkout, verifies import/version/CLI behavior, and can optionally run
selected `oref-examples` pytest paths via `--examples-pytest`.

## Vision

RPA Core aims to be a deterministic, stateful RPA framework in Python, designed for
AI-assisted development without AI-controlled execution.
