# Changelog

All notable user-facing changes are recorded here.

## Unreleased

### Added

- Notification dispatch retries selected transient SMTP/HTTP/socket failures
  up to three attempts, with 1- and 2-second waits and attempt logs. Terminal
  failure accounting and transaction outcomes remain separate. This replaces
  dispatch's single attempt: delivery can duplicate and take longer; exhaustion
  or process exit can still lose notifications. Direct notifier `send` remains
  single-attempt and preserves readable HTTP error responses for caller-owned
  cleanup; dispatch closes the errors it consumes. No schema migration,
  dependency or examples update is needed.
- Read-only queue item, attempt and poison inspection through top-level query
  functions and `rpacore queue list/show/attempts/poison`. Bounded keyset pages
  and queue-specific JSON v1 omit payloads/tokens from CLI output. Inspection
  requires current schemas, refuses WAL, and never creates, migrates, reclaims
  or quarantines storage. No migration or examples-project change is required.
- `doctor` offers nonfatal, privacy-bounded guidance for likely root, queue and
  notification setting typos and divergent explicit config/manifest transaction
  paths. Doctor-v1 config details optionally include ordered advisory codes,
  known suggested keys and counts; human summaries explain the same findings.
  Custom keys remain valid and database selection is unchanged.
- Transaction list filters for status, aware creation-time windows, reference,
  typed metadata and application run IDs, with cursor continuation and stored
  outcome fields in human list/show output. Single-record export is available
  through `transaction export --transaction-id ID` in both existing formats.
- A disposable scaffold tutorial demonstrates failure after file publication,
  separate-process recovery of the same ID, completed-step preservation,
  persisted arguments, reports and selected exports. Installed-wheel CI covers
  Windows and Ubuntu on Python 3.11–3.14 using one shared wheel artifact.
- Newly scaffolded projects enable configured framework logging on stderr,
  including verbatim step-failure exception messages under the existing
  security policy. Logging values retain existing load-time validation;
  generated entrypoints treat `NOTSET` as `DEBUG`. They
  print a human run receipt with transaction identity, saved outcome, storage
  path, and inspection guidance. Ordinary execution/checkpoint exceptions
  instead report unconfirmed durability on stderr and propagate the original
  error.
  Another plain run creates a new transaction. Existing projects are unchanged
  and require no migration.

### Breaking

- Queue delivery retries now persist a next-eligible UTC `available_at`, with
  default delay 1 second, backoff 2 and a one-day cap. Retryable failures, lease
  expiry and retrying administrative overrides share this policy. Set queue
  `retry_delay=0` for immediate redelivery. Drain-once workers can return with
  delayed pending work; applications/examples need later invocations or the
  explicit zero-delay setting. No examples files are changed automatically.
  Claims reject selected noncanonical stored deadlines before claiming or
  quarantining the item, preserving the malformed record for diagnosis.
- Queue SQLite schema advances from v4 to v5. Stop all workers and back up both
  databases before the forward transactional migration. Existing items become
  immediately eligible without changing their IDs/bindings/history. Rollback
  requires the matching pre-upgrade code and database backup.
- All queue CLI JSON envelopes advance to schema v2; item records add nullable
  `available_at`, also visible in human output. Library QueuePage format is 2;
  cursor format stays 1 and existing query positions remain valid. Historical
  v1 JSON fixtures are retained; consumers must accept v2 and its item field.

- Transaction list/show JSON advance to CLI schema v3 for truthful captured
  outcomes beside unchanged transaction-v3 records. List consumers must read
  `entry.transaction` and `entry.outcome`; show retains `payload.transaction`
  and adds `payload.outcome`. Gate interpretation on `schema_version`.
  List adds `has_more` and `next_cursor`. SQLite, query/cursor, export and report
  formats remain unchanged; no database migration is required.

- `rpacore init` now rejects invalid package basenames with usage error `2`
  before creating directories or files. Use a valid basename such as
  `my-project` instead of `my project`; accepted metadata spelling is preserved.
  Existing generated projects and durable data require no migration.

### Fixed

- Queue runners retry built-in SQLite claim lock/busy failures with three total
  attempts and 0.05/0.1-second waits, checking for stop before each call. Custom
  providers and overridden claim methods retain single-call behavior. Claim
  preparation now precedes commit, so read/decoding failures cannot strand a new
  claim. Propagating run errors emit an ERROR abort event with partial counts
  instead of normal completion, including fatal final-summary callbacks. JSON
  log v3 names the new events `rpacore.queue.claim.retry` and
  `rpacore.queue.run.aborted`. Public signatures and schemas are unchanged;
  no migration, dependency or examples-repository update is required.
