# Security and Privacy

RPA Core is a local-first framework for deterministic Python automation. It does
not create a security sandbox around user code, third-party libraries, browser or
desktop automation clients, webhooks, SMTP servers, SQLite files, or generated
artifact contents.

## Trust Model

User projects run ordinary Python. A step can read files, call services, open
desktop applications, mutate external systems, and import any dependency
available in its environment. Run RPA Core projects only in environments where
the automation code and its dependencies are trusted.

RPA Core itself keeps control flow deterministic and auditable, but it does not
make external side effects reversible. Queue delivery is at least once, and a
worker that loses a queue lease cannot safely terminate already-running user
step code.

## Sensitive Data Surfaces

The following data can contain secrets, personal data, or business-sensitive
content:

- `config.toml` and `rpacore.toml`
- transaction state and metadata
- step arguments
- exception messages
- text and JSON logs
- text and HTML reports
- JSON and NDJSON exports
- execution-transition checkpoint state selected by a sink allowlist
- SQLite transaction and queue databases
- artifact names, paths, kinds, and metadata
- screenshots captured on exception
- webhook payloads and email messages

Queue inspection library functions return full records: item payloads and
item/attempt claim tokens remain sensitive. Do not log or forward them wholesale.
The [queue CLI](cli.md#queue-commands) projects an allowlist that omits payloads,
tokens, config and raw exception messages. IDs, references, worker names and
transaction IDs remain visible and can contain application data. Apply access
controls and retention to captured human/JSON output. A cursor carries only a
position and filter identity; it grants no storage access and is not a snapshot.

Queue administrative commands require `--reason` and `--confirm` before storage
access. This confirmation expresses operator intent; local file permissions
remain the access boundary. Reasons are stored verbatim after trimming in the
administrative audit and may also enter shell history or process argument
records. Keep credentials and sensitive transaction contents out of reasons.
Acknowledgements omit the reason, payload and claim token; human item IDs are
quoted with control characters escaped. A lost acknowledgement may require
Python audit inspection before retrying; repeated overrides can change durable
counters again. See [queue administration](cli.md#queue-administration) for
writable initialization, migration and journal-conversion effects.

RPA Core does not persist `ProcessContext.resources` and does not serialize
credential provider objects. That protection depends on users keeping runtime
clients, sessions, handles, and secrets in `ctx.resources` or credential
providers instead of copying them into durable state, metadata, arguments,
exceptions, artifact metadata, or output files.

Text and JSON log formatters apply the same structural extra-field policy:
top-level `config`, `credentials`, and `resources` fields are omitted, those
keys are also omitted from nested mappings, and unsupported runtime objects are
represented by type name. User extras cannot replace canonical JSON envelope
fields. The `event` extra is the documented selector for the canonical event
name.

Exception messages, formatted tracebacks, explicit stack information, and
ordinary string values are emitted verbatim; RPA Core cannot reliably infer
which free-form content is secret. Do not place credentials or sensitive
payload values in exception messages, local variables rendered by custom
tracebacks, event names, or log strings. Apply access controls and retention
policy to both text and JSON logs.

An execution-transition sink receives lifecycle identifiers, status and outcome
facts, plus only the transaction-state keys named by
`transition_state_fields`. The allowlist is a data-release boundary, not a
redaction engine: RPA Core does not inspect selected values for credentials,
personal data, or business sensitivity. Keep secrets and runtime handles in
credential providers or `ctx.resources`, select the minimum required state, and
apply authentication, encryption, access control, retention, and deletion at
the sink destination. Sink delivery occurs before the matching local checkpoint
and is not proof that either side has durably committed the fact.

## Credentials and Generated Projects

Newly generated projects log to stderr by default, including verbatim
step-failure exception text; apply access controls and retention policies to
captured logs.

Generated projects default to the environment credential provider. Environment
variables and OS keyrings are read at runtime; their resolved secret values
should not be copied into transaction state, metadata, logs, reports, or exports.
New entrypoints explicitly build and inject the selected config provider;
construction reads no secret and the greeting never requests one. Existing
projects require an explicit [injection edit](config.md#credential-selection-in-existing-projects).
Doctor reads no credentials and does not import user code. Its config advisories
expose only known setting names, aggregate counts and fixed path-mismatch facts,
never unknown names, values or paths. Similarity warnings can match intentional
application keys; they do not establish runtime wiring or actual credential use.

Generated `.gitignore` files exclude the default virtual environment, Python
caches, pytest cache, sample SQLite database, and sample output file. Extend the
generated ignore rules before adding secret files, environment files, reports,
logs, screenshots, exports, or other local output paths.

## Files, Paths, and Artifacts

RPA Core records artifact metadata and file paths; it does not read, hash,
upload, validate, scan, or execute artifact contents. An artifact record is audit
metadata, not proof that a file is safe, unchanged, or still present.

HTML reports and email HTML link only unambiguous local screenshot paths, as
described in the [API reference](api.md). Stored URL schemes, network-share or
ambiguous paths, control characters, and padded values render as escaped text.
This intentionally restricts previously clickable values without altering the
original diagnostic records. Link rendering performs no file or network I/O;
it does not attest file contents or guarantee that an email client can open
the target. Relative links may include `../`; this policy is not a filesystem
sandbox. SMTP attachment handling remains separate and unchanged.

Path helpers normalize paths and can enforce allowed roots where documented, but
they are not a general-purpose filesystem sandbox. Treat symlinks, shared
directories, inherited permissions, and local multi-user machines as part of the
operator-owned environment.

SQLite databases are ordinary local files. Protect them with filesystem
permissions, backups, disk encryption where appropriate, and normal operational
controls. Avoid placing transaction, queue, log, report, export, or screenshot
files in shared directories unless every reader is trusted.

Durable transaction fields are validated as JSON-safe data before execution and
persistence. Loading also rejects malformed or non-object persisted step
arguments with transaction and step identifiers so operators can repair the
database explicitly; RPA Core does not silently normalize corrupt records.

RPA Core rejects blank and `:memory:` durable database paths. Transaction CLI
inspection opens existing files read-only and refuses missing or incompatible
schemas without migration. Queue databases use rollback journal on supported
runtimes to avoid the documented SQLite WAL-reset race in affected embedded
runtimes. Keep database and journal files together during backup and restore.

Database paths are trusted operator configuration, not a filesystem sandbox.
They may intentionally identify storage outside the project directory, subject
to the operating-system permissions of the automation process. Do not let an
untrusted party control configuration files or command-line arguments.

## Notifications and Network Calls

Webhook URLs must use `http` or `https`. Local, private, loopback, and link-local
hosts are allowed because webhook endpoints are trusted operator configuration.
Treat webhook URLs as sensitive: outbound requests can reach internal services,
and DNS resolution or redirects may involve infrastructure outside RPA Core's
control.

Webhook timeouts bound socket operations after resolution starts; they do not
fully control operating-system DNS latency.

Notification dispatch retries selected transient failures up to three attempts,
adding 1- and 2-second waits. Each attempt may incur transport timeouts; this
is not an overall delivery deadline. Retries after an ambiguous acknowledgement
can duplicate an email or webhook side effect. Receivers should deduplicate
using transaction identity where needed. Exhaustion and process exit can still
lose notifications: there is no durable outbox or replay guarantee. Permanent
HTTP/SMTP errors and certificate verification failures are not retried, and TLS
verification remains enabled. See the [notification API contract](api.md#credentials-logging-reports-and-notifications).

Email notifications can attach screenshots referenced by reports when
`attach_screenshots` is true. Missing or unreadable screenshot files are skipped.
Reports and notification payloads include artifact records and paths, not
artifact file contents.

The optional `notification.webhook.include_report` payload is report format v2
and includes the existing report diagnostic surfaces: metadata, artifact paths,
and exception messages/actions/screenshot paths. It is disabled by default;
enable it only for a trusted endpoint with an appropriate retention policy.

`rpacore doctor` is local and read-only. Its results intentionally omit
credentials, configuration values, raw paths, transaction state/arguments,
exception text, queue payloads, artifact contents, and endpoint checks. It does
not import a project entrypoint. Treat its bounded runtime and database-health
output as operational metadata and keep it within the same trusted support
boundary as the project.

Doctor input failures expose only fixed cause codes and recognized TOML
line/column or end-of-document locations, without source excerpts or offending
values. Missing discovered databases are unchecked, not evidence of intact
storage: verify the configured location and recovery/backup before running work
that could recreate a removed database.

New generated greeting projects persist absolute output paths in step arguments,
state, and artifacts. Inspection/exports may expose local account and directory
names. Copied records keep the original output destination when resumed; moving
the project changes only the destination computed by a fresh run. The single
greeting file is replaceable output, not retained historical content. Atomic
replacement does not make file publication and SQLite persistence one commit.

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

Transaction list metadata filters can expose values through shell history or
process arguments. Continuation instructions omit those values, but a cursor
is an opaque continuation token rather than a credential or encrypted support
bundle; avoid sharing it unnecessarily. CLI list/show JSON and single-record
exports still include sensitive canonical records. Selecting one ID reduces
scope but does not redact its state, metadata, arguments, or exception messages.
