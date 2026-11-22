"""Tests for public config validation helpers."""

from __future__ import annotations

from collections.abc import Callable
import copy
import dataclasses
from enum import Enum
import math
import threading

import pytest

import rpacore
from rpacore.config_validation import (
    ConfigField,
    optional_config,
    require_config,
    require_section,
    validate_config,
)


nan = float("nan")
inf = float("inf")


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

    def test_missing_optional_without_default_omits_result_key(self) -> None:
        values = validate_config(
            {},
            (ConfigField("label", str, required=False),),
        )

        assert values == {}

    def test_explicit_none_default_is_returned_and_validated(self) -> None:
        values = validate_config(
            {},
            (ConfigField("label", (str, type(None)), required=False, default=None),),
        )

        assert values == {"label": None}

    def test_mutable_default_is_independent_from_source_and_each_result(self) -> None:
        source_default = {"labels": ["new"]}
        field = ConfigField("metadata", dict, required=False, default=source_default)
        source_default["labels"].append("caller-mutation")
        field.default["labels"].append("declaration-mutation")  # type: ignore[index]

        first = validate_config({}, (field,))
        first["metadata"]["labels"].append("result-mutation")
        second = validate_config({}, (field,))

        assert second == {"metadata": {"labels": ["new"]}}

    def test_one_shot_choices_are_frozen_for_reuse(self) -> None:
        choices = (value for value in ("quiet", "normal"))
        field = ConfigField("mode", str, choices=choices)

        assert field.choices == ("quiet", "normal")
        assert validate_config({"mode": "quiet"}, (field,)) == {"mode": "quiet"}
        assert validate_config({"mode": "normal"}, (field,)) == {"mode": "normal"}

    @pytest.mark.parametrize("choices", ["quiet", 1, ()])
    def test_invalid_choices_declaration_is_rejected(self, choices: object) -> None:
        with pytest.raises((TypeError, ValueError), match="choices must"):
            ConfigField("mode", str, choices=choices)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "expected_type",
        [(), (int, "str"), [int]],
    )
    def test_invalid_expected_type_declaration_is_rejected(self, expected_type: object) -> None:
        with pytest.raises(TypeError, match="expected_type must be a type or non-empty tuple"):
            ConfigField("timeout", expected_type)  # type: ignore[arg-type]

    @pytest.mark.parametrize("name, value", [("required", "yes"), ("allow_empty", 1)])
    def test_non_boolean_declaration_flags_are_rejected(self, name: str, value: object) -> None:
        with pytest.raises(TypeError, match=f"{name} must be bool"):
            ConfigField("timeout", int, **{name: value})  # type: ignore[arg-type]

    @pytest.mark.parametrize("key", [1, "   ", "queue. "])
    def test_invalid_field_key_type_or_whitespace_is_rejected(self, key: object) -> None:
        with pytest.raises((TypeError, ValueError), match="non-empty dotted key"):
            ConfigField(key, int)  # type: ignore[arg-type]

    def test_invalid_declaration_bounds_and_defaults_fail_before_validation(self) -> None:
        with pytest.raises(TypeError, match="min_value expected int"):
            ConfigField("timeout", int, min_value="1")
        with pytest.raises(ValueError, match="min_value must be <= max_value"):
            ConfigField("timeout", int, min_value=10, max_value=1)
        with pytest.raises(TypeError, match="timeout expected int"):
            ConfigField("timeout", int, required=False, default="30")

    def test_mutable_default_must_be_json_safe(self) -> None:
        with pytest.raises(TypeError, match="default must be JSON-safe"):
            ConfigField("values", set, required=False, default={"one"})

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


