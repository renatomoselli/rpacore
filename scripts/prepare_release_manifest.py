"""Prepare release manifest and go/no-go draft from rehearsal evidence."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_NAME = "release-manifest.json"
SUMMARY_NAME = "release-go-no-go.md"
GIT_TIMEOUT_SECONDS = 30
EVIDENCE_STATUSES = frozenset({"pass", "fail"})
LOGGER = logging.getLogger(__name__)


class ManifestError(RuntimeError):
    """Raised when release manifest inputs are incomplete or invalid."""


@dataclass(frozen=True)
class RepoState:
    name: str
    path: str
    commit: str
    branch: str
    dirty: bool
    status: list[str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _git_output(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        command = " ".join(("git", *args))
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise ManifestError(f"{command} failed in {repo}{suffix}") from exc
    except FileNotFoundError as exc:
        raise ManifestError("git executable was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        command = " ".join(("git", *args))
        raise ManifestError(f"{command} timed out after {GIT_TIMEOUT_SECONDS} seconds") from exc
    return completed.stdout.strip()


def _repo_state(name: str, path: Path) -> RepoState:
    if not (path / ".git").exists():
        raise ManifestError(f"{name} is not a git repository: {path}")
    status = _git_output(path, "status", "--porcelain").splitlines()
    branch = _git_output(path, "branch", "--show-current")
    if not branch:
        branch = "DETACHED"
    return RepoState(
        name=name,
        path=str(path),
        commit=_git_output(path, "rev-parse", "HEAD"),
        branch=branch,
        dirty=bool(status),
        status=status,
    )


def _manifest_repo_state(state: RepoState) -> dict[str, Any]:
    return {
        "name": state.name,
        "commit": state.commit,
        "branch": state.branch,
        "dirty": state.dirty,
    }


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read {label}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"{label} must be a JSON object: {path}")
    return payload


def _artifact_summary(release_candidate: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = release_candidate.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ManifestError("release-candidate evidence has no artifacts")
    summary = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ManifestError("release-candidate artifact entry must be an object")
        name = artifact.get("name")
        sha256 = artifact.get("sha256")
        size_bytes = artifact.get("size_bytes")
        if not isinstance(name, str) or not name:
            raise ManifestError("release-candidate artifact missing name")
        if not isinstance(sha256, str) or not sha256:
            raise ManifestError("release-candidate artifact missing sha256")
        if not isinstance(size_bytes, int):
            raise ManifestError(f"release-candidate artifact missing size_bytes: {name}")
        summary.append(
            {
                "name": name,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        )
    return summary


def _dependency_inventory(release_candidate: dict[str, Any]) -> dict[str, Any]:
    inventory = release_candidate.get("dependency_inventory")
    if inventory is None:
        raise ManifestError("release-candidate evidence missing dependency_inventory object")
    if not isinstance(inventory, dict):
        raise ManifestError("release-candidate dependency_inventory must be an object")
    runtime_dependencies = inventory.get("runtime_dependencies")
    if not isinstance(runtime_dependencies, list):
        raise ManifestError("release-candidate dependency_inventory.runtime_dependencies must be a list")
    return inventory


def _require_string(mapping: dict[str, Any], key: str, *, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} missing {key}")
    return value


def _optional_list(mapping: dict[str, Any], key: str, *, label: str) -> list[Any]:
    value = mapping.get(key, [])
    if not isinstance(value, list):
        raise ManifestError(f"{label} {key} must be a list")
    return value


def _optional_dict(mapping: dict[str, Any], key: str, *, label: str) -> dict[str, Any]:
    value = mapping.get(key, {})
    if not isinstance(value, dict):
        raise ManifestError(f"{label} {key} must be an object")
    return value


def _release_environment(release_candidate: dict[str, Any]) -> dict[str, Any]:
    platform_info = release_candidate.get("platform")
    python_info = release_candidate.get("python")
    if not isinstance(platform_info, dict):
        raise ManifestError("release-candidate evidence missing platform object")
    if not isinstance(python_info, dict):
        raise ManifestError("release-candidate evidence missing python object")
    return {
        "platform": {
            "system": _require_string(platform_info, "system", label="release-candidate platform"),
            "release": _require_string(platform_info, "release", label="release-candidate platform"),
            "machine": _require_string(platform_info, "machine", label="release-candidate platform"),
            "architecture": platform_info.get("architecture"),
        },
        "python": {
            "version": _require_string(python_info, "version", label="release-candidate python"),
            "executable": python_info.get("executable"),
            "implementation": python_info.get("implementation"),
        },
    }


def _expected_pypi_metadata(repo_root: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - Python 3.11+ required.
        raise ManifestError("tomllib is required to read pyproject.toml") from exc
    pyproject_path = repo_root / "pyproject.toml"
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read pyproject metadata: {pyproject_path}") from exc
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ManifestError("pyproject.toml missing [project] table")
    name = _require_string(project, "name", label="pyproject.toml project")
    version = _require_string(project, "version", label="pyproject.toml project")
    return {
        "name": name,
        "version": version,
        "description": project.get("description"),
        "requires_python": project.get("requires-python"),
        "license": project.get("license"),
        "license_files": _optional_list(project, "license-files", label="pyproject.toml project"),
        "urls": _optional_dict(project, "urls", label="pyproject.toml project"),
    }


def _status_from_evidence(
    release_candidate: dict[str, Any],
    examples_wheel: dict[str, Any],
    *,
    framework: RepoState,
    examples: RepoState,
    docs_verification: str,
    testpypi: str,
) -> str:
    release_candidate_result = _result_status(
        release_candidate,
        label="release-candidate evidence",
    )
    examples_wheel_result = _result_status(
        examples_wheel,
        label="examples-wheel evidence",
    )
    if framework.dirty or examples.dirty or docs_verification != "passed" or testpypi == "failed":
        return "no-go"
    return "go" if [release_candidate_result, examples_wheel_result] == ["pass", "pass"] else "no-go"


def _result_status(evidence: dict[str, Any], *, label: str) -> str:
    result = evidence.get("result")
    if not isinstance(result, dict):
        raise ManifestError(f"{label} result must be an object")
    if "status" not in result:
        raise ManifestError(f"{label} missing result.status")
    status = result.get("status")
    if not isinstance(status, str):
        raise ManifestError(f"{label} result.status must be a string")
    if status not in EVIDENCE_STATUSES:
        expected = ", ".join(sorted(EVIDENCE_STATUSES))
        raise ManifestError(f"{label} result.status must be one of: {expected}")
    return status


def prepare_release_manifest(
    *,
    repo_root: Path,
    examples_repo: Path,
    release_candidate_evidence: Path,
    examples_wheel_evidence: Path,
    output_dir: Path,
    tag: str,
    owner: str,
    approver: str,
    docs_verification: str,
    docs_command: str,
    docs_verification_note: str,
    sbom_path: Path | None,
    sbom_note: str,
    testpypi: str,
    testpypi_note: str,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    examples_repo = examples_repo.resolve()
    output_dir = output_dir.resolve()
    release_candidate = _read_json(release_candidate_evidence, label="release-candidate evidence")
    examples_wheel = _read_json(examples_wheel_evidence, label="examples-wheel evidence")
    framework_state = _repo_state("rpacore", repo_root)
    examples_state = _repo_state("rpacore-examples", examples_repo)
    metadata = _expected_pypi_metadata(repo_root)
    version = metadata["version"]
    sbom = {
        "status": "produced" if sbom_path else "not_produced",
        "path": str(sbom_path) if sbom_path else None,
        "note": sbom_note,
    }
    generated_at = _utc_now().isoformat()
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at,
        "decision": {
            "status": _status_from_evidence(
                release_candidate,
                examples_wheel,
                framework=framework_state,
                examples=examples_state,
                docs_verification=docs_verification,
                testpypi=testpypi,
            ),
            "owner": owner,
            "approver": approver,
            "testpypi": testpypi,
            "testpypi_note": testpypi_note,
        },
        "release": {
            "version": version,
            "tag": tag,
        },
        "repositories": {
            "framework": _manifest_repo_state(framework_state),
            "examples": _manifest_repo_state(examples_state),
        },
        "artifacts": _artifact_summary(release_candidate),
        "environment": _release_environment(release_candidate),
        "dependency_inventory": _dependency_inventory(release_candidate),
        "sbom": sbom,
        "documentation_verification": {
            "status": docs_verification,
            "command": docs_command,
            "note": docs_verification_note,
        },
        "expected_pypi_metadata": metadata,
        "evidence": {
            "release_candidate": {
                "path": str(release_candidate_evidence),
                "status": _result_status(release_candidate, label="release-candidate evidence"),
                "generated_at": release_candidate.get("generated_at"),
            },
            "examples_wheel": {
                "path": str(examples_wheel_evidence),
                "status": _result_status(examples_wheel, label="examples-wheel evidence"),
                "generated_at": examples_wheel.get("generated_at"),
            },
        },
    }
    _write_outputs(manifest, output_dir)
    return manifest


def _write_outputs(manifest: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / MANIFEST_NAME
    summary_path = output_dir / SUMMARY_NAME
    manifest_tmp = output_dir / f".{MANIFEST_NAME}.tmp"
    summary_tmp = output_dir / f".{SUMMARY_NAME}.tmp"
    manifest_replaced = False
    summary_replaced = False
    try:
        manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        summary_tmp.write_text(_summary_markdown(manifest), encoding="utf-8")
        manifest_tmp.replace(manifest_path)
        manifest_replaced = True
        summary_tmp.replace(summary_path)
        summary_replaced = True
    finally:
        if manifest_replaced and not summary_replaced:
            _unlink_missing_ok(manifest_path)
        _unlink_missing_ok(manifest_tmp)
        _unlink_missing_ok(summary_tmp)


def _unlink_missing_ok(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning("Could not remove release manifest cleanup path %s: %s", path, exc)


def _summary_markdown(manifest: dict[str, Any]) -> str:
    artifacts = "\n".join(
        f"- `{artifact['name']}` `{artifact['sha256']}` ({artifact['size_bytes']} bytes)"
        for artifact in manifest["artifacts"]
    )
    if not artifacts:
        artifacts = "- (none)"
    lines = [
        "# Release Go/No-Go Draft",
        "",
        f"- Generated at: `{manifest['generated_at']}`",
        f"- Decision status: `{manifest['decision']['status']}`",
        f"- Owner: `{manifest['decision']['owner']}`",
        f"- Approver: `{manifest['decision']['approver']}`",
        f"- Version: `{manifest['release']['version']}`",
        f"- Tag: `{manifest['release']['tag']}`",
        f"- Framework commit: `{manifest['repositories']['framework']['commit']}`",
        f"- Examples commit: `{manifest['repositories']['examples']['commit']}`",
        f"- Release-candidate evidence: `{manifest['evidence']['release_candidate']['status']}`",
        f"- Examples wheel evidence: `{manifest['evidence']['examples_wheel']['status']}`",
        f"- Documentation verification: `{manifest['documentation_verification']['status']}`",
        f"- SBOM: `{manifest['sbom']['status']}`",
        f"- Platform: `{manifest['environment']['platform']['system']}` "
        f"`{manifest['environment']['platform']['release']}` "
        f"`{manifest['environment']['platform']['machine']}`",
        f"- Python: `{manifest['environment']['python']['version']}`",
        f"- TestPyPI: `{manifest['decision']['testpypi']}`",
        f"- TestPyPI note: {_note_or_none(manifest['decision']['testpypi_note'])}",
        "",
        "## Artifacts",
        "",
        artifacts,
        "",
        "## Open Decision",
        "",
        "A human release owner must review this draft, confirm external repository",
        "settings and publication permissions, then record the final go/no-go",
        "decision before any publication action.",
        "",
    ]
    return "\n".join(lines)


def _note_or_none(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "(none)"
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare release manifest and go/no-go draft from rehearsal evidence."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--examples-repo", type=Path, default=Path.cwd().parent / "rpacore-examples")
    parser.add_argument("--release-candidate-evidence", type=Path, required=True)
    parser.add_argument("--examples-wheel-evidence", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("validation-artifacts/release-manifest"))
    parser.add_argument("--tag", default="v0.1.0")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--approver", required=True)
    parser.add_argument(
        "--docs-verification",
        choices=("passed", "failed", "skipped"),
        default="skipped",
        help="Documentation verification result; only 'passed' can produce a go decision.",
    )
    parser.add_argument("--docs-command", default="python scripts/verify_docs.py --repo-root .")
    parser.add_argument("--docs-verification-note", default="")
    parser.add_argument("--sbom-path", type=Path)
    parser.add_argument("--sbom-note", default="No SBOM was produced for this release rehearsal.")
    parser.add_argument(
        "--testpypi",
        choices=("passed", "failed", "skipped"),
        default="skipped",
    )
    parser.add_argument("--testpypi-note", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepare_release_manifest(
            repo_root=args.repo_root,
            examples_repo=args.examples_repo,
            release_candidate_evidence=args.release_candidate_evidence,
            examples_wheel_evidence=args.examples_wheel_evidence,
            output_dir=args.output_dir,
            tag=args.tag,
            owner=args.owner,
            approver=args.approver,
            docs_verification=args.docs_verification,
            docs_command=args.docs_command,
            docs_verification_note=args.docs_verification_note,
            sbom_path=args.sbom_path,
            sbom_note=args.sbom_note,
            testpypi=args.testpypi,
            testpypi_note=args.testpypi_note,
        )
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot write release manifest: {exc}", file=sys.stderr)
        return 1
    output_dir = args.output_dir.resolve()
    print(f"Wrote release manifest to {output_dir / MANIFEST_NAME}")
    print(f"Wrote release go/no-go draft to {output_dir / SUMMARY_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
