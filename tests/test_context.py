"""Tests for rpacore.context."""

from __future__ import annotations

import pytest

from rpacore.context import ProcessContext
from rpacore.exceptions import SystemException
from rpacore.transaction import Transaction


class TestProcessContextDefaults:
    def test_transaction_stored(self) -> None:
        tx = Transaction(reference="T1")
        ctx = ProcessContext(transaction=tx)
        assert ctx.transaction is tx

    def test_config_defaults_to_empty_dict(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1"))
        assert ctx.config == {}

    def test_data_defaults_to_empty_dict(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1"))
        assert ctx.data == {}

    def test_config_not_shared_between_instances(self) -> None:
        a = ProcessContext(transaction=Transaction(reference="A"))
        b = ProcessContext(transaction=Transaction(reference="B"))
        a.config["key"] = "value"
        assert "key" not in b.config

    def test_data_not_shared_between_instances(self) -> None:
        a = ProcessContext(transaction=Transaction(reference="A"))
        b = ProcessContext(transaction=Transaction(reference="B"))
        a.data["key"] = "value"
        assert "key" not in b.data


class TestProcessContextDataSharing:
    def test_data_mutation_is_visible_across_references(self) -> None:
        tx = Transaction(reference="T1")
        ctx = ProcessContext(transaction=tx)

        # Simulate two skills sharing ctx
        ctx.data["result"] = "step1_done"
        assert ctx.data["result"] == "step1_done"

        ctx.data["result"] = "step2_done"
        assert ctx.data["result"] == "step2_done"

    def test_config_accessible_alongside_data(self) -> None:
        tx = Transaction(reference="T1")
        ctx = ProcessContext(
            transaction=tx,
            config={"max_retries": 3},
            data={"input": "hello"},
        )
        assert ctx.config["max_retries"] == 3
        assert ctx.data["input"] == "hello"
        assert ctx.transaction is tx


class TestProcessContextDataGuards:
    def test_require_data_returns_value_unchanged(self) -> None:
        value = {"invoice_id": 123}
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            data={"invoice": value},
        )

        assert ctx.require_data("invoice") is value

    def test_require_data_validates_expected_type(self) -> None:
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            data={"invoice_id": "123"},
        )

        assert ctx.require_data("invoice_id", str) == "123"

    def test_require_data_missing_key_raises_system_exception(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1"))

        with pytest.raises(SystemException) as exc_info:
            ctx.require_data("invoice_id", int, action="submit_invoice")

        assert str(exc_info.value) == "Missing required data key: invoice_id"
        assert exc_info.value.action == "submit_invoice"

    def test_require_data_wrong_type_raises_actionable_system_exception(self) -> None:
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            data={"invoice_id": "123"},
        )

        with pytest.raises(SystemException) as exc_info:
            ctx.require_data("invoice_id", int, action="submit_invoice")

        assert str(exc_info.value) == "invoice_id expected int; got str value='123'"
        assert exc_info.value.action == "submit_invoice"
        assert isinstance(exc_info.value.__cause__, TypeError)

    def test_require_data_rejects_bool_for_int(self) -> None:
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            data={"retry_count": True},
        )

        with pytest.raises(SystemException) as exc_info:
            ctx.require_data("retry_count", int)

        assert str(exc_info.value) == "retry_count expected int; got bool value=True"

    def test_require_data_wraps_key_removed_during_validation(self) -> None:
        class DisappearingDict(dict[str, object]):
            def __contains__(self, key: object) -> bool:
                if super().__contains__(key):
                    super().pop(key)
                    return True
                return False

        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            data=DisappearingDict(invoice_id=123),
        )

        with pytest.raises(SystemException) as exc_info:
            ctx.require_data("invoice_id", int, action="submit_invoice")

        assert str(exc_info.value) == "Missing required data key: invoice_id"
        assert exc_info.value.action == "submit_invoice"
        assert isinstance(exc_info.value.__cause__, KeyError)

    def test_optional_data_returns_default_without_mutating_data(self) -> None:
        default = {"source": "default"}
        ctx = ProcessContext(transaction=Transaction(reference="T1"))

        assert ctx.optional_data("options", dict, default) is default
        assert ctx.data == {}

    def test_optional_data_returns_present_value(self) -> None:
        value = {"source": "context"}
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            data={"options": value},
        )

        assert ctx.optional_data("options", dict, {}) is value

    def test_optional_data_validates_present_value(self) -> None:
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            data={"timeout": "30"},
        )

        with pytest.raises(SystemException) as exc_info:
            ctx.optional_data("timeout", int, 30, action="wait")

        assert str(exc_info.value) == "timeout expected int; got str value='30'"
        assert exc_info.value.action == "wait"


class TestProcessContextConfigGuards:
    def test_require_config_returns_value_unchanged(self) -> None:
        value = {"db_path": "queue.db"}
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            config={"queue": value},
        )

        assert ctx.require_config("queue") is value

    def test_require_config_validates_expected_type(self) -> None:
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            config={"max_retries": 3},
        )

        assert ctx.require_config("max_retries", int) == 3

    def test_require_config_missing_key_raises_system_exception(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1"))

        with pytest.raises(SystemException) as exc_info:
            ctx.require_config("db_path", str, action="persist")

        assert str(exc_info.value) == "Missing required config key: db_path"
        assert exc_info.value.action == "persist"

    def test_require_config_wrong_type_raises_actionable_system_exception(self) -> None:
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            config={"max_retries": "3"},
        )

        with pytest.raises(SystemException) as exc_info:
            ctx.require_config("max_retries", int, action="configure_engine")

        assert str(exc_info.value) == "max_retries expected int; got str value='3'"
        assert exc_info.value.action == "configure_engine"

    def test_require_config_rejects_bool_for_int(self) -> None:
        ctx = ProcessContext(
            transaction=Transaction(reference="T1"),
            config={"max_retries": True},
        )

        with pytest.raises(SystemException) as exc_info:
            ctx.require_config("max_retries", int, action="configure_engine")

        assert str(exc_info.value) == "max_retries expected int; got bool value=True"
        assert exc_info.value.action == "configure_engine"
