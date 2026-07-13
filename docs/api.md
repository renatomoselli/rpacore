# API Reference

The supported `0.1.x` public API is the top-level `rpacore` package. Prefer
imports such as:

```python
from rpacore import Engine, ProcessContext, Skill, Transaction
```

Only symbols exported by `rpacore.__all__` are covered by the public compatibility
contract. Public submodules remain implementation locations; see
[Public Submodule Policy](public-submodules.md).

## Execution

| Symbol | Purpose | Durable mutations and side effects |
| --- | --- | --- |
| `Engine` | Executes ordered skills in a `Transaction`. | Mutates transaction, skill statuses, history, timestamps, retry count, state, metadata, and artifacts in memory. Persists only when `checkpoint` calls a persistence function. |
| `execute_transaction` | Run one transaction with a `ProcessContext`, optional strict SQLite checkpoints, and optional runtime resources. | Mutates the transaction through `Engine.run()`. When `transaction_db_path` is set, creates or migrates the SQLite transaction database and checkpoints each transition. |
| `ProcessContext` | Runtime context passed to skills. | Carries durable `state`, runtime-only `resources`, config, and transaction reference. Resources are not serialized. |
| `Skill` | Base class for user-authored work units. | User subclasses implement `execute(ctx)`. Side effects belong to user code. |
| `Transaction` | Unit of execution and persistence. | Stores reference, status, skills, durable state, metadata, artifacts, and history. |
| `Status` | Transaction and skill status enum. | No side effects. |

`Engine.run(ctx, checkpoint=...)` validates wiring before user skill code runs.
When a checkpoint callback is supplied, it is called after each transaction or
skill state transition. Checkpoint failures propagate and stop execution.

Use `execute_transaction(transaction, transaction_db_path=...)` for ordinary
one-off runs that should persist strict SQLite checkpoints. Use raw
`Engine.run(ctx, checkpoint=...)` when you need to build the full
`ProcessContext` or custom persistence callback yourself.

## Exceptions

| Symbol | Purpose | Retry classification |
| --- | --- | --- |
| `BusinessException` | Expected business-rule failure. | Terminal for that skill unless user data or code changes. Downstream skills continue unless `stop=True`. |
| `SystemException` | Technical failure such as file, network, or service errors. | Retryable by `Engine(max_retries=...)`. |
| `ExecutionValidationError` | Invalid transaction wiring or invalid durable state. | Not retryable; fix code or persisted state. |

Unhandled exceptions from skill code are recorded as system failures.
`MemoryError` is not masked by checkpoint errors.

## Persistence, Serialization, and Recovery

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `save_transaction(transaction, db_path)` | Save one transaction to SQLite. | Creates or migrates the SQLite database and writes transaction rows. |
| `load_transaction(transaction_id, db_path, readonly=False)` | Load one transaction from SQLite. `readonly=True` requires an existing current-schema database and never migrates it. | Reads SQLite and preserves persisted status values; default mode can migrate older schemas. |
| `list_transactions(db_path, readonly=False)` | List persisted transactions. `readonly=True` requires an existing current-schema database and never migrates it. | Reads SQLite; default mode can migrate older schemas. |
| `resume_transaction(transaction_id, skills, db_path=...)` | Load and prepare a persisted transaction for retry. | Mutates in-memory statuses, reattaches executable skills, and appends resume history when needed. |
| `serialize_transaction(transaction)` | Convert a transaction to JSON-safe data. | No I/O. |
| `TRANSACTION_FORMAT_VERSION` | Current serialized transaction format version. | No side effects. |

## Configuration and Paths

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `load_config(path)` | Load and validate TOML configuration. | Reads a TOML file and resolves configured paths relative to it. |
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

The result is keyed by the same flat names, including dotted keys. Optional
defaults are validated like configured values. The helper does not mutate
`config`, resolve paths, access the filesystem, load secrets, interpolate
environment variables, or enforce cross-field rules.

## Credentials, Logging, Reports, and Notifications

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `CredentialProvider`, `EnvCredentialProvider`, `KeyringCredentialProvider`, `CredentialNotFoundError`, `build_credential_provider` | Resolve credentials from documented providers. | Environment/keyring reads depending on provider. Credentials are not persisted by RPA Core. |
| `configure_logger`, `get_logger` | Configure stdlib logging. | Mutates logger handlers/formatters. |
| `ArtifactReport`, `SkillReport`, `TransactionReport`, `generate_report`, `render_html`, `render_text` | Build and render transaction reports. | Report generation reads transaction data; rendering has no file I/O. |
| `Notifier`, `EmailNotifier`, `WebhookNotifier`, `build_notifiers`, `dispatch` | Send notifications. | SMTP or HTTP requests when configured. Payloads can contain sensitive transaction data. |

## Queue Processing

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `QueueItem`, `QueueStatus`, `QueueLeaseLostError`, `QueueProvider`, `SqliteQueue` | Queue item model, statuses, provider contract, and SQLite implementation. | `SqliteQueue` creates/migrates and mutates SQLite queue state. |
| `QueueRunSummary`, `run_queue_loop` | Process claimed queue items through user factories and `Engine`. Fatal `MemoryError`, `KeyboardInterrupt`, and `SystemExit` signals propagate after deterministic cleanup. | Mutates queue and transaction SQLite databases; renews leases; checkpoints transactions when `transaction_db_path` is configured. Every post-claim exit stops and joins its lease heartbeat. |

## Manifest and Project Entrypoints

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `ProjectManifest`, `find_project_manifest`, `load_project_manifest`, `resolve_project_entrypoint` | Locate and load `rpacore.toml` and resolve the configured Python entrypoint. | Reads files and imports the configured module during entrypoint resolution. |

## Artifacts and History

| Symbol | Purpose | Side effects |
| --- | --- | --- |
| `Artifact`, `HistoryEntry`, `HistoryEvent` | Durable records attached to transactions. | No I/O. Artifact records store paths and metadata, not file contents. |
