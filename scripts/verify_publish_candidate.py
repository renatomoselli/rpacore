"""Verify an immutable release-candidate artifact set before publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Any, Callable


LOCK_NAME = "artifact-lock.json"
LOCK_SCHEMA_VERSION = 1
RELEASE_CANDIDATE_WORKFLOW = "Release candidate"
RELEASE_CANDIDATE_WORKFLOW_PATH = ".github/workflows/release-candidate.yml"


class CandidateVerificationError(RuntimeError):
    """Raised when a candidate cannot safely be published."""


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CandidateVerificationError(f"cannot read {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CandidateVerificationError(f"invalid JSON in {label}: {path}") from exc
    if not isinstance(value, dict):
        raise CandidateVerificationError(f"{label} must be a JSON object")
    return value


def _require_string(mapping: dict[str, Any], key: str, *, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise CandidateVerificationError(f"{label} missing {key}")
    return value


def _require_sha(value: str, *, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise CandidateVerificationError(f"{label} must be a lowercase SHA-256 digest")


def _require_commit(value: str, *, label: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise CandidateVerificationError(f"{label} must be a full lowercase git commit")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_set_sha256(artifacts: list[dict[str, Any]]) -> str:
    canonical = [
        {key: artifact[key] for key in ("name", "kind", "sha256", "size_bytes")}
        for artifact in sorted(artifacts, key=lambda artifact: str(artifact["name"]))
    ]
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _metadata_identity(metadata: bytes, *, path: Path) -> tuple[str, str]:
    parsed = BytesParser(policy=default).parsebytes(metadata)
    name = parsed.get("Name")
    version = parsed.get("Version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise CandidateVerificationError(f"artifact metadata is missing Name or Version: {path.name}")
    return name, version


def _package_metadata(path: Path, *, archive: str) -> tuple[str, str]:
    try:
        if archive == "wheel":
            with zipfile.ZipFile(path) as wheel:
                metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
                if len(metadata_names) != 1:
                    raise CandidateVerificationError(f"wheel must contain one METADATA file: {path.name}")
                metadata = wheel.read(metadata_names[0])
        else:
            with tarfile.open(path, "r:gz") as sdist:
                metadata_members = [member for member in sdist.getmembers() if member.name.endswith("/PKG-INFO")]
                root = path.name.removesuffix(".tar.gz")
                root_members = [member for member in metadata_members if member.name == f"{root}/PKG-INFO"]
                if len(root_members) != 1:
                    raise CandidateVerificationError(f"source distribution must contain one root PKG-INFO file: {path.name}")
                file = sdist.extractfile(root_members[0])
                if file is None:
                    raise CandidateVerificationError(f"cannot read source distribution metadata: {path.name}")
                metadata = file.read()
                identity = _metadata_identity(metadata, path=path)
                for member in metadata_members:
                    if member is root_members[0]:
                        continue
                    duplicate = sdist.extractfile(member)
                    if duplicate is None or _metadata_identity(duplicate.read(), path=path) != identity:
                        raise CandidateVerificationError(f"source distribution metadata copies disagree: {path.name}")
                return identity
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise CandidateVerificationError(f"cannot read {archive} metadata: {path.name}") from exc
    return _metadata_identity(metadata, path=path)


def _candidate_lock(
    candidate_dir: Path,
    *,
    expected_run_id: str,
    expected_version: str,
    expected_core_commit: str,
    expected_examples_commit: str,
    expected_wheel_sha256: str,
    expected_sdist_sha256: str,
    aggregate_evidence: Path | None,
) -> dict[str, Any]:
    if candidate_dir.is_symlink() or not candidate_dir.is_dir():
        raise CandidateVerificationError(f"candidate artifact directory is not a regular directory: {candidate_dir}")
    lock = _read_json(candidate_dir / LOCK_NAME, label="candidate artifact lock")
    if lock.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise CandidateVerificationError(f"candidate artifact lock must use schema_version {LOCK_SCHEMA_VERSION}")
    candidate = lock.get("release_candidate")
    source = lock.get("source")
    release = lock.get("release")
    artifacts = lock.get("artifacts")
    if not isinstance(candidate, dict) or not isinstance(source, dict) or not isinstance(release, dict):
        raise CandidateVerificationError("candidate artifact lock is missing candidate, source, or release identity")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise CandidateVerificationError("candidate artifact lock must contain exactly a wheel and source distribution")
    if _require_string(candidate, "workflow", label="candidate workflow") != RELEASE_CANDIDATE_WORKFLOW:
        raise CandidateVerificationError("candidate artifact lock has an unexpected workflow identity")
    if _require_string(candidate, "workflow_path", label="candidate workflow") != RELEASE_CANDIDATE_WORKFLOW_PATH:
        raise CandidateVerificationError("candidate artifact lock has an unexpected workflow path")
    if _require_string(candidate, "run_id", label="candidate workflow") != expected_run_id:
        raise CandidateVerificationError("candidate artifact lock run ID differs from the requested candidate run")
    core_commit = _require_string(source, "core_commit", label="candidate source")
    examples_commit = _require_string(source, "examples_commit", label="candidate source")
    _require_commit(core_commit, label="candidate core_commit")
    _require_commit(examples_commit, label="candidate examples_commit")
    if core_commit != expected_core_commit or examples_commit != expected_examples_commit:
        raise CandidateVerificationError("candidate source commits differ from the requested publication identity")
    version = _require_string(release, "version", label="candidate release")
    if version != expected_version:
        raise CandidateVerificationError(
            f"candidate version {version!r} does not match the required release version {expected_version!r}"
        )
    if _require_string(release, "tag", label="candidate release") != f"v{version}":
        raise CandidateVerificationError("candidate release tag does not match its version")

    expected_names = {f"rpacore-{version}-py3-none-any.whl", f"rpacore-{version}.tar.gz"}
    declared_names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise CandidateVerificationError("candidate artifact entry must be a JSON object")
        name = _require_string(artifact, "name", label="candidate artifact")
        if Path(name).name != name or name not in expected_names:
            raise CandidateVerificationError(f"candidate artifact has an unexpected name: {name}")
        if name in declared_names:
            raise CandidateVerificationError(f"candidate artifact is declared more than once: {name}")
        declared_names.add(name)
        kind = _require_string(artifact, "kind", label=f"candidate artifact {name}")
        if kind not in {"wheel", "sdist"}:
            raise CandidateVerificationError(f"candidate artifact has an invalid kind: {name}")
        sha256 = _require_string(artifact, "sha256", label=f"candidate artifact {name}")
        _require_sha(sha256, label=f"candidate artifact {name} sha256")
        size_bytes = artifact.get("size_bytes")
        if not isinstance(size_bytes, int) or size_bytes < 1:
            raise CandidateVerificationError(f"candidate artifact has an invalid size: {name}")
        path = candidate_dir / name
        if path.is_symlink() or not path.is_file():
            raise CandidateVerificationError(f"candidate artifact is missing or not a regular file: {name}")
        if path.stat().st_size != size_bytes:
            raise CandidateVerificationError(f"candidate artifact size mismatch: {name}")
        if _sha256(path) != sha256:
            raise CandidateVerificationError(f"candidate artifact SHA-256 mismatch: {name}")
    if declared_names != expected_names:
        raise CandidateVerificationError("candidate artifact lock does not declare the exact expected artifact names")
    actual_names = {path.name for path in candidate_dir.iterdir()}
    if actual_names != declared_names | {LOCK_NAME}:
        raise CandidateVerificationError("candidate artifact directory contains missing or unexpected files")

    wheel = candidate_dir / f"rpacore-{version}-py3-none-any.whl"
    sdist = candidate_dir / f"rpacore-{version}.tar.gz"
    if _package_metadata(wheel, archive="wheel") != ("rpacore", version):
        raise CandidateVerificationError("wheel package metadata does not match the candidate release identity")
    if _package_metadata(sdist, archive="source distribution") != ("rpacore", version):
        raise CandidateVerificationError("source distribution package metadata does not match the candidate release identity")
    hashes = {str(artifact["name"]): str(artifact["sha256"]) for artifact in artifacts}
    if hashes[wheel.name] != expected_wheel_sha256 or hashes[sdist.name] != expected_sdist_sha256:
        raise CandidateVerificationError("candidate artifact hashes differ from the requested publication identity")
    digest = _artifact_set_sha256(artifacts)
    if _require_string(lock, "artifact_set_sha256", label="candidate artifact lock") != digest:
        raise CandidateVerificationError("candidate artifact-set digest mismatch")
    if aggregate_evidence is None:
        raise CandidateVerificationError("aggregate release-candidate evidence is required")
    aggregate = _read_json(aggregate_evidence, label="release-candidate aggregate evidence")
    if aggregate.get("gate_status") != "pass" or aggregate.get("artifact_lock") != lock:
        raise CandidateVerificationError("aggregate release-candidate evidence does not exactly match the passing artifact lock")
    return lock


def _github_json(
    url: str,
    *,
    token: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any] | None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with opener(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CandidateVerificationError(f"GitHub API request failed with HTTP {exc.code}: {url}") from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise CandidateVerificationError(f"GitHub API request failed: {url}") from exc
    if not isinstance(value, dict):
        raise CandidateVerificationError(f"GitHub API returned a non-object response: {url}")
    return value


def _public_json(url: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CandidateVerificationError(f"public registry request failed with HTTP {exc.code}: {url}") from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise CandidateVerificationError(f"public registry request failed: {url}") from exc
    if not isinstance(value, dict):
        raise CandidateVerificationError(f"public registry returned a non-object response: {url}")
    return value


def _verify_remote_identity(
    lock: dict[str, Any],
    *,
    repository: str,
    token: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    if not repository or "/" not in repository:
        raise CandidateVerificationError("repository must use owner/name form")
    if not token:
        raise CandidateVerificationError("a GitHub token is required for remote candidate identity checks")
    candidate = lock["release_candidate"]
    source = lock["source"]
    release = lock["release"]
    if _require_string(lock, "repository", label="candidate artifact lock") != repository:
        raise CandidateVerificationError("candidate artifact lock repository differs from the publishing repository")
    repository_path = urllib.parse.quote(repository, safe="/")
    run = _github_json(
        f"https://api.github.com/repos/{repository_path}/actions/runs/{candidate['run_id']}",
        token=token,
        opener=opener,
    )
    if run is None:
        raise CandidateVerificationError("requested candidate workflow run no longer exists")
    if (
        run.get("name") != RELEASE_CANDIDATE_WORKFLOW
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_sha") != source["core_commit"]
        or run.get("head_branch") != "main"
        or run.get("repository", {}).get("full_name") != repository
    ):
        raise CandidateVerificationError("requested workflow run does not match the completed release-candidate identity")
    workflow_id = run.get("workflow_id")
    if not isinstance(workflow_id, int):
        raise CandidateVerificationError("requested workflow run has no numeric workflow identity")
    workflow = _github_json(
        f"https://api.github.com/repos/{repository_path}/actions/workflows/{workflow_id}",
        token=token,
        opener=opener,
    )
    if workflow is None or workflow.get("path") != RELEASE_CANDIDATE_WORKFLOW_PATH:
        raise CandidateVerificationError("requested workflow run does not use release-candidate.yml")
    if _github_json(
        f"https://api.github.com/repos/{repository_path}/git/ref/tags/{urllib.parse.quote(release['tag'], safe='')}",
        token=token,
        opener=opener,
    ) is not None:
        raise CandidateVerificationError(f"GitHub tag already exists: {release['tag']}")
    if _github_json(
        f"https://api.github.com/repos/{repository_path}/releases/tags/{urllib.parse.quote(release['tag'], safe='')}",
        token=token,
        opener=opener,
    ) is not None:
        raise CandidateVerificationError(f"GitHub release already exists: {release['tag']}")
    if _public_json(
        f"https://pypi.org/pypi/rpacore/{urllib.parse.quote(release['version'], safe='')}/json",
        opener=opener,
    ) is not None:
        raise CandidateVerificationError(f"PyPI version already exists: rpacore {release['version']}")


def verify_publish_candidate(
    *,
    candidate_dir: Path,
    expected_run_id: str,
    expected_version: str,
    expected_core_commit: str,
    expected_examples_commit: str,
    expected_wheel_sha256: str,
    expected_sdist_sha256: str,
    publish_confirm: str,
    repository: str | None = None,
    github_token: str | None = None,
    expected_lock: Path | None = None,
    aggregate_evidence: Path | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    if not expected_run_id.isdecimal() or int(expected_run_id) < 1:
        raise CandidateVerificationError("candidate run ID must be a positive decimal workflow run ID")
    expected_confirmation = f"publish rpacore candidate {expected_run_id} to pypi"
    if publish_confirm != expected_confirmation:
        raise CandidateVerificationError(f"expected publish confirmation: {expected_confirmation}")
    _require_commit(expected_core_commit, label="requested core_commit")
    _require_commit(expected_examples_commit, label="requested examples_commit")
    _require_sha(expected_wheel_sha256, label="requested wheel SHA-256")
    _require_sha(expected_sdist_sha256, label="requested source distribution SHA-256")
    lock = _candidate_lock(
        candidate_dir.resolve(),
        expected_run_id=expected_run_id,
        expected_version=expected_version,
        expected_core_commit=expected_core_commit,
        expected_examples_commit=expected_examples_commit,
        expected_wheel_sha256=expected_wheel_sha256,
        expected_sdist_sha256=expected_sdist_sha256,
        aggregate_evidence=aggregate_evidence,
    )
    if expected_lock is not None:
        if lock != _read_json(expected_lock, label="previously verified candidate lock"):
            raise CandidateVerificationError("downloaded candidate lock differs from the previously verified candidate lock")
    _verify_remote_identity(lock, repository=repository or "", token=github_token or "", opener=opener)
    return lock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a frozen release-candidate artifact set before publication.")
    parser.add_argument("--candidate-artifacts-dir", type=Path, required=True)
    parser.add_argument("--candidate-run-id", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-core-commit", required=True)
    parser.add_argument("--expected-examples-commit", required=True)
    parser.add_argument("--expected-wheel-sha256", required=True)
    parser.add_argument("--expected-sdist-sha256", required=True)
    parser.add_argument("--publish-confirm", required=True)
    parser.add_argument("--repository")
    parser.add_argument("--github-token")
    parser.add_argument("--expected-lock", type=Path)
    parser.add_argument("--aggregate-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        lock = verify_publish_candidate(
            candidate_dir=args.candidate_artifacts_dir,
            expected_run_id=args.candidate_run_id,
            expected_version=args.expected_version,
            expected_core_commit=args.expected_core_commit,
            expected_examples_commit=args.expected_examples_commit,
            expected_wheel_sha256=args.expected_wheel_sha256,
            expected_sdist_sha256=args.expected_sdist_sha256,
            publish_confirm=args.publish_confirm,
            repository=args.repository,
            github_token=args.github_token,
            expected_lock=args.expected_lock,
            aggregate_evidence=args.aggregate_evidence,
        )
        if args.output is not None:
            args.output.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (CandidateVerificationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
