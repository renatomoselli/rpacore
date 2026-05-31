"""Tests for rpacore.skill."""

import pytest

from rpacore.exceptions import BusinessException, SystemException
from rpacore.skill import Skill
from rpacore.status import Status


class TestSkillFreshState:
    """A freshly created Skill has predictable defaults."""

    def test_name(self) -> None:
        skill = Skill("login", 1)
        assert skill.name == "login"

    def test_execution_order(self) -> None:
        skill = Skill("login", 1)
        assert skill.execution_order == 1

    def test_status_defaults_to_pending(self) -> None:
        skill = Skill("login", 1)
        assert skill.status is Status.PENDING

    def test_arguments_defaults_to_empty_dict(self) -> None:
        skill = Skill("login", 1)
        assert skill.arguments == {}

    def test_exceptions_defaults_to_empty_list(self) -> None:
        skill = Skill("login", 1)
        assert skill.exceptions == []

    def test_arguments_not_shared_between_instances(self) -> None:
        a = Skill("a", 1)
        b = Skill("b", 2)
        a.arguments["key"] = "value"
        assert "key" not in b.arguments

    def test_exceptions_not_shared_between_instances(self) -> None:
        a = Skill("a", 1)
        b = Skill("b", 2)
        a.exceptions.append(BusinessException("err"))
        assert len(b.exceptions) == 0


class TestSkillCustomArguments:
    def test_custom_arguments(self) -> None:
        skill = Skill("login", 1, arguments={"user": "admin"})
        assert skill.arguments == {"user": "admin"}

    def test_custom_arguments_does_not_mutate_original(self) -> None:
        args = {"user": "admin"}
        skill = Skill("login", 1, arguments=args)
        skill.arguments["password"] = "secret"
        assert "password" not in args


class TestSkillExecute:
    def test_base_skill_raises_not_implemented(self) -> None:
        from rpacore.context import ProcessContext
        from rpacore.transaction import Transaction
        skill = Skill("login", 1)
        ctx = ProcessContext(transaction=Transaction(reference="test"))
        with pytest.raises(NotImplementedError, match="login"):
            skill.execute(ctx)

    def test_subclass_can_implement_execute(self) -> None:
        from rpacore.context import ProcessContext
        from rpacore.transaction import Transaction

        class LoginSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                ctx.data["logged_in"] = True

        skill = LoginSkill("login", 1)
        ctx = ProcessContext(transaction=Transaction(reference="test"))
        skill.execute(ctx)
        assert ctx.data["logged_in"] is True


class TestSkillStatusTransitions:
    def test_status_can_be_set_to_in_progress(self) -> None:
        skill = Skill("login", 1)
        skill.status = Status.IN_PROGRESS
        assert skill.status is Status.IN_PROGRESS

    def test_status_can_be_set_to_successful(self) -> None:
        skill = Skill("login", 1)
        skill.status = Status.SUCCESSFUL
        assert skill.status is Status.SUCCESSFUL

    def test_status_can_be_set_to_failed(self) -> None:
        skill = Skill("login", 1)
        skill.status = Status.FAILED
        assert skill.status is Status.FAILED

    def test_exceptions_can_be_recorded(self) -> None:
        skill = Skill("login", 1)
        exc = SystemException("timeout", action="login")
        skill.exceptions.append(exc)
        skill.status = Status.FAILED
        assert len(skill.exceptions) == 1
        assert skill.status is Status.FAILED
