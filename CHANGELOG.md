# Changelog

All notable user-facing changes are recorded here.

## Unreleased

### Added

- Added `atomic_output_path()` for content-agnostic file publication that
  replaces destinations only after writer success.
- Added `execute_transaction()` as a one-off transaction runner with strict
  SQLite checkpoint persistence and explicit `resource_scope` support.
- Added `ConfigField` and `validate_config()` for immutable batch configuration
  specifications with dotted nested keys and plain-dictionary results.

### Breaking

- Renamed maintainer release validation interfaces from evidence/go-no-go
  wording to validation results and approval wording.

  | Old | New |
  | --- | --- |
  | `--release-candidate-evidence` | `--release-candidate-validation-results` |
  | `--examples-wheel-evidence` | `--examples-wheel-validation-results` |
  | `release-candidate-evidence.json` | `release-candidate-validation-results.json` |
  | `release-go-no-go.md` | `release-approval.md` |
  | `evidence` manifest key | `validation_results` manifest key |
  | `go` / `no-go` decision values | `approved` / `rejected` decision values |
  | default `evidence/` output directory | default `validation-results/` output directory |

### Changed

- Updated public docs to describe the maintained `0.1.x` release line where the
  wording applies to current documentation and API coverage.
- Moved package license-file metadata to `project.license-files` and declared an
  explicit setuptools build backend to avoid deprecated build configuration.

### Fixed

- Guaranteed that every queue-item exit stops and joins its lease heartbeat.
- Prevented resource and lifecycle cleanup from suppressing or replacing
  `MemoryError`, `KeyboardInterrupt`, and `SystemExit`.
- Enabled certificate and hostname verification for SMTP STARTTLS before email
  credentials are sent. Private certificate authorities must be installed in
  the Python runtime's trust store.
- Rejected blank and in-memory durable SQLite paths, protected future schema
  markers from mutation, made transaction CLI inspection read-only, selected
  rollback journal for queue safety, and moved initial-transaction cleanup into
  persistence ownership.
- Validated transaction wiring and all durable JSON data before initial
  persistence, rejected skill-argument shape coercion and corrupt stored
  arguments, and preserved history-proven downstream skips from stopping
  business failures during recovery.
- Preserved exception and stack diagnostics in text and JSON logs, protected
  canonical JSON fields, made logger reconfiguration atomic and ownership-aware,
  and isolated report and notifier snapshots from observer mutation. Text logs
  can now append traceback and stack sections, and JSON v1 can include additive
  `exception` and `stack` fields.
- Prevented paused transaction exports from blocking concurrent checkpoints by
  snapshotting the ordered matching identifiers before records are yielded.
  Export intentionally has no transaction-list limit and omits identifiers
  removed by concurrent cleanup before deferred loading.

## v0.1.1 - 2026-07-06

### Fixed

- Cleaned public documentation and package metadata language after the v0.1.0
  public release while preserving existing maintainer script names.
- Kept public docs focused on supported local-first behavior.

## v0.1.0 - 2026-07-05

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
- Canonical `rpacore.dev` homepage metadata, with `rpacore.org` and
  `rpacore.net` reserved as defensive redirects.
- Release rehearsal documentation for freeze inputs, validation results, examples
  validation, and approval decisions.
- Release manifest preparation script for combining framework and examples
  rehearsal validation results into an approval draft.

### Compatibility

- Python 3.11, 3.12, and 3.13 are supported.
- Runtime dependencies remain empty by default.
- Package license metadata uses the PEP 621 table form, with license file
  inclusion configured through the current build backend.
- The supported import boundary is the top-level `rpacore` package.
- `transaction_db_path` is the public transaction database config key.
- `ProcessContext.state` stores durable shared state; `ProcessContext.resources`
  stores runtime-only objects that are not persisted.
- Queue lease configuration uses `lease_timeout`.

### Known Limits

- RPA Core is not a sandbox; it executes user-authored Python.
- There is no runtime AI behavior or AI dependency.
- There is no managed execution service or remote worker protocol in v0.1.0.
- There is no generic per-skill hard timeout or arbitrary cancellation.
- There is no automatic skill discovery or configuration-defined pipeline model.
- Artifact records store paths and metadata, not file contents.

### Validation

- Installed-wheel validation passed on Windows.
- Deterministic examples validation passed from copied workspaces against a
  freshly built wheel.
- Public documentation links, stale public-doc patterns, and API-reference
  coverage are verified in CI.
