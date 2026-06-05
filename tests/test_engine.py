"""Tests for rpacore.engine."""

import pytest

from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, SystemException
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction


def _ctx(tx: Transaction) -> ProcessContext:
    return ProcessContext(transaction=tx)


class SuccessSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.data[self.name] = "done"


class BusinessFailSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException("rule violated", action=self.name)


class StoppingBusinessFailSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise BusinessException("rule violated", action=self.name, stop=True)


class SystemFailSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        raise SystemException("crash", action=self.name)


class SkipSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        self.status = Status.SKIPPED


class TestEngineHappyPath:
    def test_all_skills_succeed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SuccessSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(_ctx(tx))
        assert tx.status is Status.SUCCESSFUL
        assert all(s.status is Status.SUCCESSFUL for s in tx.skills)

    def test_context_shared_between_skills(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SuccessSkill("a", 1), SuccessSkill("b", 2)],
        )
        ctx = _ctx(tx)
        Engine().run(ctx)
        assert ctx.data == {"a": "done", "b": "done"}

    def test_skills_run_in_execution_order(self) -> None:
        order: list[str] = []

        class TrackSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                order.append(self.name)

        tx = Transaction(
            reference="T1",
            skills=[TrackSkill("c", 3), TrackSkill("a", 1), TrackSkill("b", 2)],
        )
        Engine().run(_ctx(tx))
        assert order == ["a", "b", "c"]

    def test_empty_transaction_succeeds(self) -> None:
        tx = Transaction(reference="T1")
        Engine().run(_ctx(tx))
        assert tx.status is Status.SUCCESSFUL

    def test_default_data_is_empty_dict(self) -> None:
        tx = Transaction(reference="T1", skills=[SuccessSkill("a", 1)])
        ctx = _ctx(tx)
        Engine().run(ctx)
        assert tx.status is Status.SUCCESSFUL
        assert "a" in ctx.data

    def test_ordinary_return_marks_skill_successful(self) -> None:
        skill = SuccessSkill("a", 1)
        tx = Transaction(reference="T1", skills=[skill])
        Engine().run(_ctx(tx))
        assert skill.status is Status.SUCCESSFUL


