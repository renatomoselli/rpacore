# Configuration Reference

RPA Core uses two small TOML files.

## `rpacore.toml`

`rpacore.toml` is the project manifest consumed by the CLI. For a generated or
CLI-managed project, its transaction path is the storage authority for the
generated entrypoint, default transaction commands, and default doctor checks:

```toml
[project]
entrypoint = "main:main"

[storage]
transaction_db_path = "rpacore.db"
```

`[project].entrypoint` is a `module:callable` string. The callable is invoked by
`rpacore run`.

Relative paths are resolved relative to the manifest file.

## `config.toml`

`config.toml` is loaded by user code with `load_config()`:

```toml
max_retries = 2
retry_delay = 0.0
retry_backoff = 1.0
log_level = "INFO"
log_format = "text"
transaction_db_path = "rpacore.db"
screenshot_dir = ""
credential_provider = "env"

[queue]
db_path = "queue.db"
lease_timeout = 30
max_retries = 3
retry_delay = 1.0
retry_backoff = 2.0
```

Relative `transaction_db_path`, `screenshot_dir`, and `queue.db_path` values are
resolved relative to the config file. The exact empty `screenshot_dir = ""`
value remains unchanged and disables automatic screenshots. Whitespace-only
values and the SQLite `:memory:` sentinel, with or without surrounding
whitespace, are invalid screenshot directories.
Absolute paths and parent-directory components remain supported operator-owned
filesystem paths; config path resolution is not a filesystem sandbox.
Direct library calls may use `config.toml`'s `transaction_db_path`; RPA Core
does not infer that an arbitrary entrypoint uses its config. New generated
projects instead keep their declared transaction path only in `rpacore.toml`.

### Which code consumes a setting?

Loading configuration does not automatically configure execution. Application
code makes these choices explicitly:

| Consumer | What it does |
| --- | --- |
| `load_config` | Loads, validates known values, preserves custom keys and normalizes paths. |
| Generated `main.py` | Builds Engine, logger and credential provider from config; uses manifest transaction storage. |
| Library application | Chooses Engine options, provider and checkpoint storage; may use config's transaction path. |
| `SqliteQueue(config["queue"])` | Consumes queue settings when that section is passed to its constructor. |
| `build_notifiers(config, credentials)` | Consumes notification sections only when called; the application chooses when to dispatch. |

Root `max_retries`, `retry_delay` and `retry_backoff` configure Engine retries
only when supplied to Engine. Queue `max_retries` instead controls queue
redelivery; `lease_timeout` controls claim lifetime. Queue `retry_delay` and
`retry_backoff` control durable delivery timing, independently of the root Engine
settings. Passing `config=` to `execute_transaction` makes it available to
steps; it does not build an Engine or select storage or a credential provider.

Queue delay defaults to 1 second and backoff to 2. For delivery retry number `n`
(starting at 1), the delay is `min(86400, retry_delay * retry_backoff ** (n - 1))`
seconds. Both values accept finite numbers excluding booleans; delay must be
between 0 and 86400 and backoff at least 1. Positive delays have a minimum of one
microsecond and round up to whole microseconds to avoid early eligibility.
Set `[queue].retry_delay = 0` to retain immediate redelivery.
These settings are validated by `SqliteQueue` before opening storage; loading a
TOML section alone does not instantiate a queue or validate its retry policy.

Retryable failure, lease expiry and `force_fail(retry=True)` persist the next
eligible UTC instant. Changing the constructor settings does not alter existing
deadlines. A drain-once runner returns when no work is currently eligible; arrange
a later invocation through your external scheduler or supervisor. It does not
wait for delayed pending items. Queue-backed examples and applications that
previously drained all retries in one invocation must adopt later invocations or
explicitly select zero delay; their files are not changed automatically.

### Credential selection in existing projects

New generated entrypoints construct the configured `env` or `keyring` provider
and inject it before durable work. Existing projects remain user-owned code.
To adopt this behavior, add the factory import and explicit injection to your
entrypoint, keeping its other execution arguments:

```python
from rpacore import build_credential_provider, execute_transaction

credentials = build_credential_provider(str(config["credential_provider"]))
execute_transaction(
    transaction,
    config=config,
    credentials=credentials,
    engine=engine,
    transaction_db_path=manifest.transaction_db_path,
)
```

This example uses the existing transaction, Engine and manifest from a generated
entrypoint; keep your application's own checkpoint choice if it differs.
Provider construction reads no secret. A step reads a credential only by calling
`ctx.credentials.get(name)`; actual keyring access requires the optional
`rpacore[keyring]` package. The generated greeting needs no secret and performs
no credential read. Library callers that omit `credentials=` still use env.

### Static guidance before execution

`rpacore doctor` suggests likely misspellings at the root and within `queue`,
`notification`, `notification.email` and `notification.webhook`. It compares
unknown names only with known siblings using a deterministic similarity cutoff
of 0.80. Root table names are compared too: `[notifications]` can suggest
`notification`, but its contents are not analyzed. Doctor inspects nested keys
only under the recognized contexts above; it does not descend into arbitrary
application tables or misspelled sections. Intentional custom keys or table
names can resemble framework names, and unrelated typos can be missed; keys
are never rejected or corrected by this heuristic.

Doctor also warns when an explicitly supplied config transaction path differs
from a valid manifest's path after resolving each relative to its own file.
An omitted default or equivalent path does not warn. This does not change which
database is inspected, including with an explicit database override. A library
application may intentionally choose the config path.

Warnings are nonfatal. Human summaries and JSON advisories contain only known
suggested setting names, counts and a path-ownership explanation, without
unknown key names, values or paths. Doctor never imports the entrypoint or
reads credentials. It cannot certify callable availability, definition identity,
step wiring, or whether application code invokes any config consumer above.

Transaction and queue database paths must name non-empty filesystem paths.
Blank paths and `:memory:` with or without surrounding whitespace are rejected
because RPA Core opens multiple SQLite connections for checkpoints, inspection,
queue transitions, and heartbeats; SQLite in-memory databases would give those
connections unrelated state.

Notification settings are optional:

`dispatch` automatically retries recognized transient transport failures up to
three total attempts, with 1- and 2-second waits. No retry setting is required
or supported. Direct notifier `send` calls remain single-attempt. See the
[notification API contract](api.md#credentials-logging-reports-and-notifications)
for retryable errors, failure accounting and best-effort delivery limits.

```toml
[notification.email]
host = "smtp.example.com"
port = 587
from_addr = "rpacore@example.com"
to_addrs = ["admin@example.com"]
attach_screenshots = true

[notification.webhook]
url = "https://hooks.example.com/rpacore"
include_transaction = false
include_report = false
timeout = 10
```

Email delivery negotiates STARTTLS with certificate validation and hostname
checking before SMTP credentials are sent. Servers using a private or
self-signed certificate authority must install that CA in the trust store used
by the Python runtime. RPA Core does not provide a switch to disable TLS
verification.

Webhook URLs must use `http` or `https`. Local and private hosts are allowed
because webhook endpoints are trusted operator configuration. Treat webhook
targets as sensitive. `include_transaction` adds the existing canonical
transaction JSON v3 payload. `include_report` separately adds report JSON v2;
it is disabled by default because it includes diagnostic metadata, artifact
paths, and exception details. Enabling either option does not alter the default
webhook fields.
