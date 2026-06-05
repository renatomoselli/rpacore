# Pre-v0.1.0 API Migration Inventory

This inventory records breaking API changes made before the first supported
public release of RPA Core.

This is a maintainer-facing release-planning document for pre-`v0.1.0`
changes. It avoids compatibility shims for unreleased APIs and records the
intended migration path before the first public compatibility baseline.

## Compatibility Policy

`v0.1.0` is the first supported public compatibility baseline. APIs changed
before that release are private-development APIs, not supported public
contracts.

Breaking changes before `v0.1.0` are allowed when they make the framework more
deterministic, auditable, durable, or maintainable. Each breaking change
must update this inventory in the same change that updates code, tests,
templates, or documentation.

Do not add aliases, compatibility modes, or deprecation shims for
private-development APIs unless a concrete external adopter requires one. If a
shim is accepted, record the adopter need, removal rule, and test coverage here.

Broad `rpacore-examples` updates are deferred until the release-adoption pass.
Earlier breaking changes record required external example updates here instead
of preserving old APIs inside the framework.

Installable pre-release builds may use normal `0.1.0aN` versions for external
testers. They do not create a `0.0.x` compatibility promise.

## Required Update Rule

Every later pre-`v0.1.0` change that affects public or documented behavior must
add or update one row below before the change is complete. Each row must include
one of:

- a direct replacement
- a migration example
- a removal rationale

Rows marked `pending` are planned breaking changes that have not landed yet.
Rows marked `done` have been implemented in this repository.

## Inventory

| Status | API or Behavior | Replacement or Disposition | Release Milestone |
|--------|------------------|----------------------------|------------|
| done | `Skill(..., timeout=...)` and `skill.timeout` | Removed. Configure I/O timeouts in the library doing the work. Hard worker deadlines belong outside the in-process engine. | pre-`v0.1.0` |
| done | Generic in-process skill timeout guarantee | Removed. Python threads cannot be safely terminated, and process termination cannot make arbitrary external side effects reversible. | pre-`v0.1.0` |
| done | Malformed transaction wiring could reach execution | `Engine.run()` now validates before skill execution and raises `ExecutionValidationError` for permanent structural defects. Fix transaction references, skill names, and execution orders rather than retrying. | pre-`v0.1.0` |
| pending | Top-level config key `db_path` | Rename to `transaction_db_path`. Migration example: `db_path = "rpacore.db"` becomes `transaction_db_path = "rpacore.db"`. | pre-`v0.1.0` |
| pending | `ProcessContext.data` as a catch-all shared dictionary | Split into durable `ProcessContext.state` and ephemeral `ProcessContext.resources`. Migration example: durable values move from `ctx.data["invoice"]` to `ctx.state["invoice"]`; handles, clients, and sessions move to `ctx.resources`. | pre-`v0.1.0` |
| pending | `ProcessContext.require_data()` | Rename to `require_state()` for durable transaction state. Resource access is explicit through `resources`. | pre-`v0.1.0` |
| pending | `ProcessContext.optional_data()` | Rename to `optional_state()` for durable transaction state. Resource access is explicit through `resources`. | pre-`v0.1.0` |
| pending | Runner `on_start` lifecycle hook | Replace with `resource_scope`, a paired setup/cleanup resource contract. The temporary `on_start` intermediate state must not remain in the first public compatibility baseline. | pre-`v0.1.0` |
| pending | Queue `claim_timeout` | Rename to `lease_timeout` when queue leases are made explicit. Migration example: `claim_timeout = 30` becomes `lease_timeout = 30`. | pre-`v0.1.0` |

## External Example Notes

Broad `rpacore-examples` adoption is intentionally deferred to the
release-adoption pass. Known example updates likely include:

- remove any `Skill.timeout` usage
- rename `db_path` to `transaction_db_path`
- move durable skill state from `ctx.data` to `ctx.state`
- move runtime clients, sessions, and handles to `ctx.resources`
- replace `require_data` / `optional_data` calls
- replace `on_start` setup with `resource_scope`
- rename queue `claim_timeout` to `lease_timeout`
