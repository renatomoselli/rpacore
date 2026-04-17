"""ProcessContext — shared execution context passed to every skill."""

from __future__ import annotations

from dataclasses import dataclass, field

from oref.transaction import Transaction


@dataclass
class ProcessContext:
    """Carries all state a skill may need during execution.

    Attributes:
        transaction: The active transaction being executed.
        config:      Framework configuration (from config.toml / load_config()).
        data:        Mutable dict for skills to share data within one run.
    """

    transaction: Transaction
    config: dict[str, object] = field(default_factory=dict)
    data: dict[str, object] = field(default_factory=dict)
