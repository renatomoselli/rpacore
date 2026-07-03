# Governance and Release Process

RPA Core is a local-first Python framework. Governance should keep the public
surface small, deterministic, auditable, and easy to validate from installed
artifacts.

## Decision Records

Release-impacting decisions need a durable public record before they are treated
as settled. Use a short decision record when a change affects:

- public APIs or supported imports
- SQLite schemas, migrations, queue semantics, or checkpoint timing
- JSON or NDJSON export formats
- CLI commands, exit codes, or machine-readable output
- runtime, optional, development, or release dependencies
- generated project structure or persistence defaults
- rejected features being reconsidered

Each decision record should state:

- context and problem
- options considered
- decision and rationale
- compatibility and migration impact
- tests, docs, and examples affected
- revisit trigger

Small decisions can live in the pull request when the durable public docs are
updated in the same change. Larger decisions should be recorded in the relevant
public document, such as [Runtime Dependency Decisions](runtime-dependencies.md),
[Public Submodule Policy](public-submodules.md), [Export Format Reference](export-format.md),
[Durability and Storage](durability.md), or this page.

## Required Review Areas

Before merging a release-impacting change, reviewers should check:

- behavior changes have focused tests
- public behavior changes update public docs
- compatibility notes appear in [CHANGELOG.md](../CHANGELOG.md) when users need
  to know about them
- persistence, queue, export, CLI, and generated-project changes preserve the
  documented v0.1.0 contracts
- runtime dependency changes have a dependency decision
- `rpacore-examples` either receives a matching update or the change records why
  no example update is required

## Required Checks

The release branch should require the CI jobs that prove the public product:

- documentation verification on supported Python versions
- framework tests on supported Python versions
- package build and metadata checks
- installed-artifact validation from built distributions
- Windows checkpoint/resume validation subset
- security policy, sensitive-data surfaces, dependency posture, and package
  content review before release approval

Local release rehearsal may run stricter checks than CI. Publication still uses
the frozen prebuilt artifacts and the release manifest evidence; do not rebuild
artifacts during upload.

## Release Ownership

A release owner is responsible for:

- confirming the framework and examples commits are clean and named
- verifying wheel and source distribution hashes
- confirming package metadata, license files, and repository URLs
- checking public docs, security policy, support route, and governance route
- confirming compatible `rpacore-examples` instructions point to the intended
  framework version
- preserving release evidence, logs, and rollback or hotfix decisions

Publication requires explicit approval at release time. A plan, release
rehearsal, or successful CI run does not authorize publishing by itself.

## Examples Repository Alignment

The examples repository is consumer-facing evidence. Framework changes are not
release-ready until the examples impact is explicit:

- update the affected examples, tests, requirements, and README files; or
- record why no example change is required.

For release-bound framework changes, the examples repository should identify the
compatible RPA Core version and separate public package installation from local
wheel rehearsal. Example-only changes remain in the examples repository and
should not require framework runtime changes unless they expose a real public API
or correctness gap.

## Compatibility Policy

Before v1.0, documented v0.1 public APIs, CLI behavior, storage schemas, export
formats, and generated-project persistence patterns are still compatibility
boundaries. Breaking them requires a correctness, security, or release-blocking
reason, a documented migration path, and focused validation.

Patch releases should prefer narrow fixes. Ordinary defects should use a new
patch release rather than replacing existing artifacts or rewriting public tags.
