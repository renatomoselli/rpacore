# Durability and Storage

RPA Core persists transaction audit data to SQLite using only the Python
standard library. Persistence is local-first: users can inspect database files
without a service, and queue storage remains separate from transaction history
by default.

SQLite is the built-in persistence backend for `v0.1.0`. Users can choose where
the SQLite files live, but RPA Core does not yet ship alternate transaction
persistence adapters for databases such as PostgreSQL or MySQL.

The storage contract is still kept explicit and portable so a future backend can
preserve the same transaction semantics without changing how skills are written.

## Durable State and Resources

`Transaction.state` is the durable, transaction-owned state mapping. Skills
access it through `ProcessContext.state`, and queue item payload initializes it
when `run_queue_loop()` builds an item context.

If `build_transaction()` pre-populates `Transaction.state` with a key that is
also present in the queue item payload, the queue payload value wins. The runner
logs the collision at warning level with the overlapping keys.

State must be JSON-safe: dictionaries with string keys, lists, strings, numbers,
booleans, or `None`. RPA Core validates durable state at explicit boundaries
where bytes are produced or persisted. `save_transaction()` validates the full
transaction state before writing, and the queue runner validates item payload
before seeding state. Errors include the path to the offending value and direct
runtime objects toward `ProcessContext.resources` or durable artifact paths.

`ProcessContext.resources` is for ephemeral runtime objects such as clients,
sessions, handles, and open files. Resources are never persisted with
transactions and are not included in reports or notifications.

`Transaction.metadata` is a JSON-safe, transaction-owned mapping for durable
tags and descriptors used outside skill execution. Metadata is persisted with
the transaction, but it is not exposed through `ProcessContext.state` and should
not be used as mutable workflow state. `save_transaction()` validates metadata
with the same JSON-safe value rules as transaction state, and validation errors
include the offending `transaction.metadata[...]` path.

SQLite stores top-level metadata entries in a dedicated table as canonical JSON
values. `list_transactions(..., metadata_filter={...})` supports deterministic
exact-match filters on those top-level entries, including list and object values.
The filter is intentionally limited: it does not provide nested path queries,
partial matches, or SQLite JSON-extension behavior.

`Transaction.artifacts` records generated or captured file paths as durable audit
records. Skills register artifacts with
`ProcessContext.add_artifact(name, path, kind="", metadata=None)`. Each artifact
has a stable id, name, path, free-form kind, UTC creation timestamp, and
JSON-safe metadata. Artifact files are not read, hashed, uploaded, or required by
default; missing files do not prevent save, load, reporting, or notification.
Framework-captured screenshots are registered as screenshot artifacts while the
exception `screenshot_path` remains intact.

Artifacts are part of transaction recovery because they are loaded with the
transaction. They remain attached to the durable transaction that recorded them,
including interrupted or `IN_PROGRESS` transactions left behind by a failed
checkpoint. RPA Core does not clean up those records automatically; queue-to-
transaction binding makes stranded transaction records discoverable from queue
retries. Transaction schema migrations are forward-only; older code must reject
a newer artifact schema version rather than silently ignoring artifact rows.

`atomic_output_path(destination)` supports content-agnostic file publication for
skills that write JSON, CSV, workbooks, or library-owned formats. It yields a
temporary path in the destination directory and calls `os.replace()` only after
the context exits successfully. If writer code raises, the previous destination
is preserved and the temporary file is removed without masking the original
exception. The helper fsyncs the temporary file before replacement, but it does
not create parent directories, preserve prior file permissions, coordinate
cross-process writers, or make file publication atomic with SQLite checkpoints.
Register artifacts only after the context succeeds so durable transaction
records do not point at partial files.

Queue resource lifecycle:

- queue item payload populates `ctx.state`
- `resource_scope` is entered once before queue claims and exited once after
  processing
- `resource_scope` may yield resources that populate `ctx.resources`
- each item context receives a shallow copy of the resource mapping, so
  top-level resource names are isolated while nested resource objects retain
  identity
