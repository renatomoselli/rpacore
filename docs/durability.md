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

Durable transaction data must be JSON-safe: dictionaries with string keys,
lists, strings, numbers, booleans, or `None`. This contract covers transaction
state and metadata, skill arguments, and artifact metadata. RPA Core validates
the complete durable data model at explicit execution, serialization, recovery,
and persistence boundaries. Errors include the path to the offending value and
direct runtime objects toward `ProcessContext.resources` or durable artifact
paths. Tuples and other Python-only containers are rejected rather than silently
coerced into a different shape during persistence.

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
`query_transactions(..., metadata_filter={...})` provides the same exact-match
boundary as a versioned, read-only summary page. The filter is intentionally
limited: it does not provide nested path queries, partial matches, or SQLite
JSON-extension behavior.

`Transaction.status` remains a lifecycle value. Its durable
`outcome_category`, `retry_disposition`, and optional `failure_code` describe
terminal work truth independently: they are not derived from exception prose or
retry counts. The Engine sets its own terminal outcome and retry decision at the
point it decides them. A queue attempt keeps its separate delivery outcome, so a
later lease loss or requeue does not rewrite an already completed transaction.
Legacy or incomplete rows use `unknown`; they are not backfilled with invented
causes. Failure codes are empty or lowercase dot-separated ASCII namespaces;
`rpacore.*` is reserved for framework-owned causes.

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
- `MemoryError`, `KeyboardInterrupt`, and `SystemExit` cannot be suppressed by
  `resource_scope`; cleanup still runs before the original fatal signal
  propagates

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
also retain a durable `transaction_id` binding. This mode requires the concrete
`SqliteQueue`; a custom `QueueProvider` cannot promise one atomic transaction
across RPA Core's SQLite queue and transaction rows. The runner creates or
migrates transaction storage before claiming work so an old/future schema cannot
strand a newly claimed item. On the first claim, it builds the transaction,
seeds queue payload into transaction state, and validates transaction wiring and
all durable data before writing a transaction row. Only a valid, unstarted
pending transaction is persisted and bound before skill execution begins.
Deterministic validation failure terminally fails the queue item without
creating or binding a durable transaction row. The preflight database and
current schema may already exist.

Every claim has a fresh opaque `claim_token`; `claimed_by` remains a diagnostic
worker label and is not a credential. Binding, renewal, completion, and failure
require both the label and current token. A reclaimed item receives a different
token even when the new worker uses the same label. Queue-run transaction
checkpoints also require the token and the transaction's expected persistence
revision. The queue guard, revision update, transaction header, skills,
exceptions, history, metadata, and artifacts are committed in one SQLite
transaction. A stale token or revision raises `TransactionFenceError` before
child rows are changed, and any later write failure rolls the entire snapshot
back.

The token, revision, and queue-item binding are persistence-fencing records,
not public `Transaction` attributes. `load_transaction()` returns the domain
transaction snapshot; the runner owns the private fencing values needed to
persist its next checkpoint safely.

Every new claim also opens one durable `QueueAttempt`. Completion, explicit
retry, terminal failure, lease expiry, and an administrative override close that
attempt with a distinct outcome. Lease expiry consumes the same queue retry
budget as a retriable failure, so repeated worker crashes eventually reach a
terminal queue state. A malformed pending payload is not claimed: the queue
leaves its raw stored value in place, marks the item failed, appends a
`QueuePoisonEvent` containing only the parser error type, and continues to the
next valid item. Use `list_attempts()` and `list_poison_events()` for focused
operator inspection.

On a later queue retry, a bound item resumes the same persisted transaction with
fresh executable skill instances from `build_transaction(item)`. Persisted state
is authoritative on retry; queue payload is not applied a second time. If the
bound transaction is missing or cannot be matched to the supplied skills, the
runner fails the item loudly instead of creating a replacement transaction. When
transaction persistence is not configured, queue retries rebuild work from the
beginning because no durable transaction binding exists.

