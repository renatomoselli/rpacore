from __future__ import annotations

import re
from urllib.parse import urlparse
from pathlib import Path

from scripts.verify_docs import (
    ROOT_MARKDOWN_DOCS,
    _anchors,
    _check_api_reference,
    _check_forbidden_patterns,
    _check_links,
    _is_dev_url,
    _public_exports,
    _release_series,
    _run_check,
    _slug,
    main,
    verify_docs,
)


def _write_minimal_repo(root: Path, *, release_metadata: bool = False) -> None:
    (root / "docs").mkdir()
    (root / "rpacore").mkdir()
    (root / "README.md").write_text("# Root\n\n[Docs](docs/README.md)\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (root / "SECURITY.md").write_text("# Security\n", encoding="utf-8")
    (root / "CONTRIBUTING.md").write_text("# Contributing\n", encoding="utf-8")
    (root / "CODE_OF_CONDUCT.md").write_text("# Code of Conduct\n", encoding="utf-8")
    (root / "SUPPORT.md").write_text("# Support\n", encoding="utf-8")
    (root / "AUTHORS.md").write_text("# Authors\n", encoding="utf-8")
    (root / "MAINTAINERS.md").write_text("# Maintainers\n", encoding="utf-8")
    (root / "docs" / "README.md").write_text("# Docs\n\n[API](api.md)\n", encoding="utf-8")
    (root / "docs" / "api.md").write_text("# API Reference\n\n`Engine`\n", encoding="utf-8")
    (root / "rpacore" / "__init__.py").write_text('__all__ = ["Engine"]\n', encoding="utf-8")
    if release_metadata:
        (root / "README.md").write_text(
            "# Root\n\n[Docs](docs/README.md)\n",
            encoding="utf-8",
        )
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## v0.3.0 - Unreleased\n\n## v0.2.0 - 2026-07-29\n",
            encoding="utf-8",
        )
        (root / "SECURITY.md").write_text("# Security\n\n| Version | Supported |\n| --- | --- |\n| 0.2.x | Yes |\n", encoding="utf-8")
        (root / "SUPPORT.md").write_text("# Support\n\nThe latest public `0.2.x` release line.\n", encoding="utf-8")
        (root / "docs" / "README.md").write_text(
            "# Docs\n\n[API](api.md)\n",
            encoding="utf-8",
        )
        (root / "pyproject.toml").write_text('[project]\nversion = "0.3.0"\n', encoding="utf-8")


def test_verify_docs_main_returns_zero_for_current_repo() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert main(["--repo-root", str(repo_root)]) == 0


def test_verify_docs_reports_non_literal_public_exports(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "rpacore" / "__init__.py").write_text("__all__ = tuple(['Engine'])\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "__all__ must be a list or tuple literal for docs verification"
    ]


def test_verify_docs_reports_release_line_mismatch(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "SUPPORT.md").write_text("# Support\n\nThe latest public `0.1.x` release line.\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert [(finding.path, finding.message) for finding in findings] == [
        (Path("SUPPORT.md"), "release version documentation must contain: `0.2.x`")
    ]


def test_verify_docs_reports_top_changelog_version_mismatch(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v0.1.1 - Unreleased\n\n## v0.2.0 - 2026-07-29\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [(finding.path, finding.message) for finding in findings] == [
        (
            Path("CHANGELOG.md"),
            "expected exactly one Unreleased heading at the top for v0.3.0",
        ),
        (Path("CHANGELOG.md"), "top changelog release version must be 0.3.0, got 0.1.1"),
    ]


def test_verify_docs_reports_expected_release_version_mismatch(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)

    findings = verify_docs(tmp_path, expected_release_version="0.3.1")

    assert [(finding.path, finding.message) for finding in findings] == [
        (
            Path("pyproject.toml"),
            "project.version must match expected release version 0.3.1, got 0.3.0",
        )
    ]


def test_verify_docs_requires_pyproject_for_expected_release_version(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)

    findings = verify_docs(tmp_path, expected_release_version="0.3.0")

    assert [(finding.path, finding.message) for finding in findings] == [
        (
            Path("<docs-verifier>"),
            "release-version check could not complete; fix the doc input or verifier: "
            "ValueError: cannot read pyproject.toml for expected release-version verification",
        )
    ]


