# CLI Reference

The `rpacore` command is installed by the `rpacore` package.

## `rpacore init <project_name>`

Creates a normal Python project directory containing:

- `pyproject.toml`
- `rpacore.toml`
- `config.toml`
- `main.py`
- `skills/`
- `tests/`
- `.gitignore`

The generated `main.py` uses `execute_transaction(transaction_db_path=...)` so
the scaffold demonstrates strict checkpoint persistence with public APIs.

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
and calls that Python callable. RPA Core does not discover skills or build a
pipeline from configuration; user code owns transaction wiring.

Exit codes:

- `0`: entrypoint returns `None` or `0`
- `1`: entrypoint raises
- `2`: manifest, entrypoint, or return-value validation fails
- `0` through `255`: propagated when the entrypoint returns an integer in that
  range

## `rpacore version`

Prints the installed package version.

## Transaction Commands

Transaction commands inspect the SQLite database at `[storage].transaction_db_path`
from `rpacore.toml`. Pass `--db path/to/rpacore.db` to use a specific database.

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

Human output is for operators. `--json` and `export` output are parseable on
stdout; diagnostics go to stderr.
