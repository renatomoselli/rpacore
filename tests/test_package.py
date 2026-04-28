"""Tests for package exports."""

from __future__ import annotations

from importlib import metadata

import oref


class TestPackageVersion:
    def test_resolve_version_uses_installed_metadata(self, monkeypatch) -> None:
        monkeypatch.setattr(metadata, "version", lambda name: "1.2.3")

        assert oref._resolve_version() == "1.2.3"

    def test_resolve_version_falls_back_when_package_missing(self, monkeypatch) -> None:
        def raise_not_found(name: str) -> str:
            raise metadata.PackageNotFoundError(name)

        monkeypatch.setattr(metadata, "version", raise_not_found)

        assert oref._resolve_version() == "0.0.0+local"

    def test_exported_version_is_non_empty(self) -> None:
        assert isinstance(oref.__version__, str)
        assert oref.__version__