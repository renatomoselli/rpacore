"""Tests for preparing release manifests from validation results."""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


@functools.cache
def _load_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "prepare_release_manifest.py"
    spec = importlib.util.spec_from_file_location("prepare_release_manifest", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git_init(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=path, check=True)
    (path / "tracked.txt").write_text("tracked", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_pyproject(path: Path) -> None:
    (path / "pyproject.toml").write_text(
        """
[project]
name = "rpacore"
version = "0.1.0"
description = "RPA Core — deterministic, stateful RPA in Python"
requires-python = ">=3.11"
license = "Apache-2.0"
license-files = ["LICENSE", "NOTICE"]

[project.urls]
Homepage = "https://rpacore.dev"
Repository = "https://github.com/renatomoselli/rpacore"
""",
        encoding="utf-8",
    )


def _write_pyproject_without_version(path: Path) -> None:
    (path / "pyproject.toml").write_text(
        """
[project]
name = "rpacore"
license = "Apache-2.0"
license-files = ["LICENSE", "NOTICE"]
""",
        encoding="utf-8",
    )


def _write_pyproject_without_name(path: Path) -> None:
    (path / "pyproject.toml").write_text(
        """
[project]
version = "0.1.0"
license = "Apache-2.0"
license-files = ["LICENSE", "NOTICE"]
""",
        encoding="utf-8",
    )


def _write_pyproject_with_invalid_license_files(path: Path, value: str, *, location: str) -> None:
    if location == "project":
        license_files = f"license-files = {value}\n"
        setuptools_section = ""
    elif location == "tool.setuptools":
        license_files = ""
        setuptools_section = f"""
[tool.setuptools]
license-files = {value}
"""
    else:
        raise AssertionError(f"Unsupported license-files location: {location}")
    (path / "pyproject.toml").write_text(
        f"""
[project]
name = "rpacore"
version = "0.1.0"
license = "Apache-2.0"
{license_files}

[project.urls]
Homepage = "https://rpacore.dev"
{setuptools_section}
""",
        encoding="utf-8",
    )


def _write_pyproject_with_invalid_urls(path: Path) -> None:
    (path / "pyproject.toml").write_text(
        """
[project]
name = "rpacore"
version = "0.1.0"
license = "Apache-2.0"
urls = []
license-files = ["LICENSE", "NOTICE"]
""",
        encoding="utf-8",
    )


def _write_validation_results(tmp_path: Path) -> tuple[Path, Path]:
    release_candidate = tmp_path / "release-candidate-validation-results.json"
    examples_wheel = tmp_path / "examples-wheel-validation.json"
    artifact_root = tmp_path / "artifacts"
    artifact_payloads = {
        "rpacore-0.1.0-py3-none-any.whl": b"wheel bytes",
        "rpacore-0.1.0.tar.gz": b"sdist bytes",
    }
    artifact_metadata = [
        {
            "name": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        for name, payload in artifact_payloads.items()
    ]
    artifact_set = hashlib.sha256(
        b"".join(
            f"{artifact['name']}\0{artifact['size_bytes']}\0{artifact['sha256']}\n".encode("utf-8")
            for artifact in sorted(artifact_metadata, key=lambda item: item["name"])
        )
    ).hexdigest()
    artifacts_dir = artifact_root / artifact_set
    artifacts_dir.mkdir(parents=True)
    for name, payload in artifact_payloads.items():
        (artifacts_dir / name).write_bytes(payload)
    wheel = artifacts_dir / "rpacore-0.1.0-py3-none-any.whl"
    sdist = artifacts_dir / "rpacore-0.1.0.tar.gz"
    release_candidate.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "generated_at": "2026-07-03T00:00:00+00:00",
                "platform": {
                    "system": "Windows",
                    "release": "11",
                    "machine": "AMD64",
                    "architecture": "64bit",
                },
                "python": {
                    "executable": "python",
                    "version": "3.11.0",
                    "implementation": "CPython",
                },
                "environment": {
                    "sqlite": {"library_version": "3.45.1"},
                    "journal": {
                        "policy": "rollback_delete",
                        "transaction": {"effective_mode": "delete"},
                        "queue": {"effective_mode": "delete"},
                    },
                },
                "tools": {"pip": "25", "build": "1", "twine": "6"},
                "result": {"status": "pass"},
                "artifacts": [
                    {
                        "name": wheel.name,
                        "path": str(wheel),
                        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                        "size_bytes": wheel.stat().st_size,
                    },
                    {
                        "name": sdist.name,
                        "path": str(sdist),
                        "sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
                        "size_bytes": sdist.stat().st_size,
                    },
                ],
                "dependency_inventory": {"runtime_dependencies": []},
            }
        ),
        encoding="utf-8",
    )
    examples_wheel.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-03T00:00:00+00:00",
                "result": {"status": "pass"},
            }
        ),
        encoding="utf-8",
    )
    return release_candidate, examples_wheel


