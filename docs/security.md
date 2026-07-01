# Security and Privacy

RPA Core is a local-first framework for deterministic Python automation. It does
not create a security sandbox around user code, third-party libraries, browser or
desktop automation clients, webhooks, SMTP servers, SQLite files, or generated
artifact contents.

## Trust Model

User projects run ordinary Python. A skill can read files, call services, open
desktop applications, mutate external systems, and import any dependency
available in its environment. Run RPA Core projects only in environments where
the automation code and its dependencies are trusted.

RPA Core itself keeps control flow deterministic and auditable, but it does not
make external side effects reversible. Queue delivery is at least once, and a
worker that loses a queue lease cannot safely terminate already-running user
skill code.

## Sensitive Data Surfaces

The following data can contain secrets, personal data, or business-sensitive
content:

- `config.toml` and `rpacore.toml`
- transaction state and metadata
- skill arguments
- exception messages
- text and JSON logs
- text and HTML reports
- JSON and NDJSON exports
- SQLite transaction and queue databases
- artifact names, paths, kinds, and metadata
- screenshots captured on exception
- webhook payloads and email messages

RPA Core does not persist `ProcessContext.resources` and does not serialize
credential provider objects. That protection depends on users keeping runtime
clients, sessions, handles, and secrets in `ctx.resources` or credential
providers instead of copying them into durable state, metadata, arguments,
exceptions, artifact metadata, or output files.

## Credentials and Generated Projects

Generated projects default to the environment credential provider. Environment
variables and OS keyrings are read at runtime; their resolved secret values
should not be copied into transaction state, metadata, logs, reports, or exports.

Generated `.gitignore` files exclude the default virtual environment, Python
caches, pytest cache, sample SQLite database, and sample output file. Extend the
generated ignore rules before adding secret files, environment files, reports,
logs, screenshots, exports, or other local output paths.

## Files, Paths, and Artifacts

RPA Core records artifact metadata and file paths; it does not read, hash,
upload, validate, scan, or execute artifact contents. An artifact record is audit
metadata, not proof that a file is safe, unchanged, or still present.

Path helpers normalize paths and can enforce allowed roots where documented, but
they are not a general-purpose filesystem sandbox. Treat symlinks, shared
directories, inherited permissions, and local multi-user machines as part of the
operator-owned environment.

SQLite databases are ordinary local files. Protect them with filesystem
permissions, backups, disk encryption where appropriate, and normal operational
controls. Avoid placing transaction, queue, log, report, export, or screenshot
files in shared directories unless every reader is trusted.

## Notifications and Network Calls

Webhook URLs must use `http` or `https`. Local, private, loopback, and link-local
hosts are allowed because webhook endpoints are trusted operator configuration.
Treat webhook URLs as sensitive: outbound requests can reach internal services,
and DNS resolution or redirects may involve infrastructure outside RPA Core's
control.

Webhook timeouts bound socket operations after resolution starts; they do not
fully control operating-system DNS latency.

Email notifications can attach screenshots referenced by reports when
`attach_screenshots` is true. Missing or unreadable screenshot files are skipped.
Reports and notification payloads include artifact records and paths, not
artifact file contents.

## Security Review Checklist

Use this checklist for changes that touch security-sensitive surfaces:

- persistence: schema migrations are explicit, forward-only, and reject newer
  schemas loudly
- paths: user-controlled paths are resolved intentionally and containment checks
  are used where a root boundary is required
- webhooks: scheme, timeout, payload shape, credentials redaction, and redirect
  behavior are reviewed
- credentials: secret values are not persisted, logged, exported, or included in
  reports by framework code
- serialization: exports include only documented transaction fields and never
  runtime resources or credential provider objects
- subprocesses and generated templates: command execution is explicit and does
  not depend on unpublished checkout paths
- artifacts: framework code records metadata only and does not execute or trust
  artifact contents
- CLI: machine-readable stdout stays parseable and diagnostics go to stderr

Report potential vulnerabilities through [Security Policy](../SECURITY.md).