class TestNonFiniteBounds:
    @pytest.mark.parametrize(
        ("expected_type", "bounds", "message"),
        [
            pytest.param(
                float,
                {"min_value": nan, "max_value": 1.0},
                "v min_value expected finite float; got float value=nan",
                id="min_nan_with_max",
            ),
            pytest.param(
                float,
                {"max_value": nan},
                "v max_value expected finite float; got float value=nan",
                id="max_nan",
            ),
            pytest.param(
                (int, float),
                {"max_value": inf},
                "v max_value expected finite int | float; got float value=inf",
                id="union_max_inf",
            ),
            pytest.param(
                float,
                {"min_value": -inf},
                "v min_value expected finite float; got float value=-inf",
                id="min_neg_inf",
            ),
            pytest.param(
                float,
                {"min_value": -inf, "max_value": inf},
                "v min_value expected finite float; got float value=-inf",
                id="removed_infinite_pair",
            ),
            pytest.param(
                float,
                {"min_value": inf, "max_value": 1.0},
                "v min_value expected finite float; got float value=inf",
                id="min_inf_before_range",
            ),
            pytest.param(
                float,
                {"required": False, "default": 0.5, "min_value": nan},
                "v min_value expected finite float; got float value=nan",
                id="default_does_not_mask",
            ),
        ],
    )
    def test_declared_non_finite_bound_is_rejected(
        self, expected_type: object, bounds: dict[str, object], message: str
    ) -> None:
        with pytest.raises(ValueError) as exc_info:
            ConfigField("v", expected_type, **bounds)  # type: ignore[arg-type]
        assert str(exc_info.value) == message

    @pytest.mark.parametrize(
        ("expected_type", "bounds", "exception", "message"),
        [
            pytest.param(
                int,
                {"min_value": nan},
                TypeError,
                "v min_value expected int; got float value=nan",
                id="int_field_nan_type_error",
            ),
            pytest.param(
                float,
                {"min_value": True},
                TypeError,
                "v min_value expected float; got bool value=True",
                id="bool_bound",
            ),
            pytest.param(
                int,
                {"min_value": 10, "max_value": 1},
                ValueError,
                "v min_value must be <= max_value",
                id="inverted_range",
            ),
        ],
    )
    def test_declared_bound_rules_unchanged(
        self,
        expected_type: object,
        bounds: dict[str, object],
        exception: type[Exception],
        message: str,
    ) -> None:
        with pytest.raises(exception) as exc_info:
            ConfigField("v", expected_type, **bounds)  # type: ignore[arg-type]
        assert str(exc_info.value) == message

    def test_finite_declared_bounds_still_validate(self) -> None:
        field = ConfigField("v", float, min_value=0.0, max_value=1.0)

        assert validate_config({"v": 0.5}, (field,)) == {"v": 0.5}
        with pytest.raises(ValueError) as exc_info:
            validate_config({"v": 1.5}, (field,))
        assert str(exc_info.value) == "v expected float <= 1.0; got float value=1.5"

    def test_large_finite_declared_bounds_are_accepted(self) -> None:
        field = ConfigField("v", float, min_value=-1e308, max_value=1e308)

        assert validate_config({"v": 0.0}, (field,)) == {"v": 0.0}

    @pytest.mark.parametrize(
        ("call", "message"),
        [
            pytest.param(
                lambda: require_config({"v": -1e20}, "v", float, min_value=nan, max_value=1.0),
                "v min_value expected finite float; got float value=nan",
                id="require_min_nan",
            ),
            pytest.param(
                lambda: require_config({"v": 5}, "v", int, min_value=nan),
                "v min_value expected finite int; got float value=nan",
                id="require_int_min_nan",
            ),
            pytest.param(
                lambda: require_config({"v": 5.0}, "v", float, min_value=-inf),
                "v min_value expected finite float; got float value=-inf",
                id="require_min_neg_inf",
            ),
            pytest.param(
                lambda: optional_config({"v": 5.0}, "v", float, 0.0, max_value=-inf),
                "v max_value expected finite float; got float value=-inf",
                id="optional_max_neg_inf",
            ),
            pytest.param(
                lambda: optional_config({}, "v", float, -1e20, min_value=nan),
                "v min_value expected finite float; got float value=nan",
                id="optional_default_path",
            ),
            pytest.param(
                lambda: optional_config({"v": 9e9}, "v", float, 0.0, max_value=nan),
                "v max_value expected finite float; got float value=nan",
                id="optional_present_path",
            ),
            pytest.param(
                lambda: require_config({"v": 0.5}, "v", float, min_value=0.0, max_value=inf),
                "v max_value expected finite float; got float value=inf",
                id="value_inside_range",
            ),
            pytest.param(
                lambda: require_config({"v": "x"}, "v", float, min_value=nan),
                "v min_value expected finite float; got float value=nan",
                id="bound_before_value_type",
            ),
        ],
    )
    def test_ad_hoc_non_finite_bound_is_rejected(
        self, call: Callable[[], object], message: str
    ) -> None:
        with pytest.raises(ValueError) as exc_info:
            call()
        assert str(exc_info.value) == message

    @pytest.mark.parametrize(
        ("call", "expected_error", "is_expected_result"),
        [
            pytest.param(
                lambda: require_config({"v": 5}, "v", int, min_value=True),
                None,
                lambda result: result == 5,
                id="bool_bound_accepted",
            ),
            pytest.param(
                lambda: require_config({"v": 5}, "v", int, min_value=2, max_value=1),
                "v expected int <= 1; got int value=5",
                None,
                id="inverted_range_reports_value",
            ),
            pytest.param(
                lambda: require_config({"v": nan}, "v", float),
                None,
                lambda result: isinstance(result, float) and math.isnan(result),
                id="nan_value_without_bounds",
            ),
        ],
    )
    def test_ad_hoc_bound_behavior_unchanged(
        self,
        call: Callable[[], object],
        expected_error: str | None,
        is_expected_result: Callable[[object], bool] | None,
    ) -> None:
        if expected_error is not None:
            with pytest.raises(ValueError) as exc_info:
                call()
            assert str(exc_info.value) == expected_error
            return
        assert is_expected_result is not None
        assert is_expected_result(call())


