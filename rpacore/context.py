"""ProcessContext — shared execution context passed to every skill."""

from __future__ import annotations

from dataclasses import dataclass, field

from rpacore.config_validation import ExpectedType, require_config as validate_required
from rpacore.credentials import CredentialProvider, EnvCredentialProvider
from rpacore.exceptions import SystemException
from rpacore.transaction import Transaction


@dataclass
class ProcessContext:
    """Carries all state a skill may need during execution.

    Attributes:
        transaction:  The active transaction being executed.
        config:       Framework configuration (from config.toml / load_config()).
        state:        Durable transaction state persisted with the transaction.
        resources:    Ephemeral runtime objects available only during this run.
        credentials:  Provider for named credentials. Defaults to EnvCredentialProvider.
    """

    transaction: Transaction
    config: dict[str, object] = field(default_factory=dict)
    resources: dict[str, object] = field(default_factory=dict)
    credentials: CredentialProvider = field(default_factory=EnvCredentialProvider)

    @property
    def state(self) -> dict[str, object]:
        """Return the transaction's durable JSON-safe state mapping."""
        return self.transaction.state

    def require_state(
        self,
        key: str,
        expected_type: ExpectedType | None = None,
        *,
        action: str = "",
    ) -> object:
        """Return required durable state or raise an actionable SystemException."""
        return self._require_value(
            self.state,
            key,
            expected_type,
            source="state",
            action=action,
        )

    def optional_state(
        self,
        key: str,
        expected_type: ExpectedType,
        default: object,
        *,
        action: str = "",
    ) -> object:
        """Return optional durable state or default, validating present values."""
        if key not in self.state:
            return default
        return self._require_value(
            self.state,
            key,
            expected_type,
            source="state",
            action=action,
        )

    def require_config(
        self,
        key: str,
        expected_type: ExpectedType | None = None,
        *,
        action: str = "",
    ) -> object:
        """Return required configuration or raise an actionable SystemException."""
        return self._require_value(
            self.config,
            key,
            expected_type,
            source="config",
            action=action,
        )

    @staticmethod
    def _require_value(
        values: dict[str, object],
        key: str,
        expected_type: ExpectedType | None,
        *,
        source: str,
        action: str,
    ) -> object:
        if key not in values:
            raise SystemException(
                f"Missing required {source} key: {key}",
                action=action,
            )
        if expected_type is None:
            return values[key]
        try:
            return validate_required(values, key, expected_type)
        except KeyError as exc:
            raise SystemException(
                f"Missing required {source} key: {key}",
                action=action,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise SystemException(str(exc), action=action) from exc
