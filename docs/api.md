# API Reference

The supported API is exposed from the top-level `rpacore` package. Prefer
imports such as:

```python
from rpacore import Engine, ProcessContext, Step, Transaction
```

Only symbols exported by `rpacore.__all__` are covered by the public compatibility
contract. Public submodules remain implementation locations; see
[Public Submodule Policy](public-submodules.md).

The package includes PEP 561 type information. Mypy is the supported static
checker for this release line. When `require_config`, `optional_config`, or the
matching `ProcessContext` accessors receive one concrete runtime type such as
`str` or `int`, their result is typed as that type. Tuple-type and untyped
lookups remain `object`; durable JSON-safe data does not have a public recursive
type alias.

## Execution

| Symbol | Purpose | Durable mutations and side effects |
| --- | --- | --- |
| `Engine` | Executes ordered steps in a `Transaction`. | Mutates transaction, step statuses, history, timestamps, retry count, state, metadata, and artifacts in memory. Persists only when `checkpoint` calls a persistence function. |
| `ExecutionTransition` | Frozen execution-transition-v2 fact emitted by an optional `Engine.run()` sink. | Contains closed lifecycle truth and only explicitly allowlisted checkpoint state. `checkpoint_state` and `to_record()` return fresh detached dictionaries; the fact performs no I/O. |
| `execute_transaction` | Run one transaction with a `ProcessContext`, optional strict SQLite checkpoints, and optional runtime resources. | Mutates the transaction through `Engine.run()`. When `transaction_db_path` is set, requires a non-empty definition identity before opening the database, then creates or migrates SQLite storage and checkpoints each transition. |
| `ProcessContext` | Runtime context passed to steps. | Carries durable `state`, runtime-only `resources`, config, and transaction reference. Resources are not serialized. |
| `Step` | Base class for user-authored work units. | User subclasses implement `execute(ctx)`. Side effects belong to user code. |
| `Transaction` | Unit of execution and persistence. | Stores reference, caller-owned automation `definition_identity`, lifecycle status, terminal outcome/retry truth, steps, durable state, metadata, artifacts, and history. Validates wiring and JSON-safe durable data before execution or persistence. |
| `Status` | Transaction and step status enum. | No side effects. |
| `OutcomeCategory`, `RetryDisposition` | Stable terminal work reason and actual retry decision. | No side effects. `unknown` represents legacy or incomplete truth; it is never inferred from message text or retry count. |

`Engine.run(ctx, checkpoint=...)` validates wiring and JSON-safe transaction
state, metadata, step arguments, and artifact metadata before user step code
runs. When a checkpoint callback is supplied, it is called after each
transaction or step state transition. Checkpoint failures propagate and stop
execution.

Use `execute_transaction(transaction, transaction_db_path=...)` for ordinary
one-off runs that should persist strict SQLite checkpoints. Use raw
`Engine.run(ctx, checkpoint=...)` when you need to build the full
`ProcessContext` or custom persistence callback yourself.

### Execution transition sink

`Engine.run()` accepts optional keyword-only `transition_sink` and
`transition_state_fields` arguments. The sink receives one
`ExecutionTransition` after each lifecycle mutation and history append, before
the matching strict checkpoint callback. The sink is synchronous: if it raises,
the exception propagates, user execution stops before the next enforceable
effect boundary, and the already-applied in-memory mutation is not rolled back.
If the sink raises while another exception is already being handled, the sink
exception supersedes it; the lifecycle status and history already applied remain
available in memory.
No retry, replay, transport, persistence, or delivery guarantee is implied.

`transition_state_fields` is an explicit allowlist of non-empty, unique state
keys. It requires a sink and is validated before transaction mutation. Selected
values must be JSON-safe, are detached at emission time, and cannot be changed
by later transaction or decoded-record mutation. Selected keys absent from
`transaction.state` are omitted. Config, metadata, resources, credentials,
exceptions, paths, and retry disposition are never copied automatically.

