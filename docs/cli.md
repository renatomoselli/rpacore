# CLI Reference

The `rpacore` command is installed by the `rpacore` package.

## `rpacore init <project_name>`

Creates a normal Python project directory containing:

- `pyproject.toml`
- `rpacore.toml`
- `config.toml`
- `main.py`
- `steps/`
- `tests/`
- `.gitignore`

The generated `main.py` uses `execute_transaction(transaction_db_path=...)`
with the project manifest's storage path, so execution and default inspection
refer to the same durable transaction history. It also supplies a stable
application-owned `definition_identity` so an interrupted run can be resumed
only by a compatible automation definition.

RPA Core distributions include the framework's `LICENSE` and `NOTICE`. The
generated project does not copy those files or add a package license field,
because the generated automation is user-owned project code. Add your own
license or provenance files when you publish or share it.

Exit codes:

- `0`: project created
- `1`: project creation failed after filesystem work started
- `2`: target path already exists or input is invalid

## `rpacore run`

Finds `rpacore.toml` from the current directory, resolves `[project].entrypoint`,
and calls that Python callable. RPA Core does not discover steps or build a
pipeline from configuration; user code owns transaction wiring. Resolution
temporarily prioritizes the manifest's project directory and replaces a cached
entrypoint package only when it belongs to another project. The resolver never
purges modules outside the entrypoint's top-level package, although ordinary
Python imports may cache external dependencies. The process working directory
is unchanged.

Exit codes:

- `0`: entrypoint returns `None` or `0`
- `1`: entrypoint raises
- `2`: manifest, entrypoint, or return-value validation fails
- `0` through `255`: propagated when the entrypoint returns an integer in that
  range

## `rpacore version`

Prints the installed package version.

## `rpacore doctor`

Runs privacy-bounded, read-only diagnostics for the local Python/SQLite runtime,
the nearest project manifest, an optional `config.toml`, and selected existing
transaction or queue databases. It never imports the project entrypoint,
creates or migrates a database, changes journal mode, contacts an endpoint, or
reads credentials, transaction state/arguments, exception messages, queue
payloads, artifact contents, or file paths from those records.

```bash
rpacore doctor
rpacore doctor --json
rpacore doctor --transaction-db path/to/rpacore.db
rpacore doctor --queue-db path/to/queue.db
rpacore doctor --config path/to/config.toml --json
```

The default command discovers `rpacore.toml` from the current directory and,
when present, uses its transaction database path and sibling `config.toml`.
`--transaction-db`, `--queue-db`, and `--config` select explicit inputs.
Missing default inputs are reported as `not_applicable`; unavailable, malformed,
or incompatible explicit inputs are `fail` checks. Database checks cover current
schema compatibility, journal mode, SQLite `quick_check`, foreign-key integrity,
and bounded queue counts/claim-binding facts. The JSON result is doctor format
v1 with `pass`, `warning`, `fail`, or `not_applicable` check statuses. JSON
stdout contains one document only.

Doctor format v1 always emits these check IDs: `runtime.python`,
`runtime.sqlite`, `project.manifest`, `project.config`,
`transactions.schema`, `transactions.journal`, `transactions.quick_check`,
`transactions.foreign_keys`, `queue.schema`, `queue.journal`,
`queue.quick_check`, `queue.foreign_keys`, and `queue.health`. Database schema
details contain `version`; journal details contain `mode`; queue-health details
contain scalar `pending_items`, `in_progress_items`, `successful_items`,
`failed_items`, `unknown_status_items`, `bound_items`, and, when available,
`oldest_age_seconds`. When a SQLite header reports WAL, Doctor does not open the
database at all: it reports a journal warning and marks deeper checks
`not_applicable` to preserve WAL sidecars.

Exit codes:

- `0`: no failing checks (warnings are operator action items, not failures)
- `1`: one or more checks failed
- `2`: invalid command syntax

## Transaction Commands

Transaction commands inspect the SQLite database at `[storage].transaction_db_path`
from `rpacore.toml`. Pass `--db path/to/rpacore.db` to use a specific database.
Inspection opens an existing database through SQLite's read-only URI mode. It
does not create a missing database, migrate an older schema, rewrite a future
schema marker, or change journal mode. Missing or incompatible databases fail
with an actionable diagnostic; initialize or migrate storage through the
owning application version before inspecting it.

```bash
rpacore transaction list
rpacore transaction list --json
rpacore transaction show <transaction_id>
rpacore transaction show <transaction_id> --json
rpacore transaction export --format json
rpacore transaction export --format ndjson
```

`transaction list` returns the latest 100 transactions by default. Use
`--limit N` to choose a different cap.

### Machine-readable compatibility

The `--json` list and show envelopes are independently versioned by their
`command` and `schema_version` fields. Their version-2 framework-owned fields
are closed: a field cannot be added, removed, renamed, retyped, or given a new
meaning without a new schema version. The nested transaction is separately
versioned by `transaction_format_version`. Export follows the corresponding
version rules in the [Export Format Reference](export-format.md).

Human output is for operators. `--json` and `export` output are parseable on
stdout; diagnostics go to stderr. List and export select records through the
same normalized-UTC cursor pages as `query_transactions()`. Export keeps only
one page of summary records at a time, so it does not retain an unbounded
identifier snapshot or hold a SQLite read lock against checkpoints. Inserts
before the current cursor do not appear later; inserts after it can appear in a
later export page. A selected transaction deleted by concurrent cleanup before
its record is loaded is omitted by both list and export. Export has no
transaction-list cap and walks additional pages as needed.
