"""Fail closed before building a release candidate with a claimed identity."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


class ReleaseIdentityError(RuntimeError):
    """Raised when a release candidate identity is unsafe to build."""


def _canonical_commit(value: str) -> str:
    if len(value) != 40 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ReleaseIdentityError("expected_core_commit must be a full git commit")
    return value.lower()


def _current_commit(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseIdentityError("cannot determine the frozen framework commit") from exc


def _release_version(repo_root: Path) -> str:
    try:
        metadata = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseIdentityError("cannot read package metadata") from exc
    project = metadata.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise ReleaseIdentityError("package metadata is missing project.version")
    return version


def _require_unreleased_changelog_entry(repo_root: Path, version: str) -> None:
    heading = f"## v{version} - Unreleased"
    try:
        headings = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseIdentityError("cannot read CHANGELOG.md") from exc
    if heading not in {line.rstrip() for line in headings}:
        raise ReleaseIdentityError(f"CHANGELOG.md is missing the required heading: {heading}")


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
        raise ReleaseIdentityError(f"GitHub API request failed with HTTP {exc.code}: {url}") from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ReleaseIdentityError(f"GitHub API request failed: {url}") from exc
    if not isinstance(value, dict):
        raise ReleaseIdentityError(f"GitHub API returned a non-object response: {url}")
    return value


def _pypi_json(
    url: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any] | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ReleaseIdentityError(f"PyPI request failed with HTTP {exc.code}: {url}") from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ReleaseIdentityError(f"PyPI request failed: {url}") from exc
    if not isinstance(value, dict):
        raise ReleaseIdentityError(f"PyPI returned a non-object response: {url}")
    return value


def verify_release_candidate_identity(
    *,
    repo_root: Path,
    expected_core_commit: str,
    repository: str,
    github_token: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Verify an unclaimed version/tag identity for the frozen framework commit."""

    expected_core_commit = _canonical_commit(expected_core_commit)
    if _current_commit(repo_root) != expected_core_commit:
        raise ReleaseIdentityError("checked-out framework commit differs from expected_core_commit")
    if not repository or "/" not in repository:
        raise ReleaseIdentityError("repository must use owner/name form")
    if not github_token:
        raise ReleaseIdentityError("a GitHub token is required for release identity checks")

    version = _release_version(repo_root)
    tag = f"v{version}"
    _require_unreleased_changelog_entry(repo_root, version)
    repository_path = urllib.parse.quote(repository, safe="/")
    encoded_tag = urllib.parse.quote(tag, safe="")
    if _github_json(
        f"https://api.github.com/repos/{repository_path}/git/ref/tags/{encoded_tag}",
        token=github_token,
        opener=opener,
    ) is not None:
        raise ReleaseIdentityError(f"GitHub tag already exists: {tag}")
    if _github_json(
        f"https://api.github.com/repos/{repository_path}/releases/tags/{encoded_tag}",
        token=github_token,
        opener=opener,
    ) is not None:
        raise ReleaseIdentityError(f"GitHub release already exists: {tag}")
    if _pypi_json(
        f"https://pypi.org/pypi/rpacore/{urllib.parse.quote(version, safe='')}/json",
        opener=opener,
    ) is not None:
        raise ReleaseIdentityError(f"PyPI version already exists: rpacore {version}")
    return {
        "schema_version": 1,
        "status": "pass",
        "source": {"core_commit": expected_core_commit},
        "release": {"version": version, "tag": tag},
    }


def _write_output(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-core-commit", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--github-token", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify_release_candidate_identity(
            repo_root=args.repo_root.resolve(),
            expected_core_commit=args.expected_core_commit,
            repository=args.repository,
            github_token=args.github_token,
        )
    except ReleaseIdentityError as exc:
        result = {"schema_version": 1, "status": "fail", "error": str(exc)}
        _write_output(args.output, result)
        print(str(exc), file=sys.stderr)
        return 1
    _write_output(args.output, result)
    print(f"Verified unclaimed release identity: {result['release']['tag']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