Execution transition format v2 is a closed 17-field record: `schema_version`,
`transition_id`, `sequence`, `occurred_at`, `event`, `transaction_id`,
`transaction_reference`, `definition_identity`, `transaction_status`,
`execution_pass`, `step_name`, `step_execution_order`, `step_status`,
`outcome_category`, `failure_code`, `retry_recommended`, and
`checkpoint_state`. `retry_recommended` is a Core classification fact, not a
durable retry decision. Consumers should call `to_record()` and reject schema
versions they do not support.

## Exceptions

| Symbol | Purpose | Retry classification |
| --- | --- | --- |
| `BusinessException` | Expected business-rule failure. | Terminal for that step unless user data or code changes. Downstream steps continue unless `halts_remaining_steps=True`. |
| `SystemException` | Technical failure such as file, network, or service errors. | Retryable by `Engine(max_retries=...)`. |
| `ExecutionValidationError` | Invalid transaction wiring or invalid durable state. | Not retryable; fix code or persisted state. |
| `DefinitionIdentityError` | Missing, invalid, changed, unidentified, or incompatible automation definition identity. | Not retryable with the current definition; supply the exact compatible identity or start a new transaction. |
| `TransactionFenceError` | A durable queue checkpoint has a stale claim token or transaction revision. | Not retryable by that worker; stop and reacquire the queue item. |

Unhandled exceptions from step code are recorded as system failures.
`MemoryError` is not masked by checkpoint errors.

`BusinessException` and `SystemException` accept an optional `code=`. Codes are
empty or lowercase dot-separated ASCII namespaces, such as
`acme.invoice.missing_number`; `rpacore.*` is reserved for framework-owned
causes. The Engine records its terminal transaction outcome separately from
`Status`: success is `successful`/`not_applicable`, a terminal business or
validation result is `not_requested`, and a system failure after the Engine's
configured retry budget is `retry_exhausted`. Interrupted and legacy/incomplete
records remain explicit rather than guessed. Validation failures receive their
in-memory outcome before they are re-raised, but strict persistence still does
not create a transaction row for invalid wiring or durable data.

## Persistence, Serialization, and Recovery

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `save_transaction(transaction, db_path)` | Validate and unconditionally save one non-queue transaction to SQLite. | Invalid wiring or durable data fails before the database is opened; valid input creates or migrates the database, writes transaction rows, and advances its persistence revision. A transaction's first persisted definition identity, including an empty inspection-only identity, is immutable. A non-successful record first saved without an identity cannot later be resumed. Queue runners use a fenced internal checkpoint instead. |
| `load_transaction(transaction_id, db_path, readonly=False)` | Load one transaction from SQLite. `readonly=True` requires an existing current-schema database and never migrates it. | Reads SQLite and preserves persisted status values; default mode can migrate older schemas. |
| `list_transactions(db_path, readonly=False)` | List persisted transactions. `readonly=True` requires an existing current-schema database and never migrates it. | Reads SQLite; default mode can migrate older schemas. A selected transaction removed by concurrent cleanup before deferred load is omitted; other load failures propagate. |
| `TransactionSummary`, `TransactionPage`, `query_transactions(...)` | Versioned, lightweight transaction query page. | Read-only by default and requires a current schema. Supports exact status-set, UTC window, reference, and top-level metadata filters. Results are ordered by normalized UTC creation time descending then id ascending; opaque cursors are bound to those filters. |
| `resume_transaction(transaction_id, steps, db_path=..., definition_identity=...)` | Load, validate, and prepare a persisted transaction for retry. | Requires an exact identity match for non-successful records before any in-memory mutation, reattaches executable steps, preserves history-proven skips caused by a halting business failure, and appends resume history when needed. Successful records return unchanged as an idempotent no-op. |
| `serialize_transaction(transaction)` | Convert a transaction to a JSON-safe format-v3 record. | Includes `definition_identity`, validates durable JSON fields without requiring executable wiring, and performs no I/O. |
| `TRANSACTION_FORMAT_VERSION` | Current serialized transaction format version (`3`). | No side effects. |

