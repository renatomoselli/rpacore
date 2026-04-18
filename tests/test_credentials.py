"""Tests for oref.credentials."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from oref.credentials import (
    CredentialNotFoundError,
    CredentialProvider,
    EnvCredentialProvider,
    KeyringCredentialProvider,
    build_credential_provider,
)


class TestEnvCredentialProvider:
    def test_returns_value_from_environ(self) -> None:
        with patch.dict("os.environ", {"OREF_CRED_SAP_PASSWORD": "secret123"}):
            provider = EnvCredentialProvider()
            assert provider.get("sap_password") == "secret123"

    def test_name_uppercased_for_env_key(self) -> None:
        with patch.dict("os.environ", {"OREF_CRED_MY_TOKEN": "tok"}):
            provider = EnvCredentialProvider()
            assert provider.get("my_token") == "tok"

    def test_raises_when_env_var_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            provider = EnvCredentialProvider()
            with pytest.raises(CredentialNotFoundError, match="sap_password"):
                provider.get("sap_password")

    def test_error_message_includes_env_key_name(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            provider = EnvCredentialProvider()
            with pytest.raises(CredentialNotFoundError, match="OREF_CRED_DB_PASS"):
                provider.get("db_pass")

    def test_satisfies_credential_provider_protocol(self) -> None:
        assert isinstance(EnvCredentialProvider(), CredentialProvider)


class TestKeyringCredentialProvider:
    def test_returns_value_from_keyring(self) -> None:
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "keyring_secret"

        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            provider = KeyringCredentialProvider(service="myapp")
            assert provider.get("db_password") == "keyring_secret"

        mock_keyring.get_password.assert_called_once_with("myapp", "db_password")

    def test_default_service_is_oref(self) -> None:
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "val"

        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            provider = KeyringCredentialProvider()
            provider.get("api_key")

        mock_keyring.get_password.assert_called_once_with("oref", "api_key")

    def test_raises_when_keyring_returns_none(self) -> None:
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = None

        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            provider = KeyringCredentialProvider()
            with pytest.raises(CredentialNotFoundError, match="api_key"):
                provider.get("api_key")

    def test_raises_import_error_when_keyring_not_installed(self) -> None:
        with patch.dict("sys.modules", {"keyring": None}):
            provider = KeyringCredentialProvider()
            with pytest.raises(ImportError, match="pip install oref\\[keyring\\]"):
                provider.get("api_key")

    def test_satisfies_credential_provider_protocol(self) -> None:
        assert isinstance(KeyringCredentialProvider(), CredentialProvider)


class TestBuildCredentialProvider:
    def test_env_returns_env_provider(self) -> None:
        provider = build_credential_provider("env")
        assert isinstance(provider, EnvCredentialProvider)

    def test_keyring_returns_keyring_provider(self) -> None:
        provider = build_credential_provider("keyring")
        assert isinstance(provider, KeyringCredentialProvider)

    def test_unknown_value_falls_back_to_env(self) -> None:
        provider = build_credential_provider("unknown_provider")
        assert isinstance(provider, EnvCredentialProvider)

    def test_unknown_value_logs_warning(self) -> None:
        mock_logger = MagicMock()
        build_credential_provider("unknown_provider", logger=mock_logger)
        mock_logger.warning.assert_called_once()


class TestProcessContextCredentials:
    def test_default_credentials_is_env_provider(self) -> None:
        from oref.context import ProcessContext
        from oref.transaction import Transaction

        ctx = ProcessContext(transaction=Transaction(reference="T1"))
        assert isinstance(ctx.credentials, EnvCredentialProvider)

    def test_custom_provider_accepted(self) -> None:
        from oref.context import ProcessContext
        from oref.transaction import Transaction

        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "val"

        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            provider = KeyringCredentialProvider()

        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            credentials=provider,
        )
        assert isinstance(ctx.credentials, KeyringCredentialProvider)

    def test_skill_can_access_credential_via_ctx(self) -> None:
        from oref.context import ProcessContext
        from oref.transaction import Transaction

        with patch.dict("os.environ", {"OREF_CRED_API_KEY": "my_secret"}):
            ctx = ProcessContext(transaction=Transaction(reference="T1"))
            assert ctx.credentials.get("api_key") == "my_secret"