- resource setup failures prevent queue claims
- resource cleanup failures propagate after already-decided queue outcomes

One-off transaction lifecycle:

- `execute_transaction(transaction, transaction_db_path=...)` builds a
  `ProcessContext` and supplies strict SQLite checkpoints to `Engine.run()`
- transient SQLite `locked` or `busy` checkpoint failures are retried briefly
  before propagating
- pass `engine=Engine(...)` when the run needs custom retry or screenshot
  settings
- pass `checkpoint=...` for custom persistence instead of `transaction_db_path`;
  supplying both is rejected
- `resource_scope` is entered once before the transaction starts and exited once
  after `Engine.run()` returns or raises
- `resource_scope` may yield resources that populate `ctx.resources`
- the context receives a shallow copy of the resource mapping
- resource setup failures prevent skill execution
- resource cleanup failures propagate after the transaction outcome is already
  decided
- a custom context manager that returns truthy from `__exit__` may suppress an
  execution exception; the transaction status and history still record the
  engine outcome

When `run_queue_loop()` is configured with `transaction_db_path`, queue items
also retain a durable `transaction_id` binding. On the first claim, the runner
builds the transaction, validates and seeds queue payload into transaction state,
strictly persists the pending transaction, binds that transaction id to the
claimed queue item, and only then begins skill execution. The binding is guarded
by the queue claim owner, so a worker that no longer owns the claim cannot attach
a transaction.

On a later queue retry, a bound item resumes the same persisted transaction with
fresh executable skill instances from `build_transaction(item)`. Persisted state
is authoritative on retry; queue payload is not applied a second time. If the
bound transaction is missing or cannot be matched to the supplied skills, the
runner fails the item loudly instead of creating a replacement transaction. When
transaction persistence is not configured, queue retries rebuild work from the
beginning because no durable transaction binding exists.

Initial transaction persistence and queue binding touch separate SQLite write
domains. The runner persists the pending transaction first, then binds the queue
item to that id with bounded retry for transient SQLite `locked` or `busy`
errors. If binding still fails, the runner attempts to delete the unbound
pending transaction before failing the queue item. If both binding and cleanup
fail, the transaction row can remain in the transaction database without a queue
item reference; this is logged as `transaction_initial_cleanup_error` with the
queue item and transaction identifiers.

Queue claims are leases, not ownership forever. `SqliteQueue` uses
`lease_timeout` to decide when an `IN_PROGRESS` item is abandoned and may be
claimed by another worker. While an item is running, `run_queue_loop()` starts
one runner-owned heartbeat thread that only calls `queue.renew_lease()`; it does
not run skill code or user callbacks. The heartbeat starts immediately after
claim and remains active through processing, reporting, callbacks, and the final
queue transition.

The renewal interval is shorter than `lease_timeout`; for `SqliteQueue` the
runner uses one third of the lease timeout, bounded between 0.1 and 10 seconds.
Transient SQLite `locked` or `busy` renewal errors are retried briefly before
they are treated as renewal failures.

If renewal proves the worker no longer owns the item, the runner records a
`QueueLeaseLostError`, logs the item and worker identifiers, skips the final
`complete()` or `fail()` call, and claims no further work. A skill already
executing when the lease is lost cannot be safely terminated by RPA Core and may
finish external side effects before the next checkpoint or final transition
observes the loss. Queue delivery is therefore at least once; skills should be
idempotent when they perform external side effects.

## Runner Failure Policy

`run_queue_loop()` keeps framework lifecycle behavior separate from user
automation behavior. `resource_scope` is the only setup/cleanup mechanism for
shared runtime resources. It is entered before queue claims and exited after
item processing. Setup failures prevent queue claims. Cleanup failures propagate
after already-decided queue outcomes.

