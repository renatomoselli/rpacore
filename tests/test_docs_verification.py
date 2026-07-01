from __future__ import annotations

from urllib.parse import urlparse
from pathlib import Path

from scripts.verify_docs import (
    _anchors,
    _check_api_reference,
    _check_forbidden_patterns,
    _check_links,
    _is_dev_url,
    _public_exports,
    _run_check,
    _slug,
    main,
    verify_docs,
)


def _write_minimal_repo(root: Path) -> None:
    (root / "docs").mkdir()
    (root / "rpacore").mkdir()
    (root / "README.md").write_text("# Root\n\n[Docs](docs/README.md)\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (root / "SECURITY.md").write_text("# Security\n", encoding="utf-8")
    (root / "docs" / "README.md").write_text("# Docs\n\n[API](api.md)\n", encoding="utf-8")
    (root / "docs" / "api.md").write_text("# API Reference\n\n`Engine`\n", encoding="utf-8")
    (root / "rpacore" / "__init__.py").write_text('__all__ = ["Engine"]\n', encoding="utf-8")


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
