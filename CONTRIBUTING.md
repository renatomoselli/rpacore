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
- User automations belong in generated projects, examples, or a `steps/`
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
- Release-impacting public API, schema, export, CLI, dependency, or generated
  project changes should follow [Governance and Release Process](docs/governance.md).

## Runtime-Sensitive Tests

Some behavior depends on the runtime rather than our code: standard-library
exception details, recursion limits, the operating system, filesystem or SQLite
behavior, text encoding, and the exact Python version. When a change depends on
any of these, the author or reviewer should state whether the behavior is
guaranteed on every supported runtime, and give evidence for any guarantee they
assume.

When a unit test covers a fallback triggered by a particular exception type,
inject that exception exactly at the collaborator boundary by default. For
example, use `monkeypatch` to make the encoder raise `RecursionError`, then assert
the complete fallback outcome, not merely that some error occurred.

If a test uses a real trigger instead, such as pinning the recursion limit, it
needs evidence that it passes on Python 3.11 and Python 3.14; a green
candidate-branch run on a `ci/` branch is that evidence. In the incident behind
this rule, a pinned limit of 1000 and a 5000-deep list through `json.dumps`
raised `RecursionError` on Python 3.12 but not on Python 3.14, so the test
reached a different fallback — checking both supported edges would have caught
it.

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

## Candidate Branches For Full CI

Maintainers who push directly to `main` can run the complete `CI` matrix on a
commit before `main` moves. This routine is voluntary: nothing forces a
maintainer to use it. The `CI` workflow runs all required jobs on that exact
commit SHA before `main` moves, across every supported Python version and
operating system.

1. Push the candidate commit to a branch:
   ```bash
   git push origin HEAD:ci/<topic>
   ```
2. Check out the candidate (`git checkout ci/<topic>`), resolve the run for its
   SHA, then wait with `gh run watch` (or the Actions page) until the `CI` run
   for that SHA finishes:
   ```bash
   RUN_ID=$(gh run list --commit "$(git rev-parse HEAD)" --workflow CI \
     --limit 1 --json databaseId --jq '.[0].databaseId')
   gh run watch "$RUN_ID"
   ```
3. IF any job fails THEN do not push `main`. Fix the problem, push `ci/<topic>`
   again, and wait for a new green run before continuing.
4. On a fully green run, fast-forward `main` to the same SHA:
   ```bash
   git push origin ci/<topic>:main
   ```
5. Delete the candidate branch:
   ```bash
   git push origin --delete ci/<topic>
   ```

IF `main` moved after the candidate was pushed THEN Git refuses the push as
`non-fast-forward`. Rebase onto the updated `main`, delete the stale candidate
with `git push origin --delete ci/<topic>`, push the rebased commit with
`git push origin HEAD:ci/<topic>`, and wait for a new green run before trying
again. The full `CI` run then covers the exact SHA that reaches `main`.

## Security

Do not report vulnerabilities in public issues with exploit details or sensitive
data. Follow [SECURITY.md](SECURITY.md).
