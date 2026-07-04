# Changelog

All notable user-facing changes are recorded here.

## v0.1.0 - Unreleased

RPA Core v0.1.0 is the first public compatibility baseline for local-first,
deterministic Python automation.

### Added

- `rpacore` package and `rpacore` CLI.
- `rpacore init` project scaffold with strict checkpoint persistence.
- `rpacore run` manifest-based entrypoint execution.
- Transaction inspection and export commands:
  - `rpacore transaction list`
  - `rpacore transaction show`
  - `rpacore transaction export --format json`
  - `rpacore transaction export --format ndjson`
- Public top-level API for skills, transactions, engine execution, persistence,
  recovery, queue processing, config, logging, reports, notifications,
  credentials, and project manifests.
- SQLite transaction persistence with explicit schema versions and migrations.
- SQLite queue provider with leases, reclaim behavior, retry accounting, and
  transaction binding.
- JSON-safe durable transaction state, metadata, history, and artifact records.
- Optional screenshot capture via the `screenshots` extra.
- Optional OS credential-store integration via the `keyring` extra.
- Repository settings documentation for release metadata, branch
  protection, required checks, and issue/security routes.
- Release rehearsal documentation for freeze inputs, evidence, examples
  validation, and go/no-go decisions.
- Release manifest preparation script for combining framework and examples
  rehearsal evidence into a go/no-go draft.

### Compatibility

- Python 3.11, 3.12, and 3.13 are supported.
- Runtime dependencies remain empty by default.
- The supported import boundary is the top-level `rpacore` package.
- `transaction_db_path` is the public transaction database config key.
- `ProcessContext.state` stores durable shared state; `ProcessContext.resources`
  stores runtime-only objects that are not persisted.
- Queue lease configuration uses `lease_timeout`.

### Known Limits

- RPA Core is not a sandbox; it executes user-authored Python.
- There is no runtime AI behavior or AI dependency.
- There is no cloud orchestrator or remote worker protocol in v0.1.0.
- There is no generic per-skill hard timeout or arbitrary cancellation.
- There is no automatic skill discovery or configuration-defined pipeline model.
- Artifact records store paths and metadata, not file contents.

### Validation

- Installed-wheel validation passed on Windows.
- Deterministic examples validation passed from copied workspaces against a
  freshly built wheel.
- Public documentation links, stale public-doc patterns, and API-reference
  coverage are verified in CI.
