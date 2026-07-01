# Configuration Reference

RPA Core uses two small TOML files.

## `rpacore.toml`

`rpacore.toml` is the project manifest consumed by the CLI:

```toml
[project]
entrypoint = "main:main"

[storage]
transaction_db_path = "rpacore.db"
```

`[project].entrypoint` is a `module:callable` string. The callable is invoked by
`rpacore run`.

`[storage].transaction_db_path` is used by CLI transaction inspection commands.
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
```

Relative `transaction_db_path`, `screenshot_dir`, and `queue.db_path` values are
resolved relative to the config file. The removed top-level `db_path` key is
rejected; use `transaction_db_path`.

Notification settings are optional:

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
timeout = 10
```

Webhook URLs must use `http` or `https`. Local and private hosts are allowed
because webhook endpoints are trusted operator configuration. Treat webhook
targets as sensitive.
