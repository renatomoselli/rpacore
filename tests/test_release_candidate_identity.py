"""Focused tests for release-candidate identity preflight."""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
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
    (root / "pyproject.toml").write_text("[project]\nversion = '0.3.0'\n", encoding="utf-8")
    if changelog:
        (root / "CHANGELOG.md").write_text(
            "## v0.3.0 - 2026-09-03\n\n## v0.2.0 - 2026-07-29\n",
            encoding="utf-8",
        )
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
        "release": {"version": "0.3.0", "tag": "v0.3.0"},
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


def test_release_identity_accepts_dated_changelog_heading_with_trailing_whitespace(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = _repo(tmp_path)
    (repo_root / "CHANGELOG.md").write_text(
        "## v0.3.0 - 2026-09-03   \n\n## v0.2.0 - 2026-07-29\n",
        encoding="utf-8",
    )

    with patch.object(script, "_current_commit", return_value=COMMIT):
        result = script.verify_release_candidate_identity(
            repo_root=repo_root,
            expected_core_commit=COMMIT,
            repository="renatomoselli/rpacore",
            github_token="token",
            opener=_absent_opener,
        )

    assert result["release"] == {"version": "0.3.0", "tag": "v0.3.0"}


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
            return _Response({"info": {"version": "0.3.0"}})
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
            return _Response({"ref": "refs/tags/v0.3.0"})
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


def test_release_identity_rejects_unreleased_changelog_entry(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = _repo(tmp_path)
    (repo_root / "CHANGELOG.md").write_text(
        "## v0.3.0 - Unreleased\n\n## v0.2.0 - 2026-07-29\n",
        encoding="utf-8",
    )

    with patch.object(script, "_current_commit", return_value=COMMIT):
        try:
            script.verify_release_candidate_identity(
                repo_root=repo_root,
                expected_core_commit=COMMIT,
                repository="renatomoselli/rpacore",
                github_token="token",
                opener=_absent_opener,
            )
        except script.ReleaseIdentityError as exc:
            assert "dated top release heading" in str(exc)
            assert "found: ## v0.3.0 - Unreleased" in str(exc)
        else:
            raise AssertionError("expected ReleaseIdentityError")


def test_release_identity_rejects_invalid_changelog_date(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = _repo(tmp_path)
    (repo_root / "CHANGELOG.md").write_text(
        "## v0.3.0 - 2026-02-30\n\n## v0.2.0 - 2026-07-29\n",
        encoding="utf-8",
    )

    with patch.object(script, "_current_commit", return_value=COMMIT):
        try:
            script.verify_release_candidate_identity(
                repo_root=repo_root,
                expected_core_commit=COMMIT,
                repository="renatomoselli/rpacore",
                github_token="token",
                opener=_absent_opener,
            )
        except script.ReleaseIdentityError as exc:
            assert "invalid release date" in str(exc)
        else:
            raise AssertionError("expected ReleaseIdentityError")


def test_release_identity_rejects_missing_changelog(tmp_path: Path) -> None:
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
    assert "Verify public release documentation" in workflow
    assert workflow.index("Verify public release documentation") < workflow.index("Build one artifact set")
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
    assert 'DISPATCHED_MAIN_COMMIT: ${{ github.sha }}' in workflow
    assert 'test "${{ github.ref }}" = "refs/heads/main"' in workflow
    assert 'test "$EXPECTED_COMMIT" = "$DISPATCHED_MAIN_COMMIT"' in workflow
    assert "-r requirements/release.txt" in workflow
    assert "python -m pip install build twine" not in workflow


def test_release_candidate_python_matrix_matches_package_and_support_policy() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "release-candidate.yml").read_text(encoding="utf-8")
    matrix = re.search(r"python-version:\s*\[([^]]+)\]", workflow)
    assert matrix is not None
    workflow_versions = set(re.findall(r'"(3\.\d+)"', matrix.group(1)))

    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = metadata["project"]["classifiers"]
    classifier_versions = {
        classifier.removeprefix("Programming Language :: Python :: ")
        for classifier in classifiers
        if classifier.startswith("Programming Language :: Python :: 3.")
    }

    support = (root / "SUPPORT.md").read_text(encoding="utf-8")
    supported_versions = set(
        re.findall(r"(?<![\d.])3\.\d+", support.split("## Where To Ask", 1)[0])
    )

    assert workflow_versions == {"3.11", "3.12", "3.13", "3.14"}
    assert classifier_versions == workflow_versions
    assert supported_versions == workflow_versions


def test_release_candidate_platform_cells_keep_artifacts_outside_checkout() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-candidate.yml"
    ).read_text(encoding="utf-8")
    platform_cell = workflow.split("  platform-cell:", 1)[1].split("  examples-wheel:", 1)[0]

    assert "path: ${{ runner.temp }}/artifact-set" in platform_cell
    assert 'ARTIFACT_SET_DIR: ${{ runner.temp }}/artifact-set' in platform_cell
    assert "root=pathlib.Path(os.environ['ARTIFACT_SET_DIR'])" in platform_cell
    assert '--prebuilt-artifacts-dir "${{ runner.temp }}/artifact-set"' in platform_cell
    assert "path: artifact-set" not in platform_cell
    assert "--prebuilt-artifacts-dir artifact-set" not in platform_cell


def test_release_toolchain_is_pinned_and_shared_by_ci_candidate_and_publisher() -> None:
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements" / "release.txt").read_text(encoding="utf-8").splitlines()
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    candidate = (root / ".github" / "workflows" / "release-candidate.yml").read_text(encoding="utf-8")
    publisher = (root / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")

    assert [line for line in requirements if line and not line.startswith("#")] == [
        "build==1.5.0",
        "twine==6.2.0",
        "packaging==24.2",
    ]
    for workflow in (ci, candidate, publisher):
        assert "-r requirements/release.txt" in workflow
        assert "pip install build twine" not in workflow
