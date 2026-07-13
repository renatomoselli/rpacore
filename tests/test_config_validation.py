"""Tests for public config validation helpers."""

from __future__ import annotations

import pytest

import rpacore
from rpacore.config_validation import (
    ConfigField,
    optional_config,
    require_config,
    require_section,
    validate_config,
)


class TestRequireConfig:
    def test_missing_required_key_raises(self) -> None:
        with pytest.raises(KeyError) as exc_info:
            require_config({}, "timeout", int)

        assert str(exc_info.value) == "'Missing required config key: timeout'"

    def test_valid_value_is_returned(self) -> None:
        config: dict[str, object] = {"timeout": 30}

        assert require_config(config, "timeout", int) == 30

    def test_wrong_type_raises_actionable_error(self) -> None:
        config: dict[str, object] = {"timeout": "30"}

        with pytest.raises(TypeError) as exc_info:
            require_config(config, "timeout", int)

        assert str(exc_info.value) == "timeout expected int; got str value='30'"

    def test_bool_rejected_for_int(self) -> None:
        config: dict[str, object] = {"timeout": True}

        with pytest.raises(TypeError) as exc_info:
            require_config(config, "timeout", int)

        assert str(exc_info.value) == "timeout expected int; got bool value=True"

    def test_bool_allowed_when_expected_type_is_bool(self) -> None:
        config: dict[str, object] = {"enabled": True}

        assert require_config(config, "enabled", bool) is True

    def test_tuple_expected_type_allows_matching_type(self) -> None:
        config: dict[str, object] = {"delay": 0.5}

        assert require_config(config, "delay", (int, float)) == 0.5

    def test_bool_rejected_for_numeric_tuple_without_bool(self) -> None:
        config: dict[str, object] = {"delay": False}

        with pytest.raises(TypeError) as exc_info:
            require_config(config, "delay", (int, float))

        assert str(exc_info.value) == "delay expected int | float; got bool value=False"

    def test_choices_validation(self) -> None:
        config: dict[str, object] = {"mode": "loud"}

        with pytest.raises(ValueError) as exc_info:
            require_config(config, "mode", str, choices=["quiet", "normal"])

        assert str(exc_info.value) == (
            "mode expected one of 'quiet', 'normal'; got str value='loud'"
        )

    def test_string_choices_are_rejected_as_an_invalid_specification(self) -> None:
        with pytest.raises(TypeError, match="mode choices must be a non-string iterable"):
            require_config({"mode": "normal"}, "mode", str, choices="normal")

    def test_empty_choices_are_rejected_as_an_invalid_specification(self) -> None:
        with pytest.raises(ValueError, match="mode choices must not be empty"):
            require_config({"mode": "normal"}, "mode", str, choices=())

    def test_min_value_validation(self) -> None:
        config: dict[str, object] = {"timeout": 0}

        with pytest.raises(ValueError) as exc_info:
            require_config(config, "timeout", int, min_value=1)

        assert str(exc_info.value) == "timeout expected int >= 1; got int value=0"

    def test_incomparable_min_value_raises_value_error(self) -> None:
        config: dict[str, object] = {"timeout": 5}

        with pytest.raises(ValueError) as exc_info:
            require_config(config, "timeout", int, min_value="1")

        assert str(exc_info.value) == "timeout expected int comparable to '1'; got int value=5"

    def test_max_value_validation(self) -> None:
        config: dict[str, object] = {"timeout": 61}

        with pytest.raises(ValueError) as exc_info:
            require_config(config, "timeout", int, max_value=60)

        assert str(exc_info.value) == "timeout expected int <= 60; got int value=61"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_is_rejected_when_bounds_are_configured(
        self,
        value: float,
    ) -> None:
        with pytest.raises(ValueError, match="ratio expected finite float"):
            require_config(
                {"ratio": value},
                "ratio",
                float,
                min_value=0.0,
                max_value=1.0,
            )

    def test_incomparable_max_value_raises_value_error(self) -> None:
        config: dict[str, object] = {"timeout": 5}

        with pytest.raises(ValueError) as exc_info:
            require_config(config, "timeout", int, max_value="10")

        assert str(exc_info.value) == "timeout expected int comparable to '10'; got int value=5"

    def test_allow_empty_false_rejects_empty_string(self) -> None:
        config: dict[str, object] = {"path": ""}

        with pytest.raises(ValueError) as exc_info:
            require_config(config, "path", str, allow_empty=False)

        assert str(exc_info.value) == "path expected non-empty str; got str value=''"


