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

`execute_transaction(config=...)` exposes config to steps without automatically
applying Engine, storage or provider settings. Inject `credentials=` explicitly
to select a provider; omission preserves the env default. Provider construction
does not read secrets. See [configuration consumers](config.md#which-code-consumes-a-setting)
and the [existing-project edit](config.md#credential-selection-in-existing-projects).

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `CredentialProvider`, `EnvCredentialProvider`, `KeyringCredentialProvider`, `CredentialNotFoundError`, `build_credential_provider` | Resolve credentials from documented providers. | Environment/keyring reads depending on provider. Credentials are not persisted by RPA Core. |
| `configure_logger`, `get_logger`, `bind_log_context` | Configure stdlib logging and scoped correlation. | Successful configuration atomically replaces RPA Core-owned handlers while preserving application handlers. Invalid configuration leaves the logger unchanged. Context is restored when a binding scope exits. |
| `ArtifactReport`, `OutcomeReport`, `ReportRecord`, `StepReport`, `TransactionReport`, `generate_report`, `render_json`, `render_html`, `render_text` | Build and render transaction reports. | `TransactionReport.outcome` directly projects captured terminal category, retry disposition, and optional failure code; it does not infer them from status, history, or queue attempts. `TransactionReport.record` is an immutable, JSON-safe report-v2 record; `render_json()` returns its canonical JSON. Report generation snapshots JSON-safe nested data and exceptions; rendering has no file I/O. |
| `Notifier`, `EmailNotifier`, `WebhookNotifier`, `build_notifiers`, `dispatch` | Send notifications. | Dispatch gives each notifier an isolated report snapshot. SMTP or HTTP requests occur when configured. Payloads can contain sensitive transaction data. |

`dispatch(notifiers, report, *, logger=None, on_failure=None)` makes at most
three attempts per notifier, waiting 1 then 2 seconds after retryable failures.
Every attempt receives a fresh isolated report snapshot. It retries HTTP
408/429/500/502/503/504; SMTP response errors in 400-499, unexpected SMTP
disconnects, or all-recipient refusal with only 400-499 codes; socket timeouts,
connection errors and temporary DNS failures (`EAI_AGAIN`). For `URLError`, only
those socket errors in its exception-valued `reason` qualify. Other failures,
including permanent HTTP/SMTP responses, certificate verification errors and
unknown/string reasons, stop immediately. Direct notifier `send(report)` calls
remain single-attempt and propagate their errors. A direct webhook caller
owns the propagated `HTTPError` response: its body stays readable, and the
caller should close it when finished. Dispatch consumes and closes HTTP error
responses before retrying or swallowing terminal failure.

Retry warnings use `rpacore.notifier.retry` in JSON logs, with `notifier`,
`attempt`, `max_attempts` and `wait_seconds` attributes. Terminal errors use
`rpacore.notifier.error`, retaining exception evidence and actual attempt/max
attributes. `on_failure` receives the notifier class name once for a terminal
failure; successful retries do not call it. Ordinary notification, backoff and
callback failures are contained so later notifiers and queue processing can
continue. `MemoryError`, `KeyboardInterrupt` and `SystemExit` propagate.
Notification retries do not re-execute steps or change transaction outcomes or
transaction retry counts. Queue `notification_errors` counts terminal notifier
failures, rather than individual attempts.

This changes dispatch's previous single-attempt behavior: allow up to three
seconds of extra synchronous waits plus transport time. There is no total
deadline, configurable retry policy, jitter or `Retry-After` scheduling.
Delivery is best-effort; an ambiguous acknowledgement may cause duplicates,
while exhaustion or process exit may lose a notification. No durable outbox,
delivery history or replay is provided. Use receiver-side deduplication with
transaction identity, or own delivery directly through `send` when single-call
control is needed. No schema migration or examples-project update is required.

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

`render_html()` links only unambiguous local screenshot paths: relative paths
(including `./` and `../`), single-root POSIX paths, and fully qualified Windows
drive paths. Relative backslashes become separators; Windows drive paths become
local `file:///` targets. Filename spaces and URL delimiters are percent-encoded.
Explicit URLs (including `file:`), UNC/device paths, drive-relative or
drive-root-relative paths, authority-style prefixes, control characters, and
leading/trailing whitespace render as escaped text. Paths that cannot be
encoded as Unicode URL targets also remain text. Empty paths add no link.
Rendering does not check file existence or resolve paths against the working
directory; relative links use the report location as their base. Original
values remain intact in report JSON, text, and persisted exceptions.

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

Runner claim retries use `queue_claim_retry` (`rpacore.queue.claim.retry` in
JSON v3). A propagating run failure uses `queue_run_aborted`
(`rpacore.queue.run.aborted`) at ERROR, with partial claim/transition counts.
Normal returns retain the completion event above. See the
[runner failure policy](durability.md#runner-failure-policy) for the three-call
SQLite claim budget and the single-call custom-provider boundary.

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

Doctor format v1 check details include the optional `reason`, `cause`,
`location` and config `advisories` diagnostics documented in the
[CLI reference](cli.md#rpacore-doctor). These documented additions preserve the
envelope, check IDs and status vocabulary. Config advisories contain fixed
codes, known suggested setting names and counts; they contain no config values
or unknown application key names.

Query cursors are opaque. Pass a cursor only back to the matching
`query_transactions()` call; the framework accepts its supported cursor version
and rejects unsupported versions. JSON log v3 keeps a protected envelope and
confines event attributes to `attributes`. Doctor
consumers should use check `id`, `status`, and documented detail fields;
summary prose is for people rather than semantic parsing.

## Queue Processing

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `get_queue_item(item_id, db_path)` | Return a persisted `QueueItem`, or `None` if absent. IDs are opaque nonblank strings. | Always read-only; the database path is required. |
| `QueuePage`, `query_queue_items(db_path, *, statuses=None, cursor=None, limit=100)`, `query_queue_attempts(db_path, *, item_id=None, cursor=None, limit=100)`, `query_queue_poison_events(db_path, *, item_id=None, cursor=None, limit=100)` | One typed page of items, claim attempts or poison events. | Always read-only; never creates, migrates, reclaims or quarantines. Requires existing current-schema storage and refuses WAL before opening SQLite. |
| `QueueItem`, `QueueStatus`, `QueueLeaseLostError`, `QueueAdminEvent`, `QueueAttempt`, `QueueAttemptOutcome`, `QueuePoisonEvent`, `QueueProvider`, `SqliteQueue` | Queue item model, statuses, claim-loss error, audited overrides, attempt/disposition records, provider contract, and SQLite implementation. | `SqliteQueue` creates/migrates and mutates SQLite queue state. Every claim receives an opaque `claim_token`; claimed-worker mutations require the current token. Its `fail()` returns the durable `retry_scheduled` or `failed` attempt disposition; custom providers may return `None` when they cannot attest it. `list_attempts()` exposes one final outcome per post-v4 claim without fabricating history for older rows. |
| `QueueRunSummary`, `run_queue_loop` | Process claimed queue items through user factories and `Engine`. Fatal `MemoryError`, `KeyboardInterrupt`, and `SystemExit` signals propagate after deterministic cleanup. | `processed` and `failed` remain compatibility aggregates. Use `retry_scheduled`, `terminal_failed`, `lease_lost`, and `transition_unknown` for disposition truth; unknown is never inferred from a requested retry flag. Mutates queue and transaction SQLite databases; renews leases; checkpoints transactions when `transaction_db_path` is configured. Durable queue checkpoints require `SqliteQueue` and atomically validate its claim token plus transaction revision. Every post-claim exit stops and joins its lease heartbeat. |

`QueueItem.available_at: datetime | None` exposes the persisted next eligible UTC
instant; `None` means no outstanding delay. The field is appended after existing
constructor fields. Inserts accept only aware datetimes or `None` and normalize
them to UTC microseconds. Claims clear the field. `SqliteQueue` accepts
`retry_delay` (default 1 second, range 0–86400) and `retry_backoff` (default 2,
finite and at least 1) in its config dictionary. Boolean values are invalid.
Retryable failures, expired leases and retrying administrative overrides persist
`min(86400, retry_delay * retry_backoff ** (retry_count - 1))` seconds of delay,
rounded up to whole microseconds, with a minimum of one for positive delays. Zero selects immediate
redelivery. Terminal transitions clear the deadline. Settings apply to newly
scheduled retries; reopening with different settings retains stored deadlines.

`next_item()` claims the oldest currently eligible pending item, allowing later
ready work to bypass delayed items. `run_queue_loop()` still drains once and can
return with delayed pending work; schedule a later invocation to process it.
The queue clock is UTC wall time, so clock adjustments affect eligibility. This
adds no exactly-once guarantee and does not change Engine retry behavior.
Stored deadlines must use canonical UTC text with six fractional digits. A
selected noncanonical deadline raises `ValueError` before claiming or
quarantining that item; the queue never repairs it by erasing the deadline.

With `transaction_db_path`, the SQLite runner creates the initial pending
snapshot and queue binding in one commit, before executing steps. The new ID
must be a nonblank string and unused, including by revision-zero records. An ID
collision raises an execution validation failure and terminally fails the item without overwriting
the existing record. Initialization bypasses `SqliteQueue.bind_transaction()`
overrides; direct provider binding calls retain their behavior. Use the factory,
step code or `after_item` for application observation at those lifecycle points;
there is no transactional binding callback. No schema migration or
examples-repository update is required. Existing orphan records are retained.

If initial persistence rejects a blank/non-string or already-used ID,
`after_item` receives `transaction=None` and an error whose `__cause__` is an
`ExecutionValidationError`. The item fails terminally without queue retry. The
wrapper's private class is not part of the supported API.

`QueuePage[T]` has `records: tuple[T, ...]`, `has_more: bool`,
`next_cursor: str | None` and `format_version: int` (currently `2`). Limits are
integers from 1 through 1000. Item `statuses` takes a union of `QueueStatus`
values: `None` selects all, an empty iterable selects none, and repetitions or
reordering do not change cursor identity. Event `item_id` selectors use exact
equality; no selector means all events. Unknown selectors return an empty page.

Items sort by persisted `(created_at, id)` text ascending; events sort by
sequence ascending. Cursors are opaque, versioned and bound to the query kind
and normalized filters; malformed, unsupported or mismatched cursors raise
`ValueError`. Pass them back to the same query with the same filters. Each call
returns one page and closes its connection. Page format 2 adds the item deadline;
cursor format remains 1 because query positions and ordering are unchanged.
Saved v1 cursors remain accepted. Pages are current reads, not a
global snapshot: filter changes or new/backdated rows behind the cursor require
a fresh traversal. Complete enumeration requires quiescent storage.

Library reads return full records, including item **payloads and claim tokens**
and attempt **claim tokens**. Treat them as sensitive; the
[queue CLI](cli.md#queue-commands) uses minimized projections instead. Invalid
payloads fail item decoding without quarantine or partial results; attempts and
poison events remain independently inspectable. No attempt history is invented
for pre-tracking rows. Storage errors propagate without repair or migration.

## Manifest and Project Entrypoints

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `ProjectManifest`, `find_project_manifest`, `load_project_manifest`, `resolve_project_entrypoint` | Locate and load `rpacore.toml` and resolve the configured Python entrypoint. | Reads files, temporarily modifies `sys.path`, and replaces cached modules only within a conflicting entrypoint package namespace. Failed resolution restores that namespace; dependencies outside it retain normal Python import-cache side effects. |

## Artifacts and History

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `Artifact`, `HistoryEntry`, `HistoryEvent` | Durable records attached to transactions. | No I/O. Artifact records store paths and metadata, not file contents. |

The [CLI inspection surface](cli.md#machine-readable-compatibility) exposes the
existing query filters and stored outcomes without changing `TransactionSummary`
or the query API. For executable same-ID recovery with persisted arguments, see
the [tutorial](tutorial.md#same-id-recovery-from-a-disposable-scaffold).
