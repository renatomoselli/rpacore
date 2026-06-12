# Public API and Rejected Feature Audit

This audit closes the Phase 5 review of the intended RPA Core v0.1.0 public
surface before example adoption.

## Public API Boundary

The supported import surface is `rpacore.__all__`. Tests pin the exact exported
names so new public names require an explicit review. The public API covers:

- execution primitives: `Skill`, `Engine`, `ProcessContext`, `Transaction`,
  `Status`, `BusinessException`, `SystemException`, `ExecutionValidationError`
- durable records: `Artifact`, `HistoryEntry`, `HistoryEvent`, reports,
  canonical transaction serialization, persistence load/list/save helpers, and
  `resume_transaction`
- configuration and project wiring: `load_config`, public config validators,
  path resolution helpers, project manifest loading, and entrypoint resolution
- queue and runner: `QueueItem`, `QueueStatus`, `SqliteQueue`, queue protocols,
  `run_queue_loop`, and `QueueRunSummary`
- operator output and integration: logger configuration, reports, notifiers,
  and notification dispatch
- credential providers: environment and optional keyring providers

Public objects keep explicit type hints on constructors, public functions, and
public attributes. Mutation behavior is intentionally concrete:

- skills mutate durable transaction state through `ctx.state`
- runtime handles live in `ctx.resources` and are not persisted
- artifacts are durable records of paths and metadata, not automatic content
  attachments
- callbacks and notifiers observe outcomes but do not silently alter transaction
  success/failure decisions

Installed-wheel import behavior is covered by package export tests, CLI tests,
and the installed-wheel validation script used at release gates.

## Migration Inventory

`docs/pre-v0.1.0-api-migration.md` records every known breaking pre-v0.1.0 API
change with a replacement or removal rationale:

- `Skill(..., timeout=...)` and generic in-process timeout guarantees removed
- malformed transaction wiring now fails validation before execution
- top-level `db_path` renamed to `transaction_db_path`
- `ProcessContext.data` split into durable `state` and ephemeral `resources`
- `require_data()` / `optional_data()` renamed to state-focused accessors
- runner `on_start` removed in favor of `resource_scope`
- queue `claim_timeout` renamed to `lease_timeout`

No compatibility shims are carried for private-development APIs.

## Rejected v0.1.0 Features

The following features remain intentionally absent:

- config-defined pipelines or declarative skill graphs
- generic per-skill timeout or worker termination API
- generic synchronous `Engine` event bus
- `SkillTestCase`, custom test runner, or pytest plugin
- CLI transaction resume command

Tests assert rejected public names are absent from `rpacore.__all__`, that
`rpacore transaction resume` is not accepted by the CLI, and that the repository
sample config uses `lease_timeout` rather than the removed `claim_timeout`.

## Carried Review-Debt Dispositions

| Item | v0.1.0 disposition |
| --- | --- |
| Engine terminal-status/history assignment before a strict checkpoint, and whether terminal completion can be made atomic with durable persistence | Accepted current behavior for v0.1.0. Strict checkpoints already persist before queue transitions where configured. Full atomicity across Python execution, SQLite persistence, and external side effects is not guaranteed by the framework. Revisit only if release testing shows duplicate terminal work or ambiguous terminal records. |
| Checkpoint-failure queue retry decisions that read stale durable state or fallback in-memory state, including last-succeeded-skill false negatives | Accepted current behavior for v0.1.0 with documented retry disposition in `docs/durability.md`. The runner errs toward retry before durable evidence proves terminal work is complete. Deeper transaction/queue co-ordination belongs in a post-v0.1.0 durability hardening task. |
| Runner broad exception classification, especially corruption versus retryable transient failures | Accepted with documented boundaries. Broad catches are limited to runner lifecycle and queue-disposition boundaries, do not catch `MemoryError`, and are described in `docs/durability.md` and `docs/core-validation-audit.md`. Future narrowing should replace them only with explicit expected exception classes and tests. |
| Queue schema versioning and migration policy before further queue schema changes | Accepted current queue schema versioning for v0.1.0. Any future queue schema change must include an explicit migration step, version bump, and tests before adding new columns or indexes. |
| SQLite connection, timeout, lock, and transaction-boundary consistency across persistence and queue modules | Accepted current behavior for v0.1.0. Persistence and queue use short SQLite timeouts and local retry loops at runner transition/checkpoint boundaries. A shared connection policy can be evaluated after example adoption if lock contention appears. |
| Public typing story for JSON-safe `ProcessContext.state` | Deferred post-v0.1.0. The runtime enforces JSON safety at persistence/checkpoint boundaries. A static `JSONValue` type alias could improve hints, but adding it now would expand the public type surface late in the release. |
| Transaction history query/indexing needs once export, CLI inspection, and reports settle | Deferred post-v0.1.0. Current CLI/export/report paths can inspect history through full transaction records. Add indexes/query APIs only after real usage demonstrates query pressure. |
| Hypothesis/property-based tests after a serialization or recursive JSON validation bug escapes example-based tests | Deferred post-v0.1.0. Do not add Hypothesis now. Revisit if a serialization field-loss bug or recursive JSON validation bug escapes the current example-based suite. |

## Decision

The v0.1.0 public API is intentionally small and documentable. Rejected features
have not re-entered through names, generated templates, docs, or CLI commands.
Remaining review debt is either accepted as current v0.1.0 behavior or moved to
explicit post-v0.1.0 evaluation triggers.