`after_item` is a per-item observer for ordinary item outcomes. It receives
`(QueueItem, Transaction | None, Exception | None)` after execution,
checkpointing, report generation, and notification dispatch, immediately before
the intended final queue transition. It never receives `ProcessContext` or
resources. Ordinary `after_item` exceptions are logged as `after_item_error`; on
successful item paths they increment `QueueRunSummary.callback_errors`; on
failure paths they are logged but do not increment that counter. Ordinary
callback failures do not change the intended `complete()` or `fail()`
transition. Confirmed lease loss skips `after_item` because the worker no
longer owns the item outcome. `MemoryError` from `after_item` propagates. User
callbacks own their mutations and external side effects.

`on_finish` is a final summary observer. It receives `QueueRunSummary` exactly
once from the runner's finalization path, including empty-queue runs and
`resource_scope` setup failures that happen before the item loop starts.
Ordinary `on_finish` exceptions are logged as `on_finish_error`, increment
`QueueRunSummary.lifecycle_errors`, and are swallowed so cleanup callbacks do
not change the run outcome. `MemoryError` from `on_finish` propagates.

Runner failure dispositions:

| Boundary | Ordinary outcome | MemoryError outcome |
| --- | --- | --- |
| resource setup | propagates before queue claim | propagates |
| transaction build | item fails with queue retry unless validation is terminal | propagates |
| payload/state validation | terminal queue failure without retry | propagates |
| engine system failure | queue failure with retry | propagates |
| engine business-only failure | terminal queue failure unless `retry_business_failures=True` | propagates |
| transaction checkpoint | queue retry only before successful/skipped/terminal business work is durably at risk | propagates |
| reporting or notification | logged; intended queue outcome preserved | propagates |
| `after_item` | logged; intended queue outcome preserved; skipped after confirmed lease loss | propagates |
| final `complete()`/`fail()` transition | transient SQLite lock/busy is retried; non-transient errors propagate | propagates |
| lease loss | increments `QueueRunSummary.failed`, logs `queue_item_lease_lost`, skips final transition, stops claiming work | propagates if heartbeat failure is `MemoryError` |
| resource cleanup | propagates after already-decided queue outcomes | propagates |
| `on_finish` | logged and swallowed | propagates |

The runner keeps a small number of broad `except Exception` boundaries to
convert unexpected ordinary failures into explicit queue retry decisions, to
preserve already-decided outcomes after reporting/callback failures, and to
keep lifecycle cleanup observers from changing the run result. Those catch-all
boundaries do not catch `MemoryError`.

After confirmed lease loss, the old worker records the item as failed in its run
summary but leaves the queue row untouched. If another worker already claimed
the item, that worker owns the next transition. If no worker owns it, normal
lease expiry makes the item reclaimable later.

## Extension and Event API Decision

RPA Core v0.1.0 does not provide a generic synchronous `Engine` event bus.
The extension surface stays explicit:

- persisted transaction history records the durable execution timeline
- structured logs expose operational events for observers and log pipelines
- `after_item` observes per-item outcomes after reporting and notification
- `on_finish` observes the final runner summary exactly once
- `resource_scope` owns paired setup and cleanup for shared runtime resources
- reports, notification payloads, and canonical transaction serialization expose
  completed transaction records without reading artifact contents

These mechanisms cover the demonstrated extension needs without adding an
in-process handler chain inside `Engine.run()`. A generic event bus would require
new decisions about handler timing, handler failure disposition, transaction
mutation rights, ordering between handlers, and checkpoint boundaries. Those
decisions affect determinism and durability: a handler that mutates transaction
state before a checkpoint, raises after external side effects, or depends on
relative ordering with another handler could change retry behavior in ways that
are hard to audit.

If a future workflow cannot be expressed with durable history, structured logs,
runner callbacks, resource scopes, reports, notifications, or explicit skill
code, add a narrow extension point for that workflow rather than a broad event
bus. The extension point must document when it runs, whether ordinary exceptions
propagate or are swallowed, whether mutation is allowed, and how it orders
relative to persistence checkpoints and queue transitions.

