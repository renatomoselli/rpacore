"""Configuration loader for OREF."""

from __future__ import annotations

import tomllib
from pathlib import Path

_DEFAULTS: dict[str, object] = {
    "max_retries": 0,
    "log_level": "INFO",
    "db_path": "oref.db",
}


def _validate(config: dict[str, object]) -> None:
    """Validate known configuration keys, raising TypeError or ValueError on bad values."""
    max_retries = config["max_retries"]
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise TypeError(f"max_retries must be an int, got {type(max_retries).__name__!r}")
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")

    log_level = config["log_level"]
    if not isinstance(log_level, str):
        raise TypeError(f"log_level must be a str, got {type(log_level).__name__!r}")

    db_path = config["db_path"]
    if not isinstance(db_path, str):
        raise TypeError(f"db_path must be a str, got {type(db_path).__name__!r}")


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