class TestRequireSection:
    def test_nested_section_is_returned(self) -> None:
        section: dict[str, object] = {"db_path": "queue.db"}
        config: dict[str, object] = {"queue": section}

        assert require_section(config, "queue") is section

    def test_missing_section_raises(self) -> None:
        with pytest.raises(KeyError, match="queue"):
            require_section({}, "queue")

    def test_wrong_section_type_raises(self) -> None:
        config: dict[str, object] = {"queue": "queue.db"}

        with pytest.raises(TypeError) as exc_info:
            require_section(config, "queue")

        assert str(exc_info.value) == "queue expected dict; got str value='queue.db'"


class TestOptionalConfig:
    def test_missing_optional_returns_default(self) -> None:
        config: dict[str, object] = {}

        assert optional_config(config, "timeout", int, 30) == 30

    def test_missing_optional_does_not_mutate_config(self) -> None:
        config: dict[str, object] = {}

        optional_config(config, "timeout", int, 30)

        assert config == {}

    def test_missing_optional_default_is_validated(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            optional_config({}, "timeout", int, 0, min_value=1)

        assert str(exc_info.value) == "timeout expected int >= 1; got int value=0"

    def test_present_optional_is_validated(self) -> None:
        config: dict[str, object] = {"timeout": "30"}

        with pytest.raises(TypeError) as exc_info:
            optional_config(config, "timeout", int, 30)

        assert str(exc_info.value) == "timeout expected int; got str value='30'"

    def test_present_optional_returns_value(self) -> None:
        config: dict[str, object] = {"timeout": 10}

        assert optional_config(config, "timeout", int, 30, min_value=1, max_value=60) == 10


class TestValidateConfig:
    def test_batch_validates_required_optional_and_dotted_values(self) -> None:
        config: dict[str, object] = {
            "log_level": "INFO",
            "queue": {"lease_timeout": 30, "enabled": True},
        }

        values = validate_config(
            config,
            (
                ConfigField("log_level", str, choices=("INFO", "ERROR")),
                ConfigField("queue.lease_timeout", int, min_value=1, max_value=60),
                ConfigField("queue.enabled", bool),
                ConfigField("max_retries", int, required=False, default=3, min_value=0),
            ),
        )

        assert values == {
            "log_level": "INFO",
            "queue.lease_timeout": 30,
            "queue.enabled": True,
            "max_retries": 3,
        }
        assert "max_retries" not in config

    def test_dotted_error_uses_complete_key(self) -> None:
        config: dict[str, object] = {"queue": {"lease_timeout": True}}

        with pytest.raises(TypeError) as exc_info:
            validate_config(config, (ConfigField("queue.lease_timeout", int),))

        assert str(exc_info.value) == (
            "queue.lease_timeout expected int; got bool value=True"
        )

    def test_non_mapping_parent_has_actionable_error(self) -> None:
        with pytest.raises(TypeError) as exc_info:
            validate_config(
                {"queue": "queue.db"},
                (ConfigField("queue.lease_timeout", int),),
            )

        assert str(exc_info.value) == "queue expected dict; got str value='queue.db'"

    def test_missing_dotted_value_uses_complete_key(self) -> None:
        with pytest.raises(KeyError) as exc_info:
            validate_config({"queue": {}}, (ConfigField("queue.db_path", str),))

        assert str(exc_info.value) == "'Missing required config key: queue.db_path'"

    def test_optional_default_is_validated(self) -> None:
        with pytest.raises(TypeError, match="max_retries expected int"):
            validate_config(
                {},
                (ConfigField("max_retries", int, required=False, default="3"),),
            )

    def test_duplicate_fields_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="Duplicate config field: timeout"):
            validate_config(
                {"timeout": 1},
                (ConfigField("timeout", int), ConfigField("timeout", int)),
            )

    @pytest.mark.parametrize("key", ["", ".queue", "queue.", "queue..timeout"])
    def test_invalid_field_keys_are_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match="non-empty dotted key"):
            ConfigField(key, int)

    def test_required_field_cannot_define_default(self) -> None:
        with pytest.raises(ValueError, match="cannot define a default"):
            ConfigField("timeout", int, default=30)

    def test_batch_rejects_non_finite_bounded_value(self) -> None:
        with pytest.raises(ValueError, match="ratio expected finite float"):
            validate_config(
                {"ratio": float("nan")},
                (ConfigField("ratio", float, min_value=0.0),),
            )


class TestPackageExports:
    def test_helpers_are_reexported(self) -> None:
        assert rpacore.require_config is require_config
        assert rpacore.require_section is require_section
        assert rpacore.optional_config is optional_config
        assert rpacore.ConfigField is ConfigField
        assert rpacore.validate_config is validate_config
