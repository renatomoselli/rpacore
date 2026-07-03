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

The project does not provide a cloud service SLA. Issues in user-authored
automation code, third-party websites, desktop applications, credentials,
network services, or optional libraries may be redirected when they are outside
the framework boundary.

## Compatibility Policy

Before v1.0, compatibility is still conservative: documented v0.1 public APIs,
storage schemas, export formats, and CLI behavior should not break without a
documented correctness, security, or release-blocking reason. Additive changes
should include tests and public documentation.
