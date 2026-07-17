# RPA Core Agent Guide

These rules apply repository-wide. A nested `AGENTS.md` may add stricter local rules but may not weaken them.

## Operating protocol

1. Run `git status --short --branch`; inspect staged and unstaged diffs for files you may touch. Existing changes are user-owned—preserve their content and staged state.
2. Classify the request. Reviews, diagnosis, explanations, and status reports are read-only unless the user also asks for a fix.
3. Read the minimum authority set below and exactly the governing task/decision. Do not bulk-load archives, old handoffs, or unrelated evidence.
4. Before editing, bound the work: goal, non-goals, affected public/durability contracts, likely files, and acceptance evidence.
5. Inspect implementation, focused tests, public exports, and relevant schema/migrations/docs.
6. Implement the smallest coherent vertical slice, prove its failure path, and validate from narrow to broad.
7. Re-read the final diff. Code, tests, public docs, migrations, compatibility notes, and task state must agree where affected.

## Authority router

Always read [docs/project-context.md](docs/project-context.md), use [docs/README.md](docs/README.md) to select only affected public contracts, and inspect live code/tests.

For roadmap work, when the private planning tree exists, read `.internal/plans/README.md`, `.internal/plans/roadmap.md`, exactly the current linked task, and only its referenced decisions/evidence. The roadmap alone owns global order. `.internal/archive/` is historical, never a next-step list. If the private tree is absent, do not reconstruct or infer it.

The user request defines the desired outcome; the current task defines its boundary. Live `rpacore/`, tests, public docs, and installed/released artifacts define actual behavior. Reconcile disagreements within scope or report the exact blocker—never silently choose one source.

Ignored reviews/evidence are inputs only; reproduce findings against the live tree. `.rpiv/` belongs to the agent harness, while framework validation output belongs under `validation-artifacts/`.

## Hard stops

- Work on one bounded task or reproduced defect. Missing future/rejected behavior is not automatically a defect.
- Do not reset, restore, stash, stage, broadly format, normalize line endings, or clean unrelated work.
- Do not weaken tests, hide errors, or replace installed-artifact/integration proof with mocks to make checks pass.
- Do not add a compatibility shim, abstraction, backend, protocol, or dependency without demonstrated need and the repository decision gate.
- Do not silently edit `rpacore-examples`; record its impact unless that repository is explicitly in scope.
- After a failure, inspect the exact error and change one causal hypothesis; never rerun unchanged. After two failed fixes, diagnose; after three evidence-based blockers, stop and report.
- Do not commit, push, tag, publish, or rewrite release artifacts without explicit authorization.

## Architecture, durability, and compatibility

- RPA Core stays deterministic, stateful, auditable, resilient, local-first, and usable without an orchestrator/service.
- Never introduce runtime AI behavior, AI-controlled execution, or AI runtime dependencies.
- Framework code belongs in `rpacore/`. Repository `examples/` are integration fixtures; fuller/domain automation belongs in user projects or the examples repository.
- Keep public APIs small, typed, explicit, and primarily exported from top-level `rpacore`; do not expose implementation submodules accidentally.
- Prefer flat, concrete, synchronous, visible control flow. Add abstractions only after a real repeated use case proves the boundary.
- Standard library is the default, not dogma. A runtime dependency requires a documented correctness guarantee, Windows behavior, packaging/transitive cost, and skill-author impact.
- Keep business versus system failure classification explicit; never catch a failure merely to report success.
- Durable state/metadata remain JSON-safe; runtime handles stay outside durable state.
- Queue delivery is at least once. Preserve claim/revision fencing, attempt identity, poison disposition, checkpoint truth, and idempotent side effects.
- Read-only inspection/query paths must not create, migrate, or mutate databases or journals.
- SQLite changes require a version bump, forward sequential transactional migration, tests from every supported public schema, and rejection of unknown future schemas before mutation.
- APIs, CLI behavior, schemas, exports, generated-project persistence, and machine-readable output are compatibility boundaries. Breaking them requires a correctness/security/release reason, migration path, changelog, docs, and focused proof.
- Atomic publication writes a complete temporary sibling and replaces the destination only after success. Machine-readable stdout keeps diagnostics on stderr.

Split a module before adding behavior when it mixes unrelated responsibilities, layers, or independently testable phases. Extract a cohesive rule/adapter with focused tests; never split only by line count or create forwarding wrappers.

## Change-impact router

- Public API/imports: `rpacore/__init__.py`, `docs/api.md`, `docs/public-submodules.md`, typing/tests, and `CHANGELOG.md`.
- Persistence/queue/recovery: `docs/durability.md`, schema constants/migrations, concurrency/fencing, rollback, and failure paths.
- CLI/config/generated projects: matching reference docs plus exit codes, stdout/stderr, path resolution, and installed-package tests.
- Export/report/logging/notifications: versioning and sensitive-data contracts.
- Runtime dependency: `docs/runtime-dependencies.md`.
- Release/package: `docs/governance.md`, `docs/supply-chain.md`, and `docs/release-rehearsal.md`; publication requires separate approval.

Public docs stay reader-facing: exclude private task IDs, review artifacts, test totals, and phase mechanics. State whether each framework behavior change requires an examples-repository update.

## Validation

Run focused, non-interactive proof first:

```powershell
# Behavior/shared runtime
python -m pytest <focused-test-files> -q
python -m pytest -q

# Tracked documentation
python scripts/verify_docs.py --repo-root .
git diff --check

# Private plans, only when changed
python .internal\plans\tools\validate_plans.py

# Public API, CLI, packaging, generated-project, import, or version changes
python scripts/validate_installed_wheel.py --repo-root .
```

Add `python -m build` and artifact metadata checks only for package/release work. SQLite tests/smoke runs use temporary paths or explicit disposable work directories—never repository/default `rpacore.db`, `queue.db`, or user-valued files. Never print credentials or sensitive transaction contents.

Report exact commands/results and skipped gates with reasons. Never claim a check passed from an earlier run.

## Completion

Report outcome, changed contracts/files, compatibility and migration impact, examples-repository impact, remaining risks, and final staged/unstaged state. Do not call work complete because code exists, a prior run was green, or an ignored review says so.
