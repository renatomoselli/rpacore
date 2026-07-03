"""Tests for package exports."""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

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

    def test_pyproject_license_metadata_is_build_backend_compatible(self) -> None:
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"

        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        assert pyproject["project"]["license"] == "Apache-2.0"
        assert pyproject["project"]["license-files"] == ["LICENSE", "NOTICE"]
        assert "pytest>=8.1.1" in pyproject["project"]["optional-dependencies"]["dev"]
        assert "twine>=5.1.0" in pyproject["project"]["optional-dependencies"]["dev"]
        assert "wheel>=0.46.2" in pyproject["project"]["optional-dependencies"]["dev"]
        assert pyproject["project"]["urls"]["Documentation"].endswith("/tree/main/docs")
        assert "license-files" not in pyproject.get("tool", {}).get("setuptools", {})
        for license_file in pyproject["project"]["license-files"]:
            assert (pyproject_path.parent / license_file).is_file()

    def test_public_repository_readiness_files_exist(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        required_files = [
            "AUTHORS.md",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "LICENSE",
            "MAINTAINERS.md",
            "NOTICE",
            "SECURITY.md",
            "SUPPORT.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/ISSUE_TEMPLATE/bug_report.md",
            ".github/ISSUE_TEMPLATE/documentation.md",
            ".github/ISSUE_TEMPLATE/feature_request.md",
            ".github/ISSUE_TEMPLATE/config.yml",
        ]

        missing = [path for path in required_files if not (repo_root / path).is_file()]

        assert missing == []

    def test_public_repository_readiness_files_have_content(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        markdown_files = [
            "AUTHORS.md",
            "CODE_OF_CONDUCT.md",
            "CONTRIBUTING.md",
            "MAINTAINERS.md",
            "SECURITY.md",
            "SUPPORT.md",
        ]
        for path in markdown_files:
            text = (repo_root / path).read_text(encoding="utf-8")
            lines = [line for line in text.splitlines() if line.strip()]
            assert lines[0].startswith("# "), path
            assert len(lines) >= 3, path

    def test_license_and_notice_content_match_package_metadata(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        license_text = (repo_root / "LICENSE").read_text(encoding="utf-8")
        notice_text = (repo_root / "NOTICE").read_text(encoding="utf-8")

        assert "Apache License" in license_text
        assert "Version 2.0, January 2004" in license_text
        assert "Copyright 2026 Renato Moselli" in notice_text

    def test_pull_request_template_covers_release_sensitive_contribution_requirements(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        contributing_text = (repo_root / "CONTRIBUTING.md").read_text(encoding="utf-8")
        template_text = (repo_root / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        )

        assert "CHANGELOG.md" in contributing_text
        assert "CHANGELOG.md" in template_text
        assert "Runtime dependency changes require a documented dependency decision." in contributing_text
        assert "Runtime dependency decisions were documented when relevant." in template_text

    def test_history_types_are_reexported(self) -> None:
        assert rpacore.HistoryEntry.__name__ == "HistoryEntry"
        assert rpacore.HistoryEvent.TRANSACTION_STARTED == "transaction_started"

    def test_serializer_is_reexported(self) -> None:
        assert rpacore.TRANSACTION_FORMAT_VERSION == 1
        assert callable(rpacore.serialize_transaction)

    def test_public_api_exports_are_deliberate(self) -> None:
        expected = {
            "Artifact",
            "ArtifactReport",
            "BusinessException",
            "CredentialNotFoundError",
            "CredentialProvider",
            "EmailNotifier",
            "Engine",
            "EnvCredentialProvider",
            "ExecutionValidationError",
            "HistoryEntry",
            "HistoryEvent",
            "KeyringCredentialProvider",
            "Notifier",
            "ProcessContext",
            "ProjectManifest",
            "QueueItem",
            "QueueLeaseLostError",
            "QueueProvider",
            "QueueRunSummary",
            "QueueStatus",
            "Skill",
            "SkillReport",
            "SqliteQueue",
            "Status",
            "SystemException",
            "TRANSACTION_FORMAT_VERSION",
            "Transaction",
            "TransactionReport",
            "WebhookNotifier",
            "build_credential_provider",
            "build_notifiers",
            "configure_logger",
            "dispatch",
            "find_project_manifest",
            "generate_report",
            "get_logger",
            "list_transactions",
            "load_config",
            "load_project_manifest",
            "load_transaction",
            "optional_config",
            "render_html",
            "render_text",
            "require_config",
            "require_section",
            "resolve_config_path",
            "resolve_config_paths",
            "resolve_project_entrypoint",
            "resume_transaction",
            "run_queue_loop",
            "save_transaction",
            "serialize_transaction",
        }
        actual = set(rpacore.__all__)

        assert not actual - expected, f"Unexpected public exports: {sorted(actual - expected)}"
        assert not expected - actual, f"Missing public exports: {sorted(expected - actual)}"
        assert rpacore.__all__ == sorted(rpacore.__all__)

    def test_rejected_features_are_not_public_exports(self) -> None:
        rejected = {
            "SkillTestCase",
            "pipeline_from_config",
            "resume_cli_transaction",
            "EventBus",
            "EngineEventBus",
        }

        assert rejected.isdisjoint(rpacore.__all__)


class TestCli:
    def test_version_command_prints_version(self, capsys) -> None:
        result = main(["version"])

        assert result == 0
        assert capsys.readouterr().out.strip() == rpacore.__version__