def _manifest_args(
    *,
    repo_root: Path,
    examples_repo: Path,
    release_candidate: Path,
    examples_wheel: Path,
    output_dir: Path,
) -> dict[str, object]:
    return {
        "repo_root": repo_root,
        "examples_repo": examples_repo,
        "release_candidate_validation_results": release_candidate,
        "examples_wheel_validation_results": examples_wheel,
        "output_dir": output_dir,
        "tag": "v0.1.0",
        "owner": "release owner",
        "approver": "approver",
        "docs_verification": "passed",
        "docs_command": "python scripts/verify_docs.py --repo-root .",
        "docs_verification_note": "",
        "sbom_path": None,
        "sbom_note": "No SBOM was produced for this release rehearsal.",
        "testpypi": "skipped",
        "testpypi_note": "",
    }


def test_prepare_release_manifest_writes_manifest_and_approval_draft(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    framework_commit = _git_init(repo_root)
    examples_commit = _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)
    output_dir = tmp_path / "out"

    manifest = script.prepare_release_manifest(
        repo_root=repo_root,
        examples_repo=examples_repo,
        release_candidate_validation_results=release_candidate,
        examples_wheel_validation_results=examples_wheel,
        output_dir=output_dir,
        tag="v0.1.0",
        owner="release owner",
        approver="approver",
        docs_verification="passed",
        docs_command="python scripts/verify_docs.py --repo-root .",
        docs_verification_note="",
        sbom_path=None,
        sbom_note="No SBOM was produced for this release rehearsal.",
        testpypi="skipped",
        testpypi_note="direct artifact validation is representative",
    )

    assert manifest["decision"]["status"] == "approved"
    assert manifest["schema_version"] == 3
    assert manifest["repositories"]["framework"]["commit"] == framework_commit
    assert manifest["repositories"]["examples"]["commit"] == examples_commit
    assert "path" not in manifest["repositories"]["framework"]
    assert "status" not in manifest["repositories"]["framework"]
    assert manifest["environment"]["platform"]["system"] == "Windows"
    assert manifest["documentation_verification"]["status"] == "passed"
    assert manifest["sbom"]["status"] == "not_produced"
    assert manifest["environment"]["sqlite"]["library_version"] == "3.45.1"
    assert manifest["tools"] == {"pip": "25", "build": "1", "twine": "6"}
    assert manifest["release"] == {"version": "0.1.0", "tag": "v0.1.0"}
    assert Path(manifest["artifacts"][0]["path"]).parent.parent == tmp_path / "artifacts"
    assert manifest["expected_pypi_metadata"]["license"] == "Apache-2.0"
    assert manifest["expected_pypi_metadata"]["license_files"] == ["LICENSE", "NOTICE"]
    assert (output_dir / "release-manifest.json").is_file()
    summary = (output_dir / "release-approval.md").read_text(encoding="utf-8")
    assert "Release Approval Draft" in summary
    assert "Examples commit" in summary


