"""Credential management for OREF."""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from oref.logger import get_logger


class CredentialNotFoundError(Exception):
    """Raised when a requested credential cannot be found."""


@runtime_checkable
class CredentialProvider(Protocol):
    """Protocol for credential providers.

    All providers must implement get(name) -> str.
    """

    def get(self, name: str) -> str:
        """Return the credential value for the given name.

        Raises CredentialNotFoundError if the credential is absent.
        """
        ...


class EnvCredentialProvider:
    """Reads credentials from environment variables.

    Convention: credential name is uppercased and prefixed with OREF_CRED_.

    Example:
        EnvCredentialProvider().get("sap_password")
        → reads os.environ["OREF_CRED_SAP_PASSWORD"]
    """

    def get(self, name: str) -> str:
        env_key = f"OREF_CRED_{name.upper()}"
        value = os.environ.get(env_key)
        if value is None:
            raise CredentialNotFoundError(
                f"Credential {name!r} not found. "
                f"Set environment variable {env_key!r}."
            )
        return value


class KeyringCredentialProvider:
    """Reads credentials from the system keyring via the keyring library.

    Requires: pip install oref[keyring]

    Falls back with a clear error if keyring is not installed.
    """

    def __init__(self, service: str = "oref") -> None:
        self.service: str = service

    def get(self, name: str) -> str:
        try:
            import keyring  # type: ignore[import]
        except ImportError:
            raise ImportError(
                "keyring is not installed. Run: pip install oref[keyring]"
            ) from None

        value = keyring.get_password(self.service, name)
        if value is None:
            raise CredentialNotFoundError(
                f"Credential {name!r} not found in keyring service {self.service!r}."
            )
        return value


def build_credential_provider(
    provider: str,
    *,
    logger: logging.Logger | None = None,
) -> CredentialProvider:
    """Instantiate a CredentialProvider from a config string.

    Supported values: "env", "keyring".
    Unknown values log a warning and fall back to EnvCredentialProvider.
    """
    log = logger if logger is not None else get_logger()
    if provider == "env":
        return EnvCredentialProvider()
    if provider == "keyring":
        return KeyringCredentialProvider()
    log.warning(
        "Unknown credential_provider %r — falling back to EnvCredentialProvider",
        provider,
    )
    return EnvCredentialProvider()
