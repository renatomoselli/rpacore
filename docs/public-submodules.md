# Public Submodule Policy

For the current `0.3.0` development API, the supported import boundary is the
top-level `rpacore` package. The latest published API remains `v0.2.0`; use its
[versioned policy](https://github.com/renatomoselli/rpacore/blob/v0.2.0/docs/public-submodules.md)
when working from the published package.

Use:

```python
from rpacore import Engine, ExecutionTransition, Transaction, resolve_config_paths
```

Avoid importing from implementation submodules in application code unless a
future release explicitly documents that submodule as public.

This policy keeps the compatibility surface small while still allowing the
package internals to stay organized by module. The complete supported symbol list
is in [API Reference](api.md).