def test_verify_docs_reports_missing_project_version_as_release_metadata_error(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'rpacore'\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert [(finding.path, finding.message) for finding in findings] == [
        (
            Path("<docs-verifier>"),
            "release-version check could not complete; fix the doc input or verifier: "
            "ValueError: pyproject.toml project.version must be a non-empty string",
        )
    ]


def test_verify_docs_reports_malformed_release_metadata_as_release_metadata_error(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "pyproject.toml").write_text("[project\nversion = '0.2.0'\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert len(findings) == 1
    assert findings[0].path == Path("<docs-verifier>")
    assert "release-version check could not complete" in findings[0].message
    assert "TOMLDecodeError" in findings[0].message


def test_verify_docs_accepts_published_steady_state(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v0.3.0 - 2026-08-01\n\n## v0.2.0 - 2026-07-29\n",
        encoding="utf-8",
    )
    (tmp_path / "SECURITY.md").write_text(
        "# Security\n\n| Version | Supported |\n| --- | --- |\n| 0.3.x | Yes |\n",
        encoding="utf-8",
    )
    (tmp_path / "SUPPORT.md").write_text(
        "# Support\n\nThe latest public `0.3.x` release line.\n",
        encoding="utf-8",
    )

    assert verify_docs(tmp_path) == []


def test_verify_docs_does_not_require_release_versions_in_reader_docs(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)

    findings = verify_docs(tmp_path)

    assert findings == []


def test_verify_docs_rejects_duplicate_development_changelog_heading(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v0.3.0 - Unreleased\n\n## v0.3.0 - Unreleased\n\n"
        "## v0.2.0 - 2026-07-29\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "expected exactly one Unreleased heading at the top for v0.3.0"
    ]


def test_verify_docs_rejects_misplaced_development_changelog_heading(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v0.2.0 - 2026-07-29\n\n## v0.3.0 - Unreleased\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "expected exactly one Unreleased heading at the top for v0.3.0",
        "top changelog release version must be 0.3.0, got 0.2.0",
    ]


def test_verify_docs_rejects_stale_published_unreleased_heading(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v0.3.0 - Unreleased\n\n## v0.2.0 - Unreleased\n\n"
        "## v0.2.0 - 2026-07-29\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "expected exactly one Unreleased heading at the top for v0.3.0"
    ]


def test_verify_docs_rejects_unpublished_series_as_supported(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "SECURITY.md").write_text(
        "# Security\n\n| Version | Supported |\n| --- | --- |\n"
        "| 0.3.x | Yes |\n| 0.2.x | Yes |\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [(finding.path, finding.message) for finding in findings] == [
        (
            Path("SECURITY.md"),
            "unpublished development series must not be marked supported: 0.3.x",
        )
    ]


def test_verify_docs_rejects_prose_support_claim_for_unpublished_version(
    tmp_path: Path,
) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "SUPPORT.md").write_text(
        "# Support\n\nThe latest public `0.2.x` release line receives fixes.\n\n"
        "The unreleased 0.3.0 line on main is fully supported.\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [(finding.path, finding.line, finding.message) for finding in findings] == [
        (
            Path("SUPPORT.md"),
            5,
            "unpublished development version must not be claimed as supported: 0.3.0",
        )
    ]


def test_verify_docs_reports_missing_required_release_document_at_its_path(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path, release_metadata=True)
    (tmp_path / "SUPPORT.md").unlink()

    findings = verify_docs(tmp_path)

    assert len(findings) == 1
    assert findings[0].path == Path("SUPPORT.md")
    assert findings[0].message.startswith("cannot read release version documentation:")


def test_release_series_requires_numeric_patch_and_accepts_pep440_suffixes() -> None:
    for version in ("0.2.0", "0.2.0a1", "0.2.0rc1", "0.2.0.post1", "0.2.0+local", "0.2.0.1"):
        assert _release_series(version) == "0.2.x"

    try:
        _release_series("0.2.a1")
    except ValueError as exc:
        assert "major.minor.patch" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_verify_docs_accepts_tuple_public_exports(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "rpacore" / "__init__.py").write_text('__all__ = ("Engine",)\n', encoding="utf-8")

    assert verify_docs(tmp_path) == []


def test_verify_docs_reports_public_export_syntax_error(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "rpacore" / "__init__.py").write_text("__all__ = [\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert len(findings) == 1
    assert findings[0].message.startswith("cannot parse public API exports:")


def test_verify_docs_reports_missing_public_exports_file(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "rpacore" / "__init__.py").unlink()

    findings = verify_docs(tmp_path)

    assert len(findings) == 1
    assert findings[0].message.startswith("cannot read public API exports:")


def test_public_exports_reports_non_string_entries(tmp_path: Path) -> None:
    (tmp_path / "rpacore").mkdir()
    (tmp_path / "rpacore" / "__init__.py").write_text('__all__ = ["Engine", 1]\n', encoding="utf-8")

    exports, finding = _public_exports(tmp_path)

    assert exports == []
    assert finding is not None
    assert finding.message == "__all__ entries must be string literals"


def test_verify_docs_reports_directory_anchor_without_crashing(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Root\n\n[Docs](docs/#intro)\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "heading anchor target is not a file: docs/#intro"
    ]


def test_verify_docs_validates_fragment_only_links_against_source_file(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Root\n\n[Missing](#missing)\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "missing heading anchor: #missing"
    ]


def test_check_links_rejects_links_leaving_repository(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Root\n\n[Outside](../secret.md)\n", encoding="utf-8")

    findings = _check_links(tmp_path, [doc])

    assert [finding.message for finding in findings] == [
        "link leaves repository: ../secret.md"
    ]


def test_verify_docs_rejects_local_development_urls(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "README.md").write_text(
        "# Docs\n\n[API](api.md)\n[Local](http://localhost:8000/)\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "external link uses local development host: http://localhost:8000/"
    ]


def test_verify_docs_scans_all_markdown_for_sensitive_local_paths(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "design-note.md").write_text(
        "# Design Note\n\nDo not publish D:\\repos\\rpacore paths.\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "forbidden public-doc pattern: local checkout path"
    ]


def test_verify_docs_scans_root_security_policy(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "SECURITY.md").write_text(
        "# Security\n\nUse `pip install -e .` before reporting issues.\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "forbidden public-doc pattern: editable install instruction"
    ]


def test_verify_docs_scans_root_governance_docs(tmp_path: Path) -> None:
    governance_docs = [path.as_posix() for path in ROOT_MARKDOWN_DOCS]
    for index, doc_name in enumerate(governance_docs):
        root = tmp_path / str(index)
        root.mkdir()
        _write_minimal_repo(root)
        (root / doc_name).write_text(
            "# Governance\n\nUse `pip install -e .` for setup.\n",
            encoding="utf-8",
        )

        findings = verify_docs(root)

        assert [finding.message for finding in findings] == [
            "forbidden public-doc pattern: editable install instruction"
        ]


def test_maintainers_verifier_scope_matches_root_markdown_docs() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / "MAINTAINERS.md").read_text(encoding="utf-8")

    for path in ROOT_MARKDOWN_DOCS:
        assert f"`{path.as_posix()}`" in text
    assert "`docs/*.md`" in text

    normalized_text = re.sub(r"\s+", " ", text)
    scope_section = normalized_text.split("The docs verifier checks markdown links in ", 1)[1]
    scope_section = scope_section.split(" ## Release Ownership", 1)[0]
    documented_paths = set(re.findall(r"`([^`]+)`", scope_section))
    expected_paths = {path.as_posix() for path in ROOT_MARKDOWN_DOCS} | {"docs/*.md"}

    assert documented_paths == expected_paths


def test_check_forbidden_patterns_scans_new_docs_for_public_patterns(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    doc = tmp_path / "docs" / "new-guide.md"
    doc.write_text("# New Guide\n\nUse `pip install -e .` here.\n", encoding="utf-8")

    findings = _check_forbidden_patterns(tmp_path, [doc])

    assert [finding.message for finding in findings] == [
        "forbidden public-doc pattern: editable install instruction"
    ]


def test_verify_docs_reports_public_forbidden_patterns(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "README.md").write_text(
        "# Docs\n\n[API](api.md)\nUse `pip install -e .` while following public docs.\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "forbidden public-doc pattern: editable install instruction"
    ]


def test_verify_docs_rejects_mutable_release_status_banners(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "README.md").write_text(
        "# Root\n\nCurrent development version: `0.3.0`\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "forbidden public-doc pattern: mutable current-version banner"
    ]


def test_verify_docs_rejects_retired_execution_vocabulary_in_ordinary_docs(
    tmp_path: Path,
) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "design-note.md").write_text(
        "# Design Note\n\nUse `Transaction.skills` for execution.\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "retired execution vocabulary outside a migration record: skills"
    ]


def test_verify_docs_does_not_match_retired_vocabulary_inside_larger_names(
    tmp_path: Path,
) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "design-note.md").write_text(
        "# Design Note\n\nA reskill_worker setting belongs to another tool.\n",
        encoding="utf-8",
    )

    assert verify_docs(tmp_path) == []


def test_verify_docs_limits_durability_exception_to_migration_section(
    tmp_path: Path,
) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "durability.md").write_text(
        "# Durability\n\n## Runtime\n\nUse `skills` during execution.\n",
        encoding="utf-8",
    )

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "retired execution vocabulary outside a migration record: skills"
    ]


def test_verify_docs_allows_retired_vocabulary_in_durability_migration_section(
    tmp_path: Path,
) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "durability.md").write_text(
        "# Durability\n\n## Migrations\n\nVersion 1 stores `skills`.\n",
        encoding="utf-8",
    )

    assert verify_docs(tmp_path) == []


