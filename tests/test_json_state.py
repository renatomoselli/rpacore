"""Tests for durable JSON state validation."""

import math

import pytest

from rpacore._json_state import JsonStateError, validate_json_object


def test_accepts_nested_json_object() -> None:
    validate_json_object(
        {"invoice": {"id": 42, "tags": ["new"], "paid": False, "note": None}},
        path="transaction.state",
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(JsonStateError) as exc_info:
        validate_json_object({"amount": value}, path="transaction.state")

    assert "transaction.state['amount'] expected finite JSON number" in str(exc_info.value)


def test_rejects_non_string_dict_keys() -> None:
    with pytest.raises(JsonStateError) as exc_info:
        validate_json_object({1: "invoice"}, path="transaction.state")

    assert "transaction.state[1] expected string key" in str(exc_info.value)


def test_rejects_circular_references() -> None:
    state: dict[str, object] = {}
    state["self"] = state

    with pytest.raises(JsonStateError) as exc_info:
        validate_json_object(state, path="transaction.state")

    assert "transaction.state['self'] expected acyclic JSON value" in str(exc_info.value)