- Persisted SQLite queue initialization now commits the initial pending
  transaction and its item binding together, in shared or separate database
  files, before steps run. It rejects every existing transaction ID, including
  revision-zero records, without overwriting it. This closes the process-crash
  save/bind window; old orphan records remain audit data. The runner now bypasses
  `bind_transaction` overrides during persisted initialization; direct binding
  calls retain their behavior. Move application observation to the factory,
  step code or `after_item` as appropriate. No schema migration, dependency or
  examples-repository update is required.
- New generated entrypoints explicitly build and inject the configured credential
  provider before durable work. The default remains env; construction and the
  greeting read no secrets. Existing projects can add explicit provider injection
  as described in the configuration reference; no data migration is required.
- `doctor` treats only absent discovered transaction/queue storage as unchecked
  `not_applicable`, including queue paths from explicit `--config`. Direct DB
  flags still fail on absence; an absent/invalid explicit config also fails.
  Verify configured locations and recovery/backup before recreating storage.
  Format v1 adds optional bounded `reason`, `cause`, and TOML `location` details;
  input errors omit offending values/source excerpts. Transaction inspection
  resolution errors no longer announce an empty database path.
- Future scaffolds use `generated-greeting/v2` and atomically replace one
  project-root greeting file, including nested runs and same-ID replay after
  publication failure. Persisted output arguments/state/artifacts expose absolute
  local paths; copied records keep their original destination, while a fresh run
  uses the current project root. Existing v1 projects need no migration and
  cannot be resumed with the v2 definition. Python APIs, SQLite and transaction/
  export schemas, and dependencies are unchanged.

- HTML reports and email HTML now render unsafe or ambiguous screenshot paths
  as escaped text. Local screenshot paths remain clickable, with Windows drive
  paths converted to local file URLs and filename delimiters encoded. Explicit
  URLs, UNC/device paths, control characters, and padded values are no longer
  links. Persisted screenshot values, report JSON, and text are unchanged.

## v0.3.0 - 2026-09-03

### Added

- Added caller-owned automation definition identity to durable transactions.
  Persistent one-off and queue execution require a non-empty identity, and
  recovery accepts only an exact identity match before mutating resumable state.
  The identity is independent of the RPA Core, source-control, and deployment
  versions.
- Added `DefinitionIdentityError` for missing, invalid, changed, unidentified,
  or incompatible durable definition identities.
- Added an optional synchronous execution-transition sink to `Engine.run()` and
  a frozen top-level `ExecutionTransition` format-v2 fact. Standalone calls are
  unchanged when no sink is supplied; selected checkpoint state is explicit,
  JSON-safe, minimized, and detached.

### Breaking

- Renamed the public execution object from `Skill` to `Step`, including
  transaction collections/helpers, report views, generated project packages,
  history, transitions, logging fields, and machine-record keys. The retired
  Python names have no compatibility aliases. Generic terms such as
  `arguments`, `exceptions`, `BusinessException`, `SystemException`,
  `Status.SUCCESSFUL`, and `execution_order` are unchanged.
- Renamed exception timestamps to `occurred_at` and the business-failure halt
  option to `halts_remaining_steps`. System failures still always halt the
  current execution pass.
- Advanced transaction SQLite schema 9 to 10 with a transactional, one-way
  migration from the retired table, column, and history-event vocabulary.
  Queue schema remains version 4.
- Advanced machine-readable contracts without reusing old version numbers:

  | Contract | v0.2 | v0.3 |
  | --- | --- | --- |
  | Canonical transaction | 2 | 3 |
  | Report | 1 | 2 |
  | Execution transition | 1 | 2 |
  | CLI transaction list/show | 1 | 2 |
  | JSON/NDJSON export | 1 | 2 |
  | JSON log | 1 or 2 | 3 |

  Doctor and query-cursor formats remain version 1. JSON log v3 is the sole
  JSON output; the old format selector has been removed. See
  [Migrating from v0.2 to v0.3](docs/v0.3-migration.md).

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
  persistence, rejected execution-argument shape coercion and corrupt stored
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
- Public top-level API for execution steps, transactions, engine execution,
  persistence, recovery, queue processing, config, logging, reports,
  notifications, credentials, and project manifests.
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

- Python 3.11, 3.12, 3.13, and 3.14 are supported.
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
- There is no generic per-step hard timeout or arbitrary cancellation.
- There is no automatic step discovery or configuration-defined pipeline model.
- Artifact records store paths and metadata, not file contents.

### Validation

- Installed-wheel validation passed on Windows.
- Deterministic examples validation passed from copied workspaces against a
  freshly built wheel.
- Public documentation links, stale public-doc patterns, and API-reference
  coverage are verified in CI.
