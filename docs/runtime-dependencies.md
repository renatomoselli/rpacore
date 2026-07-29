# Runtime Dependency Decisions

RPA Core v0.2.0 keeps the default runtime dependency set empty. The framework
uses the Python standard library for persistence, configuration, CLI wiring,
logging, notification transport, and queue processing. Existing optional extras
remain:

- `rpacore[screenshots]` installs `mss` for framework-captured screenshots.
- `rpacore[keyring]` installs `keyring` for OS credential-store integration.

No new runtime dependency is adopted by the v0.2.0 architecture.

## Decision Matrix

| Candidate | Decision | Correctness guarantee | Windows support | Packaging and transitive cost | Maintenance and skill-author effect |
| --- | --- | --- | --- | --- | --- |
| Process timeout / termination libraries, including Pebble-style worker termination | Rejected for v0.1.0. | They can stop a process boundary, but they cannot make arbitrary browser, desktop, database, or HTTP side effects reversible. An in-process timeout can mark work failed while code keeps running; process termination can still leave external systems partially mutated. Hard deadlines belong outside RPA Core, behind an operator-owned worker process boundary. | Cross-platform termination semantics differ, especially around child processes, handles, desktop automation, and cleanup on Windows. | Adds a runtime dependency and extra process-management behavior without solving rollback or idempotency. | Skill authors still need library-specific I/O timeouts and idempotent external actions. A generic timeout API would imply a stronger guarantee than the framework can provide. |
| Pydantic | Rejected for v0.1.0. | Current dataclasses, explicit validation, and JSON-safety checks provide deterministic, actionable errors. Pydantic would not replace persistence repair errors, queue retry classifications, or domain-specific `SystemException` paths cleanly. | Supported on Windows, but not needed for current models. | Adds a non-trivial dependency and migration surface for plain models that are already small and explicit. | Skill authors would have to learn a model layer for little benefit, and public validation behavior would become coupled to Pydantic semantics. |
| Tenacity | Rejected for v0.1.0. | Runner, queue, and engine retry behavior is state-driven and auditable. Generic retry decorators would hide retry disposition, durable checkpoints, queue transition rules, and business-vs-system classification. | Supported on Windows, but the current retry logic is stdlib-only and deterministic. | Adds a dependency to shorten clear local loops. | Skill authors should use explicit retries inside their own domain clients when needed; framework retries must remain visible in transaction history and queue state. |
| AnyIO | Rejected for v0.1.0. | The framework is synchronous. Queue workers, SQLite persistence, SMTP/webhook notification, and skill execution do not require an async abstraction layer. | Supported on Windows, but async backend and cancellation semantics would introduce new decisions unrelated to demonstrated workflows. | Adds runtime and conceptual cost without a current async API. | Skill authors can use async libraries inside their own skill code if they own that boundary; RPA Core should not require async concepts for synchronous automations. |
| APSW or another SQLite driver | Rejected for v0.1.2. | Changing the embedded SQLite runtime would require migrating framework code and semantics to that driver; installing a package alone does not change Python's `sqlite3`. PATCH-003 needs only path validation, compatibility-first connections, read-only URI inspection, and rollback journal. | Would require a second Windows/Linux binary and wheel matrix. | Adds a runtime dependency and duplicate driver behavior without a demonstrated missing stdlib guarantee. | The framework retains explicit stdlib SQLite semantics; revisit only if a required guarantee cannot be implemented or measured with `sqlite3`. |

## Adopted Optional Dependencies

`mss` remains optional for screenshot capture. If absent, screenshot capture logs
a warning and returns an empty path so execution can continue.

`keyring` remains optional for OS credential storage. If absent and selected,
`KeyringCredentialProvider` raises an actionable import error. The default
credential provider remains environment-variable based and uses only the
standard library.

These optional dependencies already have tests for absence/failure behavior and
do not change the default install.

## Future Rule

Do not add a runtime dependency merely to shorten clear standard-library code.
Adopt one only when a demonstrated v0.1.0-or-later workflow requires a
correctness guarantee the standard library cannot provide, the Windows behavior
is explicit, transitive dependencies are acceptable, and the effect on skill
authors is documented.
