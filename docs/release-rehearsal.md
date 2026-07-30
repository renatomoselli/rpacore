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
- reconciliation of every tracked reader-facing Markdown document: `README.md`,
  `CHANGELOG.md`, `docs/`, and root policy/community documents; check release
  wording, installation and migration guidance, public links, and support routes
- resolved security, supply-chain, governance, and support decisions

Both repositories should be clean before rehearsal starts. Any framework code,
schema, API, CLI, storage, export, generated-project behavior, public
documentation, package metadata, release workflow/script/tool constraint, or
examples dependency-metadata change after this point invalidates the candidate
and requires a new rehearsal.

The rehearsal uses the isolated PEP 517 build environment declared by the
project. It may need access to the declared build requirements; it does not
silently substitute host build tooling.

## Manifest Contents

The release manifest should contain:

- framework and examples commit IDs
- tag name
- Python and operating-system matrix results
- embedded SQLite version, effective transaction/queue journal modes, and exact
  build-tool versions used by the candidate gate
- wheel and source distribution names, durable paths, sizes, and SHA-256 hashes
- dependency inventory location
- SBOM location, or a note that no SBOM was produced
- documentation verification command and result
- expected PyPI metadata, including project URLs and license files
- release owner and approver
- release-candidate validation results
- external examples wheel-validation results
- TestPyPI result, or a note that TestPyPI was intentionally skipped

Use the prebuilt, hashed artifacts recorded in the manifest for publication. Do
not rebuild artifacts during upload. A publication run must name one passing
Release candidate workflow run and provide its full framework/examples commits
and both artifact SHA-256 values. It downloads only that run's locked artifact
set and aggregate result, then rejects any mismatch in run identity, commits,
version/tag, filenames, sizes, hashes, or artifact-set digest. Successful
candidate artifacts are kept as an immutable content-addressed set under the
candidate output directory's `artifacts/` directory. Manifest preparation
verifies those files again before it records their upload paths.

## Required Rehearsal Checks

Run the framework release-candidate validation from a clean repository state:

```bash
python scripts/validate_release_candidate.py \
  --repo-root . \
  --examples-repo ../rpacore-examples \
  --examples-wheel-matrix \
  --output-dir validation-artifacts/release-candidate-validation
```

With `--examples-wheel-matrix`, the candidate validator runs the frozen,
deterministic external examples against its exact candidate wheel and records
their evidence alongside the candidate result. Do not run a second checkout
build for the same rehearsal. A standalone examples rerun must use
`--prebuilt-wheel` with the recorded candidate wheel.

For the full supported Windows and Linux matrix, use the manually dispatched
**Release candidate** workflow with the exact framework and examples commits.
Before artifact construction, that workflow verifies the frozen commit is the
dispatched `main` head, verifies the frozen package version, unreleased changelog
entry, and release tag against GitHub and PyPI, and runs the documentation
verifier; an existing tag, release, package version, unavailable registry, or
documentation mismatch stops the candidate. It also verifies the frozen examples
revision's dependency metadata. CI package, candidate, and publish workflows
install the same pinned release-toolchain requirements; candidate and publish
run Twine against the exact candidate artifacts. The workflow then builds one
locked wheel/source-distribution set, and every platform cell validates that
downloaded set rather than rebuilding it. The examples job proves the installed
RPA Core package both before and after example requirements are installed;
requirements must not replace the supplied candidate wheel. Preserve the
uploaded artifact set, preflight, eight platform-cell, examples-wheel, and
aggregate evidence artifacts with the release decision.
The aggregate result is the definitive matrix verdict and fails closed when any
required evidence is missing. Workflow-artifact retention is not permanent
release storage.

The publish workflow requires the candidate run to be a successful manually
dispatched Release candidate run on `main` for the locked framework commit. Its
required release-version input must match the candidate lock; it derives the
artifact filenames from that version and includes both version and run ID in the
typed confirmation. It rejects an existing GitHub tag or release for the target
version and an existing PyPI version; an unavailable or unexpected remote
response is a blocker. Both publish jobs verify that the workflow dispatch and
fresh `main` checkout still equal the frozen framework commit, rerun public
documentation and package-version checks, and re-download and re-hash the same
named candidate set. The protected PyPI job repeats those checks after approval,
immediately before upload.

Then verify:

- required CI jobs are green
- documentation verification passed
- every frozen reader-facing Markdown document agrees with the candidate's
  version/release line, public API and compatibility posture, installation and
  migration guidance, links, and support routes; correct any discrepancy before
  artifact construction. Private planning, validation evidence, and agent-harness
  Markdown are not public release surfaces
- complete one claim-by-claim semantic review of behavioral public prose; the
  automated version-line checks cannot determine whether a behavior claim is
  still true
- wheel and source distribution pass metadata checks
- wheel and source distribution contain `LICENSE` and `NOTICE`
- wheel and source distribution do not contain examples, private notes, review
  artifacts, local caches, credentials, or local checkout paths
- wheel install smoke test passes
- source distribution install smoke test passes
- generated project scaffold runs from the installed package
- deterministic representative examples pass from the built wheel
- installed-wheel SQLite probes confirm the required rollback (`delete`) journal
  mode for disposable transaction and queue databases
- repository settings match [Repository Settings](repository-settings.md)

Prepare the release manifest and approval draft from the candidate result and
the linked external-examples evidence:

```bash
python scripts/prepare_release_manifest.py \
  --repo-root . \
  --examples-repo ../rpacore-examples \
  --release-candidate-validation-results validation-artifacts/release-candidate-validation/release-candidate-validation-results.json \
  --examples-wheel-validation-results validation-artifacts/release-candidate-validation/examples-wheel-validation/examples-wheel-validation.json \
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