def test_verify_docs_reports_missing_link_targets(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Root\n\n[Missing](docs/missing.md)\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "missing link target: docs/missing.md"
    ]


def test_verify_docs_reports_missing_api_reference(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "README.md").write_text("# Docs\n", encoding="utf-8")
    (tmp_path / "docs" / "api.md").unlink()

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == ["missing API reference"]


def test_verify_docs_reports_missing_public_export_in_api_reference(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "docs" / "api.md").write_text("# API Reference\n", encoding="utf-8")

    findings = verify_docs(tmp_path)

    assert [finding.message for finding in findings] == [
        "missing public export in API reference: Engine"
    ]


def test_check_api_reference_reports_documented_symbols_missing_from_public_exports(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    api_path = tmp_path / "docs" / "api.md"
    api_path.write_text(
        "# API Reference\n\n"
        "| Symbol | Purpose |\n"
        "| --- | --- |\n"
        "| `Engine`, `RemovedSymbol` | grouped symbols |\n",
        encoding="utf-8",
    )

    findings = _check_api_reference(tmp_path, [api_path])

    assert [finding.message for finding in findings] == [
        "API reference documents non-public export: RemovedSymbol"
    ]


def test_verify_docs_main_returns_one_for_findings(tmp_path: Path) -> None:
    _write_minimal_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Root\n\n[Missing](docs/missing.md)\n", encoding="utf-8")

    assert main(["--repo-root", str(tmp_path)]) == 1


def test_run_check_reports_actionable_structured_finding() -> None:
    findings = _run_check("links", lambda: (_ for _ in ()).throw(ValueError("bad doc")))

    assert len(findings) == 1
    assert findings[0].path == Path("<docs-verifier>")
    assert findings[0].message == (
        "links check could not complete; fix the doc input or verifier: ValueError: bad doc"
    )


def test_slug_strips_markup_and_special_characters() -> None:
    assert _slug("`Engine.run()` and Storage!") == "enginerun-and-storage"


def test_anchors_handles_duplicate_headings(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("# Intro\n\n## Intro\n", encoding="utf-8")

    assert _anchors(doc) == {"intro", "intro-1"}


def test_anchors_skips_empty_slugs(tmp_path: Path) -> None:
    doc = tmp_path / "doc.md"
    doc.write_text("# !!!\n\n## Intro\n", encoding="utf-8")

    assert _anchors(doc) == {"intro"}


def test_is_dev_url_detects_loopback_hosts() -> None:
    assert _is_dev_url(urlparse("http://localhost:8000/")) is True
    assert _is_dev_url(urlparse("http://127.0.0.1:8000/")) is True
    assert _is_dev_url(urlparse("https://example.com/")) is False
