"""Tests for rpacore.logger."""

import io
import json
import logging
import sys
from contextlib import contextmanager
from datetime import datetime

import pytest

from rpacore import bind_log_context as public_bind_log_context
from rpacore.context import ProcessContext
from rpacore.credentials import EnvCredentialProvider
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException
from rpacore.logger import (
    LOG_FORMAT_VERSION,
    JsonFormatter,
    TextFormatter,
    bind_log_context,
    configure_logger,
    get_logger,
)
from rpacore.runner import run_queue_loop
from rpacore.step import Step
from rpacore.status import Status
from rpacore.transaction import Transaction


class SuccessStep(Step):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state[self.name] = "done"


class BusinessFailStep(Step):
    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException("rule violated", action=self.name)


class EmptyQueue:
    def next_item(self, worker_id: str = "") -> None:
        return None

    def complete(self, item_id: str, *, claimed_by: str, claim_token: str) -> None:
        raise AssertionError("empty queue has no item to complete")

    def fail(
        self,
        item_id: str,
        *,
        retry: bool = True,
        claimed_by: str,
        claim_token: str,
    ) -> None:
        raise AssertionError("empty queue has no item to fail")

    def bind_transaction(
        self,
        item_id: str,
        transaction_id: str,
        *,
        claimed_by: str,
        claim_token: str,
    ) -> None:
        raise AssertionError("empty queue has no item to bind")

    def renew_lease(self, item_id: str, *, claimed_by: str, claim_token: str) -> None:
        raise AssertionError("empty queue has no lease to renew")


