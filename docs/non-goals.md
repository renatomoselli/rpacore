# Non-Goals

RPA Core intentionally does not ship:

- runtime AI behavior or AI service calls
- a managed execution service or remote worker protocol
- automatic step discovery or configuration-defined pipelines
- generic per-step hard timeouts or arbitrary cancellation
- async execution
- an event bus
- a custom step test framework
- CLI resume/restart commands
- distributed queue backends
- artifact content storage or upload
- browser, desktop, PDF, spreadsheet, or HTTP action wrappers

These omissions are part of the current local-first Core contract. User automations
remain ordinary Python projects, and side-effecting work belongs in user-authored
steps.
