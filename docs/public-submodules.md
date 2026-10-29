# Public Submodule Policy

The supported import boundary is the top-level `rpacore` package.

Use:

```python
from rpacore import Engine, ExecutionTransition, Transaction, resolve_config_paths
```

Avoid importing from implementation submodules in application code unless a
future release explicitly documents that submodule as public.

This policy keeps the compatibility surface small while still allowing the
package internals to stay organized by module. The complete supported symbol list
is in [API Reference](api.md).

Queue inspection follows the same boundary: import `QueuePage`, `get_queue_item`,
`query_queue_items`, `query_queue_attempts` and `query_queue_poison_events` from
`rpacore`. The `_queue_read` and `_queue_cli` modules are private implementation
details. Delivery timing in `_queue_retry` is private as well; applications use
`SqliteQueue` settings and the top-level `QueueItem.available_at` field.
