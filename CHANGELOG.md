# Changelog

All notable user-facing changes are recorded here.

## v0.2.0 - 2026-07-29

### Added

- Added PEP 561 type information and a pinned development-only Mypy check for
  the supported top-level public API.
- Added a read-only, cursor-paginated `query_transactions()` API with compact
  transaction summaries, deterministic UTC ordering, exact filters, and
  query-specific SQLite indexes.
- Added `atomic_output_path()` for content-agnostic file publication that
  replaces destinations only after writer success.
- Added `execute_transaction()` as a one-off transaction runner with strict
  SQLite checkpoint persistence and explicit `resource_scope` support.
- Added `ConfigField` and `validate_config()` for immutable batch configuration
  specifications with dotted nested keys, frozen reusable choices, independent
  JSON-safe defaults, and plain-dictionary results.
- Added reason-bearing `SqliteQueue.force_complete()` and `force_fail()`
  administrative overrides with durable `QueueAdminEvent` inspection.
- Added durable `QueueAttempt` outcomes and `QueuePoisonEvent` inspection.
- Added `OutcomeCategory` and `RetryDisposition` terminal work vocabulary,
  plus optional namespaced failure codes on framework exceptions and persisted
  transaction outcomes.
- Added disposition-specific queue-run summary counters. `SqliteQueue.fail()`
  now returns the actual durable attempt disposition; custom providers can
  return `None`, which the runner reports as an unknown transition.
- Added `OutcomeReport` and text/HTML report fields that directly expose a
  transaction's captured outcome category, retry disposition, and optional
  failure code. The canonical JSON transaction export remains format version 1.
- Added opt-in JSON log format v2 with a protected nested attribute envelope,
  scoped scalar correlation context, corrected application logger hierarchy,
  and a terminal queue-run summary event. JSON log format v1 remains the
  default compatibility format.
- Added immutable report format v1 via `ReportRecord` and `render_json()`.
  Text and HTML reports derive from the same record, whose explicit incomplete
  state preserves visible report-generation errors without changing transaction
  outcome. Webhooks can opt in to a `report` v1 member with
  `include_report = true`; their existing default payload is unchanged.
- Added `rpacore doctor`, a versioned, privacy-bounded read-only diagnostic
  command for runtime, project, transaction, and queue storage checks. It never
  creates, migrates, repairs, imports project code, or contacts endpoints.

### Changed

- New generated projects declare their transaction path only in `rpacore.toml`;
  generated `main.py` reads that manifest path for checkpoint persistence.
- Transaction CLI list and export now select membership through normalized-UTC
  query pages, avoiding export's unbounded identifier snapshot while preserving
  JSON and NDJSON export format version 1.
- Expired leases now consume queue retry budget, and malformed pending payloads
  are quarantined so later valid work can continue.

### Breaking

- Queue claims now carry an opaque `claim_token`. Claimed-worker
  `bind_transaction()`, `renew_lease()`, `complete()`, and `fail()` calls must
  provide the current worker label and token; unguarded transitions were
  replaced by separately named administrative methods. Durable
  `run_queue_loop(transaction_db_path=...)` now requires `SqliteQueue` so claim
  and transaction writes can be fenced in one SQLite transaction.

- Queue schema version 4 records attempts and poison dispositions. Upgrade queue
  databases offline with the same stop, backup, migrate, and restart procedure
  required for the queue/transaction fencing upgrade.

- Transaction schema version 8 records terminal outcome/retry truth and optional
  failure codes. Stop workers and back up transaction databases before upgrade;
  older runtimes reject the newer schema rather than silently ignoring it.

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

- Updated public docs to describe the maintained `0.2.x` release line where the
  wording applies to current documentation and API coverage.
- Moved package license-file metadata to `project.license-files` and declared an
  explicit setuptools build backend to avoid deprecated build configuration.

### Fixed

- Preserved resumability for existing transactions with timezone-naive legacy
  timestamps without inventing a UTC instant for query filtering or ordering.
- Fenced administrative queue overrides before their claim snapshot so an
  override cannot transition a claim acquired concurrently by another worker.
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
- Resolved dotted entrypoints from each manifest's project even when another
  same-named package was cached, and made non-empty `screenshot_dir` paths
  resolve relative to their configuration file. Whitespace-only values and the
  SQLite-only `:memory:` sentinel, including whitespace-padded forms, are
  rejected as screenshot directories and durable database paths.
- Prevented stale or same-label workers from renewing, binding, transitioning,
  or checkpointing a reclaimed queue item by combining per-claim tokens with
  transaction revisions. Fenced checkpoint failures leave transaction headers
  and child rows unchanged, while legacy active leases are invalidated for
  token-bearing reacquisition during schema migration.

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