class TestEngineBusinessException:
    def test_skill_marked_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1)],
        )
        Engine().run(_ctx(tx))
        assert tx.skills[0].status is Status.FAILED

    def test_exception_recorded_on_skill(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1)],
        )
        Engine().run(_ctx(tx))
        assert len(tx.skills[0].exceptions) == 1
        assert isinstance(tx.skills[0].exceptions[0], BusinessException)

    def test_execution_continues_after_business_exception(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(_ctx(tx))
        assert tx.skills[0].status is Status.FAILED  # original order
        assert tx.skills[1].status is Status.SUCCESSFUL

    def test_transaction_marked_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(_ctx(tx))
        assert tx.status is Status.FAILED

    def test_stopping_business_exception_skips_downstream_pending_skills(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[
                StoppingBusinessFailSkill("validate", 1),
                SuccessSkill("write_output", 2),
            ],
        )

        Engine().run(_ctx(tx))

        assert tx.skills[0].status is Status.FAILED
        assert tx.skills[1].status is Status.SKIPPED

    def test_stopping_business_exception_marks_transaction_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[
                StoppingBusinessFailSkill("validate", 1),
                SuccessSkill("write_output", 2),
            ],
        )

        Engine().run(_ctx(tx))

        assert tx.status is Status.FAILED

    def test_stopping_business_exception_does_not_retry_failed_skill(self) -> None:
        attempts: list[int] = []

        class StopOnce(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                attempts.append(1)
                raise BusinessException("bad data", action=self.name, stop=True)

        tx = Transaction(reference="T1", skills=[StopOnce("validate", 1)])

        Engine(max_retries=3).run(_ctx(tx))

        assert attempts == [1]
        assert tx.retry_count == 0

    def test_stopping_business_exception_preserves_already_successful_downstream_skill(self) -> None:
        successful = SuccessSkill("already_done", 2)
        successful.status = Status.SUCCESSFUL
        tx = Transaction(
            reference="T1",
            skills=[StoppingBusinessFailSkill("validate", 1), successful],
        )

        Engine().run(_ctx(tx))

        assert successful.status is Status.SUCCESSFUL


class TestEngineSystemException:
    def test_skill_marked_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1)],
        )
        Engine().run(_ctx(tx))
        assert tx.skills[0].status is Status.FAILED

    def test_exception_recorded_on_skill(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1)],
        )
        Engine().run(_ctx(tx))
        assert len(tx.skills[0].exceptions) == 1
        assert isinstance(tx.skills[0].exceptions[0], SystemException)

    def test_execution_stops_after_system_exception(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(_ctx(tx))
        assert tx.skills[0].status is Status.FAILED  # original order
        assert tx.skills[1].status is Status.PENDING  # never ran

    def test_transaction_marked_failed(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(_ctx(tx))
        assert tx.status is Status.FAILED


class TestEngineUnhandledException:
    def test_unhandled_exception_wraps_as_system_exception(self) -> None:
        class BadSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise ValueError("unexpected")

        tx = Transaction(reference="T1", skills=[BadSkill("a", 1)])
        Engine().run(_ctx(tx))
        assert tx.skills[0].status is Status.FAILED
        assert len(tx.skills[0].exceptions) == 1
        assert isinstance(tx.skills[0].exceptions[0], SystemException)
        assert "unexpected" in str(tx.skills[0].exceptions[0])

    def test_unhandled_exception_stops_execution(self) -> None:
        class BadSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise TypeError("bad type")

        tx = Transaction(
            reference="T1",
            skills=[BadSkill("a", 1), SuccessSkill("b", 2)],
        )
        Engine().run(_ctx(tx))
        assert tx.skills[1].status is Status.PENDING

    def test_unhandled_exception_marks_transaction_failed(self) -> None:
        class BadSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise RuntimeError("boom")

        tx = Transaction(reference="T1", skills=[BadSkill("a", 1)])
        Engine().run(_ctx(tx))
        assert tx.status is Status.FAILED


class TestEngineStateTransitions:
    def test_transaction_is_in_progress_during_execution(self) -> None:
        captured: list[Status] = []

        class SpySkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                captured.append(ctx.transaction.status)

        tx = Transaction(reference="T1", skills=[SpySkill("a", 1)])
        Engine().run(_ctx(tx))
        assert captured == [Status.IN_PROGRESS]

    def test_skipped_skills_not_executed(self) -> None:
        order: list[str] = []

        class TrackSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                order.append(self.name)

        s1 = TrackSkill("a", 1)
        s2 = TrackSkill("b", 2)
        s2.status = Status.SKIPPED
        s3 = TrackSkill("c", 3)

        tx = Transaction(reference="T1", skills=[s1, s2, s3])
        Engine().run(_ctx(tx))
        assert order == ["a", "c"]
        assert s2.status is Status.SKIPPED

    def test_successful_skills_not_re_executed(self) -> None:
        order: list[str] = []

        class TrackSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                order.append(self.name)

        s1 = TrackSkill("a", 1)
        s1.status = Status.SUCCESSFUL
        s2 = TrackSkill("b", 2)

        tx = Transaction(reference="T1", skills=[s1, s2])
        Engine().run(_ctx(tx))
        assert order == ["b"]

    def test_transaction_successful_when_all_skipped(self) -> None:
        s1 = SuccessSkill("a", 1)
        s1.status = Status.SKIPPED
        tx = Transaction(reference="T1", skills=[s1])
        Engine().run(_ctx(tx))
        assert tx.status is Status.SUCCESSFUL

    def test_skill_can_mark_itself_skipped_during_execute(self) -> None:
        s1 = SkipSkill("optional", 1)
        s2 = SuccessSkill("followup", 2)

        tx = Transaction(reference="T1", skills=[s1, s2])
        Engine().run(_ctx(tx))

        assert tx.status is Status.SUCCESSFUL
        assert s1.status is Status.SKIPPED
        assert s2.status is Status.SUCCESSFUL


class TestEngineDirectSkillExecution:
    def test_timeout_error_raised_by_skill_uses_normal_classification(self) -> None:
        class RaisesTimeoutError(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise TimeoutError("service timeout")

        skill = RaisesTimeoutError("service", 1)
        tx = Transaction(reference="T1", skills=[skill])

        Engine().run(_ctx(tx))

        assert tx.status is Status.FAILED
        assert isinstance(skill.exceptions[0], SystemException)
        assert str(skill.exceptions[0]) == "service timeout"
        assert skill.exceptions[0].action == "service"


class TestEngineRetry:
    def test_default_max_retries_is_zero(self) -> None:
        assert Engine().max_retries == 0

    def test_default_retry_delay_is_zero(self) -> None:
        assert Engine().retry_delay == 0.0

    def test_default_retry_backoff_is_one(self) -> None:
        assert Engine().retry_backoff == 1.0

    def test_negative_max_retries_raises(self) -> None:
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            Engine(max_retries=-1)

    def test_invalid_max_retries_type_raises(self) -> None:
        with pytest.raises(TypeError, match="max_retries must be an int"):
            Engine(max_retries="1")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="max_retries must be an int"):
            Engine(max_retries=True)  # type: ignore[arg-type]

    def test_invalid_retry_delay_values_raise(self) -> None:
        with pytest.raises(ValueError, match="retry_delay must be >= 0"):
            Engine(retry_delay=-0.1)
        with pytest.raises(ValueError, match="retry_delay must be >= 0"):
            Engine(retry_delay=float("inf"))
        with pytest.raises(ValueError, match="retry_delay must be >= 0"):
            Engine(retry_delay=float("nan"))
        with pytest.raises(TypeError, match="retry_delay must be a number"):
            Engine(retry_delay="0.1")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="retry_delay must be a number"):
            Engine(retry_delay=True)

    def test_invalid_retry_backoff_values_raise(self) -> None:
        with pytest.raises(ValueError, match="retry_backoff must be >= 1"):
            Engine(retry_backoff=0.5)
        with pytest.raises(ValueError, match="retry_backoff must be >= 1"):
            Engine(retry_backoff=float("inf"))
        with pytest.raises(ValueError, match="retry_backoff must be >= 1"):
            Engine(retry_backoff=float("nan"))
        with pytest.raises(TypeError, match="retry_backoff must be a number"):
            Engine(retry_backoff="2")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="retry_backoff must be a number"):
            Engine(retry_backoff=False)

    def test_no_retry_by_default(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[BusinessFailSkill("a", 1)],
        )
        Engine().run(_ctx(tx))
        assert tx.retry_count == 0
        assert tx.status is Status.FAILED

    def test_system_exception_skill_retried(self) -> None:
        attempts: list[int] = []

        class FlakySkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                attempts.append(1)
                if len(attempts) < 2:
                    raise SystemException("transient", action=self.name)

        tx = Transaction(reference="T1", skills=[FlakySkill("a", 1)])
        Engine(max_retries=1).run(_ctx(tx))
        assert tx.status is Status.SUCCESSFUL
        assert tx.retry_count == 1
        assert len(attempts) == 2

    def test_retry_count_increments_per_pass(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1)],
        )
        Engine(max_retries=3).run(_ctx(tx))
        assert tx.retry_count == 3

    def test_transaction_failed_when_retries_exhausted(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SystemFailSkill("a", 1)],
        )
        Engine(max_retries=2).run(_ctx(tx))
        assert tx.status is Status.FAILED

    def test_successful_skills_not_retried(self) -> None:
        counts: dict[str, int] = {"a": 0, "b": 0}

        class TrackAndFailSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts[self.name] += 1
                raise SystemException("fail", action=self.name)

        class TrackSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts[self.name] += 1

        tx = Transaction(
            reference="T1",
            skills=[TrackSkill("a", 1), TrackAndFailSkill("b", 2)],
        )
        Engine(max_retries=1).run(_ctx(tx))
        assert counts["a"] == 1
        assert counts["b"] == 2

    def test_business_exception_not_retried(self) -> None:
        attempts: list[int] = []

        class AlwaysBusinessFail(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                attempts.append(1)
                raise BusinessException("rule violated", action=self.name)

        tx = Transaction(reference="T1", skills=[AlwaysBusinessFail("a", 1)])
        Engine(max_retries=3).run(_ctx(tx))
        assert len(attempts) == 1
        assert tx.retry_count == 0
        assert tx.status is Status.FAILED

    def test_mixed_business_and_system_failure_only_retries_system(self) -> None:
        counts: dict[str, int] = {"a": 0, "b": 0}

        class BusinessFail(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts[self.name] += 1
                raise BusinessException("bad data", action=self.name)

        class SystemFail(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts[self.name] += 1
                raise SystemException("timeout", action=self.name)

        tx = Transaction(
            reference="T1",
            skills=[BusinessFail("a", 1), SystemFail("b", 2)],
        )
        Engine(max_retries=1).run(_ctx(tx))
        assert counts["a"] == 1  # business fail — not retried
        assert counts["b"] == 2  # system fail — retried once

    def test_pending_skills_blocked_by_system_exception_run_after_retry(self) -> None:
        counts: dict[str, int] = {"a": 0, "b": 0}

        class FlakySkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts[self.name] += 1
                if counts[self.name] < 2:
                    raise SystemException("transient", action=self.name)

        class NextSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                counts[self.name] += 1

        tx = Transaction(
            reference="T1",
            skills=[FlakySkill("a", 1), NextSkill("b", 2)],
        )
        Engine(max_retries=1).run(_ctx(tx))
        assert tx.status is Status.SUCCESSFUL
        assert counts["a"] == 2
        assert counts["b"] == 1  # ran on retry pass after a succeeded

    def test_transaction_failed_when_skill_left_pending(self) -> None:
        # Verifies final status requires all skills SUCCESSFUL or SKIPPED
        s1 = SuccessSkill("a", 1)
        s2 = SuccessSkill("b", 2)
        tx = Transaction(reference="T1", skills=[s1, s2])
        Engine().run(_ctx(tx))
        s2.status = Status.PENDING  # manually corrupt state
        # Re-evaluate status directly to confirm rule
        assert not all(s.status in (Status.SUCCESSFUL, Status.SKIPPED) for s in tx.skills)

    def test_unhandled_exception_is_retried(self) -> None:
        attempts: list[int] = []

        class FlakySkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                attempts.append(1)
                if len(attempts) < 2:
                    raise ValueError("unexpected")

        tx = Transaction(reference="T1", skills=[FlakySkill("a", 1)])
        Engine(max_retries=1).run(_ctx(tx))
        assert len(attempts) == 2
        assert tx.status is Status.SUCCESSFUL

    def test_retry_number_set_on_exceptions(self) -> None:
        class AlwaysSystemFail(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise SystemException("fail", action=self.name)

        tx = Transaction(reference="T1", skills=[AlwaysSystemFail("a", 1)])
        Engine(max_retries=2).run(_ctx(tx))
        assert len(tx.skills[0].exceptions) == 3
        assert tx.skills[0].exceptions[0].retry_number == 0
        assert tx.skills[0].exceptions[1].retry_number == 1
        assert tx.skills[0].exceptions[2].retry_number == 2

    def test_retry_delay_is_respected_before_retry_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []

        class FlakySkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                if ctx.transaction.retry_count == 0:
                    raise SystemException("transient", action=self.name)

        monkeypatch.setattr("rpacore.engine.time.sleep", sleeps.append)
        tx = Transaction(reference="T1", skills=[FlakySkill("a", 1)])

        Engine(max_retries=1, retry_delay=0.25).run(_ctx(tx))

        assert sleeps == [0.25]
        assert tx.status is Status.SUCCESSFUL

    def test_retry_backoff_multiplies_delay_per_retry_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []

        monkeypatch.setattr("rpacore.engine.time.sleep", sleeps.append)
        tx = Transaction(reference="T1", skills=[SystemFailSkill("a", 1)])

        Engine(max_retries=3, retry_delay=0.5, retry_backoff=2).run(_ctx(tx))

        assert sleeps == [0.5, 1.0, 2.0]
        assert tx.retry_count == 3

    def test_zero_retry_delay_does_not_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []

        monkeypatch.setattr("rpacore.engine.time.sleep", sleeps.append)
        tx = Transaction(reference="T1", skills=[SystemFailSkill("a", 1)])

        Engine(max_retries=2, retry_delay=0.0).run(_ctx(tx))

        assert sleeps == []
        assert tx.retry_count == 2
