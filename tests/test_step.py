"""Tests for rpacore.step."""

import pytest

from rpacore.exceptions import BusinessException, SystemException
from rpacore.step import Step
from rpacore.status import Status


class TestStepFreshState:
    """A freshly created Step has predictable defaults."""

    def test_name(self) -> None:
        step = Step("login", 1)
        assert step.name == "login"

    def test_execution_order(self) -> None:
        step = Step("login", 1)
        assert step.execution_order == 1

    def test_status_defaults_to_pending(self) -> None:
        step = Step("login", 1)
        assert step.status is Status.PENDING

    def test_arguments_defaults_to_empty_dict(self) -> None:
        step = Step("login", 1)
        assert step.arguments == {}

    def test_exceptions_defaults_to_empty_list(self) -> None:
        step = Step("login", 1)
        assert step.exceptions == []

    def test_no_public_timeout_attribute(self) -> None:
        step = Step("login", 1)
        assert not hasattr(step, "timeout")

    def test_arguments_not_shared_between_instances(self) -> None:
        a = Step("a", 1)
        b = Step("b", 2)
        a.arguments["key"] = "value"
        assert "key" not in b.arguments

    def test_exceptions_not_shared_between_instances(self) -> None:
        a = Step("a", 1)
        b = Step("b", 2)
        a.exceptions.append(BusinessException("err"))
        assert len(b.exceptions) == 0


class TestStepCustomArguments:
    def test_custom_arguments(self) -> None:
        step = Step("login", 1, arguments={"user": "admin"})
        assert step.arguments == {"user": "admin"}

    def test_custom_arguments_does_not_mutate_original(self) -> None:
        args = {"user": "admin"}
        step = Step("login", 1, arguments=args)
        step.arguments["password"] = "secret"
        assert "password" not in args


class TestStepTimeoutRemoved:
    def test_timeout_keyword_is_not_public_api(self) -> None:
        with pytest.raises(TypeError, match="timeout"):
            Step("login", 1, timeout=0.5)  # type: ignore[call-arg]


class TestStepExecute:
    def test_base_step_raises_not_implemented(self) -> None:
        from rpacore.context import ProcessContext
        from rpacore.transaction import Transaction
        step = Step("login", 1)
        ctx = ProcessContext(transaction=Transaction(reference="test"))
        with pytest.raises(NotImplementedError, match="login"):
            step.execute(ctx)

    def test_subclass_can_implement_execute(self) -> None:
        from rpacore.context import ProcessContext
        from rpacore.transaction import Transaction

        class LoginStep(Step):
            def execute(self, ctx: ProcessContext) -> None:
                ctx.state["logged_in"] = True

        step = LoginStep("login", 1)
        ctx = ProcessContext(transaction=Transaction(reference="test"))
        step.execute(ctx)
        assert ctx.state["logged_in"] is True


class TestStepStatusTransitions:
    def test_status_can_be_set_to_in_progress(self) -> None:
        step = Step("login", 1)
        step.status = Status.IN_PROGRESS
        assert step.status is Status.IN_PROGRESS

    def test_status_can_be_set_to_successful(self) -> None:
        step = Step("login", 1)
        step.status = Status.SUCCESSFUL
        assert step.status is Status.SUCCESSFUL

    def test_status_can_be_set_to_failed(self) -> None:
        step = Step("login", 1)
        step.status = Status.FAILED
        assert step.status is Status.FAILED

    def test_exceptions_can_be_recorded(self) -> None:
        step = Step("login", 1)
        exc = SystemException("timeout", action="login")
        step.exceptions.append(exc)
        step.status = Status.FAILED
        assert len(step.exceptions) == 1
        assert step.status is Status.FAILED