class OneItemQueue:
    def __init__(self) -> None:
        self._item = "item-001"

    def next_item(self, worker_id: str = ""):
        if self._item is None:
            return None
        item_id = self._item
        self._item = None
        return type(
            "QueueItemStub",
            (),
            {
                "id": item_id,
                "reference": "ref-001",
                "payload": {},
                "claimed_by": worker_id,
                "claim_token": "test-token",
                "transaction_id": "",
            },
        )()

    def complete(self, item_id: str, *, claimed_by: str, claim_token: str) -> None:
        raise AssertionError("test should not complete")

    def fail(
        self,
        item_id: str,
        *,
        retry: bool = True,
        claimed_by: str,
        claim_token: str,
    ) -> None:
        raise AssertionError("suppressed exception should skip failure transition")

    def bind_transaction(
        self,
        item_id: str,
        transaction_id: str,
        *,
        claimed_by: str,
        claim_token: str,
    ) -> None:
        pass

    def renew_lease(self, item_id: str, *, claimed_by: str, claim_token: str) -> None:
        pass


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
            "Step started",
            extra={"event": "step_started", "step_name": "login"},
        )

        payload = json.loads(stream.getvalue())
        assert payload["log_format_version"] == LOG_FORMAT_VERSION
        assert datetime.fromisoformat(payload["timestamp"]).tzinfo is not None
        assert payload["severity"] == "info"
        assert payload["message"] == "Step started"
        assert payload["event"] == "rpacore.step.started"
        assert payload["attributes"]["step_name"] == "login"

    def test_json_format_defaults_event_and_redacts_runtime_objects(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.json.redact", fmt="json", stream=stream)
        logger.info(
            "Runtime details",
            extra={
                "config": {"token": "secret"},
                "credentials": object(),
                "resources": {"browser": object()},
                "runtime_object": object(),
                "labels": {"beta", "alpha"},
                "mixed": {"a", 1},
            },
        )

        payload = json.loads(stream.getvalue())
        assert payload["event"] == "rpacore.log"
        attributes = payload["attributes"]
        assert "config" not in attributes
        assert "credentials" not in attributes
        assert "resources" not in attributes
        assert attributes["runtime_object"] == "object"
        assert attributes["labels"] == ["alpha", "beta"]
        assert attributes["mixed"] == ["a", 1]

    def test_json_v3_uses_protected_envelope_and_correlation_context(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(
            name="rpacore.test.json.v3",
            fmt="json",
            stream=stream,
        )

        with bind_log_context(transaction_id="tx-1", retry_number=2):
            logger.info(
                "Step started",
                extra={
                    "event": "step_started",
                    "transaction_id": "forged",
                    "step_name": "login",
                    "config": {"token": "secret"},
                    "details": {"metadata": {"secret": "also-hidden"}},
                    "severity": "critical",
                    "logger": "forged",
                    "attributes": {"forged": True},
                },
            )

        payload = json.loads(stream.getvalue())
        assert payload["log_format_version"] == LOG_FORMAT_VERSION
        assert payload["event"] == "rpacore.step.started"
        assert payload["severity"] == "info"
        assert payload["logger"] == "rpacore.test.json.v3"
        assert payload["attributes"] == {
            "details": {},
            "retry_number": 2,
            "step_name": "login",
            "transaction_id": "tx-1",
        }
        assert "config" not in payload["attributes"]

    def test_json_v3_context_is_scoped_and_validated(self) -> None:
        assert public_bind_log_context is bind_log_context
        formatter = JsonFormatter()
        logger = logging.getLogger("rpacore.test.context")
        record = logger.makeRecord(
            logger.name,
            logging.INFO,
            __file__,
            1,
            "outside",
            (),
            None,
            extra={"event": "transaction_started"},
        )
        with bind_log_context(transaction_id="tx-1"):
            assert json.loads(formatter.format(record))["attributes"] == {
                "transaction_id": "tx-1"
            }
        assert json.loads(formatter.format(record))["attributes"] == {}
        with pytest.raises(ValueError, match="Unsupported log context"):
            with bind_log_context(config="secret"):
                pass
        with pytest.raises(TypeError, match="must be a str or int"):
            with bind_log_context(retry_number=True):
                pass
        with pytest.raises(ValueError, match="must not be empty"):
            with bind_log_context(transaction_id=""):
                pass
        with pytest.raises(ValueError, match="must be >= 0"):
            with bind_log_context(retry_number=-1):
                pass

    def test_json_v3_uses_structured_exception_and_stack_names(self) -> None:
        logger = logging.getLogger("rpacore.test.v3.exception")
        try:
            raise RuntimeError("diagnostic detail")
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logger.makeRecord(
            logger.name,
            logging.ERROR,
            __file__,
            1,
            "operation failed",
            (),
            exc_info,
            extra={"event": "operation_failed"},
        )

        payload = json.loads(JsonFormatter().format(record))

        assert payload["exception"]["type"] == "RuntimeError"
        assert payload["exception"]["message"] == "diagnostic detail"
        assert "RuntimeError: diagnostic detail" in payload["exception"]["stacktrace"]
        assert "level" not in payload
        assert "stack" not in payload

    def test_get_logger_child_uses_rpacore_root_configuration(self) -> None:
        root = logging.getLogger("rpacore")
        previous_handlers = list(root.handlers)
        previous_level = root.level
        previous_propagate = root.propagate
        stream = io.StringIO()
        try:
            root.handlers.clear()
            configure_logger(name="rpacore", fmt="json", stream=stream)
            child = get_logger("example.automation")
            child.info("child event", extra={"event": "child_event"})
            assert child.name == "rpacore.application.example.automation"
            assert json.loads(stream.getvalue())["event"] == "rpacore.child.event"
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers = previous_handlers
            root.setLevel(previous_level)
            root.propagate = previous_propagate

    def test_removed_json_version_keyword_is_rejected_without_reconfiguration(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(
            name="rpacore.test.json-version",
            fmt="json",
            stream=stream,
        )
        handlers = list(logger.handlers)
        with pytest.raises(TypeError, match="unexpected keyword argument 'json_version'"):
            configure_logger(
                name="rpacore.test.json-version",
                fmt="json",
                json_version=3,
                stream=stream,
            )
        assert logger.handlers == handlers

    def test_configure_logger_resolves_application_name_like_get_logger(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="example.automation", fmt="json", stream=stream)

        assert logger is get_logger("example.automation")
        logger.info("configured child", extra={"event": "child_event"})
        assert json.loads(stream.getvalue())["event"] == "rpacore.child.event"

    def test_text_and_json_formats_include_exception_diagnostics(self) -> None:
        logger = logging.getLogger("rpacore.test.exception-format")
        try:
            raise RuntimeError("diagnostic detail")
        except RuntimeError:
            exc_info = sys.exc_info()
        record = logger.makeRecord(
            logger.name,
            logging.ERROR,
            __file__,
            1,
            "operation failed",
            (),
            exc_info,
            extra={"event": "operation_failed"},
        )

        text = TextFormatter().format(record)
        payload = json.loads(JsonFormatter().format(record))

        assert "RuntimeError: diagnostic detail" in text
        assert "Traceback (most recent call last)" in text
        assert payload["exception"]["type"] == "RuntimeError"
        assert payload["exception"]["message"] == "diagnostic detail"
        assert "RuntimeError: diagnostic detail" in payload["exception"]["stacktrace"]

    def test_text_and_json_formats_include_stack_information(self) -> None:
        record = logging.makeLogRecord(
            {
                "levelno": logging.WARNING,
                "levelname": "WARNING",
                "msg": "slow operation",
                "stack_info": "Stack (most recent call last):\n  operator.py:10",
            }
        )

        text = TextFormatter().format(record)
        payload = json.loads(JsonFormatter().format(record))

        assert "operator.py:10" in text
        assert "operator.py:10" in payload["stacktrace"]

    def test_text_and_json_formats_share_sensitive_extra_redaction(self) -> None:
        record = logging.makeLogRecord(
            {
                "levelno": logging.INFO,
                "levelname": "INFO",
                "msg": "safe",
                "config": {"token": "top-secret"},
                "credentials": "credential-secret",
                "resources": {"session": "resource-secret"},
                "details": {
                    "credentials": "nested-secret",
                    "visible": "ok",
                },
            }
        )

        text = TextFormatter().format(record)
        structured = JsonFormatter().format(record)

        for secret in (
            "top-secret",
            "credential-secret",
            "resource-secret",
            "nested-secret",
        ):
            assert secret not in text
            assert secret not in structured
        assert "visible" in text
        assert json.loads(structured)["attributes"]["details"] == {"visible": "ok"}

    def test_json_extras_cannot_replace_canonical_envelope(self) -> None:
        record = logging.makeLogRecord(
            {
                "levelno": logging.INFO,
                "levelname": "INFO",
                "msg": "ok",
                "event": "operation_completed",
                "level": "critical",
                "log_format_version": 999,
                "timestamp": "forged",
                "exception": {"type": "Forged"},
                "stack": "forged",
            }
        )

        payload = json.loads(JsonFormatter().format(record))

        assert payload["log_format_version"] == LOG_FORMAT_VERSION
        assert datetime.fromisoformat(payload["timestamp"]).tzinfo is not None
        assert payload["severity"] == "info"
        assert payload["event"] == "rpacore.operation.completed"
        assert "exception" not in payload
        assert "stacktrace" not in payload
        assert payload["attributes"]["level"] == "critical"
        assert payload["attributes"]["stack"] == "forged"

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="fmt must be 'text' or 'json'"):
            configure_logger(name="rpacore.test.invalid", fmt="xml")

    def test_invalid_reconfiguration_preserves_working_logger(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(
            name="rpacore.test.atomic",
            fmt="text",
            stream=stream,
        )
        original_handlers = list(logger.handlers)
        original_level = logger.level

        with pytest.raises(ValueError, match="fmt must be"):
            configure_logger(
                name="rpacore.test.atomic",
                fmt="xml",
                stream=stream,
            )
        with pytest.raises(ValueError, match="Unknown level"):
            configure_logger(
                name="rpacore.test.atomic",
                level="LOUD",
                stream=stream,
            )

        assert logger.handlers == original_handlers
        assert logger.level == original_level
        logger.info("still available")
        assert "still available" in stream.getvalue()

    def test_reconfiguration_preserves_application_handlers(self) -> None:
        name = "rpacore.test.application-handler"
        logger = logging.getLogger(name)
        logger.handlers.clear()
        application_handler = logging.StreamHandler(io.StringIO())
        logger.addHandler(application_handler)
        try:
            configure_logger(name=name, fmt="text", stream=io.StringIO())
            configure_logger(name=name, fmt="json", stream=io.StringIO())

            assert application_handler in logger.handlers
            assert len(logger.handlers) == 2
        finally:
            logger.handlers.clear()
            application_handler.close()

    def test_reconfigure_does_not_duplicate_handlers(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.reset", fmt="text", stream=stream)
        logger = configure_logger(name="rpacore.test.reset", fmt="text", stream=stream)
        logger.info("Only once", extra={"event": "transaction_completed"})

        assert stream.getvalue().strip().count("Only once") == 1


class TestEngineLogging:
    def test_v3_engine_events_include_transaction_correlation(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(
            name="rpacore.test.engine.v3",
            fmt="json",
            stream=stream,
        )
        tx = Transaction(reference="T1", steps=[SuccessStep("a", 1)])

        Engine(logger=logger).run(ProcessContext(transaction=tx))

        started = json.loads(stream.getvalue().splitlines()[0])
        assert started["event"] == "rpacore.transaction.started"
        assert started["attributes"]["transaction_id"] == tx.id
        assert started["attributes"]["transaction_reference"] == "T1"

    def test_checkpoint_event_is_runtime_log_not_history(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.engine.checkpoint", fmt="json", stream=stream)
        tx = Transaction(reference="T1", steps=[SuccessStep("a", 1)])

        Engine(logger=logger).run(ProcessContext(transaction=tx), checkpoint=lambda tx: None)

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        checkpoint = next(
            record
            for record in records
            if record["event"] == "rpacore.transaction.checkpoint"
        )
        assert checkpoint["attributes"]["transaction_id"] == tx.id
        assert checkpoint["attributes"]["transaction_status"] in {
            Status.IN_PROGRESS,
            Status.SUCCESSFUL,
        }
        assert "transaction_checkpoint" not in [str(entry.event) for entry in tx.history]

    def test_success_flow_emits_standardized_events(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.engine.success", fmt="json", stream=stream)
        tx = Transaction(
            reference="T1",
            steps=[SuccessStep("a", 1), SuccessStep("b", 2)],
        )

        Engine(logger=logger).run(ProcessContext(transaction=tx))

        events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
        assert events == [
            "rpacore.transaction.started",
            "rpacore.step.started",
            "rpacore.step.completed",
            "rpacore.step.started",
            "rpacore.step.completed",
            "rpacore.transaction.completed",
        ]

    def test_failed_step_event_contains_failure_details(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.engine.fail", fmt="json", stream=stream)
        tx = Transaction(reference="T1", steps=[BusinessFailStep("validate", 1)])

        Engine(logger=logger).run(ProcessContext(transaction=tx))

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        failed = next(
            record for record in records if record["event"] == "rpacore.step.failed"
        )

        assert failed["attributes"]["step_name"] == "validate"
        assert failed["attributes"]["step_status"] == Status.FAILED
        assert failed["attributes"]["exception_type"] == "BusinessException"
        assert failed["attributes"]["retry_number"] == 0

    def test_transaction_completed_event_reports_final_status(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.engine.complete", fmt="json", stream=stream)
        tx = Transaction(reference="T1", steps=[SuccessStep("a", 1)])

        Engine(logger=logger).run(ProcessContext(transaction=tx))

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        completed = records[-1]
        assert completed["event"] == "rpacore.transaction.completed"
        assert completed["attributes"]["transaction_status"] == Status.SUCCESSFUL


class TestRunnerLogging:
    def test_v3_queue_run_completion_has_summary_attributes(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(
            name="rpacore.test.runner.v3",
            fmt="json",
            stream=stream,
        )

        run_queue_loop(
            queue=EmptyQueue(),
            engine=Engine(logger=logger),
            build_transaction=lambda item: Transaction(reference=item.reference),
            config={},
            credentials=EnvCredentialProvider(),
            worker_id="worker-a",
            logger=logger,
        )

        record = json.loads(stream.getvalue())
        assert record["event"] == "rpacore.queue.run_completed"
        assert record["attributes"] == {
            "completed": 0,
            "failed": 0,
            "lease_lost": 0,
            "processed": 0,
            "retry_scheduled": 0,
            "terminal_failed": 0,
            "transition_unknown": 0,
            "worker_id": "worker-a",
        }

    def test_resource_scope_events_log_names_not_objects(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.runner.resources", fmt="json", stream=stream)
        resource = object()

        @contextmanager
        def scope():
            yield {"browser": resource}

        run_queue_loop(
            queue=EmptyQueue(),
            engine=Engine(logger=logger),
            build_transaction=lambda item: Transaction(
                reference=item.reference,
                steps=[SuccessStep("a", 1)],
            ),
            config={"token": "secret"},
            credentials=EnvCredentialProvider(),
            worker_id="worker-a",
            logger=logger,
            resource_scope=scope(),
        )

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        acquired = next(
            record
            for record in records
            if record["event"] == "rpacore.resource.scope.acquired"
        )
        released = next(
            record
            for record in records
            if record["event"] == "rpacore.resource.scope.released"
        )
        assert acquired["attributes"]["resource_names"] == ["browser"]
        assert acquired["attributes"]["resource_count"] == 1
        assert released["attributes"]["resource_names"] == ["browser"]
        assert "resources" not in acquired["attributes"]
        assert "config" not in acquired["attributes"]

    def test_resource_scope_suppressed_exception_returns_summary(self) -> None:
        stream = io.StringIO()
        logger = configure_logger(name="rpacore.test.runner.suppressed", fmt="json", stream=stream)

        class SuppressingScope:
            def __enter__(self):
                return {"browser": object()}

            def __exit__(self, exc_type, exc, tb):
                return True

        def build_transaction(_item):
            raise RuntimeError("suppressed")

        summary = run_queue_loop(
            queue=OneItemQueue(),
            engine=Engine(logger=logger),
            build_transaction=build_transaction,
            config={},
            credentials=EnvCredentialProvider(),
            worker_id="worker-a",
            logger=logger,
            resource_scope=SuppressingScope(),
        )

        assert summary.processed == 1
        assert summary.failed == 0
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert any(
            record["event"] == "rpacore.resource.scope.released"
            for record in records
        )
