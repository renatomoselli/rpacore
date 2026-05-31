"""Configuration loader for rpacore."""

from __future__ import annotations

import tomllib
from pathlib import Path

from rpacore._validation import type_error, value_error

_DEFAULTS: dict[str, object] = {
    "max_retries": 0,
    "log_level": "INFO",
    "db_path": "rpacore.db",
    "screenshot_dir": "",
    "credential_provider": "env",
}


def _validate(config: dict[str, object]) -> None:
    """Validate known configuration keys, raising TypeError or ValueError on bad values."""
    max_retries = config["max_retries"]
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise type_error("max_retries", "int", max_retries)
    if max_retries < 0:
        raise value_error("max_retries", "int >= 0", max_retries)

    log_level = config["log_level"]
    if not isinstance(log_level, str):
        raise type_error("log_level", "str", log_level)

    db_path = config["db_path"]
    if not isinstance(db_path, str):
        raise type_error("db_path", "str", db_path)

    screenshot_dir = config["screenshot_dir"]
    if not isinstance(screenshot_dir, str):
        raise type_error("screenshot_dir", "str", screenshot_dir)

    credential_provider = config["credential_provider"]
    if not isinstance(credential_provider, str):
        raise type_error("credential_provider", "str", credential_provider)


def load_config(path: str | Path = "config.toml") -> dict[str, object]:
    """Load configuration from a TOML file and return a plain dict.

    Missing keys fall back to defaults. A missing file returns defaults without error.
    Known keys are validated at load time. Unknown keys pass through.
    db_path is resolved relative to the config file's directory.
    """
    config = dict(_DEFAULTS)
    resolved = Path(path)

    if resolved.exists():
        with open(resolved, "rb") as f:
            overrides = tomllib.load(f)
        config.update(overrides)

        db_path = config["db_path"]
        if isinstance(db_path, str) and not Path(db_path).is_absolute():
            config["db_path"] = str(resolved.resolve().parent / db_path)

    _validate(config)
    return config
