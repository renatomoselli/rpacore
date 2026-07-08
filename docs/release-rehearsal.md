# Release Rehearsal

Release rehearsal proves the exact source commits and exact built artifacts
before any public publication step. It does not authorize publishing by itself.

## Freeze Inputs

Record these before building artifacts:

- framework repository: `rpacore`
- framework commit
- examples repository: `rpacore-examples`
- examples commit
- version
- tag to create
- release owner
- release approver
- changelog entry
- documentation commit
- resolved security, supply-chain, governance, and support decisions

Both repositories should be clean before rehearsal starts. Any framework code,
schema, API, CLI, storage, export, or generated-project behavior change after
this point invalidates the candidate and requires a new rehearsal.

## Manifest Contents

The release manifest should contain:

- framework and examples commit IDs
- tag name
- Python and operating-system matrix results
- wheel and source distribution names, sizes, and SHA-256 hashes
- dependency inventory location
- SBOM location, or a note that no SBOM was produced
- documentation verification command and result
- expected PyPI metadata, including project URLs and license files
- release owner and approver
- release-candidate validation results
- external examples wheel-validation results
- TestPyPI result, or a note that TestPyPI was intentionally skipped

Use the prebuilt, hashed artifacts recorded in the manifest for publication. Do
not rebuild artifacts during upload.

## Required Rehearsal Checks

Run the framework release-candidate validation from a clean repository state:

```bash
python scripts/validate_release_candidate.py --repo-root . --examples-repo ../rpacore-examples
```

Run external examples validation against the built wheel:

```bash
python scripts/validate_examples_against_wheel.py --repo-root . --examples-repo ../rpacore-examples --venv-mode work-dir
```

Then verify:

- required CI jobs are green
- documentation verification passed
- wheel and source distribution pass metadata checks
- wheel and source distribution contain `LICENSE` and `NOTICE`
- wheel and source distribution do not contain examples, private notes, review
  artifacts, local caches, credentials, or local checkout paths
- wheel install smoke test passes
- source distribution install smoke test passes
- generated project scaffold runs from the installed package
- deterministic representative examples pass from the built wheel
- repository settings match [Repository Settings](repository-settings.md)

Prepare the release manifest and approval draft from the two validation result
files:

```bash
python scripts/prepare_release_manifest.py \
  --repo-root . \
  --examples-repo ../rpacore-examples \
  --release-candidate-validation-results <path-to-release-candidate-validation-results.json> \
  --examples-wheel-validation-results <path-to-examples-wheel-validation.json> \
  --owner "<release owner>" \
  --approver "<release approver>" \
  --docs-verification passed \
  --docs-command "python scripts/verify_docs.py --repo-root ." \
  --sbom-note "No SBOM was produced for this release rehearsal." \
  --testpypi skipped \
  --testpypi-note "<reason when skipped>"
```

Manual, browser, desktop, network, or account-backed examples should be recorded
separately from deterministic release validation results unless the release
owner makes them required for the candidate.

## Release Approval

Record a written decision before publication:

- approval or rejection
- release owner
- approver
- date
- framework commit
- examples commit
- artifact hashes
- validation result locations
- open blockers
- known non-blocking limits
- rollback or hotfix path

The decision should approve publication only when there are no runtime,
data-loss, security, artifact, source, version, tag, documentation, examples,
repository-readiness, or publication-permission blockers.

## TestPyPI

TestPyPI is useful when credentials, project name state, and dependency
resolution make it representative. It is not required when direct artifact
validation is more reliable for the candidate. If skipped, record the reason in
the release manifest.

## Related Documentation

- [Supply Chain](supply-chain.md)
- [Governance and Release Process](governance.md)
- [Repository Settings](repository-settings.md)
- [Security and Privacy](security.md)
