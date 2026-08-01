# Security Policy

RPA Core is a local Python automation framework. It executes user-authored
Python code and is not a sandbox. Treat automations, configuration, transaction
databases, logs, reports, exports, artifact paths, and screenshots as potentially
sensitive.

## Supported Versions

Security fixes target the latest public release line.

| Version | Supported |
| --- | --- |
| 0.2.x | Yes |
| earlier release lines | No |

The unreleased `0.3.0` development line on `main` is not a published support
line. This table changes only after that version is released.

## Reporting a Vulnerability

Use GitHub's private vulnerability reporting flow for this repository when it is
available:

<https://github.com/renatomoselli/rpacore/security/advisories/new>

If that route is unavailable, open a GitHub issue asking for a private security
contact and do not include exploit details, credentials, customer data, or
private infrastructure names in the public issue.

Please include:

- affected version or commit
- operating system and Python version
- affected feature, such as persistence, queue processing, CLI, credentials,
  notifications, packaging, or generated projects
- reproduction steps using non-sensitive sample data
- expected impact, such as secret exposure, data loss, unsafe filesystem access,
  or package integrity risk

## Response Expectations

The maintainer will acknowledge actionable private reports as availability
permits, triage severity, and coordinate a fix or disclosure note before public
details are shared. RPA Core does not provide a service-level agreement for user
automations or third-party systems.

Issues that affect user-authored automation code, third-party services, or
optional dependency behavior may be redirected when they are outside the
framework boundary.

## Security Documentation

See [Security and Privacy](docs/security.md) for the local trust model and
[Supply Chain](docs/supply-chain.md) for dependency and release-artifact
controls.
