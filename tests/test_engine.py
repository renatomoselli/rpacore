"""Tests for oref.engine."""

import pytest

from oref.engine import Engine
from oref.exceptions import BusinessException, SystemException
from oref.skill import Skill
from oref.status import Status
from oref.transaction import Transaction


class SuccessSkill(Skill):
    def execute(self, context: dict[str, object]) -> None:
        context[self.name] = "done"


class BusinessFailSkill(Skill):
    def execute(self, context: dict[str, object]) -> None:
        raise BusinessException("rule violated", action=self.name)


class SystemFailSkill(Skill):
    def execute(self, context: dict[str, object]) -> None:
        raise SystemException("crash", action=self.name)


class TestEngineHappyPath:
    def test_all_skills_succeed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SuccessSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(tx)
        assert tx.status is Status.SUCCESSFUL
        assert all(s.status is Status.SUCCESSFUL for s in tx.skills)

    def test_context_shared_between_skills(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SuccessSkill("a", 1), SuccessSkill("b", 2)],
        )
        ctx: dict[str, object] = {}
        Engine().run(tx, ctx)
        assert ctx == {"a": "done", "b": "done"}

    def test_skills_run_in_execution_order(self) -> None:
        order: list[str] = []

        class TrackSkill(Skill):
            def execute(self, context: dict[str, object]) -> None:
                order.append(self.name)

        tx = Transaction(
            reference="T1",
            skills=[TrackSkill("c", 3), TrackSkill("a", 1), TrackSkill("b", 2)],
        )
        Engine().run(tx)
        assert order == ["a", "b", "c"]

    def test_empty_transaction_succeeds(self) -> None:
        tx = Transaction(reference="T1")
        Engine().run(tx)
        assert tx.status is Status.SUCCESSFUL

    def test_default_context_is_empty_dict(self) -> None:
        tx = Transaction(reference="T1", skills=[SuccessSkill("a", 1)])
        Engine().run(tx)
        assert tx.status is Status.SUCCESSFUL


class TestEngineBusinessException:
    def test_skill_marked_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1)],
        )
        Engine().run(tx)
        assert tx.skills[0].status is Status.FAILED

    def test_exception_recorded_on_skill(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1)],
        )
        Engine().run(tx)
        assert len(tx.skills[0].exceptions) == 1
        assert isinstance(tx.skills[0].exceptions[0], BusinessException)

    def test_execution_continues_after_business_exception(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(tx)
        assert tx.skills[0].status is Status.FAILED  # original order
        assert tx.skills[1].status is Status.SUCCESSFUL

    def test_transaction_marked_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(tx)
        assert tx.status is Status.FAILED


class TestEngineSystemException:
    def test_skill_marked_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1)],
        )
        Engine().run(tx)
        assert tx.skills[0].status is Status.FAILED

    def test_exception_recorded_on_skill(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1)],
        )
        Engine().run(tx)
        assert len(tx.skills[0].exceptions) == 1
        assert isinstance(tx.skills[0].exceptions[0], SystemException)

    def test_execution_stops_after_system_exception(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(tx)
        assert tx.skills[0].status is Status.FAILED  # original order
        assert tx.skills[1].status is Status.PENDING  # never ran

    def test_transaction_marked_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(tx)
        assert tx.status is Status.FAILED


class TestEngineUnhandledException:
    def test_unhandled_exception_wraps_as_system_exception(self) -> None:
        class BadSkill(Skill):
            def execute(self, context: dict[str, object]) -> None:
                raise ValueError("unexpected")

        tx = Transaction(reference="T1", skills=[BadSkill("a", 1)])
        Engine().run(tx)
        assert tx.skills[0].status is Status.FAILED
        assert len(tx.skills[0].exceptions) == 1
        assert isinstance(tx.skills[0].exceptions[0], SystemException)
        assert "unexpected" in str(tx.skills[0].exceptions[0])

    def test_unhandled_exception_stops_execution(self) -> None:
        class BadSkill(Skill):
            def execute(self, context: dict[str, object]) -> None:
                raise TypeError("bad type")

        tx = Transaction(
            reference="T1",
            skills=[BadSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(tx)
        assert tx.skills[1].status is Status.PENDING

    def test_unhandled_exception_marks_transaction_failed(self) -> None:
        class BadSkill(Skill):
            def execute(self, context: dict[str, object]) -> None:
                raise RuntimeError("boom")

        tx = Transaction(reference="T1", skills=[BadSkill("a", 1)])
        Engine().run(tx)
        assert tx.status is Status.FAILED


class TestEngineStateTransitions:
    def test_transaction_is_in_progress_during_execution(self) -> None:
        captured: list[Status] = []

        class SpySkill(Skill):
            def execute(self, context: dict[str, object]) -> None:
                captured.append(context["_tx"].status)

        tx = Transaction(reference="T1", skills=[SpySkill("a", 1)])
        Engine().run(tx, {"_tx": tx})
        assert captured == [Status.IN_PROGRESS]

    def test_skipped_skills_not_executed(self) -> None:
        order: list[str] = []

        class TrackSkill(Skill):
            def execute(self, context: dict[str, object]) -> None:
                order.append(self.name)

        s1 = TrackSkill("a", 1)
        s2 = TrackSkill("b", 2)
        s2.status = Status.SKIPPED
        s3 = TrackSkill("c", 3)

        tx = Transaction(reference="T1", skills=[s1, s2, s3])
        Engine().run(tx)
        assert order == ["a", "c"]
        assert s2.status is Status.SKIPPED

    def test_successful_skills_not_re_executed(self) -> None:
        order: list[str] = []

        class TrackSkill(Skill):
            def execute(self, context: dict[str, object]) -> None:
                order.append(self.name)

        s1 = TrackSkill("a", 1)
        s1.status = Status.SUCCESSFUL
        s2 = TrackSkill("b", 2)

        tx = Transaction(reference="T1", skills=[s1, s2])
        Engine().run(tx)
        assert order == ["b"]

    def test_transaction_successful_when_all_skipped(self) -> None:
        s1 = SuccessSkill("a", 1)
        s1.status = Status.SKIPPED
        tx = Transaction(reference="T1", skills=[s1])
        Engine().run(tx)
        assert tx.status is Status.SUCCESSFUL
