"""Tests for rpacore.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from rpacore.config import load_config


class TestLoadConfig:
    def test_repository_sample_config_uses_lease_timeout(self) -> None:
        sample = Path(__file__).resolve().parents[1] / "config.toml"
        text = sample.read_text(encoding="utf-8")

        assert "lease_timeout = 30" in text
        assert "claim_timeout" not in text

    def test_returns_defaults_when_file_missing(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "nonexistent.toml")

        assert config["max_retries"] == 0
        assert config["retry_delay"] == 0.0
        assert config["retry_backoff"] == 1.0
        assert config["log_level"] == "INFO"
        assert config["log_format"] == "text"
        assert config["transaction_db_path"] == "rpacore.db"

    def test_require_file_missing_file_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.toml"

        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config(missing, require_file=True)

    def test_loads_values_from_file(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text(
            'max_retries = 3\nlog_level = "DEBUG"\nlog_format = "json"\n'
            'transaction_db_path = "custom.db"\n',
            encoding="utf-8",
        )

        config = load_config(toml)

        assert config["max_retries"] == 3
        assert config["retry_delay"] == 0.0
        assert config["retry_backoff"] == 1.0
        assert config["log_level"] == "DEBUG"
        assert config["log_format"] == "json"
        assert config["transaction_db_path"] == str(tmp_path / "custom.db")

    def test_partial_file_merges_with_defaults(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("max_retries = 5\n", encoding="utf-8")

        config = load_config(toml)

        assert config["max_retries"] == 5
        assert config["log_level"] == "INFO"
        assert config["log_format"] == "text"
        assert config["transaction_db_path"] == str(tmp_path / "rpacore.db")

    def test_returns_plain_dict(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "nonexistent.toml")

        assert type(config) is dict

    def test_does_not_mutate_defaults_across_calls(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("max_retries = 9\n", encoding="utf-8")

        load_config(toml)
        config = load_config(tmp_path / "nonexistent.toml")

        assert config["max_retries"] == 0

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("max_retries = 2\n", encoding="utf-8")

        config = load_config(str(toml))

        assert config["max_retries"] == 2

    def test_unknown_keys_are_included(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('custom_key = "custom_value"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["custom_key"] == "custom_value"

    def test_old_top_level_db_path_raises_migration_error(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('db_path = "old.db"\n', encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == (
            "db_path expected renamed to transaction_db_path; got str value='old.db'"
        )

    def test_transaction_db_path_resolved_relative_to_config_directory(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('transaction_db_path = "mydb.db"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["transaction_db_path"] == str(tmp_path.resolve() / "mydb.db")

    def test_queue_db_path_resolved_relative_to_config_directory(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('[queue]\ndb_path = "queue.db"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["queue"]["db_path"] == str(tmp_path.resolve() / "queue.db")  # type: ignore[index]

    def test_screenshot_dir_resolved_relative_to_nested_config_directory(
        self,
        tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / "project" / "config"
        config_dir.mkdir(parents=True)
        toml = config_dir / "settings.toml"
        toml.write_text('screenshot_dir = "screenshots"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["screenshot_dir"] == str(config_dir.resolve() / "screenshots")

    def test_empty_screenshot_dir_remains_disabled(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('screenshot_dir = ""\n', encoding="utf-8")

        config = load_config(toml)

        assert config["screenshot_dir"] == ""

    @pytest.mark.parametrize("value", ["   ", ":memory:", " :memory: "])
    def test_invalid_screenshot_dir_raises(
        self,
        tmp_path: Path,
        value: str,
    ) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text(f'screenshot_dir = "{value}"\n', encoding="utf-8")

        with pytest.raises(ValueError, match="screenshot_dir expected"):
            load_config(toml)

    def test_parent_relative_screenshot_dir_remains_supported(
        self,
        tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / "project" / "config"
        config_dir.mkdir(parents=True)
        toml = config_dir / "settings.toml"
        toml.write_text('screenshot_dir = "../screenshots"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["screenshot_dir"] == str(
            config_dir.resolve() / ".." / "screenshots"
        )

    def test_absolute_screenshot_dir_not_modified(self, tmp_path: Path) -> None:
        absolute = str((tmp_path / "screenshots").resolve())
        toml_value = absolute.replace("\\", "\\\\")
        toml = tmp_path / "config.toml"
        toml.write_text(f'screenshot_dir = "{toml_value}"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["screenshot_dir"] == absolute

    def test_absolute_transaction_db_path_not_modified(self, tmp_path: Path) -> None:
        absolute = str((tmp_path / "absolute.db").resolve())
        toml_value = absolute.replace("\\", "\\\\")
        toml = tmp_path / "config.toml"
        toml.write_text(f'transaction_db_path = "{toml_value}"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["transaction_db_path"] == absolute

    def test_absolute_queue_db_path_not_modified(self, tmp_path: Path) -> None:
        absolute = str((tmp_path / "queue.db").resolve())
        toml_value = absolute.replace("\\", "\\\\")
        toml = tmp_path / "config.toml"
        toml.write_text(f'[queue]\ndb_path = "{toml_value}"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["queue"]["db_path"] == absolute  # type: ignore[index]

    def test_invalid_max_retries_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('max_retries = "3"\n', encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "max_retries expected int; got str value='3'"

    def test_negative_max_retries_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("max_retries = -1\n", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "max_retries expected int >= 0; got int value=-1"

    def test_bool_max_retries_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("max_retries = true\n", encoding="utf-8")

        with pytest.raises(TypeError, match="max_retries"):
            load_config(toml)

    def test_loads_retry_delay_and_backoff(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("retry_delay = 0.5\nretry_backoff = 2\n", encoding="utf-8")

        config = load_config(toml)

        assert config["retry_delay"] == 0.5
        assert config["retry_backoff"] == 2.0

    def test_invalid_retry_delay_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('retry_delay = "0.5"\n', encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "retry_delay expected number >= 0; got str value='0.5'"

    def test_negative_retry_delay_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("retry_delay = -0.1\n", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "retry_delay expected number >= 0; got float value=-0.1"

    def test_non_finite_retry_delay_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("retry_delay = inf\n", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "retry_delay expected number >= 0; got float value=inf"

    def test_bool_retry_delay_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("retry_delay = true\n", encoding="utf-8")

        with pytest.raises(TypeError, match="retry_delay"):
            load_config(toml)

    def test_invalid_retry_backoff_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('retry_backoff = "2"\n', encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "retry_backoff expected number >= 1; got str value='2'"

    def test_retry_backoff_below_one_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("retry_backoff = 0.5\n", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "retry_backoff expected number >= 1; got float value=0.5"

    def test_non_finite_retry_backoff_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("retry_backoff = nan\n", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "retry_backoff expected number >= 1; got float value=nan"

    def test_bool_retry_backoff_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("retry_backoff = false\n", encoding="utf-8")

        with pytest.raises(TypeError, match="retry_backoff"):
            load_config(toml)

    def test_invalid_log_level_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("log_level = 1\n", encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "log_level expected str; got int value=1"

    def test_invalid_log_level_value_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('log_level = "LOUD"\n', encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == (
            "log_level expected one of CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET; "
            "got str value='LOUD'"
        )

    def test_lowercase_log_level_is_normalized(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('log_level = "debug"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["log_level"] == "DEBUG"

    def test_invalid_log_format_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("log_format = 1\n", encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "log_format expected str; got int value=1"

    def test_invalid_log_format_value_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('log_format = "xml"\n', encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == (
            "log_format expected one of text, json; got str value='xml'"
        )

    def test_uppercase_log_format_is_normalized(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('log_format = "JSON"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["log_format"] == "json"

    def test_invalid_transaction_db_path_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("transaction_db_path = 123\n", encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "transaction_db_path expected str; got int value=123"

    @pytest.mark.parametrize("db_path", ["", "   ", ":memory:", " :memory: "])
    def test_transient_transaction_db_path_rejected(
        self,
        tmp_path: Path,
        db_path: str,
    ) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text(f'transaction_db_path = "{db_path}"\n', encoding="utf-8")

        with pytest.raises(ValueError, match="transaction_db_path"):
            load_config(toml)

    @pytest.mark.parametrize("db_path", ["", "   ", ":memory:", " :memory: "])
    def test_transient_queue_db_path_rejected(
        self,
        tmp_path: Path,
        db_path: str,
    ) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text(f'[queue]\ndb_path = "{db_path}"\n', encoding="utf-8")

        with pytest.raises(ValueError, match="queue.db_path"):
            load_config(toml)

    def test_invalid_credential_provider_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("credential_provider = 123\n", encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "credential_provider expected str; got int value=123"

    def test_invalid_credential_provider_value_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('credential_provider = "vault"\n', encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == (
            "credential_provider expected one of env, keyring; got str value='vault'"
        )
