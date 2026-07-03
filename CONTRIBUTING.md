# Contributing

Thanks for helping improve RPA Core. The project is a deterministic, local-first
Python automation framework, so changes should keep behavior explicit,
auditable, and easy to test.

## Development Setup

Use Python 3.11 or newer.

```bash
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install ".[dev]"
.venv\Scripts\python -m pytest
```

On POSIX shells, use `.venv/bin/python` instead of `.venv\Scripts\python`.

## Architecture Boundaries

- Framework code belongs in `rpacore/`.
- User automations belong in generated projects, examples, or a `skills/`
  package outside the framework core.
- RPA Core has no runtime AI behavior and no runtime AI dependency.
- Prefer standard library solutions for runtime behavior.
- Keep public APIs small, typed, and explicit.

## Change Requirements

- Behavior changes require focused tests.
- Public behavior changes require matching documentation.
- Persistence, queue, export, CLI, and public API changes need compatibility
  notes in `CHANGELOG.md` when user-visible.
- Generated-project changes must keep strict checkpoint persistence clear.
- Runtime dependency changes require a documented dependency decision.

## Validation

Before proposing a change, run the smallest relevant test first, then the full
suite when the change touches shared behavior.

Common checks:

```bash
python -m pytest
python scripts/verify_docs.py --repo-root .
python -m build
python scripts/validate_installed_wheel.py --repo-root . --wheel-dir dist
```

Release-candidate validation and external examples validation are maintainer
gates. They are documented in [docs/supply-chain.md](docs/supply-chain.md) and
kept outside the runtime package.

## Pull Requests

Use the pull request template. Include:

- the problem being solved
- tests and docs updated
- compatibility or migration impact
- persistence, checkpoint, queue, export, and CLI impact when relevant
- whether examples need a matching change

## Security

Do not report vulnerabilities in public issues with exploit details or sensitive
data. Follow [SECURITY.md](SECURITY.md).
