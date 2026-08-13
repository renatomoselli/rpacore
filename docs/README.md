# RPA Core Documentation

RPA Core is a local-first Python framework for deterministic, durable
automation. Users write ordinary Python steps, wire them into a transaction,
and choose when to persist checkpoints.

> Current development version: `0.3.0` (unreleased).
> Latest published release: `v0.2.0`.

This documentation map describes the current `0.3.0` development API. For the
latest published and supported `0.2.x` contract, use the
[`v0.2.0` documentation](https://github.com/renatomoselli/rpacore/tree/v0.2.0/docs).

Start here:

- [Tutorial](tutorial.md): install RPA Core, scaffold a project, write a step,
  run it, inspect persisted transactions, and resume work.
- [Durability and Storage](durability.md): transaction persistence, checkpoint
  timing, schema versions, migrations, recovery, and queue persistence.
- [API Reference](api.md): supported top-level imports from `rpacore`.
- [CLI Reference](cli.md): `rpacore init`, `run`, `doctor`, `version`, and
  transaction inspection/export commands.
- [Configuration Reference](config.md): `rpacore.toml`, `config.toml`, path
  resolution, queue settings, notifications, and credentials.
- [Project Manifest Reference](project-manifest.md): `rpacore.toml` schema and
  CLI entrypoint resolution.
- [Export Format Reference](export-format.md): JSON and NDJSON transaction
  export envelopes and data-sensitivity notes.
- [Security and Privacy](security.md): local trust model, sensitive data
  surfaces, credentials, paths, notifications, and review checklist.
- [Supply Chain](supply-chain.md): dependency inventory, artifact checks,
  publishing posture, and dependency update policy.
- [Governance and Release Process](governance.md): decision records, review
  areas, required checks, release ownership, examples-repo alignment, and
  compatibility policy.
- [Repository Settings](repository-settings.md): public repository metadata,
  branch protection, required checks, and issue/security routes.
- [Release Rehearsal](release-rehearsal.md): freeze inputs, release manifest,
  required checks, examples validation, and release approval.
- [Testing Steps](testing.md): plain pytest patterns for user-authored steps.
- [Public Submodule Policy](public-submodules.md): supported import boundary for
  the current development API.
- [Migrating from v0.2 to v0.3](v0.3-migration.md): breaking vocabulary,
  record-version, scaffold, and SQLite migration changes.
- [Runtime Dependency Decisions](runtime-dependencies.md): runtime, optional,
  and development dependency posture.
- [Non-Goals](non-goals.md): deferred features and rejected runtime behavior.

The runtime has no AI dependency and does not call AI services. AI tools can help
author automation code, but execution remains deterministic Python.
