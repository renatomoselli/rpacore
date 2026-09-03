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
