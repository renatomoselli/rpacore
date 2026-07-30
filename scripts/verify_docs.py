from __future__ import annotations

import argparse
import ast
import ipaddress
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


API_REFERENCE_DOC = Path("docs/api.md")
ROOT_MARKDOWN_DOCS = (
    Path("README.md"),
    Path("CHANGELOG.md"),
    Path("SECURITY.md"),
    Path("CONTRIBUTING.md"),
    Path("CODE_OF_CONDUCT.md"),
    Path("SUPPORT.md"),
    Path("AUTHORS.md"),
    Path("MAINTAINERS.md"),
)
SENSITIVE_PATTERNS = {
    r"\b[A-Za-z]:\\repos\\": "local checkout path",
    r"\.internal": "private notes path",
}

PUBLIC_FORBIDDEN_PATTERNS = {
    r"pip install -e": "editable install instruction",
}

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
API_TABLE_ROW = re.compile(r"^\|\s*(?P<cell>.+?)\s*\|")
API_SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
CHANGELOG_RELEASE_HEADING = re.compile(r"^## v(?P<version>[^\s]+) - ")
RELEASE_SERIES = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?=\d)")
DEV_HOSTS = {"localhost"}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    message: str


def _slug(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text.strip().lower())
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text.strip("-")


def _markdown_files(root: Path) -> list[Path]:
    candidates = [*(root / path for path in ROOT_MARKDOWN_DOCS), *sorted((root / "docs").glob("*.md"))]
    return [path for path in candidates if path.is_file()]


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        base = _slug(match.group(2))
        if not base:
            continue
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def _line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _check_forbidden_patterns(root: Path, docs: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    patterns = {**SENSITIVE_PATTERNS, **PUBLIC_FORBIDDEN_PATTERNS}
    for path in docs:
        relative = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        for pattern, label in patterns.items():
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                findings.append(
                    Finding(
                        relative,
                        _line_number(text, match.start()),
                        f"forbidden public-doc pattern: {label}",
                    )
                )
    return findings


def _is_dev_url(parsed) -> bool:
    host = parsed.hostname
    if not host:
        return False
    if host.lower() in DEV_HOSTS:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def _check_links(root: Path, docs: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    anchor_cache = {path: _anchors(path) for path in docs}
    for path in docs:
        text = path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(text):
            target = match.group(1).strip()
            parsed = urlparse(target)
            if parsed.scheme in {"http", "https", "mailto"}:
                if parsed.scheme in {"http", "https"} and _is_dev_url(parsed):
                    findings.append(
                        Finding(
                            path.relative_to(root),
                            _line_number(text, match.start()),
                            f"external link uses local development host: {target}",
                        )
                    )
                continue
            if parsed.scheme:
                findings.append(
                    Finding(
                        path.relative_to(root),
                        _line_number(text, match.start()),
                        f"unsupported link scheme: {target}",
                    )
                )
                continue

            link_path = unquote(parsed.path)
            if parsed.fragment and not link_path:
                target_path = path.resolve()
            else:
                target_path = (path.parent / link_path).resolve() if link_path else path.resolve()
            if root.resolve() not in (target_path, *target_path.parents):
                findings.append(
                    Finding(
                        path.relative_to(root),
                        _line_number(text, match.start()),
                        f"link leaves repository: {target}",
                    )
                )
                continue
            if link_path and not target_path.exists():
                findings.append(
                    Finding(
                        path.relative_to(root),
                        _line_number(text, match.start()),
                        f"missing link target: {target}",
                    )
                )
                continue
            if parsed.fragment:
                if not target_path.is_file():
                    findings.append(
                        Finding(
                            path.relative_to(root),
                            _line_number(text, match.start()),
                            f"heading anchor target is not a file: {target}",
                        )
                    )
                    continue
                anchors = anchor_cache.get(target_path)
                if anchors is None and target_path.suffix.lower() == ".md":
                    anchors = _anchors(target_path)
                if anchors is not None and parsed.fragment not in anchors:
                    findings.append(
                        Finding(
                            path.relative_to(root),
                            _line_number(text, match.start()),
                            f"missing heading anchor: {target}",
                        )
                    )
    return findings


def _public_exports(root: Path) -> tuple[list[str], Finding | None]:
    init_path = root / "rpacore" / "__init__.py"
    relative = init_path.relative_to(root)
    try:
        module = ast.parse(init_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [], Finding(relative, 1, f"cannot read public API exports: {exc}")
    except SyntaxError as exc:
        return [], Finding(relative, exc.lineno or 1, f"cannot parse public API exports: {exc.msg}")
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            return [], Finding(
                relative,
                node.lineno,
                "__all__ must be a list or tuple literal for docs verification",
            )
        exports: list[str] = []
        for element in node.value.elts:
            if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
                return [], Finding(relative, element.lineno, "__all__ entries must be string literals")
            exports.append(element.value)
        return exports, None
    return [], Finding(relative, 1, "missing __all__ for public API docs verification")


def _documented_api_symbols(text: str) -> set[str]:
    symbols: set[str] = set()
    for line in text.splitlines():
        row = API_TABLE_ROW.match(line)
        if row is None:
            continue
        for symbol in API_SYMBOL.findall(row.group("cell")):
            symbols.add(symbol)
    return symbols


def _check_api_reference(root: Path, docs: list[Path]) -> list[Finding]:
    api_path = root / API_REFERENCE_DOC
    if api_path not in docs:
        return [Finding(API_REFERENCE_DOC, 1, "missing API reference")]
    text = api_path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    exports, export_finding = _public_exports(root)
    if export_finding is not None:
        findings.append(export_finding)
        return findings
    export_set = set(exports)
    for export in exports:
        if not re.search(rf"`{re.escape(export)}(?:\b|\()", text):
            findings.append(
                Finding(
                    api_path.relative_to(root),
                    1,
                    f"missing public export in API reference: {export}",
                )
            )
    for symbol in sorted(_documented_api_symbols(text) - export_set):
        findings.append(
            Finding(
                api_path.relative_to(root),
                1,
                f"API reference documents non-public export: {symbol}",
            )
        )
    return findings


def _release_version(root: Path) -> str:
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml project.version must be a non-empty string")
    return version


def _release_series(version: str) -> str:
    match = RELEASE_SERIES.match(version)
    if match is None:
        raise ValueError(f"project.version must begin with major.minor.patch: {version!r}")
    return f"{match.group('major')}.{match.group('minor')}.x"


def _check_release_version_docs(
    root: Path, *, expected_release_version: str | None = None
) -> list[Finding]:
    if not (root / "pyproject.toml").is_file():
        if expected_release_version is not None:
            raise ValueError("cannot read pyproject.toml for expected release-version verification")
        return []
    version = _release_version(root)
    series = _release_series(version)
    findings: list[Finding] = []
    if expected_release_version is not None and version != expected_release_version:
        findings.append(
            Finding(
                Path("pyproject.toml"),
                1,
                f"project.version must match expected release version {expected_release_version}, got {version}",
            )
        )
    expected_text = {
        Path("README.md"): f"v{version}",
        Path("SUPPORT.md"): f"`{series}`",
        Path("SECURITY.md"): f"| {series} | Yes |",
        Path("docs/README.md"): f"public `{series}` entry point",
    }
    for relative, expected in expected_text.items():
        try:
            text = (root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(
                Finding(relative, 1, f"cannot read release version documentation: {exc}")
            )
            continue
        if expected not in text:
            findings.append(
                Finding(relative, 1, f"release version documentation must contain: {expected}"))

    changelog = root / "CHANGELOG.md"
    try:
        changelog_lines = changelog.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        findings.append(Finding(Path("CHANGELOG.md"), 1, f"cannot read release changelog: {exc}"))
        return findings
    for line_number, line in enumerate(changelog_lines, start=1):
        match = CHANGELOG_RELEASE_HEADING.match(line)
        if match is not None:
            if match.group("version") != version:
                findings.append(
                    Finding(
                        Path("CHANGELOG.md"),
                        line_number,
                        f"top changelog release version must be {version}, got {match.group('version')}",
                    )
                )
            break
    else:
        findings.append(Finding(Path("CHANGELOG.md"), 1, "missing top-level release heading"))
    return findings


def _run_check(name: str, check) -> list[Finding]:
    try:
        return check()
    except (OSError, UnicodeError, ValueError, re.error) as exc:
        return [
            Finding(
                Path("<docs-verifier>"),
                1,
                f"{name} check could not complete; fix the doc input or verifier: {type(exc).__name__}: {exc}",
            )
        ]


def verify_docs(root: Path, *, expected_release_version: str | None = None) -> list[Finding]:
    docs = _markdown_files(root)
    findings: list[Finding] = []
    findings.extend(_run_check("links", lambda: _check_links(root, docs)))
    findings.extend(_run_check("forbidden-patterns", lambda: _check_forbidden_patterns(root, docs)))
    findings.extend(_run_check("api-reference", lambda: _check_api_reference(root, docs)))
    findings.extend(
        _run_check(
            "release-version",
            lambda: _check_release_version_docs(
                root, expected_release_version=expected_release_version
            ),
        )
    )
    return sorted(findings, key=lambda finding: (str(finding.path), finding.line, finding.message))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify public Markdown docs.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--expected-release-version",
        help="require pyproject.toml project.version to match this release version",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    findings = verify_docs(
        args.repo_root.resolve(), expected_release_version=args.expected_release_version
    )
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.message}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