class _ChoiceEnum(Enum):
    VALUE = "value"


class _CopyErrorChoice:
    def __deepcopy__(self, memo: object) -> object:
        raise copy.Error("cannot copy choice")


def _field_after_list_choice_mutation() -> ConfigField:
    source = [1]
    field = ConfigField("v", list, choices=[source])
    source.append(2)
    return field


def _field_after_dict_choice_mutation() -> ConfigField:
    source = {"a": 1}
    field = ConfigField("v", dict, choices=[source])
    source["a"] = 2
    return field


def _field_after_nested_choice_mutation() -> ConfigField:
    inner = ["x"]
    field = ConfigField("v", dict, choices=[{"tags": inner}])
    inner.append("y")
    return field


class TestChoiceOwnership:
    @pytest.mark.parametrize(
        ("build_field", "config", "expected", "error"),
        [
            pytest.param(
                _field_after_list_choice_mutation,
                {"v": [1]},
                {"v": [1]},
                None,
                id="list_original_accepted",
            ),
            pytest.param(
                _field_after_list_choice_mutation,
                {"v": [1, 2]},
                None,
                "v expected one of [1]; got list value=[1, 2]",
                id="list_mutation_rejected",
            ),
            pytest.param(
                _field_after_dict_choice_mutation,
                {"v": {"a": 1}},
                {"v": {"a": 1}},
                None,
                id="dict_original_accepted",
            ),
            pytest.param(
                _field_after_dict_choice_mutation,
                {"v": {"a": 2}},
                None,
                "v expected one of {'a': 1}; got dict value={'a': 2}",
                id="dict_mutation_rejected",
            ),
            pytest.param(
                _field_after_nested_choice_mutation,
                {"v": {"tags": ["x"]}},
                {"v": {"tags": ["x"]}},
                None,
                id="nested_member",
            ),
        ],
    )
    def test_caller_mutation_does_not_change_choices(
        self,
        build_field: Callable[[], ConfigField],
        config: dict[str, object],
        expected: dict[str, object] | None,
        error: str | None,
    ) -> None:
        field = build_field()
        if error is None:
            assert validate_config(config, (field,)) == expected
        else:
            with pytest.raises(ValueError) as exc_info:
                validate_config(config, (field,))
            assert str(exc_info.value) == error

    def test_public_choices_mutation_does_not_change_domain(self) -> None:
        field = ConfigField("v", list, choices=[[1]])
        field.choices[0].append(9)  # type: ignore[index]

        assert validate_config({"v": [1]}, (field,)) == {"v": [1]}
        with pytest.raises(ValueError) as exc_info:
            validate_config({"v": [1, 9]}, (field,))
        assert str(exc_info.value) == "v expected one of [1]; got list value=[1, 9]"

    def test_default_matching_mutated_choice_is_returned(self) -> None:
        inner = [1]
        field = ConfigField("v", list, required=False, default=[1], choices=[inner])
        inner.append(5)

        assert validate_config({}, (field,)) == {"v": [1]}

    def test_uncopyable_choice_is_rejected(self) -> None:
        with pytest.raises(TypeError) as exc_info:
            ConfigField("v", object, choices=[threading.Lock()])

        assert str(exc_info.value) == "v choices must be copyable"
        assert isinstance(exc_info.value.__cause__, TypeError)

    def test_uncopyable_choice_type_error_runs_first(self) -> None:
        with pytest.raises(TypeError) as exc_info:
            ConfigField("v", int, choices=[threading.Lock()])

        assert str(exc_info.value).startswith("v expected int; got lock value=")

    def test_copy_error_choice_is_rejected(self) -> None:
        with pytest.raises(TypeError) as exc_info:
            ConfigField("v", object, choices=[_CopyErrorChoice()])

        assert str(exc_info.value) == "v choices must be copyable"
        assert isinstance(exc_info.value.__cause__, copy.Error)

    @pytest.mark.parametrize(
        ("call", "expected"),
        [
            pytest.param(
                lambda: validate_config({"v": "x"}, (ConfigField("v", str),)),
                {"v": "x"},
                id="no_choices",
            ),
            pytest.param(
                lambda: validate_config(
                    {}, (ConfigField("v", str, required=False, default="d"),)
                ),
                {"v": "d"},
                id="no_choices_default",
            ),
            pytest.param(
                lambda: ConfigField("v", str, choices=["a"]).choices,
                ("a",),
                id="public_tuple",
            ),
            pytest.param(
                lambda: validate_config(
                    {"v": _ChoiceEnum.VALUE},
                    (ConfigField("v", _ChoiceEnum, choices=[_ChoiceEnum.VALUE]),),
                ),
                {"v": _ChoiceEnum.VALUE},
                id="enum_member",
            ),
            pytest.param(
                lambda: validate_config({"v": {1}}, (ConfigField("v", set, choices=[{1}]),)),
                {"v": {1}},
                id="set_choice",
            ),
        ],
    )
    def test_existing_choice_behavior_unchanged(
        self, call: Callable[[], object], expected: object
    ) -> None:
        assert call() == expected

    def test_equal_declarations_compare_and_hash_equal(self) -> None:
        first = ConfigField("v", str, choices=["a"])
        second = ConfigField("v", str, choices=["a"])

        assert first == second
        assert hash(first) == hash(second)

    def test_repr_omits_choices_snapshot(self) -> None:
        assert "_choices_snapshot" not in repr(ConfigField("v", str, choices=["a"]))

    def test_replace_keeps_accepted_values(self) -> None:
        field = dataclasses.replace(ConfigField("v", str, choices=["a", "b"]), key="w")

        assert validate_config({"w": "b"}, (field,)) == {"w": "b"}
        with pytest.raises(ValueError) as exc_info:
            validate_config({"w": "c"}, (field,))
        assert str(exc_info.value) == "w expected one of 'a', 'b'; got str value='c'"

    def test_ad_hoc_choices_unchanged(self) -> None:
        assert require_config({"v": "b"}, "v", str, choices=["a", "b"]) == "b"

    def test_identity_choice_no_longer_matches(self) -> None:
        sentinel = object()
        field = ConfigField("v", object, choices=[sentinel])

        with pytest.raises(ValueError) as exc_info:
            validate_config({"v": sentinel}, (field,))
        assert str(exc_info.value).startswith("v expected one of ")