## Configuration and Paths

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `load_config(path)` | Load and validate TOML configuration. | Reads a TOML file and resolves database and non-empty screenshot paths relative to it. |
| `optional_config`, `require_config`, `require_section` | Validate config dictionaries. | No I/O. |
| `ConfigField`, `validate_config` | Validate immutable flat field specifications, including dotted nested keys, and return a plain dictionary. | No mutation or I/O. Path, filesystem, and cross-field rules remain explicit Python. |
| `resolve_config_path`, `resolve_config_paths` | Resolve path values from config. | No I/O beyond path normalization. |
| `atomic_output_path` | Yield a temporary sibling path and publish it to the destination only after the writer succeeds. | Creates a temporary file beside the destination, fsyncs the temporary file, replaces the destination with `os.replace()`, and removes failed temporary files. |

Use scalar helpers for one or two values. For repeated validation, define a
flat immutable specification and keep non-local rules in ordinary Python:

```python
from rpacore import ConfigField, validate_config

values = validate_config(
    config,
    (
        ConfigField("log_level", str, choices=("INFO", "ERROR")),
        ConfigField("queue.lease_timeout", int, min_value=1),
        ConfigField("max_retries", int, required=False, default=0, min_value=0),
    ),
)
```

The result is keyed by the same flat names, including dotted keys. Declarations
are validated when constructed; choices are frozen for reuse, and JSON-safe
defaults are independently copied for each missing-value result. Optional
fields without a default omit their result key. `default=None` is explicit and
must be included in the declared expected type. Defaults use Python's standard
JSON encoder with non-finite floats disallowed; values such as `datetime`,
`Decimal`, sets, bytes, and arbitrary objects are rejected. The helper does not
mutate `config`, resolve paths, access the filesystem, load secrets, interpolate
environment variables, or enforce cross-field rules.

## Credentials, Logging, Reports, and Notifications

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `CredentialProvider`, `EnvCredentialProvider`, `KeyringCredentialProvider`, `CredentialNotFoundError`, `build_credential_provider` | Resolve credentials from documented providers. | Environment/keyring reads depending on provider. Credentials are not persisted by RPA Core. |
| `configure_logger`, `get_logger`, `bind_log_context` | Configure stdlib logging and scoped correlation. | Successful configuration atomically replaces RPA Core-owned handlers while preserving application handlers. Invalid configuration leaves the logger unchanged. Context is restored when a binding scope exits. |
| `ArtifactReport`, `OutcomeReport`, `ReportRecord`, `StepReport`, `TransactionReport`, `generate_report`, `render_json`, `render_html`, `render_text` | Build and render transaction reports. | `TransactionReport.outcome` directly projects captured terminal category, retry disposition, and optional failure code; it does not infer them from status, history, or queue attempts. `TransactionReport.record` is an immutable, JSON-safe report-v2 record; `render_json()` returns its canonical JSON. Report generation snapshots JSON-safe nested data and exceptions; rendering has no file I/O. |
| `Notifier`, `EmailNotifier`, `WebhookNotifier`, `build_notifiers`, `dispatch` | Send notifications. | Dispatch gives each notifier an isolated report snapshot. SMTP or HTTP requests occur when configured. Payloads can contain sensitive transaction data. |

`ReportRecord` is report format v2: a frozen JSON-safe record formed once by
`generate_report()`. `render_json()`, `render_text()`, and `render_html()` all
derive their output from that record, so later mutation of the legacy
`TransactionReport` convenience fields cannot change rendered operator truth.
Its embedded transaction-v3 snapshot includes `definition_identity` and uses
the current step vocabulary.
The decoded record has `complete` and `errors` fields. If canonical transaction
serialization or record encoding cannot complete, it remains renderable with
`complete: false`, a stable `rpacore.report.*` error code, and only the
available transaction header; it never changes the transaction outcome.

`WebhookNotifier` preserves its existing payload by default. Set
`notification.webhook.include_report = true` to add a `report` member
containing report format v2. This is opt-in because reports include existing
diagnostic metadata, artifact paths, and exception details.

### Logging format contract