## Timestamps and History

`Transaction.created_at` is set when a new transaction object is constructed.
It records local object creation, not a durable checkpoint. Legacy persisted
transactions that predate this field expose `created_at=None` when the original
creation time is unknown.

`Transaction.started_at` records the current execution window start.
`Transaction.finished_at` records terminal completion and is cleared, along
with `started_at`, when a non-successful transaction is explicitly resumed.

`Transaction.history` is an append-only audit trail persisted separately from
logs. Each `HistoryEntry` has a transaction-local `sequence`, UTC `timestamp`,
closed `HistoryEvent`, resulting `status`, `retry_number`, and optional skill
identity.

The v0.1.0 history vocabulary is closed:

- `transaction_started`
- `skill_started`
- `skill_succeeded`
- `skill_failed`
- `skill_skipped`
- `skill_interrupted`
- `retry_scheduled`
- `transaction_resumed`
- `transaction_completed`

History entries are persisted audit records, not an in-process event bus.
Repeated `save_transaction()` calls do not duplicate history rows.

## Resume Behavior

`resume_transaction()` reloads a persisted transaction and reattaches executable
skill instances supplied by the caller. Successful skills remain successful.
Persisted `IN_PROGRESS` transaction or skill state remains visible after
`load_transaction()` and is recovered only when `resume_transaction()` is called.

For interrupted transactions, `resume_transaction()` preserves successful and
skipped skills, resets interrupted or pending work to `PENDING`, and keeps
durable state from the last successful checkpoint. For ordinary failed
transactions, failed skills whose latest exception is a `SystemException` are
reset to `PENDING` so they can be retried after recovery. Failed skills whose
latest exception is a `BusinessException` remain `FAILED`; bad input or
business-rule failures are terminal until user code or durable state is changed
explicitly. When the resumed transaction is passed to `Engine.run()`, those
recovered business-failed skills are not re-executed.

Repeated resume calls do not append duplicate `transaction_resumed` history
entries when the latest persisted history entry already records the resume.

## Transaction Persistence

Transaction history is written through:

```python
save_transaction(transaction, db_path="rpacore.db")
```

and loaded through:

```python
load_transaction(transaction_id, db_path="rpacore.db")
list_transactions(db_path="rpacore.db")
```

These function parameters are still named `db_path` because they directly name
the database file being used by the function. In configuration, the transaction
database path is named `transaction_db_path` to avoid confusing it with the
queue database path.

## Configuration

Top-level transaction persistence configuration:

```toml
transaction_db_path = "rpacore.db"
```

Queue persistence remains under the queue section:

```toml
[queue]
db_path = "queue.db"
lease_timeout = 30
max_retries = 3
```

The old top-level `db_path` configuration key is rejected with a migration
error. Rename it to `transaction_db_path`.

When loaded from `config.toml`, relative `transaction_db_path` and
`queue.db_path` values are resolved relative to the config file's directory.
This keeps both storage files stable when a process runs from a different
current working directory.

## Schema Versions

RPA Core stores component schema versions in a framework-owned table:

```text
rpacore_schema_versions
```

The current transaction persistence component is recorded as:

```text
component = "transactions"
version   = 5
```

The SQLite queue records its own component version:

```text
component = "queue"
version   = 2
```

This lets transaction and queue tables safely coexist in one SQLite file if a
user intentionally chooses the same path, while keeping their schema versions
independent.

## Migrations

Transaction schema migrations are explicit and sequential. The current latest
transaction schema is version 5.

Version 1 stores:

- transactions
- skills
- exceptions
- transaction `created_at`
- exception `stops_execution`

Version 2 adds:

- durable transaction `state`

This migration is additive and forward-only. Rolling back to version 1 code
means the older code can coexist with the extra `transactions.state` column, but
the schema version remains recorded as version 2.

Version 3 adds:

