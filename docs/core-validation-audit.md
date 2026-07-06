# Core Validation Migration Audit

This audit records the decision on whether core modules should adopt the public
config validation helpers from `rpacore.config_validation`.

The helpers are the supported API for user projects validating plain
configuration dictionaries. Core modules may still use private validation and
formatting helpers when the validation is not plain config shape checking, when
the error class is domain-specific, or when adopting the public helper would
hide important behavior.

## Decisions

| Area | Decision | Rationale |
| --- | --- | --- |
| `rpacore.config` | Keep local validation. | Config loading normalizes values (`log_level`, `log_format`), rejects legacy `db_path`, resolves paths relative to the config file, and checks finite numeric values. The public helper does not express those rules without local follow-up logic that would make the code less clear. |
| `rpacore.credentials` | Keep local validation. | `build_credential_provider()` validates a single provider name, not a config mapping. Direct `type_error` / `value_error` calls are clearer than wrapping the value in a temporary dict. |
| `rpacore.notify` | Keep local validation. | Email and webhook validation is nested and has notifier-specific policy, including URL scheme checks, embedded credential redaction, timeout ranges, and screenshot attachment behavior. Keeping explicit checks preserves the security-sensitive error paths. |
| `rpacore.queue` | Migrated plain config type/default checks. | `SqliteQueue` uses `optional_config()` for `db_path`, `lease_timeout`, and `max_retries`, then keeps local range checks so existing messages and retry policy stay unchanged. |
| `rpacore.persistence` | Keep local validation. | Persistence validates durable JSON state, persisted metadata, artifact metadata, timestamps, and schema repair cases. Failures are raised as `SystemException` where operators must repair durable storage, not as plain config errors. |
| `rpacore.manifest` | Keep local validation. | Manifest validation has a closed top-level vocabulary, required section/key wording, entrypoint syntax checks, path resolution, and callable import validation. The public config helpers would not preserve those messages or phases directly. |
| transaction metadata and artifacts | Keep local model and JSON validation. | `Transaction`, `Artifact`, persistence, reports, notifications, and serialization need defensive copies, JSON-safety checks, stable timestamps, and content-free artifact records. These are durable model rules rather than config lookup rules. |
| SQLite schema introspection helpers | Keep local for now. | Queue and persistence each use tiny private schema helpers near their migration code. A shared private module would save a few lines but would also couple two independent schema lifecycles before more duplication exists. |

## Broad Exception Boundaries

Validation failures must stay explicit at runner boundaries because they affect
queue retry decisions.

- Transaction wiring and JSON state validation failures are terminal validation
  errors, not transient queue retry signals.
- Persistence metadata/artifact corruption raises `SystemException` with repair
  guidance so the runner treats it as an ordinary system failure unless the
  specific checkpoint policy has already made retry unsafe.
- Queue and SQLite operation failures remain classified at the runner
  transition/checkpoint boundaries, where transient lock/busy errors get short
  retries and non-transient failures stay loud.
- Notification and report validation failures are logged after execution and do
  not alter already-decided transaction outcomes.

The broad `except Exception` boundaries in `runner.py` remain intentional where
they convert ordinary failures into explicit queue dispositions or preserve
already-decided outcomes. They do not catch `MemoryError`.

## Future Rule

Use `rpacore.config_validation` in core code only when the value being validated
is genuinely a plain config mapping and the helper removes duplicated type,
default, choice, or range checks without changing the domain-specific exception
class or hiding retry/disposition policy.