`TextFormatter` emits human-readable sections separated by ` | `. Exception
tracebacks and explicit stack information are appended as additional sections
when present, so consumers must not assume a fixed number of delimiters. Use
`JsonFormatter` for machine parsing.

JSON log format v3 uses a protected envelope with `log_format_version`, UTC
`timestamp`, `severity`, `logger`,
namespaced `event`, `message`, and nested `attributes`. Exception details use
`exception.type`, `exception.message`, and `exception.stacktrace`; explicit
stack information is `stacktrace`. V3 intentionally keeps attributes separate
from the envelope and omits automatic config, credential, resource, state,
metadata, path, and URL fields. Framework event names are prefixed with `rpacore.` and use dot
separators; for example, `queue_run_completed` is
`rpacore.queue.run_completed` in v3. Old JSON log format selection is no longer
available.

`bind_log_context()` temporarily attaches approved scalar correlation fields
such as transaction, queue-item, worker, step, retry, and attempt identifiers.
Nested scopes restore the prior context even when execution raises. Context is
local to the current execution context: code that starts a new thread must bind
the identifiers it needs in that thread. `get_logger("my_automation")` returns
an `rpacore.application.my_automation` child, so a configured `rpacore` root
captures both framework and application events without duplicate handlers.

### Versioned record compatibility

Canonical transaction, execution-transition, query-page/cursor, report, doctor,
CLI, export, and log records are framework-owned contracts. For a given version,
their documented framework fields are closed: adding, removing, renaming,
retyping, or changing the meaning of one requires a new version. Existing
conditional fields retain their documented conditions; they do not create a
general extension point.

Query cursors are opaque. Pass a cursor only back to the matching
`query_transactions()` call; the framework accepts its supported cursor version
and rejects unsupported versions. JSON log v3 keeps a protected envelope and
confines event attributes to `attributes`. Doctor
consumers should use check `id`, `status`, and documented scalar detail fields;
summary prose is for people rather than semantic parsing.

## Queue Processing

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `QueueItem`, `QueueStatus`, `QueueLeaseLostError`, `QueueAdminEvent`, `QueueAttempt`, `QueueAttemptOutcome`, `QueuePoisonEvent`, `QueueProvider`, `SqliteQueue` | Queue item model, statuses, claim-loss error, audited overrides, attempt/disposition records, provider contract, and SQLite implementation. | `SqliteQueue` creates/migrates and mutates SQLite queue state. Every claim receives an opaque `claim_token`; claimed-worker mutations require the current token. Its `fail()` returns the durable `retry_scheduled` or `failed` attempt disposition; custom providers may return `None` when they cannot attest it. `list_attempts()` exposes one final outcome per post-v4 claim without fabricating history for older rows. |
| `QueueRunSummary`, `run_queue_loop` | Process claimed queue items through user factories and `Engine`. Fatal `MemoryError`, `KeyboardInterrupt`, and `SystemExit` signals propagate after deterministic cleanup. | `processed` and `failed` remain compatibility aggregates. Use `retry_scheduled`, `terminal_failed`, `lease_lost`, and `transition_unknown` for disposition truth; unknown is never inferred from a requested retry flag. Mutates queue and transaction SQLite databases; renews leases; checkpoints transactions when `transaction_db_path` is configured. Durable queue checkpoints require `SqliteQueue` and atomically validate its claim token plus transaction revision. Every post-claim exit stops and joins its lease heartbeat. |

## Manifest and Project Entrypoints

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `ProjectManifest`, `find_project_manifest`, `load_project_manifest`, `resolve_project_entrypoint` | Locate and load `rpacore.toml` and resolve the configured Python entrypoint. | Reads files, temporarily modifies `sys.path`, and replaces cached modules only within a conflicting entrypoint package namespace. Failed resolution restores that namespace; dependencies outside it retain normal Python import-cache side effects. |

## Artifacts and History

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `Artifact`, `HistoryEntry`, `HistoryEvent` | Durable records attached to transactions. | No I/O. Artifact records store paths and metadata, not file contents. |
