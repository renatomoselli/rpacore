# Project Manifest

`rpacore.toml` describes how RPA Core finds a user project's Python entrypoint
and local transaction storage. It is intentionally small.

Example:

```toml
[project]
entrypoint = "main:main"

[storage]
transaction_db_path = "rpacore.db"
```

## Schema

Supported top-level sections:

- `[project]`
- `[storage]`

Supported keys:

- `project.entrypoint`: required string in `module:callable` form
- `storage.transaction_db_path`: required string path to the transaction SQLite
  database

Unknown sections or keys are rejected. This keeps `rpacore.toml` from becoming
an implicit pipeline language.

Entrypoints are explicit Python imports. `main:main` imports module `main` from
the project directory and resolves attribute `main`. Dotted attributes are
allowed, for example `main:app.run`. The resolved object must be callable with
no arguments. It may return `None` or an integer exit code; `rpacore run`
defines how those return values map to process exit codes. Resolution does not
change the process working directory. If a same-named entrypoint package from a
different project is cached, only that conflicting package namespace is
replaced. A failed import or callable validation restores previous modules and
package attributes within the entrypoint's top-level package. Dependencies
imported outside that namespace retain normal Python import-cache semantics and
are never globally purged by RPA Core.

`storage.transaction_db_path` is resolved relative to the directory containing
`rpacore.toml` unless it is already absolute. It must name a non-blank durable
SQLite file path; `:memory:` with or without surrounding whitespace is rejected.
For generated and CLI-managed projects, this manifest-relative path is the
transaction-storage authority for generated execution, default transaction
commands, and default doctor inspection. Direct library code may explicitly
use another path.

## Discovery

`find_project_manifest(start)` searches for `rpacore.toml` in `start` and then
walks upward through parent directories. `load_project_manifest()` uses that
discovery from the current working directory when no path is supplied. Passing a
directory loads `rpacore.toml` from that directory.

## Decisions

RPA Core keeps skill construction and transaction wiring in Python.
The manifest does not support:

- `pipeline_from_config`
- automatic skill discovery
- CLI transaction resume
- declarative skill graphs
- runtime AI behavior or AI dependencies

Those features require a separate deterministic contract before they can be
considered.
