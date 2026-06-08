# RPA Core Agent Guide

## Non-Negotiables

- RPA Core must stay deterministic, stateful, auditable, and resilient.
- Do not introduce runtime AI behavior or AI runtime dependencies.
- Keep framework code in `rpacore/` separate from user automation in `skills/`.
- Prefer stdlib solutions over third-party runtime dependencies.
- Prefer flat, concrete designs. Do not add abstraction layers unless a real
  use case requires them.

## Editing Rules

- Keep functions small, explicit, and readable.
- Avoid magic behavior and hidden control flow.
- Prioritize maintainability over cleverness.
- Public functions and public attributes must have type hints.
- All behavior changes must come with corresponding tests.
- Match the current architecture before inventing a new one.
- Choose clarity over flexibility; choose simpler structures over reusable
  abstractions unless reuse is already proven.
- Follow existing naming: modules/functions/variables `snake_case`,
  classes `PascalCase`, constants `UPPER_CASE`, tests `test_<module>.py`.

## Architectural Guardrails

- Skills are the unit of work. Users implement them by subclassing `Skill`.
- Retry behavior must stay deterministic and state-driven.
- Exception classification must remain explicit: business vs system.
- Persistence is part of the core design, not an optional afterthought.
- `main.py` is the user wiring layer, not the framework core.

## Development Expectations

- Build in small increments. Each commit introduces one clear concept.
- Keep documentation in lockstep with framework behavior. When touching a
  documented area, update the related doc in the same change; when touching an
  undocumented public or runtime behavior, add focused documentation or record
  why it remains intentionally internal.
- Run the test suite before considering work complete.

For broader context, see [docs/project-context.md](docs/project-context.md).
