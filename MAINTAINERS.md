# Maintainers

## Current Maintainer

- Renato Moselli

## Ownership Areas

- Runtime framework: `rpacore/`
- Public documentation: `README.md`, `docs/`, `CHANGELOG.md`, `SECURITY.md`
- Packaging and validation: `pyproject.toml`, `.github/workflows/`,
  `scripts/`
- Generated project template: `rpacore/cli.py` and related tests

## Review Expectations

Behavior changes need tests. Public behavior changes need documentation.
Persistence, queue, export, CLI, security, and dependency changes need explicit
review for compatibility and release impact.

Keep root governance cross-references as markdown links when possible. The docs
verifier checks markdown links in `README.md`, `CHANGELOG.md`, `SECURITY.md`,
`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SUPPORT.md`, `AUTHORS.md`,
`MAINTAINERS.md`, and `docs/*.md`.

## Release Ownership

A release owner must verify the release manifest, artifact hashes, package
metadata, documentation checks, installed-wheel validation, and rollback or
hotfix path before publication.