- transaction `started_at`
- transaction `finished_at`
- append-only `transaction_history`

Version 4 adds:

- transaction metadata

Version 5 adds:

- transaction artifacts

Private-development databases created before component schema versions are still
readable. When opened, they are migrated to transaction schema version 5 by
adding missing columns and recording the component version.

Migration defaults must not invent execution history. Current legacy defaults
are deliberately limited:

- missing `transactions.created_at` remains explicitly unknown
- missing `exceptions.stops_execution` defaults to `false`
- missing `transactions.state` defaults to an empty object
- missing `transactions.started_at` and `transactions.finished_at` remain
  explicitly unknown
- missing history defaults to no history entries
- missing metadata defaults to an empty object
- missing artifacts default to an empty list

Future persisted models must add fixture-based migration tests from the previous
latest schema to the new latest schema.

## Crash Behavior

`Engine.run()` accepts an optional strict checkpoint callback:

```python
Engine().run(ctx, checkpoint=lambda tx: save_transaction(tx, db_path))
```

When configured, the engine validates durable transaction state and calls the
checkpoint after each transaction or skill state transition, including the
`skill_started` checkpoint before user skill code begins. Checkpoint failures
propagate and stop execution. They are not converted into successful outcomes.

For ordinary non-queue runs, `execute_transaction()` provides the same strict
SQLite checkpoint behavior without hand-writing the callback:

```python
execute_transaction(transaction, transaction_db_path="rpacore.db")
```

Its built-in SQLite checkpoint retries match the same short-lived `locked` or
`busy` policy used by the queue runner. Other SQLite errors remain loud.

The queue runner supplies `save_transaction()` as this checkpoint when
`transaction_db_path` is configured. This means a process that exits after a
skill succeeds leaves that skill status, durable state, timestamps, and history
saved before downstream skills begin.

Checkpoint failures prevent queue completion. If the failure happens after a
successful, skipped, or terminal business-failed skill outcome, the runner marks
the queue item failed without automatic retry to avoid replaying side effects
that already occurred. Earlier checkpoint failures remain retryable.

After any checkpoint failure, treat the in-memory `Transaction` object as a
diagnostic snapshot of the interrupted run. For durable recovery, reload the
persisted transaction and call `resume_transaction()` with fresh skill
instances instead of retrying the same partially mutated object.

Runner retry has two separate layers. `Engine(max_retries=...)` owns in-process
skill retry passes for `SystemException` failures inside one claimed queue item;
it increments `Transaction.retry_count`. The queue owns item delivery retry:
`SqliteQueue(max_retries=...)` increments `QueueItem.retry_count` only when
`run_queue_loop()` calls `queue.fail(..., retry=True)`. These counters are not
interchangeable, and a queue retry may resume an already persisted transaction
when `transaction_db_path` is configured.

The runner retries only short-lived SQLite `OperationalError` messages that
contain `locked` or `busy`. This bounded retry policy uses 3 attempts with
0.05s then 0.10s sleeps before the final attempt, and each sleep is logged with
the operation, item id, worker id, attempt, maximum attempts, and delay. The
policy applies to transaction checkpoints, initial transaction persistence,
initial transaction cleanup, queue transaction binding, queue lease renewal, and
final queue `complete()`/`fail()` transitions. Other SQLite operational errors
remain loud and are not retried merely because they came from SQLite.

Deterministic input and wiring failures are terminal queue outcomes. Invalid
transaction wiring, invalid queue payload/state JSON, and invalid durable state
are failed without queue retry. System failures and unexpected processing errors
remain retryable unless a checkpoint boundary proves retry would risk replaying
already completed or terminal business-failed work.

An unavoidable boundary remains: external side effects can happen just before
the checkpoint that records their success. Skills should still be idempotent
where practical.

When loading persisted data, any `IN_PROGRESS` transaction or skill remains
visible as `IN_PROGRESS` until explicit recovery.
