"""Tests for rpacore.logger."""

import io
import json

import pytest

from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException
from rpacore.logger import configure_logger
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction


class SuccessSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state[self.name] = "done"


class BusinessFailSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException("rule violated", action=self.name)


class TestConfigureLogger:
    def test_text_format_includes_event_and_fields(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.text", fmt="text", stream=stream)
        logger.info(
            "Transaction started",
            extra={"event": "transaction_started", "transaction_id": "tx-1"},
        )

        output = stream.getvalue().strip()
        assert "INFO" in output
        assert "transaction_started" in output
        assert "Transaction started" in output
        assert "transaction_id=tx-1" in output

    def test_json_format_is_valid_json(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.json", fmt="json", stream=stream)
        logger.info(
            "Skill started",
            extra={"event": "skill_started", "skill_name": "login"},
        )

        payload = json.loads(stream.getvalue())
        assert payload["level"] == "info"
        assert payload["message"] == "Skill started"
        assert payload["event"] == "skill_started"
        assert payload["skill_name"] == "login"

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="fmt must be 'text' or 'json'"):
            configure_logger(name="rpacore.test.invalid", fmt="xml")

    def test_reconfigure_does_not_duplicate_handlers(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.reset", fmt="text", stream=stream)
        logger = configure_logger(name="rpacore.test.reset", fmt="text", stream=stream)
        logger.info("Only once", extra={"event": "transaction_completed"})

        assert stream.getvalue().strip().count("Only once") == 1


class TestEngineLogging:
    def test_success_flow_emits_standardized_events(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.engine.success", fmt="json", stream=stream)
        tx = Transaction(
            reference="T1",
            skills=[SuccessSkill("a", 1), SuccessSkill("b", 2)],
        )

        Engine(logger=logger).run(ProcessContext(transaction=tx))

        events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
        assert events == [
            "transaction_started",
            "skill_started",
            "skill_completed",
            "skill_started",
            "skill_completed",
            "transaction_completed",
        ]

    def test_failed_skill_event_contains_failure_details(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.engine.fail", fmt="json", stream=stream)
        tx = Transaction(reference="T1", skills=[BusinessFailSkill("validate", 1)])

        Engine(logger=logger).run(ProcessContext(transaction=tx))

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        failed = next(record for record in records if record["event"] == "skill_failed")

        assert failed["skill_name"] == "validate"
        assert failed["skill_status"] == Status.FAILED
        assert failed["exception_type"] == "BusinessException"
        assert failed["retry_number"] == 0

    def test_transaction_completed_event_reports_final_status(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.engine.complete", fmt="json", stream=stream)
        tx = Transaction(reference="T1", skills=[SuccessSkill("a", 1)])

        Engine(logger=logger).run(ProcessContext(transaction=tx))

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        completed = records[-1]
        assert completed["event"] == "transaction_completed"
        assert completed["transaction_status"] == Status.SUCCESSFUL
