"""Tests for rpacore.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from rpacore.config import load_config


class TestLoadConfig:
    def test_returns_defaults_when_file_missing(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "nonexistent.toml")

        assert config["max_retries"] == 0
        assert config["log_level"] == "INFO"
        assert config["db_path"] == "rpacore.db"

    def test_loads_values_from_file(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('max_retries = 3\nlog_level = "DEBUG"\ndb_path = "custom.db"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["max_retries"] == 3
        assert config["log_level"] == "DEBUG"
        assert config["db_path"] == str(tmp_path / "custom.db")

    def test_partial_file_merges_with_defaults(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("max_retries = 5\n", encoding="utf-8")

        config = load_config(toml)

        assert config["max_retries"] == 5
        assert config["log_level"] == "INFO"
        assert config["db_path"] == str(tmp_path / "rpacore.db")

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

    def test_db_path_resolved_relative_to_config_directory(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text('db_path = "mydb.db"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["db_path"] == str(tmp_path.resolve() / "mydb.db")

    def test_absolute_db_path_not_modified(self, tmp_path: Path) -> None:
        absolute = str((tmp_path / "absolute.db").resolve())
        toml_value = absolute.replace("\\", "\\\\")
        toml = tmp_path / "config.toml"
        toml.write_text(f'db_path = "{toml_value}"\n', encoding="utf-8")

        config = load_config(toml)

        assert config["db_path"] == absolute

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

    def test_invalid_log_level_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("log_level = 1\n", encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "log_level expected str; got int value=1"

    def test_invalid_db_path_type_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "config.toml"
        toml.write_text("db_path = 123\n", encoding="utf-8")

        with pytest.raises(TypeError) as exc_info:
            load_config(toml)

        assert str(exc_info.value) == "db_path expected str; got int value=123"
