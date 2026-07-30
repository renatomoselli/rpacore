# RPA Core Documentation

RPA Core is a local-first Python framework for deterministic, durable
automation. Users write ordinary Python skills, wire them into a transaction,
and choose when to persist checkpoints.

Use this documentation map as the public `0.2.x` entry point:

- [Tutorial](tutorial.md): install RPA Core, scaffold a project, write a skill,
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
- [Testing Skills](testing.md): plain pytest patterns for user-authored skills.
- [Public Submodule Policy](public-submodules.md): supported import boundary for
  the `0.2.x` release line.
- [Runtime Dependency Decisions](runtime-dependencies.md): runtime, optional,
  and development dependency posture.
- [Non-Goals](non-goals.md): deferred features and rejected runtime behavior.

The runtime has no AI dependency and does not call AI services. AI tools can help
author automation code, but execution remains deterministic Python.
