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

The destination basename becomes `[project].name` unchanged. It must start and
end with an ASCII letter or digit; interior characters may also include `.`,
`_`, and `-`. Single letters/digits and repeated separators are accepted. Parent
directories may contain spaces. Invalid basenames return exit code `2` with an
explanation on stderr before any directories or files are created. For example,
use `rpacore init my-project` instead of `rpacore init "my project"`. Existing
generated projects are unchanged and require no data migration. A valid package
name still requires an available filesystem destination.

The generated `main.py` uses `execute_transaction(transaction_db_path=...)`
with the project manifest's storage path, so execution and default inspection
refer to the same durable transaction history. It also supplies a stable
application-owned `definition_identity` so an interrupted run can be resumed
only by a compatible automation definition.
It constructs `credential_provider` from config with `build_credential_provider`
and passes it explicitly to execution. Construction reads no secret; the greeting
does not request credentials. Existing projects can adopt the
[provider-injection edit](config.md#credential-selection-in-existing-projects).

New scaffolds use `generated-greeting/v2`. Their default `greeting.txt` is
anchored to the project root, including runs from nested directories. The step
publishes a complete temporary sibling with `atomic_output_path` before recording
state/artifact metadata. Re-execution replaces the same greeting bytes rather
than appending; publication and the SQLite checkpoint are separate operations.
The output argument, state, and artifact store the same absolute destination.
Inspection and exports can therefore expose account/directory names. A copied
record retains its original destination on resume; a fresh run after moving the
project computes the new project root. Existing v1 projects/records are unchanged
and require no migration; v2 code must not be used to resume v1 records.

Newly generated entrypoints configure framework logging from `config.toml`'s
`log_level` and `log_format`; earlier scaffolds did not configure logging.
Invalid values are rejected by `load_config` before transaction execution.
Generated entrypoints treat `NOTSET` as `DEBUG`, enabling all framework levels
without inheriting the root logger's threshold. Text or JSON logs go
to stderr, including step-failure exception messages without content redaction.
After execution returns normally, stdout contains a human receipt with the
transaction ID, terminal status, resolved database path, project directory, and
`rpacore transaction show <actual-id>`. Run that inspection command from the
displayed project directory. A saved failed transaction receives the same
receipt and exits `1`; a saved successful transaction exits `0`. Log filtering
does not suppress the receipt.

If execution or a checkpoint raises an ordinary exception, the generated
entrypoint reports the attempted ID, database path, and inspection action on
stderr, with durable outcome explicitly unconfirmed. It then propagates the
original error. An in-memory successful status or completion log can precede
a failed final save; inspect the database before deciding how to recover.
Inspection may find no record or an earlier checkpoint. A second plain
`rpacore run` starts a new transaction with a new ID; it does not resume the
previous record. For application-owned same-ID continuation, follow the
[disposable recovery recipe](tutorial.md#same-id-recovery-from-a-disposable-scaffold).

`SystemExit` and `KeyboardInterrupt` propagate directly without a generated
receipt or abort diagnostic. An exit code alone does not confirm persistence;
use read-only transaction inspection after an interrupted run.

Receipts expose local project/storage paths but omit state, arguments, config
values, artifact contents, and exception payloads. Framework and CLI exception
logs still follow the [security guidance](security.md). These behaviors apply
to future scaffolds; existing projects require no migration. Arbitrary
entrypoints continue to own their output and exit semantics.

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

For generated entrypoints, see the
[generated-run output contract](#rpacore-init-project_name) for stdout receipts,
stderr logs, and unconfirmed-durability diagnostics.

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
Direct `--transaction-db` and `--queue-db` flags select explicit storage;
missing paths fail. `--config` selects an explicit configuration file whose
absence or invalidity fails. A queue database obtained from a valid config,
including one selected with `--config`, is discovered storage. Absent discovered
storage is `not_applicable`: it is unchecked and may not yet exist or may have
been removed. Verify the configured location and recovery/backup for established
deployments before running work that could create a new database. Absence does
not establish health or intact data. Missing default manifest/config inputs are
also `not_applicable`; found unreadable/invalid inputs and existing malformed,
incompatible, directory, or unreadable databases fail. Database checks cover current
schema compatibility, journal mode, SQLite `quick_check`, foreign-key integrity,
and bounded queue counts/claim-binding facts. The JSON result is doctor format
v1 with `pass`, `warning`, `fail`, or `not_applicable` check statuses. JSON
stdout contains one document only.

Doctor format v1 always emits these check IDs: `runtime.python`,
`runtime.sqlite`, `project.manifest`, `project.config`,
`transactions.schema`, `transactions.journal`, `transactions.quick_check`,
`transactions.foreign_keys`, `queue.schema`, `queue.journal`,
`queue.quick_check`, `queue.foreign_keys`, and `queue.health`. Database schema
details contain `version` when checked. An absent discovered database's schema
check instead has optional `reason="missing_discovered_database"`; no-selection,
deeper skipped checks, and WAL checks do not carry that reason. Failed manifest
and config checks have optional `cause`: `toml_syntax`, `invalid_shape`,
`invalid_type`, `invalid_value`, `unreadable`, or `unexpected`. TOML syntax errors
also have optional `location`, normalized as `line N, column M` or
`end of document` when the parser supplies a recognized location. Unrecognized
locations are omitted. Human summaries show these same safe causes/locations,
without validator messages, offending values, or source excerpts.
Journal details contain `mode`; queue-health details
contain scalar `pending_items`, `in_progress_items`, `successful_items`,
`failed_items`, `unknown_status_items`, `bound_items`, and, when available,
`oldest_age_seconds`. When a SQLite header reports WAL, Doctor does not open the
database at all: it reports a journal warning and marks deeper checks
`not_applicable` to preserve WAL sidecars.

A valid `project.config` can report `warning` with optional
`details.advisories`. A typo entry has exactly
`code="possible_framework_key_typo"`, a known dotted `suggested_key`, and
positive integer `count` (the number of unrecognized names suggesting that
setting). Entries are sorted by suggested key. A final
`{"code": "transaction_path_mismatch"}` entry warns that explicit config and
manifest transaction paths differ; it contains no paths. Human summaries show
every suggestion/count and explain manifest storage authority for generated or
CLI-managed projects versus explicit library storage choice. Clean config
checks retain their existing empty details.

These are [nonfatal heuristics](config.md#static-guidance-before-execution),
not evidence that an application's config is unused. Unknown keys remain valid;
doctor emits no unknown names, values or raw paths. It cannot validate callable
availability, definition identity or step wiring without running user code.
An internal diagnostic failure instead returns exit `1`, leaves stdout empty
even with `--json`, and reports `Could not complete doctor diagnostics` on
stderr. It does not label that framework failure as an invalid config file or
emit exception details.

Exit codes:

- `0`: no failing checks (warnings are operator action items, not failures)
- `1`: one or more checks failed
- `2`: invalid command syntax

## Queue Commands

```bash
rpacore queue list --db path/to/queue.db --json
rpacore queue show ITEM_ID --config path/to/config.toml
rpacore queue attempts [ITEM_ID] --db path/to/queue.db --json
rpacore queue poison [ITEM_ID] --db path/to/queue.db --json
rpacore queue list --status pending --status failed --limit 20 --cursor TOKEN
```

All four commands inspect existing storage without creating, migrating,
reclaiming expired leases, quarantining malformed payloads, or repairing it.
The queue schema must already be current, and all framework components must be
compatible. WAL databases are refused before SQLite is opened, preserving their
sidecars; locks and hot journals requiring recovery fail visibly. Migrate or
recover through the owning application before inspecting.

Every command accepts `--db`, `--config`, and `--json`. Selection order is:

1. `--db` selects a working-directory-relative or absolute queue database and
   bypasses all manifest/config loading, even when `--config` is supplied.
2. Otherwise `--config` selects a working-directory-relative or absolute config
   and needs no project manifest.
3. Otherwise discover the nearest `rpacore.toml` searching upward from the
   working directory, validate it without importing the entrypoint, and use its
   sibling `config.toml`.

The selected config must contain `[queue].db_path`; relative queue paths resolve
against that config's directory. There is no implicit working-directory config
or queue fallback. Missing selection, malformed manifest, missing/unreadable/
invalid config and missing selected storage fail with `1`. Unlike
`doctor --queue-db`, queue inspection uses `--db`, bypasses config completely
when it is supplied, and always fails for missing selected storage.

`list` accepts repeated `--status` values forming a union of `pending`,
`in_progress`, `successful`, and `failed`; omission selects all items. `show`
requires an ID; `attempts` and `poison` accept an optional exact ID selector.
IDs are opaque nonblank strings; hex, hyphenated and application IDs are valid.
An absent `show` item returns `1`; empty queries, including events for unknown
IDs, succeed with `0`.

The three page commands accept `--limit` (default 100, range 1–1000) and
`--cursor`. Each invocation returns one page without auto-draining. Items sort
by persisted `(created_at, id)` text ascending, without UTC normalization;
events sort by sequence ascending. Human output identifies truncation and gives
the continuation token. Continue with the same filters while `has_more` is true.
Each page is a point-in-time read, not a global snapshot: claims can remove
eligible rows; new/backdated rows or items entering a filter behind the cursor
require a fresh traversal. Complete audit enumeration needs quiescent storage.

Invalid status, limit or blank ID arguments return `2` before storage selection.
Malformed, unsupported, wrong-kind or changed-filter cursors return `1`, as do
storage, decoding and serialization failures. Failures leave stdout empty;
bounded error categories and corrective guidance go to stderr without raw
config, stored diagnostic content or exception messages. Item decode failures
do not quarantine data; attempts/poison can still be inspected independently.

### Queue machine-readable compatibility

Queue JSON has its own `schema_version: 2`, independent of transaction/export
formats. Page envelopes have exactly these fields:

```json
{"schema_version":2,"command":"queue:list","limit":100,"records":[],"has_more":false,"next_cursor":null}
```

`command` is `queue:list`, `queue:attempts` or `queue:poison`; `limit` is an
integer, `records` an array, `has_more` a boolean and `next_cursor` a string when
more records exist, otherwise null. Show has exactly `schema_version`,
`command: "queue:show"`, and `item` (one item projection).

| Record | Exact fields and types |
| --- | --- |
| Item (`list` and `show`) | `id`, `reference`, `status`, `created_at`, `claimed_by`, `transaction_id`: strings; `retry_count`: integer; `claimed_at`, `available_at`: string or null. `status` is `pending`, `in_progress`, `successful` or `failed`. Empty worker/transaction strings mean unassigned/unbound. |
| Attempt | `sequence`: integer; `item_id`, `claimed_by`, `started_at`: strings; `finished_at`: string or null; `outcome`: string or null. Outcomes are `successful`, `retry_scheduled`, `failed`, `lease_expired`, `admin_override`; null denotes an open attempt. |
| Poison event | `sequence`: integer; `item_id`, `error_type`, `created_at`: strings. `error_type` is the recorded parser error type, not an error message. |

Timestamps are ISO-8601 strings from persisted records. Projections omit payloads,
claim tokens, configuration and raw error messages in both human and JSON output.
Allowlisted references, IDs and worker names can still contain application data;
protect captured output. Full library records carry payloads/tokens. Cursors
contain no payload or token and are versioned continuation positions, not
authorization or completeness guarantees. Framework fields are closed for each
version; consumers should check `schema_version`. Checked compatibility examples
live under `tests/fixtures/record-contracts/cli-queue-*-v2.json`; v1 files remain
historical examples. Consumers upgrading from v1 must accept schema 2 for all
four queue commands and read the added nullable item `available_at` field.
Attempt/poison fields and envelope structure are unchanged. Cursor format remains
v1, so saved query positions remain usable; library pages report format 2.

Human list/show output includes `available_at` (the next eligible UTC instant),
or `(none)` when no delay is outstanding. A pending item can be delayed: it is
still listed and does not become claimed merely because inspection observes it.
A drain-once worker may have returned normally while delayed work remains;
invoke it again at or after this instant through your scheduler or supervisor.

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

`transaction list` returns the latest 100 selected summaries by default.
`--limit N` accepts any positive cap, including values above 1,000; each query
fetches at most 1,000 summaries before loading full records read-only.

List options:

| Option | Meaning |
| --- | --- |
| `--status VALUE` | Repeat for a union of `pending`, `in_progress`, `successful`, `failed`, `skipped`. |
| `--since TIME`, `--until TIME` | Inclusive ISO-8601 creation bounds with an offset or `Z`; normalized to UTC. |
| `--reference TEXT` | Exact reference. |
| `--metadata KEY=JSON` | Repeat for exact top-level metadata matches; JSON types remain distinct. |
| `--run-id TEXT` | Shorthand for the string-valued `run_id` metadata match. Creates no run ID or grouping entity. |
| `--cursor TOKEN` | Continue the same filters from the previous page. |

Different filters combine with AND. Repeated metadata keys are usage errors,
including repeated `--run-id` or `run_id` supplied through both `--metadata` and
`--run-id`.
`--run-id example` and `--metadata 'run_id="example"'` share cursor semantics.
In PowerShell, quote the entire `KEY=JSON` argument so the JSON reaches the CLI
unchanged. Naive/invalid timestamps, inverted windows, invalid statuses and
non-JSON-safe metadata return `2` before storage access; validation diagnostics
omit metadata values. Invalid/changed-filter cursors and storage, record or
serialization failures return `1` with diagnostics on stderr and no partial
JSON stdout. Normal completion returns `0`.

```bash
rpacore transaction list --run-id example --status failed --limit 20 --json
rpacore transaction list --run-id example --status failed --limit 20 --cursor TOKEN --json
rpacore transaction list --since 2026-10-15T09:00:00-03:00 --until 2026-10-15T12:00:00Z
rpacore transaction export --transaction-id ID --format json
```

Human list/show display the captured outcome category, retry disposition, and
failure code (including `unknown` and an empty code). These values are read
from the transaction, never inferred from its last step or exception text. A
failed transaction can contain a later successful step. `--run-id` is only an
exact match against application metadata; applications choose their own values.
Human continuation instructions apply whether the database was selected with
`--db` or resolved from the project manifest.

### Machine-readable compatibility

List and show now emit `schema_version: 3` to expose stored outcome truth
without changing the closed canonical transaction-v3 record. These framework
fields are closed and independently versioned from the nested transaction:

```json
{
  "schema_version": 3,
  "command": "transaction:list",
  "limit": 100,
  "transactions": [],
  "has_more": false,
  "next_cursor": null
}
```

Each list entry has exactly `transaction` (canonical transaction-v3) and
`outcome` (`category`, `retry_disposition`, `failure_code`, all strings).
Show has exactly `schema_version`, `command` (`transaction:show`), `transaction`,
and `outcome` with the same meanings. Consumers migrating from CLI v2 must check
`schema_version`, unwrap list entries as `entry.transaction` instead of treating
entries as transaction records, keep show access at `payload.transaction`, and
read the new outcome beside the record. There is no compatibility-mode flag.
SQLite schema 10, query/cursor-v1, transaction-v3, export-v2, and report-v2 remain
unchanged; this CLI consumer migration requires no database migration.

Continue using the same filters and the returned `next_cursor` while
`has_more` is true. Human list output supplies the token and continuation
instructions. Selected summaries count against `--limit`; concurrent cleanup
can shorten the rendered list, without refilling it or building a cursor from
rendered records. Null/naive legacy creation times sort after timestamped rows
and do not match time windows. Pages are current reads, not one global snapshot.

Human output is for operators. `--json` and `export` output are parseable on
stdout; diagnostics go to stderr. List and all-record export select records
through the same normalized-UTC cursor pages as `query_transactions()`. Queries
release their connections before full-record loads, avoiding an inspection-wide
read lock against checkpoints. Inserts before the cursor do not appear later;
inserts after it can appear in a later page. A selected summary deleted before
its record load is omitted. All-record export has no list cap.

`transaction export --transaction-id ID` loads exactly that ID read-only.
Blank/whitespace-only IDs return `2` before storage access; other IDs are passed
unchanged. A missing/deleted ID or unavailable/incompatible database returns `1`
with empty stdout; it never falls back to all records or a successful empty
export. Both JSON and NDJSON retain export format v2. See the
[Export Format Reference](export-format.md) for buffering and sensitive data.
