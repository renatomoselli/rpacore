"""Credential management for rpacore.

Direct provider classes are available for known-good wiring. Use
build_credential_provider() when converting configuration strings into provider
instances because it validates supported provider names.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from rpacore._validation import type_error, value_error


class CredentialNotFoundError(Exception):
    """Raised when a requested credential cannot be found."""


SUPPORTED_CREDENTIAL_PROVIDERS = frozenset({"env", "keyring"})


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

    Convention: credential name is uppercased and prefixed with RPACORE_CRED_.

    Example:
        EnvCredentialProvider().get("sap_password")
        → reads os.environ["RPACORE_CRED_SAP_PASSWORD"]
    """

    def get(self, name: str) -> str:
        env_key = f"RPACORE_CRED_{name.upper()}"
        value = os.environ.get(env_key)
        if value is None:
            raise CredentialNotFoundError(
                f"Credential {name!r} not found via {self.__class__.__name__}. "
                f"Expected environment variable {env_key!r}."
            )
        return value


class KeyringCredentialProvider:
    """Reads credentials from the system keyring via the keyring library.

    Requires: pip install rpacore[keyring]

    Falls back with a clear error if keyring is not installed.
    """

    def __init__(self, service: str = "rpacore") -> None:
        self.service: str = service

    def get(self, name: str) -> str:
        try:
            import keyring  # type: ignore[import]
        except ImportError:
            raise ImportError(
                "keyring is not installed. Run: pip install rpacore[keyring]"
            ) from None

        value = keyring.get_password(self.service, name)
        if value is None:
            raise CredentialNotFoundError(
                f"Credential {name!r} not found via {self.__class__.__name__}. "
                f"Searched keyring service {self.service!r}."
            )
        return value


def build_credential_provider(provider: str) -> CredentialProvider:
    """Instantiate a CredentialProvider from a config string.

    Supported values: "env", "keyring".
    Unknown values raise ValueError instead of falling back silently.
    """
    if not isinstance(provider, str):
        raise type_error("credential_provider", "str", provider)
    providers: dict[str, type[CredentialProvider]] = {
        "env": EnvCredentialProvider,
        "keyring": KeyringCredentialProvider,
    }
    if provider in SUPPORTED_CREDENTIAL_PROVIDERS:
        return providers[provider]()
    raise value_error("credential_provider", "one of env, keyring", provider)
