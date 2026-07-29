# Supply Chain

RPA Core v0.1.0 keeps the default runtime dependency set empty. The framework
uses the Python standard library for core execution, persistence, configuration,
CLI, logging, queue processing, reports, and notification transport.

## Dependency Inventory

Runtime dependencies:

| Group | Dependencies |
| --- | --- |
| default install | none |
| `rpacore[screenshots]` | `mss` |
| `rpacore[keyring]` | `keyring` |

Development and release dependencies are declared under the `dev` optional extra
in `pyproject.toml` and are not installed by default.

New runtime dependencies require a documented decision explaining the
correctness guarantee, Windows behavior, transitive cost, and impact on skill
authors.

## Release Artifact Checks

Before publishing a release, build fresh artifacts from the frozen commit and
verify:

```bash
python -m build
twine check dist/*
python scripts/validate_installed_wheel.py --repo-root . --wheel-dir dist
```

Record SHA-256 hashes for every wheel and source distribution that will be
published. Publication should upload the prebuilt, checked artifacts; do not
rebuild during upload. The release manifest and approval decision are described
in [Release Rehearsal](release-rehearsal.md).

The release-candidate validator accepts wheels and `.tar.gz` source
distributions, then records artifact hashes, dependency inventory, package
metadata presence, wheel `RECORD`, wheel entry points, license files, and
private-path checks in its validation manifest. After every validation step
passes, it preserves the exact artifact set in an immutable content-addressed
directory beneath the candidate output's `artifacts/` directory. The manifest
records those durable files; publication must use them without rebuilding.
Validation is fail-fast and cleans up its generated working directories on
failure, so preserve the terminal or CI log for diagnosis.

Inspect archive contents so generated examples, private notes, local caches,
credentials, and review artifacts do not ship in the package. The wheel should
contain the `rpacore` package, metadata, `LICENSE`, `NOTICE`, and the `rpacore`
console entry point.

## CI and Publishing

CI actions should remain pinned according to the repository policy in effect for
the release. Publication should use trusted publishing or another short-lived,
approval-gated credential path when available. Do not store long-lived package
publishing credentials in the repository, generated projects, ordinary local
config, or CI logs.

Release environments should require explicit approval before publication. A
failed release should preserve logs, artifact names, and hashes for diagnosis.

## Dependency Updates

Default runtime dependencies should stay empty unless a documented release
decision accepts one. Optional and development dependencies should be reviewed
before release for known vulnerabilities, license compatibility, and unnecessary
transitive expansion.

If an optional dependency has a security issue, document whether the default
install is affected and whether users of that extra need a patch release or a
configuration workaround.

## Related Documentation

- [Runtime Dependency Decisions](runtime-dependencies.md)
- [Security and Privacy](security.md)
- [Export Format Reference](export-format.md)
