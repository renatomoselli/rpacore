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

    def test_state_defaults_to_transaction_state(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1"))
        assert ctx.state == {}

    def test_resources_defaults_to_empty_dict(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1"))
        assert ctx.resources == {}

    def test_config_not_shared_between_instances(self) -> None:
        a = ProcessContext(transaction=Transaction(reference="A"))
        b = ProcessContext(transaction=Transaction(reference="B"))
        a.config["key"] = "value"
        assert "key" not in b.config

    def test_state_not_shared_between_instances(self) -> None:
        a = ProcessContext(transaction=Transaction(reference="A"))
        b = ProcessContext(transaction=Transaction(reference="B"))
        a.state["key"] = "value"
        assert "key" not in b.state

    def test_resources_not_shared_between_instances(self) -> None:
        a = ProcessContext(transaction=Transaction(reference="A"))
        b = ProcessContext(transaction=Transaction(reference="B"))
        a.resources["key"] = "value"
        assert "key" not in b.resources

class TestProcessContextStateSharing:
    def test_state_mutation_updates_transaction(self) -> None:
        tx = Transaction(reference="T1")
        ctx = ProcessContext(transaction=tx)

        # Simulate two steps sharing ctx
        ctx.state["result"] = "step1_done"
        assert tx.state["result"] == "step1_done"

        ctx.state["result"] = "step2_done"
        assert tx.state["result"] == "step2_done"

    def test_config_accessible_alongside_state_and_resources(self) -> None:
        tx = Transaction(reference="T1")
        ctx = ProcessContext(
            transaction=tx,
            config={"max_retries": 3},
            resources={"client": object()},
        )
        ctx.state["input"] = "hello"
        assert ctx.config["max_retries"] == 3
        assert ctx.state["input"] == "hello"
        assert "client" in ctx.resources
        assert ctx.transaction is tx

    def test_add_artifact_appends_to_transaction_and_returns_artifact(self) -> None:
        tx = Transaction(reference="T1")
        ctx = ProcessContext(transaction=tx)

        artifact = ctx.add_artifact(
            "invoice",
            "missing/invoice.pdf",
            kind="pdf",
            metadata={"invoice_id": 42},
        )

        assert tx.artifacts == [artifact]
        assert artifact.name == "invoice"
        assert artifact.path == "missing/invoice.pdf"
        assert artifact.kind == "pdf"
        assert artifact.metadata == {"invoice_id": 42}

    def test_add_artifact_empty_metadata_by_default(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1"))

        artifact = ctx.add_artifact("invoice", "missing/invoice.pdf")

        assert artifact.metadata == {}


class TestProcessContextStateGuards:
    def test_require_state_returns_value_unchanged(self) -> None:
        value = {"invoice_id": 123}
        ctx = ProcessContext(transaction=Transaction(reference="T1", state={"invoice": value}))

        assert ctx.require_state("invoice") is value

    def test_require_state_validates_expected_type(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1", state={"invoice_id": "123"}))

        assert ctx.require_state("invoice_id", str) == "123"

    def test_require_state_missing_key_raises_system_exception(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1"))

        with pytest.raises(SystemException) as exc_info:
            ctx.require_state("invoice_id", int, action="submit_invoice")

        assert str(exc_info.value) == "Missing required state key: invoice_id"
        assert exc_info.value.action == "submit_invoice"

    def test_require_state_wrong_type_raises_actionable_system_exception(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1", state={"invoice_id": "123"}))

        with pytest.raises(SystemException) as exc_info:
            ctx.require_state("invoice_id", int, action="submit_invoice")

        assert str(exc_info.value) == "invoice_id expected int; got str value='123'"
        assert exc_info.value.action == "submit_invoice"
        assert isinstance(exc_info.value.__cause__, TypeError)

    def test_require_state_rejects_bool_for_int(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1", state={"retry_count": True}))

        with pytest.raises(SystemException) as exc_info:
            ctx.require_state("retry_count", int)

        assert str(exc_info.value) == "retry_count expected int; got bool value=True"

    def test_require_state_wraps_key_removed_during_validation(self) -> None:
        class DisappearingDict(dict[str, object]):
            def __contains__(self, key: object) -> bool:
                if super().__contains__(key):
                    super().pop(key)
                    return True
                return False

        tx = Transaction(reference="T1")
        tx.state = DisappearingDict(invoice_id=123)
        ctx = ProcessContext(transaction=tx)

        with pytest.raises(SystemException) as exc_info:
            ctx.require_state("invoice_id", int, action="submit_invoice")

        assert str(exc_info.value) == "Missing required state key: invoice_id"
        assert exc_info.value.action == "submit_invoice"
        assert isinstance(exc_info.value.__cause__, KeyError)

    def test_optional_state_returns_default_without_mutating_state(self) -> None:
        default = {"source": "default"}
        ctx = ProcessContext(transaction=Transaction(reference="T1"))

        assert ctx.optional_state("options", dict, default) is default
        assert ctx.state == {}

    def test_optional_state_returns_present_value(self) -> None:
        value = {"source": "context"}
        ctx = ProcessContext(transaction=Transaction(reference="T1", state={"options": value}))

        assert ctx.optional_state("options", dict, {}) is value

    def test_optional_state_validates_present_value(self) -> None:
        ctx = ProcessContext(transaction=Transaction(reference="T1", state={"timeout": "30"}))

        with pytest.raises(SystemException) as exc_info:
            ctx.optional_state("timeout", int, 30, action="wait")

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