def test_prepare_release_manifest_rejects_dirty_repo(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    (examples_repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    manifest = script.prepare_release_manifest(
        repo_root=repo_root,
        examples_repo=examples_repo,
        release_candidate_validation_results=release_candidate,
        examples_wheel_validation_results=examples_wheel,
        output_dir=tmp_path / "out",
        tag="v0.1.0",
        owner="release owner",
        approver="approver",
        docs_verification="passed",
        docs_command="python scripts/verify_docs.py --repo-root .",
        docs_verification_note="",
        sbom_path=None,
        sbom_note="No SBOM was produced for this release rehearsal.",
        testpypi="skipped",
        testpypi_note="",
    )

    assert manifest["decision"]["status"] == "rejected"
    assert manifest["repositories"]["examples"]["dirty"] is True


def test_prepare_release_manifest_rejects_missing_artifacts(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate = tmp_path / "release-candidate-validation-results.json"
    release_candidate.write_text(
            json.dumps({"schema_version": 3, "result": {"status": "pass"}, "artifacts": []}),
        encoding="utf-8",
    )
    examples_wheel = tmp_path / "examples-wheel-validation.json"
    examples_wheel.write_text(json.dumps({"result": {"status": "pass"}}), encoding="utf-8")

    with pytest.raises(script.ManifestError, match="release-candidate validation results have no artifacts"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_missing_project_table(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    (repo_root / "pyproject.toml").write_text("tool = {}\n", encoding="utf-8")
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    with pytest.raises(script.ManifestError, match=r"pyproject\.toml missing \[project\] table"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_missing_name(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject_without_name(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    with pytest.raises(script.ManifestError, match="pyproject.toml project missing name"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


@pytest.mark.parametrize("license_files_value", ['"LICENSE"', "{}"])
@pytest.mark.parametrize("location", ["project", "tool.setuptools"])
def test_prepare_release_manifest_rejects_invalid_license_files(
    tmp_path: Path,
    license_files_value: str,
    location: str,
) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject_with_invalid_license_files(repo_root, license_files_value, location=location)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    with pytest.raises(script.ManifestError, match="license-files must be a list"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_missing_license_files(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    (repo_root / "pyproject.toml").write_text(
        """
[project]
name = "rpacore"
version = "0.1.0"
license = "Apache-2.0"
""",
        encoding="utf-8",
    )
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    with pytest.raises(script.ManifestError, match="missing license-files"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_empty_license_files(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    (repo_root / "pyproject.toml").write_text(
        """
[project]
name = "rpacore"
version = "0.1.0"
license = "Apache-2.0"
license-files = []
""",
        encoding="utf-8",
    )
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    with pytest.raises(script.ManifestError, match="license-files must not be empty"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_invalid_urls(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject_with_invalid_urls(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    with pytest.raises(script.ManifestError, match="urls must be an object"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


@pytest.mark.parametrize("license_value", ["Apache-2.0", {"text": "Apache-2.0"}])
def test_metadata_license_accepts_string_and_pep621_text_table(license_value: object) -> None:
    script = _load_script()

    assert script._metadata_license({"license": license_value}) == "Apache-2.0"


@pytest.mark.parametrize("license_value", [None, {}, {"file": "LICENSE"}, {"text": ""}])
def test_metadata_license_rejects_missing_text(license_value: object) -> None:
    script = _load_script()

    with pytest.raises(script.ManifestError, match="project missing license"):
        script._metadata_license({"license": license_value})


@pytest.mark.parametrize("license_value", ["Apache-2.O", {"text": "MIT"}])
def test_metadata_license_rejects_unexpected_license_expression(license_value: object) -> None:
    script = _load_script()

    with pytest.raises(script.ManifestError, match="project license must be 'Apache-2.0'"):
        script._metadata_license({"license": license_value})


def test_prepare_release_manifest_rejects_failed_testpypi(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    manifest = script.prepare_release_manifest(
        **{
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            ),
            "testpypi": "failed",
        }
    )

    assert manifest["decision"]["status"] == "rejected"
    assert manifest["decision"]["testpypi"] == "failed"


def test_prepare_release_manifest_rejects_skipped_docs(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    manifest = script.prepare_release_manifest(
        **{
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            ),
            "docs_verification": "skipped",
        }
    )

    assert manifest["decision"]["status"] == "rejected"
    assert manifest["documentation_verification"]["status"] == "skipped"


def test_prepare_release_manifest_rejects_failed_docs(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    manifest = script.prepare_release_manifest(
        **{
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            ),
            "docs_verification": "failed",
        }
    )

    assert manifest["decision"]["status"] == "rejected"
    assert manifest["documentation_verification"]["status"] == "failed"


def test_prepare_release_manifest_rejects_missing_version(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject_without_version(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    with pytest.raises(script.ManifestError, match="pyproject.toml project missing version"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_missing_validation_status(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)
    examples_wheel.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-03T00:00:00+00:00",
                "result": {"other": 1},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(script.ManifestError, match="examples wheel validation results missing result.status"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_non_object_result(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)
    payload = json.loads(release_candidate.read_text(encoding="utf-8"))
    payload["result"] = "pass"
    release_candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(script.ManifestError, match="release-candidate validation results result must be an object"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_unknown_validation_status(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)
    payload = json.loads(release_candidate.read_text(encoding="utf-8"))
    payload["result"] = {"status": "pending"}
    release_candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(script.ManifestError, match="result.status must be one of: fail, pass"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_artifact_summary_rejects_non_object_entry() -> None:
    script = _load_script()

    with pytest.raises(script.ManifestError, match="artifact entry must be an object"):
        script._artifact_summary({"artifacts": ["not-an-object"]}, artifact_root=Path("."))


def test_artifact_summary_rejects_missing_sha256() -> None:
    script = _load_script()

    with pytest.raises(script.ManifestError, match="artifact missing sha256"):
        script._artifact_summary(
            {"artifacts": [{"name": "rpacore.whl", "size_bytes": 10}]},
            artifact_root=Path("."),
        )


def test_artifact_summary_rejects_empty_name_or_sha256() -> None:
    script = _load_script()

    with pytest.raises(script.ManifestError, match="artifact missing name"):
        script._artifact_summary(
            {"artifacts": [{"name": "", "sha256": "abc", "size_bytes": 10}]},
            artifact_root=Path("."),
        )
    with pytest.raises(script.ManifestError, match="artifact missing sha256"):
        script._artifact_summary(
            {"artifacts": [{"name": "rpacore.whl", "sha256": "", "size_bytes": 10}]},
            artifact_root=Path("."),
        )


def test_artifact_summary_rejects_missing_size_bytes() -> None:
    script = _load_script()

    with pytest.raises(script.ManifestError, match="artifact missing size_bytes"):
        script._artifact_summary(
            {"artifacts": [{"name": "rpacore.whl", "sha256": "abc"}]},
            artifact_root=Path("."),
        )


def test_artifact_summary_requires_a_verified_artifact_path(tmp_path: Path) -> None:
    script = _load_script()
    artifact_root = tmp_path / "artifacts"
    record = {
        "name": "rpacore.whl",
        "sha256": hashlib.sha256(b"artifact").hexdigest(),
        "size_bytes": len(b"artifact"),
    }
    artifact_dir = artifact_root / script._artifact_set_sha256([record])
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / record["name"]
    artifact.write_bytes(b"artifact")
    record["path"] = str(artifact)

    with pytest.raises(script.ManifestError, match="artifact is outside its durable set"):
        script._artifact_summary(
            {
                "artifacts": [
                    {**record, "path": str(tmp_path / "untrusted" / "other.whl")}
                ]
            },
            artifact_root=artifact_root,
        )

    with pytest.raises(script.ManifestError, match="artifact missing path"):
        script._artifact_summary(
            {"artifacts": [{key: value for key, value in record.items() if key != "path"}]},
            artifact_root=artifact_root,
        )
    wrong_size = {**record, "size_bytes": 1}
    wrong_size_dir = artifact_root / script._artifact_set_sha256([wrong_size])
    wrong_size_dir.mkdir()
    wrong_size["path"] = str(wrong_size_dir / record["name"])
    Path(wrong_size["path"]).write_bytes(b"artifact")
    with pytest.raises(script.ManifestError, match="artifact size mismatch"):
        script._artifact_summary(
            {"artifacts": [wrong_size]},
            artifact_root=artifact_root,
        )
    wrong_hash = {**record, "sha256": "not-the-digest"}
    wrong_hash_dir = artifact_root / script._artifact_set_sha256([wrong_hash])
    wrong_hash_dir.mkdir()
    wrong_hash["path"] = str(wrong_hash_dir / record["name"])
    Path(wrong_hash["path"]).write_bytes(b"artifact")
    with pytest.raises(script.ManifestError, match="artifact hash mismatch"):
        script._artifact_summary(
            {"artifacts": [wrong_hash]},
            artifact_root=artifact_root,
        )
    artifact.unlink()
    artifact.mkdir()
    with pytest.raises(script.ManifestError, match="artifact is not a regular file"):
        script._artifact_summary({"artifacts": [record]}, artifact_root=artifact_root)


def test_artifact_summary_rejects_a_symlinked_artifact_root(tmp_path: Path) -> None:
    script = _load_script()
    target_root = tmp_path / "target" / "artifacts"
    record = {
        "name": "rpacore.whl",
        "sha256": hashlib.sha256(b"artifact").hexdigest(),
        "size_bytes": len(b"artifact"),
    }
    artifact_dir = target_root / script._artifact_set_sha256([record])
    artifact_dir.mkdir(parents=True)
    (artifact_dir / record["name"]).write_bytes(b"artifact")
    artifact_root = tmp_path / "linked-artifacts"
    try:
        artifact_root.symlink_to(target_root, target_is_directory=True)
    except OSError:
        return
    record["path"] = str(artifact_root / artifact_dir.name / record["name"])

    with pytest.raises(script.ManifestError, match="artifact root must not be a symlink"):
        script._artifact_summary({"artifacts": [record]}, artifact_root=artifact_root)


def test_release_candidate_schema_version_is_required() -> None:
    script = _load_script()

    with pytest.raises(script.ManifestError, match="schema_version 3"):
        script._require_schema_version({})


def test_prepare_release_manifest_rejects_missing_environment_subkeys(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)
    payload = json.loads(release_candidate.read_text(encoding="utf-8"))
    payload["platform"] = {}
    payload["python"] = {}
    release_candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(script.ManifestError, match="release-candidate platform missing system"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_missing_environment(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)
    payload = json.loads(release_candidate.read_text(encoding="utf-8"))
    del payload["environment"]
    release_candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(script.ManifestError, match="missing environment object"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_missing_dependency_inventory(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)
    payload = json.loads(release_candidate.read_text(encoding="utf-8"))
    del payload["dependency_inventory"]
    release_candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(script.ManifestError, match="missing dependency_inventory object"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_prepare_release_manifest_rejects_missing_runtime_dependencies(tmp_path: Path) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)
    payload = json.loads(release_candidate.read_text(encoding="utf-8"))
    payload["dependency_inventory"] = {}
    release_candidate.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(script.ManifestError, match="runtime_dependencies must be a list"):
        script.prepare_release_manifest(
            **_manifest_args(
                repo_root=repo_root,
                examples_repo=examples_repo,
                release_candidate=release_candidate,
                examples_wheel=examples_wheel,
                output_dir=tmp_path / "out",
            )
        )


def test_release_environment_uses_architecture_when_machine_is_blank() -> None:
    script = _load_script()

    environment = script._release_environment(
        {
            "platform": {
                "system": "Windows",
                "release": "10",
                "machine": "",
                "architecture": "64bit",
            },
            "python": {"version": "3.11.9"},
            "environment": {
                "sqlite": {"library_version": "3.45.1"},
                "journal": {
                    "policy": "rollback_delete",
                    "transaction": {"effective_mode": "delete"},
                    "queue": {"effective_mode": "delete"},
                },
            },
        }
    )

    assert environment["platform"]["machine"] == "64bit"


def test_release_environment_uses_unknown_when_machine_and_architecture_are_blank() -> None:
    script = _load_script()

    environment = script._release_environment(
        {
            "platform": {
                "system": "Windows",
                "release": "10",
            },
            "python": {"version": "3.11.9"},
            "environment": {
                "sqlite": {"library_version": "3.45.1"},
                "journal": {
                    "policy": "rollback_delete",
                    "transaction": {"effective_mode": "delete"},
                    "queue": {"effective_mode": "delete"},
                },
            },
        }
    )

    assert environment["platform"]["machine"] == "unknown"


def test_git_output_wraps_missing_git(tmp_path: Path, monkeypatch) -> None:
    script = _load_script()

    def raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", raise_file_not_found)

    with pytest.raises(script.ManifestError, match="git executable was not found on PATH"):
        script._git_output(tmp_path, "status")


def test_git_output_wraps_timeout(tmp_path: Path, monkeypatch) -> None:
    script = _load_script()

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=30)

    monkeypatch.setattr(subprocess, "run", raise_timeout)

    with pytest.raises(script.ManifestError, match="git status timed out after 30 seconds"):
        script._git_output(tmp_path, "status")


def test_git_output_wraps_non_zero_exit(tmp_path: Path, monkeypatch) -> None:
    script = _load_script()

    def raise_called_process_error(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=128,
            cmd=args[0],
            stderr="fatal: not a git repository",
        )

    monkeypatch.setattr(subprocess, "run", raise_called_process_error)

    with pytest.raises(script.ManifestError, match="git status failed .*fatal: not a git repository"):
        script._git_output(tmp_path, "status")


def test_repo_state_marks_detached_head(tmp_path: Path, monkeypatch) -> None:
    script = _load_script()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    def fake_git_output(repo: Path, *args: str) -> str:
        if args == ("status", "--porcelain"):
            return ""
        if args == ("rev-parse", "HEAD"):
            return "abc123"
        if args == ("branch", "--show-current"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(script, "_git_output", fake_git_output)

    state = script._repo_state("repo", repo_root)

    assert state.branch == "DETACHED"


def test_write_outputs_removes_manifest_when_summary_replace_fails(tmp_path: Path, monkeypatch) -> None:
    script = _load_script()
    manifest = {
        "generated_at": "2026-07-03T00:00:00+00:00",
        "decision": {
            "status": "approved",
            "owner": "release owner",
            "approver": "approver",
            "testpypi": "skipped",
            "testpypi_note": "",
        },
        "release": {"version": "0.1.0", "tag": "v0.1.0"},
        "repositories": {
            "framework": {"commit": "framework-commit"},
            "examples": {"commit": "examples-commit"},
        },
        "validation_results": {
            "release_candidate": {"status": "pass"},
            "examples_wheel": {"status": "pass"},
        },
        "environment": {
            "platform": {"system": "Windows", "release": "11", "machine": "AMD64"},
            "python": {"version": "3.11.0"},
        },
        "documentation_verification": {"status": "passed"},
        "sbom": {"status": "not_produced"},
        "artifacts": [],
    }
    original_replace = Path.replace

    def fail_summary_replace(self: Path, target: Path) -> Path:
        if self.name == f".{script.SUMMARY_NAME}.tmp":
            raise OSError("simulated summary replace failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_summary_replace)

    with pytest.raises(OSError, match="simulated summary replace failure"):
        script._write_outputs(manifest, tmp_path)

    assert not (tmp_path / script.MANIFEST_NAME).exists()
    assert not (tmp_path / f".{script.MANIFEST_NAME}.tmp").exists()
    assert not (tmp_path / f".{script.SUMMARY_NAME}.tmp").exists()


def test_write_outputs_attempts_temp_cleanup_when_compensation_unlink_fails(
    tmp_path: Path,
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = _load_script()
    caplog.set_level("WARNING", logger=script.LOGGER.name)
    manifest = {
        "generated_at": "2026-07-03T00:00:00+00:00",
        "decision": {
            "status": "approved",
            "owner": "release owner",
            "approver": "approver",
            "testpypi": "skipped",
            "testpypi_note": "",
        },
        "release": {"version": "0.1.0", "tag": "v0.1.0"},
        "repositories": {
            "framework": {"commit": "framework-commit"},
            "examples": {"commit": "examples-commit"},
        },
        "validation_results": {
            "release_candidate": {"status": "pass"},
            "examples_wheel": {"status": "pass"},
        },
        "environment": {
            "platform": {"system": "Windows", "release": "11", "machine": "AMD64"},
            "python": {"version": "3.11.0"},
        },
        "documentation_verification": {"status": "passed"},
        "sbom": {"status": "not_produced"},
        "artifacts": [],
    }
    cleanup_attempts: list[str] = []
    original_replace = Path.replace
    original_unlink = Path.unlink

    def fail_summary_replace(self: Path, target: Path) -> Path:
        if self.name == f".{script.SUMMARY_NAME}.tmp":
            raise OSError("simulated summary replace failure")
        return original_replace(self, target)

    def fail_manifest_unlink_once(self: Path, *args, **kwargs) -> None:
        cleanup_attempts.append(self.name)
        if self.name == script.MANIFEST_NAME:
            raise OSError("simulated manifest unlink failure")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail_summary_replace)
    monkeypatch.setattr(Path, "unlink", fail_manifest_unlink_once)

    with pytest.raises(OSError, match="simulated summary replace failure"):
        script._write_outputs(manifest, tmp_path)

    assert script.MANIFEST_NAME in cleanup_attempts
    assert f".{script.MANIFEST_NAME}.tmp" in cleanup_attempts
    assert f".{script.SUMMARY_NAME}.tmp" in cleanup_attempts
    assert (tmp_path / script.MANIFEST_NAME).exists()
    assert not (tmp_path / f".{script.SUMMARY_NAME}.tmp").exists()
    assert "simulated manifest unlink failure" in caplog.text


def test_write_outputs_cleans_temp_files_when_manifest_replace_fails(tmp_path: Path, monkeypatch) -> None:
    script = _load_script()
    manifest = {
        "generated_at": "2026-07-03T00:00:00+00:00",
        "decision": {
            "status": "approved",
            "owner": "release owner",
            "approver": "approver",
            "testpypi": "skipped",
            "testpypi_note": "",
        },
        "release": {"version": "0.1.0", "tag": "v0.1.0"},
        "repositories": {
            "framework": {"commit": "framework-commit"},
            "examples": {"commit": "examples-commit"},
        },
        "validation_results": {
            "release_candidate": {"status": "pass"},
            "examples_wheel": {"status": "pass"},
        },
        "environment": {
            "platform": {"system": "Windows", "release": "11", "machine": "AMD64"},
            "python": {"version": "3.11.0"},
        },
        "documentation_verification": {"status": "passed"},
        "sbom": {"status": "not_produced"},
        "artifacts": [],
    }

    def fail_manifest_replace(self: Path, target: Path) -> Path:
        if self.name == f".{script.MANIFEST_NAME}.tmp":
            raise OSError("simulated manifest replace failure")
        raise AssertionError("summary replace should not run")

    monkeypatch.setattr(Path, "replace", fail_manifest_replace)

    with pytest.raises(OSError, match="simulated manifest replace failure"):
        script._write_outputs(manifest, tmp_path)

    assert not (tmp_path / script.MANIFEST_NAME).exists()
    assert not (tmp_path / script.SUMMARY_NAME).exists()
    assert not (tmp_path / f".{script.MANIFEST_NAME}.tmp").exists()
    assert not (tmp_path / f".{script.SUMMARY_NAME}.tmp").exists()


def test_write_outputs_writes_manifest_and_summary(tmp_path: Path) -> None:
    script = _load_script()
    manifest = {
        "generated_at": "2026-07-03T00:00:00+00:00",
        "decision": {
            "status": "approved",
            "owner": "release owner",
            "approver": "approver",
            "testpypi": "skipped",
            "testpypi_note": "",
        },
        "release": {"version": "0.1.0", "tag": "v0.1.0"},
        "repositories": {
            "framework": {"commit": "framework-commit"},
            "examples": {"commit": "examples-commit"},
        },
        "validation_results": {
            "release_candidate": {"status": "pass"},
            "examples_wheel": {"status": "pass"},
        },
        "environment": {
            "platform": {"system": "Windows", "release": "11", "machine": "AMD64"},
            "python": {"version": "3.11.0"},
        },
        "documentation_verification": {"status": "passed"},
        "sbom": {"status": "not_produced"},
        "artifacts": [
            {"name": "rpacore.whl", "sha256": "abc", "size_bytes": 10},
        ],
    }

    script._write_outputs(manifest, tmp_path)

    written_manifest = json.loads((tmp_path / script.MANIFEST_NAME).read_text(encoding="utf-8"))
    summary = (tmp_path / script.SUMMARY_NAME).read_text(encoding="utf-8")
    assert written_manifest["release"]["version"] == "0.1.0"
    assert "`rpacore.whl` `abc` (10 bytes)" in summary


def test_summary_markdown_renders_blank_testpypi_note_as_none() -> None:
    script = _load_script()
    manifest = {
        "generated_at": "2026-07-03T00:00:00+00:00",
        "decision": {
            "status": "approved",
            "owner": "release owner",
            "approver": "approver",
            "testpypi": "skipped",
            "testpypi_note": "   ",
        },
        "release": {"version": "0.1.0", "tag": "v0.1.0"},
        "repositories": {
            "framework": {"commit": "framework-commit"},
            "examples": {"commit": "examples-commit"},
        },
        "validation_results": {
            "release_candidate": {"status": "pass"},
            "examples_wheel": {"status": "pass"},
        },
        "environment": {
            "platform": {"system": "Windows", "release": "11", "machine": "AMD64"},
            "python": {"version": "3.11.0"},
        },
        "documentation_verification": {"status": "passed"},
        "sbom": {"status": "not_produced"},
        "artifacts": [],
    }

    summary = script._summary_markdown(manifest)

    assert "TestPyPI note: (none)" in summary


def test_note_or_none_renders_non_string_as_none() -> None:
    script = _load_script()

    assert script._note_or_none(None) == "(none)"
    assert script._note_or_none(0) == "(none)"


def test_main_returns_clean_error_for_manifest_error(tmp_path: Path, capsys) -> None:
    script = _load_script()
    missing = tmp_path / "missing.json"

    exit_code = script.main(
        [
            "--repo-root",
            str(tmp_path),
            "--examples-repo",
            str(tmp_path),
            "--release-candidate-validation-results",
            str(missing),
            "--examples-wheel-validation-results",
            str(missing),
            "--owner",
            "release owner",
            "--approver",
            "approver",
            "--docs-verification",
            "passed",
        ]
    )

    assert exit_code == 1
    assert "error: cannot read release-candidate validation results" in capsys.readouterr().err


def test_main_returns_clean_error_for_write_failure(tmp_path: Path, capsys, monkeypatch) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    output_dir = tmp_path / "out"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    def fail_write_outputs(*args, **kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(script, "_write_outputs", fail_write_outputs)

    exit_code = script.main(
        [
            "--repo-root",
            str(repo_root),
            "--examples-repo",
            str(examples_repo),
            "--release-candidate-validation-results",
            str(release_candidate),
            "--examples-wheel-validation-results",
            str(examples_wheel),
            "--output-dir",
            str(output_dir),
            "--owner",
            "release owner",
            "--approver",
            "approver",
            "--docs-verification",
            "passed",
        ]
    )

    assert exit_code == 1
    assert "error: cannot write release manifest: disk full" in capsys.readouterr().err


def test_main_success_prints_output_file_paths(tmp_path: Path, capsys) -> None:
    script = _load_script()
    repo_root = tmp_path / "rpacore"
    examples_repo = tmp_path / "rpacore-examples"
    output_dir = tmp_path / "out"
    repo_root.mkdir()
    examples_repo.mkdir()
    _write_pyproject(repo_root)
    _git_init(repo_root)
    _git_init(examples_repo)
    release_candidate, examples_wheel = _write_validation_results(tmp_path)

    exit_code = script.main(
        [
            "--repo-root",
            str(repo_root),
            "--examples-repo",
            str(examples_repo),
            "--release-candidate-validation-results",
            str(release_candidate),
            "--examples-wheel-validation-results",
            str(examples_wheel),
            "--output-dir",
            str(output_dir),
            "--owner",
            "release owner",
            "--approver",
            "approver",
            "--docs-verification",
            "passed",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Wrote release manifest to {output_dir.resolve() / script.MANIFEST_NAME}" in captured.out
    assert f"Wrote release approval draft to {output_dir.resolve() / script.SUMMARY_NAME}" in captured.out


def test_build_parser_documents_docs_verification_default() -> None:
    script = _load_script()
    help_text = script.build_parser().format_help()

    assert "--docs-verification" in help_text
    assert "only 'passed' can" in help_text
    assert "produce an approved decision" in help_text
