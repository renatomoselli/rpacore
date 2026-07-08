# Support

RPA Core is a local Python framework. Support covers the framework, documented
public APIs, packaging, generated project scaffolds, and public documentation.

## Supported Versions

The latest public `0.1.x` release line receives compatibility and security fixes
as maintainer availability permits. Private pre-release commits and local forks
are not supported release lines.

## Supported Platforms

RPA Core supports Python 3.11, 3.12, and 3.13. The core framework is designed for
Windows and POSIX environments, with Windows validation required before release.
Some user automations or optional dependencies may be platform-specific.

## Where To Ask

- Use GitHub issues for reproducible bugs, documentation problems, and feature
  proposals.
- Use GitHub private vulnerability reporting for security issues, as described
  in [SECURITY.md](SECURITY.md).
- Use discussions or issues for general questions when that route is enabled.

## Boundaries

The project does not provide a service-level agreement for user automations or
third-party systems. Issues in user-authored automation code, third-party
websites, desktop applications, credentials, network services, or optional
libraries may be redirected when they are outside the framework boundary.

## Compatibility Policy

Before v1.0, compatibility is still conservative: documented v0.1 public APIs,
storage schemas, export formats, and CLI behavior should not break without a
documented correctness, security, or release-blocking reason. Additive changes
should include tests and public documentation.

RPA Core uses semantic-versioned releases beginning with v0.1.0:

- patch releases should preserve documented public APIs, CLI behavior, storage
  schemas, export formats, and generated-project persistence patterns
- minor releases may add documented APIs or behavior, but should avoid breaking
  existing v0.1 users without a recorded migration reason
- breaking changes before v1.0 require a changelog entry, public migration
  guidance, and focused validation evidence

Deprecations should remain documented for at least one patch release when that
is practical. A deprecation can be shortened only for correctness, security,
data-loss, or release-blocking reasons.

SQLite schemas and JSON/NDJSON export formats use explicit version fields.
Readers should reject newer unsupported versions loudly rather than silently
misinterpreting data. Additive fields are preferred when they preserve existing
records and documented parsing behavior.

Optional dependencies and platform-specific examples are supported only within
their documented scope. A problem in an optional library, browser driver,
desktop application, external service, or manual account may be redirected when
the RPA Core framework contract is not the failing component.

See [Governance and Release Process](docs/governance.md) for the decision and
release-readiness process behind compatibility-impacting changes.
