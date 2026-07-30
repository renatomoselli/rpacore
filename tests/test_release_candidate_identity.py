"""Focused tests for release-candidate identity preflight."""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch


COMMIT = "a" * 40


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_release_candidate_identity.py"
    spec = importlib.util.spec_from_file_location("verify_release_candidate_identity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path, *, changelog: bool = True) -> Path:
    root = tmp_path / "rpacore"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nversion = '0.2.0'\n", encoding="utf-8")
    if changelog:
        (root / "CHANGELOG.md").write_text("## v0.2.0 - Unreleased\n", encoding="utf-8")
    return root


class _Response:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return json.dumps(self.value).encode()


def _absent_opener(request, *, timeout: int):
    raise urllib.error.HTTPError(request.full_url, 404, "not found", None, None)


def test_release_identity_accepts_unclaimed_version(tmp_path: Path) -> None:
    script = _load_script()

    with patch.object(script, "_current_commit", return_value=COMMIT):
        result = script.verify_release_candidate_identity(
            repo_root=_repo(tmp_path),
            expected_core_commit=COMMIT,
            repository="renatomoselli/rpacore",
            github_token="token",
            opener=_absent_opener,
        )

    assert result == {
        "schema_version": 1,
        "status": "pass",
        "source": {"core_commit": COMMIT},
        "release": {"version": "0.2.0", "tag": "v0.2.0"},
    }


def test_release_identity_canonicalizes_uppercase_commit_input(tmp_path: Path) -> None:
    script = _load_script()

    with patch.object(script, "_current_commit", return_value=COMMIT):
        result = script.verify_release_candidate_identity(
            repo_root=_repo(tmp_path),
            expected_core_commit=COMMIT.upper(),
            repository="renatomoselli/rpacore",
            github_token="token",
            opener=_absent_opener,
        )

    assert result["source"] == {"core_commit": COMMIT}


def test_release_identity_accepts_changelog_heading_with_trailing_whitespace(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = _repo(tmp_path)
    (repo_root / "CHANGELOG.md").write_text("## v0.2.0 - Unreleased   \n", encoding="utf-8")

    with patch.object(script, "_current_commit", return_value=COMMIT):
        result = script.verify_release_candidate_identity(
            repo_root=repo_root,
            expected_core_commit=COMMIT,
            repository="renatomoselli/rpacore",
            github_token="token",
            opener=_absent_opener,
        )

    assert result["release"] == {"version": "0.2.0", "tag": "v0.2.0"}


def test_release_identity_rejects_non_commit_input(tmp_path: Path) -> None:
    script = _load_script()

    with patch.object(script, "_current_commit", return_value=COMMIT):
        try:
            script.verify_release_candidate_identity(
                repo_root=_repo(tmp_path),
                expected_core_commit="a" * 39,
                repository="renatomoselli/rpacore",
                github_token="token",
                opener=_absent_opener,
            )
        except script.ReleaseIdentityError as exc:
            assert "full git commit" in str(exc)
        else:
            raise AssertionError("expected ReleaseIdentityError")


def test_release_identity_rejects_existing_pypi_version(tmp_path: Path) -> None:
    script = _load_script()

    def opener(request, *, timeout: int):
        if "pypi.org" in request.full_url:
            return _Response({"info": {"version": "0.2.0"}})
        return _absent_opener(request, timeout=timeout)

    with patch.object(script, "_current_commit", return_value=COMMIT):
        try:
            script.verify_release_candidate_identity(
                repo_root=_repo(tmp_path),
                expected_core_commit=COMMIT,
                repository="renatomoselli/rpacore",
                github_token="token",
                opener=opener,
            )
        except script.ReleaseIdentityError as exc:
            assert "PyPI version already exists" in str(exc)
        else:
            raise AssertionError("expected ReleaseIdentityError")


def test_release_identity_rejects_existing_github_tag(tmp_path: Path) -> None:
    script = _load_script()

    def opener(request, *, timeout: int):
        if "/git/ref/tags/" in request.full_url:
            return _Response({"ref": "refs/tags/v0.2.0"})
        return _absent_opener(request, timeout=timeout)

    with patch.object(script, "_current_commit", return_value=COMMIT):
        try:
            script.verify_release_candidate_identity(
                repo_root=_repo(tmp_path),
                expected_core_commit=COMMIT,
                repository="renatomoselli/rpacore",
                github_token="token",
                opener=opener,
            )
        except script.ReleaseIdentityError as exc:
            assert "GitHub tag already exists" in str(exc)
        else:
            raise AssertionError("expected ReleaseIdentityError")


def test_release_identity_rejects_unavailable_registry(tmp_path: Path) -> None:
    script = _load_script()

    def opener(request, *, timeout: int):
        if "pypi.org" in request.full_url:
            raise OSError("offline")
        return _absent_opener(request, timeout=timeout)

    with patch.object(script, "_current_commit", return_value=COMMIT):
        try:
            script.verify_release_candidate_identity(
                repo_root=_repo(tmp_path),
                expected_core_commit=COMMIT,
                repository="renatomoselli/rpacore",
                github_token="token",
                opener=opener,
            )
        except script.ReleaseIdentityError as exc:
            assert "PyPI request failed" in str(exc)
        else:
            raise AssertionError("expected ReleaseIdentityError")


def test_release_identity_rejects_missing_unreleased_changelog_entry(tmp_path: Path) -> None:
    script = _load_script()

    with patch.object(script, "_current_commit", return_value=COMMIT):
        try:
            script.verify_release_candidate_identity(
                repo_root=_repo(tmp_path, changelog=False),
                expected_core_commit=COMMIT,
                repository="renatomoselli/rpacore",
                github_token="token",
                opener=_absent_opener,
            )
        except script.ReleaseIdentityError as exc:
            assert "cannot read CHANGELOG.md" in str(exc)
        else:
            raise AssertionError("expected ReleaseIdentityError")


def test_release_candidate_workflow_runs_identity_and_examples_preflights() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-candidate.yml"
    ).read_text(encoding="utf-8")

    assert workflow.index("Verify unclaimed release identity before build") < workflow.index(
        "Build one artifact set"
    )
    assert "verify_release_candidate_identity.py" in workflow
    assert (
        "--output validation-artifacts/release-candidate/preflight/release-identity-preflight.json"
        in workflow
    )
    assert "release-candidate-evidence-preflight" in workflow
    assert 'expected.append("release-candidate-evidence-preflight")' in workflow
    assert "release-identity-preflight.json" in workflow
    assert 'result_path = directory / "release-identity-preflight.json"' in workflow
    assert 'result.get("status") != "pass"' in workflow
    assert "preflight evidence is unreadable or malformed" in workflow
    assert "core commit differs between preflight and artifact lock" in workflow
    assert "release metadata differs between preflight and artifact lock" in workflow
    assert "json.JSONDecodeError" in workflow
    assert "Verify frozen examples dependency metadata" in workflow
    assert workflow.index("Validate and canonicalize frozen commit inputs") < workflow.index(
        "uses: actions/checkout@v7"
    )
    assert workflow.count("${{ inputs.core_commit }}") == 1
    assert workflow.count("${{ inputs.examples_commit }}") == 1
    assert "needs.freeze-artifacts.outputs.core_commit" in workflow
    assert "needs.freeze-artifacts.outputs.examples_commit" in workflow
