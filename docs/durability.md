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
Failed skills whose latest exception is a `SystemException` are reset to
`PENDING` so they can be retried after recovery. Failed skills whose latest
exception is a `BusinessException` remain `FAILED`; bad input or business-rule
failures are terminal until user code or durable state is changed explicitly.
When the resumed transaction is passed to `Engine.run()`, those recovered
business-failed skills are not re-executed.
Pending and skipped skills in a non-successful transaction are reset to
`PENDING`.

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
claim_timeout = 30
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
version   = 3
```

The SQLite queue records its own component version:

```text
component = "queue"
version   = 1
```

This lets transaction and queue tables safely coexist in one SQLite file if a
user intentionally chooses the same path, while keeping their schema versions
independent.

## Migrations

Transaction schema migrations are explicit and sequential. The current latest
transaction schema is version 3.

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

Private-development databases created before component schema versions are still
readable. When opened, they are migrated to transaction schema version 3 by
adding missing columns and recording the component version.

Migration defaults must not invent execution history. Current legacy defaults
are deliberately limited:

- missing `transactions.created_at` remains explicitly unknown
- missing `exceptions.stops_execution` defaults to `false`
- missing `transactions.state` defaults to an empty object
- missing `transactions.started_at` and `transactions.finished_at` remain
  explicitly unknown
- missing history defaults to no history entries

Future persisted models must add fixture-based migration tests from the previous
latest schema to the new latest schema.

## Crash Behavior

RPA Core currently persists transaction data when user wiring, runner wiring, or
tests explicitly call `save_transaction()`. A normal `Engine.run()` call does
not yet checkpoint after every successful skill.

That means:

- a transaction saved after `Engine.run()` completes is durable
- a transaction saved by the queue runner after engine completion is durable
- successful skill progress during a process crash is not yet guaranteed unless
  a save has already happened

When loading persisted data, any `IN_PROGRESS` transaction or skill is treated
as interrupted execution and returned as `FAILED`.

Durable per-skill checkpoints are planned separately. Until then,
`transaction_db_path` is an audit persistence path, not a guarantee that every
successful in-memory step has survived a process crash.
