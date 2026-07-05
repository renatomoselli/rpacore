# Repository Settings

This page records the public repository settings expected before RPA Core
v0.1.0 is published. GitHub settings are external state, so release rehearsal
must verify them against the live repositories rather than assuming the files in
this repository applied them.

## Framework Repository

Repository name:

- `rpacore`

Description:

- RPA Core — deterministic, stateful RPA in Python

Package keywords:

- `rpa`
- `automation`
- `workflow`
- `enterprise`

GitHub topics:

- `rpa`
- `automation`
- `python`
- `workflow`
- `sqlite`
- `local-first`

Project URLs:

- homepage: `https://rpacore.dev`
- documentation: `https://github.com/renatomoselli/rpacore/tree/main/docs`
- repository: `https://github.com/renatomoselli/rpacore`
- issues: `https://github.com/renatomoselli/rpacore/issues`
- security: `https://github.com/renatomoselli/rpacore/security/advisories/new`

Canonical web domains:

- primary: `https://rpacore.dev`
- defensive redirect: `https://rpacore.org`
- defensive redirect: `https://rpacore.net`

The description, package keywords, and project URLs should match the
`[project]` and `[project.urls]` metadata in [`pyproject.toml`](../pyproject.toml).

See [Governance and Release Process](governance.md) for the decision process
behind release-impacting repository settings.

## Examples Repository

Repository name:

- `rpacore-examples`

Description:

- Consumer-facing example automations for RPA Core.

GitHub topics:

- `rpa`
- `automation`
- `python`
- `examples`
- `rpacore`

Project URLs:

- homepage: `https://github.com/renatomoselli/rpacore-examples`
- framework: `https://github.com/renatomoselli/rpacore`
- issues: `https://github.com/renatomoselli/rpacore-examples/issues`
- security: `https://github.com/renatomoselli/rpacore/security/advisories/new`

Security reports that affect the framework, generated project defaults,
packaging, persistence, credentials, or public API behavior should use the
framework security route. The examples repository uses that route because it
does not have an independent security process for framework-level findings.

## Branch Protection

Protect the default branch before public launch:

- require pull requests before merging
- require the branch to be up to date before merging
- require conversation resolution
- require linear history when practical
- restrict force pushes and branch deletion
- require explicit release approval for publication workflows

For `rpacore`, require the CI jobs that prove the public product:

- `docs`
- `test`
- `package`
- `installed-artifact`

The release owner should also verify the Windows checkpoint/resume subset inside
the `installed-artifact` job before approving a release.

For `rpacore-examples`, require its deterministic validation checks when a CI
workflow exists. Manual, browser, desktop, network, or account-backed examples
should be labeled separately and should not block repository maintenance unless
the release decision explicitly makes them required evidence.

## Issue And Security Routes

Both repositories should expose:

- bug report template
- documentation issue template
- feature or example request template
- pull request template
- private security report route or a security contact link

Public issues should not request credentials, customer data, private URLs, local
checkout paths, screenshots with sensitive content, generated transaction
databases, or artifact contents.
