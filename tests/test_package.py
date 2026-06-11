"""Tests for package exports."""

from __future__ import annotations

from importlib import metadata

import rpacore
from rpacore.cli import main


class TestPackageVersion:
    def test_resolve_version_uses_installed_metadata(self, monkeypatch) -> None:
        monkeypatch.setattr(metadata, "version", lambda name: "1.2.3")

        assert rpacore._resolve_version() == "1.2.3"

    def test_resolve_version_falls_back_when_package_missing(self, monkeypatch) -> None:
        def raise_not_found(name: str) -> str:
            raise metadata.PackageNotFoundError(name)

        monkeypatch.setattr(metadata, "version", raise_not_found)

        assert rpacore._resolve_version() == "0.0.0+local"

    def test_exported_version_is_non_empty(self) -> None:
        assert isinstance(rpacore.__version__, str)
        assert rpacore.__version__

    def test_history_types_are_reexported(self) -> None:
        assert rpacore.HistoryEntry.__name__ == "HistoryEntry"
        assert rpacore.HistoryEvent.TRANSACTION_STARTED == "transaction_started"

    def test_serializer_is_reexported(self) -> None:
        assert rpacore.TRANSACTION_FORMAT_VERSION == 1
        assert callable(rpacore.serialize_transaction)


class TestCli:
    def test_version_command_prints_version(self, capsys) -> None:
        result = main(["version"])

        assert result == 0
        assert capsys.readouterr().out.strip() == rpacore.__version__
