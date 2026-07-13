"""Configuration loader for rpacore."""

from __future__ import annotations

import math
import tomllib
from pathlib import Path

from rpacore._validation import type_error, value_error
from rpacore._sqlite import validate_durable_sqlite_path
from rpacore.credentials import SUPPORTED_CREDENTIAL_PROVIDERS

_DEFAULTS: dict[str, object] = {
    "max_retries": 0,
    "retry_delay": 0.0,
    "retry_backoff": 1.0,
    "log_level": "INFO",
    "log_format": "text",
    "transaction_db_path": "rpacore.db",
    "screenshot_dir": "",
    "credential_provider": "env",
}
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})
_LOG_FORMATS = frozenset({"text", "json"})


def _validate(config: dict[str, object]) -> None:
    """Validate known configuration keys, raising TypeError or ValueError on bad values."""
    if "db_path" in config:
        raise value_error(
            "db_path",
            "renamed to transaction_db_path",
            config["db_path"],
        )

    max_retries = config["max_retries"]
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise type_error("max_retries", "int", max_retries)
    if max_retries < 0:
        raise value_error("max_retries", "int >= 0", max_retries)

    retry_delay = config["retry_delay"]
    if isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float)):
        raise type_error("retry_delay", "number >= 0", retry_delay)
    if retry_delay < 0 or not math.isfinite(retry_delay):
        raise value_error("retry_delay", "number >= 0", retry_delay)
    config["retry_delay"] = float(retry_delay)

    retry_backoff = config["retry_backoff"]
    if isinstance(retry_backoff, bool) or not isinstance(retry_backoff, (int, float)):
        raise type_error("retry_backoff", "number >= 1", retry_backoff)
    if retry_backoff < 1 or not math.isfinite(retry_backoff):
        raise value_error("retry_backoff", "number >= 1", retry_backoff)
    config["retry_backoff"] = float(retry_backoff)

    log_level = config["log_level"]
    if not isinstance(log_level, str):
        raise type_error("log_level", "str", log_level)
    normalized_log_level = log_level.upper()
    if normalized_log_level not in _LOG_LEVELS:
        raise value_error("log_level", "one of CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET", log_level)
    config["log_level"] = normalized_log_level

    log_format = config["log_format"]
    if not isinstance(log_format, str):
        raise type_error("log_format", "str", log_format)
    normalized_log_format = log_format.lower()
    if normalized_log_format not in _LOG_FORMATS:
        raise value_error("log_format", "one of text, json", log_format)
    config["log_format"] = normalized_log_format

    transaction_db_path = config["transaction_db_path"]
    if not isinstance(transaction_db_path, str):
        raise type_error("transaction_db_path", "str", transaction_db_path)
    validate_durable_sqlite_path(
        transaction_db_path,
        field="transaction_db_path",
    )

    queue = config.get("queue")
    if isinstance(queue, dict) and "db_path" in queue:
        validate_durable_sqlite_path(queue["db_path"], field="queue.db_path")

    screenshot_dir = config["screenshot_dir"]
    if not isinstance(screenshot_dir, str):
        raise type_error("screenshot_dir", "str", screenshot_dir)

    credential_provider = config["credential_provider"]
    if not isinstance(credential_provider, str):
        raise type_error("credential_provider", "str", credential_provider)
    if credential_provider not in SUPPORTED_CREDENTIAL_PROVIDERS:
        raise value_error("credential_provider", "one of env, keyring", credential_provider)


def _resolve_config_path_value(base: Path, value: str) -> str:
    if not value.strip() or value == ":memory:":
        return value
    path = Path(value)
    if path.is_absolute():
        return value
    return str(base / path)


def _resolve_database_paths(config: dict[str, object], *, base_dir: Path) -> None:
    transaction_db_path = config["transaction_db_path"]
    if isinstance(transaction_db_path, str):
        config["transaction_db_path"] = _resolve_config_path_value(base_dir, transaction_db_path)

    queue = config.get("queue")
    if isinstance(queue, dict):
        queue_db_path = queue.get("db_path")
        if isinstance(queue_db_path, str):
            queue["db_path"] = _resolve_config_path_value(base_dir, queue_db_path)


def load_config(path: str | Path = "config.toml", *, require_file: bool = False) -> dict[str, object]:
    """Load configuration from a TOML file and return a plain dict.

    Missing keys fall back to defaults. A missing file returns defaults without error.
    Pass require_file=True to raise FileNotFoundError when the file is missing.
    Known keys are validated at load time. Unknown keys pass through.
    log_level is normalized to uppercase and log_format is normalized to
    lowercase in the returned dict.
    transaction_db_path and queue.db_path are resolved relative to the config
    file's directory.
    """
    config = dict(_DEFAULTS)
    resolved = Path(path)

    if resolved.exists():
        with open(resolved, "rb") as f:
            overrides = tomllib.load(f)
        config.update(overrides)

        _resolve_database_paths(config, base_dir=resolved.resolve().parent)
    elif require_file:
        raise FileNotFoundError(f"Config file not found: {resolved}")

    _validate(config)
    return config
