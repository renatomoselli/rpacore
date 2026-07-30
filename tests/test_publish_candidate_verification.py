"""Focused tests for immutable publish-candidate verification."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tarfile
import urllib.error
from io import BytesIO
from pathlib import Path


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_publish_candidate.py"
    spec = importlib.util.spec_from_file_location("verify_publish_candidate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(
    tmp_path: Path,
    *,
    version: str = "0.2.0",
    run_id: str = "123",
    egg_info_version: str | None = None,
) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "candidate"
    root.mkdir()
    wheel = root / f"rpacore-{version}-py3-none-any.whl"
    import zipfile

    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"rpacore-{version}.dist-info/METADATA", f"Name: rpacore\nVersion: {version}\n")
    sdist = root / f"rpacore-{version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        metadata = f"Name: rpacore\nVersion: {version}\n".encode()
        info = tarfile.TarInfo(f"rpacore-{version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, BytesIO(metadata))
        egg_info_metadata = f"Name: rpacore\nVersion: {egg_info_version or version}\n".encode()
        egg_info = tarfile.TarInfo(f"rpacore-{version}/rpacore.egg-info/PKG-INFO")
        egg_info.size = len(egg_info_metadata)
        archive.addfile(egg_info, BytesIO(egg_info_metadata))
    artifacts = [
        {"name": path.name, "kind": kind, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}
        for path, kind in ((wheel, "wheel"), (sdist, "sdist"))
    ]
    lock: dict[str, object] = {
        "schema_version": 1,
        "repository": "renatomoselli/rpacore",
        "release_candidate": {"workflow": "Release candidate", "workflow_path": ".github/workflows/release-candidate.yml", "run_id": run_id},
        "source": {"core_commit": "a" * 40, "examples_commit": "b" * 40},
        "release": {"version": version, "tag": f"v{version}"},
        "artifacts": artifacts,
    }
    lock["artifact_set_sha256"] = hashlib.sha256(json.dumps(sorted(artifacts, key=lambda item: item["name"]), separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    (root / "artifact-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    aggregate = tmp_path / "release-candidate-aggregate.json"
    aggregate.write_text(json.dumps({"gate_status": "pass", "artifact_lock": lock}), encoding="utf-8")
    lock["_aggregate"] = str(aggregate)
    return root, lock


def _absent_opener(request, *, timeout: int):
    raise urllib.error.HTTPError(request.full_url, 404, "not found", None, None)


def _remote_opener(*, tag_exists: bool = False, release_exists: bool = False):
    class Response:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(self.value).encode()

    def opener(request, *, timeout: int):
        url = request.full_url
        if "/actions/runs/" in url:
            return Response({"name": "Release candidate", "event": "workflow_dispatch", "status": "completed", "conclusion": "success", "head_sha": "a" * 40, "head_branch": "main", "workflow_id": 1, "repository": {"full_name": "renatomoselli/rpacore"}})
        if "/actions/workflows/1" in url:
            return Response({"path": ".github/workflows/release-candidate.yml"})
        if "/git/ref/tags/" in url and tag_exists:
            return Response({"ref": "refs/tags/v0.2.0"})
        if "/releases/tags/" in url and release_exists:
            return Response({"tag_name": "v0.2.0"})
        raise urllib.error.HTTPError(url, 404, "not found", None, None)

    return opener


def _verify(module, root: Path, *, opener, **kwargs):
    lock = json.loads((root / "artifact-lock.json").read_text(encoding="utf-8"))
    return module.verify_publish_candidate(
        candidate_dir=root,
        expected_run_id="123",
        expected_version="0.2.0",
        expected_core_commit="a" * 40,
        expected_examples_commit="b" * 40,
        expected_wheel_sha256=lock["artifacts"][0]["sha256"],
        expected_sdist_sha256=lock["artifacts"][1]["sha256"],
        publish_confirm="publish rpacore candidate 123 to pypi",
        repository="renatomoselli/rpacore",
        github_token="token",
        aggregate_evidence=root.parent / "release-candidate-aggregate.json",
        opener=opener,
        **kwargs,
    )


def test_verify_publish_candidate_accepts_exact_completed_candidate(tmp_path: Path) -> None:
    module = _load_script()
    root, lock = _candidate(tmp_path)
    lock.pop("_aggregate")

    assert _verify(module, root, opener=_remote_opener()) == lock


def test_verify_publish_candidate_canonicalizes_requested_commit_inputs(tmp_path: Path) -> None:
    module = _load_script()
    root, lock = _candidate(tmp_path)
    lock.pop("_aggregate")
    artifact_lock = json.loads((root / "artifact-lock.json").read_text(encoding="utf-8"))

    assert module.verify_publish_candidate(
        candidate_dir=root,
        expected_run_id="123",
        expected_version="0.2.0",
        expected_core_commit=("a" * 40).upper(),
        expected_examples_commit=("b" * 40).upper(),
        expected_wheel_sha256=artifact_lock["artifacts"][0]["sha256"],
        expected_sdist_sha256=artifact_lock["artifacts"][1]["sha256"],
        publish_confirm="publish rpacore candidate 123 to pypi",
        repository="renatomoselli/rpacore",
        github_token="token",
        aggregate_evidence=root.parent / "release-candidate-aggregate.json",
        opener=_remote_opener(),
    ) == lock


def test_verify_publish_candidate_rejects_noncanonical_lock_commit(tmp_path: Path) -> None:
    module = _load_script()
    root, _ = _candidate(tmp_path)
    artifact_lock_path = root / "artifact-lock.json"
    artifact_lock = json.loads(artifact_lock_path.read_text(encoding="utf-8"))
    artifact_lock["source"]["core_commit"] = ("a" * 40).upper()
    artifact_lock_path.write_text(json.dumps(artifact_lock), encoding="utf-8")

    try:
        _verify(module, root, opener=_remote_opener())
    except module.CandidateVerificationError as exc:
        assert "candidate core_commit must be a full lowercase git commit" in str(exc)
    else:
        raise AssertionError("Expected CandidateVerificationError")


def test_verify_publish_candidate_rejects_wrong_run(tmp_path: Path) -> None:
    module = _load_script()
    root, _ = _candidate(tmp_path)

    try:
        module.verify_publish_candidate(candidate_dir=root, expected_run_id="124", expected_version="0.2.0", expected_core_commit="a" * 40, expected_examples_commit="b" * 40, expected_wheel_sha256=hashlib.sha256((root / "rpacore-0.2.0-py3-none-any.whl").read_bytes()).hexdigest(), expected_sdist_sha256=hashlib.sha256((root / "rpacore-0.2.0.tar.gz").read_bytes()).hexdigest(), publish_confirm="publish rpacore candidate 124 to pypi", repository="renatomoselli/rpacore", github_token="token", aggregate_evidence=root.parent / "release-candidate-aggregate.json", opener=_remote_opener())
    except module.CandidateVerificationError as exc:
        assert "run ID differs" in str(exc)
    else:
        raise AssertionError("Expected CandidateVerificationError")


def test_verify_publish_candidate_rejects_missing_artifact(tmp_path: Path) -> None:
    module = _load_script()
    root, _ = _candidate(tmp_path)
    (root / "rpacore-0.2.0.tar.gz").unlink()

    try:
        _verify(module, root, opener=_remote_opener())
    except module.CandidateVerificationError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("Expected CandidateVerificationError")


def test_verify_publish_candidate_rejects_bad_hash(tmp_path: Path) -> None:
    module = _load_script()
    root, _ = _candidate(tmp_path)
    target = root / "rpacore-0.2.0.tar.gz"
    target.write_bytes(b"x" * target.stat().st_size)

    try:
        _verify(module, root, opener=_remote_opener())
    except module.CandidateVerificationError as exc:
        assert "SHA-256 mismatch" in str(exc)
    else:
        raise AssertionError("Expected CandidateVerificationError")


def test_verify_publish_candidate_rejects_wrong_version(tmp_path: Path) -> None:
    module = _load_script()
    root, _ = _candidate(tmp_path, version="0.1.1")

    try:
        _verify(module, root, opener=_remote_opener())
    except module.CandidateVerificationError as exc:
        assert "required release version" in str(exc)
    else:
        raise AssertionError("Expected CandidateVerificationError")


def test_package_metadata_rejects_disagreeing_sdist_metadata_copies(tmp_path: Path) -> None:
    module = _load_script()
    root, _ = _candidate(tmp_path, egg_info_version="9.9.9")

    try:
        module._package_metadata(root / "rpacore-0.2.0.tar.gz", archive="source distribution")
    except module.CandidateVerificationError as exc:
        assert "metadata copies disagree" in str(exc)
    else:
        raise AssertionError("Expected CandidateVerificationError")


def test_verify_publish_candidate_rejects_existing_tag_and_release(tmp_path: Path) -> None:
    module = _load_script()
    root, _ = _candidate(tmp_path)

    for opener, expected in ((_remote_opener(tag_exists=True), "tag already exists"), (_remote_opener(release_exists=True), "release already exists")):
        try:
            _verify(module, root, opener=opener)
        except module.CandidateVerificationError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("Expected CandidateVerificationError")


def test_publish_workflow_downloads_and_rechecks_only_the_named_candidate() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")

    assert "candidate_run_id" in workflow
    assert "run-id: ${{ inputs.candidate_run_id }}" in workflow
    assert "--expected-version 0.2.0" in workflow
    assert "--expected-lock publish-lock/candidate-publish-lock.json" in workflow
    assert "python -m build" not in workflow
    assert 'python -m pip install --disable-pip-version-check "twine==5.1.1"' in workflow
