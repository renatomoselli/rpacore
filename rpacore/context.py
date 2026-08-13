"""ProcessContext — shared execution context passed to every step."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar, overload

from rpacore.config_validation import ExpectedType, require_config as validate_required
from rpacore.credentials import CredentialProvider, EnvCredentialProvider
from rpacore.exceptions import SystemException
from rpacore.transaction import Artifact, Transaction


T = TypeVar("T")


@dataclass
class ProcessContext:
    """Carries all state a step may need during execution.

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

    def add_artifact(
        self,
        name: str,
        path: str,
        *,
        kind: str = "",
        metadata: dict[str, object] | None = None,
    ) -> Artifact:
        """Register a generated file path as a durable transaction artifact."""
        artifact = Artifact(
            name=name,
            path=path,
            kind=kind,
            metadata={} if metadata is None else metadata,
        )
        self.transaction.artifacts.append(artifact)
        return artifact

    @overload
    def require_state(
        self,
        key: str,
        expected_type: type[T],
        *,
        action: str = "",
    ) -> T: ...


    @overload
    def require_state(
        self,
        key: str,
        expected_type: tuple[type, ...] | None = None,
        *,
        action: str = "",
    ) -> object: ...

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

    @overload
    def optional_state(
        self,
        key: str,
        expected_type: type[T],
        default: T,
        *,
        action: str = "",
    ) -> T: ...


    @overload
    def optional_state(
        self,
        key: str,
        expected_type: tuple[type, ...],
        default: object,
        *,
        action: str = "",
    ) -> object: ...

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

    @overload
    def require_config(
        self,
        key: str,
        expected_type: type[T],
        *,
        action: str = "",
    ) -> T: ...


    @overload
    def require_config(
        self,
        key: str,
        expected_type: tuple[type, ...] | None = None,
        *,
        action: str = "",
    ) -> object: ...

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
            if isinstance(expected_type, tuple):
                return validate_required(values, key, expected_type)
            return validate_required(values, key, expected_type)
        except KeyError as exc:
            raise SystemException(
                f"Missing required {source} key: {key}",
                action=action,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise SystemException(str(exc), action=action) from exc