If a bound transaction is missing, corrupt, or fails execution validation,
resume fails terminally without creating a replacement. The existing binding
and any durable record are retained for operator inspection and repair; the
runner never deletes a transaction that has already been bound to a queue item.

Initial transaction persistence first validates the current queue token inside
the same SQLite transaction that writes the pending transaction. Binding that
new transaction id remains a second, token-guarded queue update with bounded
retry for transient SQLite `locked` or `busy` errors. If binding still fails,
the runner attempts to delete the unbound pending transaction before failing the
queue item. If both binding and cleanup fail, the transaction row can remain in
the transaction database without a queue item reference; this is logged as
`transaction_initial_cleanup_error` with the queue item and transaction
identifiers.

Queue claims are leases, not ownership forever. `SqliteQueue` uses
`lease_timeout` to decide when an `IN_PROGRESS` item is abandoned and may be
claimed by another worker. While an item is running, `run_queue_loop()` starts
one runner-owned heartbeat thread that only calls `queue.renew_lease()`; it does
not run skill code or user callbacks. The heartbeat starts immediately after
claim and remains active through processing, reporting, callbacks, and the final
queue transition.

Every exit after a claim stops and joins that item's heartbeat exactly once,
including fatal failures from execution, reporting, notification, callbacks,
and final queue transitions. If heartbeat cleanup also fails while another
exception is active, the active exception remains primary and the cleanup
failure is attached to it as an exception note.

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

Worker transitions never double as operator overrides. `force_complete()` and
`force_fail()` are separately named administrative methods, require a non-empty
reason, invalidate any active token, and append a durable `QueueAdminEvent`.
Use `list_admin_events()` to inspect those overrides.

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
longer owns the item outcome. Fatal process signals from `after_item` propagate.
User callbacks own their mutations and external side effects.

`on_finish` is a final summary observer. It receives `QueueRunSummary` exactly
once from the runner's finalization path, including empty-queue runs and
`resource_scope` setup failures that happen before the item loop starts.
Ordinary `on_finish` exceptions are logged as `on_finish_error`, increment
`QueueRunSummary.lifecycle_errors`, and are swallowed so cleanup callbacks do
not change the run outcome. Fatal process signals from `on_finish` propagate.

For this lifecycle policy, fatal process signals are `MemoryError`,
`KeyboardInterrupt`, and `SystemExit`. They always propagate and cannot be
suppressed by `resource_scope`. Cleanup still runs. If cleanup raises while a
fatal signal is active, the fatal signal remains primary and the cleanup
failure is visible in its exception notes.

Runner failure dispositions:

| Boundary | Ordinary outcome | Fatal signal outcome |
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
| lease loss | increments `QueueRunSummary.failed`, logs `queue_item_lease_lost`, skips final transition, stops claiming work | propagates if heartbeat failure is fatal |
| resource cleanup | propagates after already-decided queue outcomes | propagates |
| `on_finish` | logged and swallowed | propagates |

The runner keeps a small number of broad `except Exception` boundaries to
convert unexpected ordinary failures into explicit queue retry decisions, to
preserve already-decided outcomes after reporting/callback failures, and to
keep lifecycle cleanup observers from changing the run result. Those catch-all
boundaries do not suppress fatal process signals.

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

`generate_report()` snapshots nested JSON-safe metadata, artifact metadata,
exceptions, history, and the canonical transaction record. Its `outcome` view
directly projects the transaction's captured terminal category, retry
disposition, and optional failure code; it reports `unknown` rather than
reconstructing missing truth from lifecycle status, history, or queue attempts.
The canonical JSON transaction record remains format version 1 and does not
yet carry those additive fields. `dispatch()` gives each notifier a fresh
snapshot, so one notifier cannot mutate the transaction, the source report, or
a later notifier's view. Unsupported arbitrary runtime objects are not
recursively cloned; they do not belong in durable report data.

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

