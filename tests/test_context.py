"""Tests for oref.context."""

from __future__ import annotations

from oref.context import ProcessContext
from oref.transaction import Transaction


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
