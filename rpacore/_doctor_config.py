"""Static configuration discovery and privacy-bounded advisory diagnostics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

from rpacore._doctor_check import NOT_APPLICABLE, PASS, WARNING, DoctorCheck, input_failure
from rpacore.config import _DEFAULTS, _load_config
from rpacore.manifest import ProjectManifest


# Advisory vocabulary follows these consumers; this is not a validation schema.
# Root: config._validate; queue: SqliteQueue.__init__; notification: build_notifiers;
# email/webhook: EmailNotifier.__init__/WebhookNotifier.__init__.
_CONTEXT_KEYS = {
    "": (*_DEFAULTS, "queue", "notification"),
    "queue": ("db_path", "lease_timeout", "max_retries", "retry_delay", "retry_backoff"),
    "notification": ("email", "webhook"),
    "notification.email": ("host", "port", "from_addr", "to_addrs", "timeout", "attach_screenshots"),
    "notification.webhook": ("url", "timeout", "include_transaction", "include_report"),
}


@dataclass(frozen=True)
class _TypoAdvisory:
    suggested_key: str
    count: int

    def to_dict(self) -> dict[str, object]:
        return {"code": "possible_framework_key_typo", "suggested_key": self.suggested_key, "count": self.count}

    def explanation(self) -> str:
        return f"Inspect {self.count} unrecognized key(s) near {self.suggested_key} for a possible typo"


def _typo_advisories(source: dict[str, object]) -> list[_TypoAdvisory]:
    suggestions: Counter[str] = Counter()
    for context, names in _CONTEXT_KEYS.items():
        section: object = source
        for part in context.split(".") if context else ():
            section = section.get(part) if isinstance(section, dict) else None
        if not isinstance(section, dict):
            continue
        for key in section:
            if key in names:
                continue
            # ponytail: heuristic may flag intentional app keys; use a separately
            # scoped declaration contract only if nonfatal guidance proves insufficient.
            matches = get_close_matches(key, sorted(names), n=1, cutoff=0.80)
            if matches:
                target = f"{context}.{matches[0]}" if context else matches[0]
                suggestions[target] += 1
    return [
        _TypoAdvisory(suggested_key=target, count=suggestions[target])
        for target in sorted(suggestions)
    ]


def discover_config(
    *,
    config_path: str | None,
    manifest: ProjectManifest | None,
) -> tuple[dict[str, object] | None, list[DoctorCheck]]:
    explicit = config_path is not None
    candidate: Path | None = None
    if config_path is not None:
        candidate = Path(config_path)
    elif manifest is not None:
        candidate = manifest.project_dir / "config.toml"
    if candidate is None:
        return None, [DoctorCheck("project.config", NOT_APPLICABLE, "No configuration file selected")]
    try:
        candidate.stat()
    except FileNotFoundError as exc:
        if explicit:
            return None, [input_failure("project.config", "Configuration file", exc)]
        return None, [DoctorCheck("project.config", NOT_APPLICABLE, "No configuration file selected")]
    except Exception as exc:
        return None, [input_failure("project.config", "Configuration file", exc)]
    try:
        loaded = _load_config(candidate, require_file=True)
    except Exception as exc:
        return None, [input_failure("project.config", "Configuration file", exc)]
    config, source = loaded.config, loaded.source
    queue = config.get("queue")
    if queue is not None and not isinstance(queue, dict):
        return None, [input_failure("project.config", "Configuration file", TypeError())]
    typos = _typo_advisories(source)
    advisories = [typo.to_dict() for typo in typos]
    explanations = [typo.explanation() for typo in typos]
    if manifest is not None and "transaction_db_path" in source:
        if Path(str(config["transaction_db_path"])).resolve() != Path(manifest.transaction_db_path).resolve():
            advisories.append({"code": "transaction_path_mismatch"})
            explanations.append(
                "Explicit config transaction path differs from the manifest; generated/CLI-managed projects use "
                "manifest storage, while library applications choose their storage explicitly"
            )
    if not advisories:
        return config, [DoctorCheck("project.config", PASS, "Configuration file is valid")]
    return config, [DoctorCheck(
        "project.config", WARNING, "; ".join(explanations), {"advisories": advisories},
    )]