When a persisted `BusinessException(stop=True)` caused the engine to skip
downstream skills, those causal skips also remain `SKIPPED`. Recovery requires
the corresponding persisted `skill_skipped` history; it does not infer causality
from status and execution order alone. Passing `retry_business_failures=True`
explicitly resets both the stopping failure and its causal downstream skips to
`PENDING`.

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

CLI list and export select full records through `query_transactions()` summary
pages ordered by normalized UTC creation time and transaction id. Each page is
loaded independently, so inserts before the cursor do not appear later while
inserts after it can appear in a later page. Updates to an already selected
transaction are visible if they commit before that individual record is loaded;
a selected transaction deleted by concurrent cleanup before its load is omitted.
This avoids a long-lived SQLite read lock and retains only one summary page at a
time. Export has no transaction-list cap and walks additional pages as needed.
`list_transactions()` remains the compatible full-record API with its existing
bounded snapshot behavior.

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

Durable database paths cannot be blank or `:memory:`. An empty path creates a
temporary database, whitespace-only paths do not identify an intentional
durable target, and `:memory:` creates connection-local state. RPA Core uses
multiple connections for normal persistence and queue operation.

## SQLite Journal Policy

Queue databases, and transaction databases attached for queue checkpoints, use
SQLite's rollback journal rather than WAL. This is a
correctness-first policy for the stdlib SQLite runtimes supported by the
`v0.1.x` line: SQLite documents a rare multi-connection WAL-reset corruption
race in affected releases. RPA Core does not claim that a Python package can
replace the SQLite library embedded in CPython. CI and installed-wheel jobs
record both Python and SQLite versions so this policy can be revisited from
runtime evidence. See SQLite's official
[WAL-reset bug documentation](https://www.sqlite.org/wal.html#walreset).

Rollback journal permits concurrent workers through SQLite's normal locking;
RPA Core keeps claim, heartbeat, checkpoint, and transition transactions short
and retains bounded lock retries at the runner boundaries. Do not manually
switch an active queue or queue-run transaction database to WAL. Fenced
checkpoints reject either file when its journal mode is not `DELETE`.

After compatibility checks pass, the rollback-journal policy is applied before
schema migration. Journal mode is an independent persistent safety setting, not
part of the schema transaction: if migration fails and rolls back, the database
deliberately remains in rollback-journal mode rather than returning to WAL.

## Schema Versions

RPA Core stores component schema versions in a framework-owned table:

```text
rpacore_schema_versions
```

The current transaction persistence component is recorded as:

```text
component = "transactions"
version   = 8
```

The SQLite queue records its own component version:

```text
component = "queue"
version   = 4
```

This lets transaction and queue tables safely coexist in one SQLite file if a
user intentionally chooses the same path, while keeping their schema versions
independent.

Compatibility is checked before migrations or persistent journal changes. A
component version newer than this runtime supports is rejected without changing
the schema marker. Transaction CLI inspection additionally requires the current
transaction schema and never migrates.

## Migrations

Transaction schema migrations are explicit and sequential. The current latest
transaction schema is version 8.

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

Version 6 adds:

- monotonic transaction persistence revision
- active queue item id and claim token for queue-run checkpoints

Version 7 adds `created_at_utc` for deterministic transaction query ordering,
plus indexes for the summary-page ordering and exact metadata filter shape.
New transaction timestamps are stored in UTC. A timezone-aware legacy timestamp
is normalized during migration; a missing or timezone-naive legacy value remains
loadable but has no invented UTC instant and sorts after timestamped query rows.
An unchanged timezone-naive timestamp loaded from such a legacy row can still be
checkpointed or resumed without coercion; genuinely new naive timestamps are
rejected because their UTC instant is ambiguous.
Transaction summaries expose the UTC query key, while `load_transaction()`
preserves the original persisted timestamp representation; aware values still
represent the same instant. The migration sequence runs in an explicit SQLite
write transaction, so a failed migration rolls back its schema changes and
component version marker together.

Version 8 adds transaction `outcome_category`, `retry_disposition`, and
`failure_code`, plus the optional `code` on persisted skill exceptions. All
new fields default to `unknown` or empty rather than inferring missing terminal
truth from prior status, messages, or retries.

Private-development databases created before component schema versions are still
readable. When opened, they are migrated to transaction schema version 8 by
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
- missing revision defaults to zero
- missing queue item id and claim token default to empty strings
- missing outcome category and retry disposition default to `unknown`
- missing transaction and exception failure codes default to empty strings

Future persisted models must add fixture-based migration tests from the previous
latest schema to the new latest schema.

Queue migrations are also sequential: version 1 creates the queue item model;
version 2 adds durable transaction binding and inspection indexes; version 3
adds opaque claim tokens and the administrative override audit; version 4 adds
append-only claim attempts and poison dispositions. Migration to version 3
invalidates legacy `IN_PROGRESS` leases by returning them to `PENDING` without
discarding their transaction binding, so they must be reacquired with a token.
Version 4 begins attempt history for future claims; it does not invent attempt
records for older rows.

Treat transaction-schema upgrades and the queue-v4 upgrade as offline: stop every worker, back up
both database files together, upgrade and open both with the new runtime, then
restart workers so pending items are reacquired. Old workers reject the newer
schema on their next framework database operation, but the offline boundary is
what closes the interval between migrating separate files. For rollback, stop
workers and restore both code and database files from the same pre-upgrade
backup; never edit version markers or point an older runtime at the newer live
files.

Cleanup of an initial transaction whose queue binding failed is owned by the
persistence module. It will delete only a pending transaction with no history.
Started or otherwise durable execution truth is never eligible for that narrow
cleanup path.

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

The queue runner supplies its claim-and-revision-fenced SQLite checkpoint when
`transaction_db_path` is configured. This means a process that exits after a
skill succeeds leaves that skill status, durable state, timestamps, and history
saved before downstream skills begin. Public `save_transaction()` remains an
unconditional non-queue API. If it is deliberately used on a queue-bound
transaction, it advances the revision so an older runner snapshot fails closed
instead of overwriting that manual save.

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

`QueueRunSummary.processed` counts claims handed to the runner, and its legacy
`failed` counter includes every non-completed claim, including a requeue or
lease loss. Use `retry_scheduled`, `terminal_failed`, and `lease_lost` for the
separate known dispositions. `SqliteQueue.fail()` returns the durable attempt
outcome it just wrote, so those counters describe the transition that happened,
not merely the retry request. A custom `QueueProvider` may return `None`; the
runner records that as `transition_unknown` rather than guessing from its retry
argument.

The runner retries only short-lived SQLite `OperationalError` messages that
contain `locked` or `busy`. This bounded retry policy uses 3 attempts with
0.05s then 0.10s sleeps before the final attempt, and each sleep is logged with
the operation, item id, worker id, attempt, maximum attempts, and delay. The
policy applies to transaction checkpoints, initial transaction persistence,
initial transaction cleanup, queue transaction binding, queue lease renewal, and
final queue `complete()`/`fail()` transitions. Other SQLite operational errors
remain loud and are not retried merely because they came from SQLite.

Deterministic input and wiring failures are terminal queue outcomes. Invalid
transaction wiring, invalid queue payload/state JSON, and invalid durable data
are failed after one delivery attempt without queue retry. Initial validation
applies queue payload precedence first, then validates wiring, state, transaction
metadata, artifact metadata, and skill arguments before any transaction database
write or queue binding. System failures and unexpected processing errors remain
retryable unless a checkpoint boundary proves retry would risk replaying already
completed or terminal business-failed work.

An unavoidable boundary remains: external side effects can happen just before
the checkpoint that records their success. Skills should still be idempotent
where practical.

When loading persisted data, any `IN_PROGRESS` transaction or skill remains
visible as `IN_PROGRESS` until explicit recovery.
