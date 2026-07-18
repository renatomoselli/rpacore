"""Tests for rpacore.engine."""

import subprocess
import sys
import textwrap

import pytest

from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.persistence import load_transaction
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


def _ctx(tx: Transaction) -> ProcessContext:
    return ProcessContext(transaction=tx)


class SuccessSkill(Skill):
    def execute(self, ctx: ProcessContext) -> None:
        ctx.state[self.name] = "done"


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
        assert tx.outcome_category is OutcomeCategory.SUCCESSFUL
        assert tx.retry_disposition is RetryDisposition.NOT_APPLICABLE
        assert tx.failure_code == ""

    def test_successful_run_records_timestamps_and_history(self) -> None:
        tx = Transaction(reference="T1", skills=[SuccessSkill("a", 1)])

        Engine().run(_ctx(tx))

        assert tx.created_at is not None
        assert tx.started_at is not None
        assert tx.finished_at is not None
        assert [entry.event for entry in tx.history] == [
            HistoryEvent.TRANSACTION_STARTED,
            HistoryEvent.SKILL_STARTED,
            HistoryEvent.SKILL_SUCCEEDED,
            HistoryEvent.TRANSACTION_COMPLETED,
        ]
        assert [entry.sequence for entry in tx.history] == [1, 2, 3, 4]

    def test_context_shared_between_skills(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[SuccessSkill("a", 1), SuccessSkill("b", 2)],
        )
        ctx = _ctx(tx)
        Engine().run(ctx)
        assert ctx.state == {"a": "done", "b": "done"}

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

    def test_default_state_is_empty_dict(self) -> None:
        tx = Transaction(reference="T1", skills=[SuccessSkill("a", 1)])
        ctx = _ctx(tx)
        Engine().run(ctx)
        assert tx.status is Status.SUCCESSFUL
        assert "a" in ctx.state

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
        assert tx.outcome_category is OutcomeCategory.BUSINESS_FAILED
        assert tx.retry_disposition is RetryDisposition.NOT_REQUESTED

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
        assert [entry.event for entry in tx.history] == [
            HistoryEvent.TRANSACTION_STARTED,
            HistoryEvent.SKILL_STARTED,
            HistoryEvent.SKILL_FAILED,
            HistoryEvent.SKILL_SKIPPED,
            HistoryEvent.TRANSACTION_COMPLETED,
        ]

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

    def test_rerun_failed_transaction_resets_skipped_downstream_skills(self) -> None:
        attempts: list[str] = []

        class FailsThenSucceeds(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                attempts.append(self.name)
                if len(attempts) == 1:
                    raise BusinessException("bad data", action=self.name, stop=True)

        tx = Transaction(
            reference="T1",
            skills=[
                FailsThenSucceeds("validate", 1),
                SuccessSkill("write_output", 2),
            ],
        )
        Engine().run(_ctx(tx))
        assert tx.status is Status.FAILED
        assert tx.skills[1].status is Status.SKIPPED

        Engine().run(_ctx(tx))

        assert tx.status is Status.SUCCESSFUL
        assert tx.skills[1].status is Status.SUCCESSFUL


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
        assert tx.skills[0].exceptions[0].code == "rpacore.system.unexpected"
        assert tx.failure_code == "rpacore.system.unexpected"

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
    def test_execution_validation_records_terminal_outcome(self) -> None:
        tx = Transaction(reference="", skills=[SuccessSkill("a", 1)])

        with pytest.raises(ExecutionValidationError, match="transaction.reference"):
            Engine().run(_ctx(tx))

        assert tx.status is Status.FAILED
        assert tx.outcome_category is OutcomeCategory.VALIDATION_FAILED
        assert tx.retry_disposition is RetryDisposition.NOT_REQUESTED
        assert tx.failure_code == "rpacore.validation.execution"

    def test_transaction_is_in_progress_during_execution(self) -> None:
        captured: list[Status] = []

        class SpySkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                captured.append(ctx.transaction.status)

        tx = Transaction(reference="T1", skills=[SpySkill("a", 1)])
        Engine().run(_ctx(tx))
        assert captured == [Status.IN_PROGRESS]

    def test_initial_blocked_skill_ids_returns_none_for_non_resumed_transaction(self) -> None:
        tx = Transaction(reference="T1")

        result = Engine()._initial_blocked_skill_ids(tx)

        assert result is None

    def test_initial_blocked_skill_ids_returns_business_failures_for_resumed_transaction(self) -> None:
        business_failed = Skill("business", 1)
        business_failed.status = Status.FAILED
        business_failed.exceptions.append(BusinessException("bad data", action="business"))
        system_failed = Skill("system", 2)
        system_failed.status = Status.FAILED
        system_failed.exceptions.append(SystemException("timeout", action="system"))
        tx = Transaction(
            reference="T1",
            status=Status.PENDING,
            skills=[business_failed, system_failed],
        )
        tx.append_history(HistoryEvent.TRANSACTION_RESUMED)

        result = Engine()._initial_blocked_skill_ids(tx)

        assert result == {id(business_failed)}

    def test_preexisting_skipped_skills_are_reset_before_run(self) -> None:
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
        assert order == ["a", "b", "c"]
        assert s2.status is Status.SUCCESSFUL

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
        assert s1.status is Status.SUCCESSFUL

    def test_skill_can_mark_itself_skipped_during_execute(self) -> None:
        s1 = SkipSkill("optional", 1)
        s2 = SuccessSkill("followup", 2)

        tx = Transaction(reference="T1", skills=[s1, s2])
        Engine().run(_ctx(tx))

        assert tx.status is Status.SUCCESSFUL
        assert s1.status is Status.SKIPPED
        assert s2.status is Status.SUCCESSFUL

    def test_invalid_transaction_shape_fails_before_skill_side_effects(self) -> None:
        effects: list[str] = []

        class TrackSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                effects.append(self.name)

        tx = Transaction(
            reference="T1",
            skills=[TrackSkill("duplicate", 1), TrackSkill("duplicate", 2)],
        )

        with pytest.raises(ExecutionValidationError, match="skill.name must be unique"):
            Engine().run(_ctx(tx))

        assert effects == []
        assert tx.status is Status.FAILED
        assert tx.finished_at is not None
        assert tx.history[-1].event is HistoryEvent.TRANSACTION_COMPLETED
        assert tx.history[-1].status is Status.FAILED


class TestEngineCheckpointing:
    def test_checkpoint_runs_after_each_state_transition(self) -> None:
        checkpoints: list[list[HistoryEvent]] = []

        class AssertStartedCheckpointSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                assert checkpoints[-1][-1] is HistoryEvent.SKILL_STARTED
                ctx.state["ran"] = True

        tx = Transaction(reference="T1", skills=[AssertStartedCheckpointSkill("a", 1)])

        Engine().run(
            _ctx(tx),
            checkpoint=lambda transaction: checkpoints.append(
                [entry.event for entry in transaction.history]
            ),
        )

        assert [events[-1] for events in checkpoints] == [
            HistoryEvent.TRANSACTION_STARTED,
            HistoryEvent.SKILL_STARTED,
            HistoryEvent.SKILL_SUCCEEDED,
            HistoryEvent.TRANSACTION_COMPLETED,
        ]

    def test_checkpoint_failure_stops_before_skill_code_runs(self) -> None:
        class TrackSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                ctx.state["ran"] = True

        def fail_on_skill_started(transaction: Transaction) -> None:
            if transaction.history[-1].event is HistoryEvent.SKILL_STARTED:
                raise RuntimeError("checkpoint failed")

        tx = Transaction(reference="T1", skills=[TrackSkill("a", 1)])

        with pytest.raises(RuntimeError, match="checkpoint failed"):
            Engine().run(_ctx(tx), checkpoint=fail_on_skill_started)

        assert tx.state == {}
        assert tx.status is Status.IN_PROGRESS
        assert tx.skills[0].status is Status.IN_PROGRESS

    def test_checkpoint_validates_durable_state_before_saving(self) -> None:
        calls = 0

        class BadStateSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                ctx.state["client"] = object()

        def checkpoint(transaction: Transaction) -> None:
            nonlocal calls
            calls += 1

        tx = Transaction(reference="T1", skills=[BadStateSkill("a", 1)])

        with pytest.raises(TypeError, match="transaction.state\\['client'\\] expected JSON value"):
            Engine().run(_ctx(tx), checkpoint=checkpoint)

        assert calls == 2
        assert tx.skills[0].status is Status.SUCCESSFUL

    def test_preflight_rejects_arguments_before_transaction_mutation(self) -> None:
        tx = Transaction(
            reference="T1",
            skills=[Skill("invalid", 1, arguments={"ids": (1, 2)})],
        )

        with pytest.raises(TypeError, match=r"arguments\['ids'\]"):
            Engine().run(_ctx(tx))

        assert tx.status is Status.PENDING
        assert tx.skills[0].status is Status.PENDING
        assert tx.history == []

    def test_memory_error_is_not_masked_by_checkpoint_error(self) -> None:
        class MemoryFailSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise MemoryError("out of memory")

        def fail_checkpoint(transaction: Transaction) -> None:
            if transaction.history[-1].event is HistoryEvent.TRANSACTION_COMPLETED:
                raise RuntimeError("checkpoint failed")

        tx = Transaction(reference="T1", skills=[MemoryFailSkill("a", 1)])

        with pytest.raises(MemoryError, match="out of memory"):
            Engine().run(_ctx(tx), checkpoint=fail_checkpoint)

        assert tx.status is Status.FAILED
        assert tx.history[-1].event is HistoryEvent.TRANSACTION_COMPLETED
        assert tx.outcome_category is OutcomeCategory.INTERRUPTED
        assert tx.retry_disposition is RetryDisposition.UNKNOWN

    def test_stopping_business_exception_checkpoints_downstream_skips_as_batch(self) -> None:
        checkpoints: list[list[Status]] = []

        tx = Transaction(
            reference="T1",
            skills=[
                StoppingBusinessFailSkill("validate", 1),
                SuccessSkill("write", 2),
                SuccessSkill("notify", 3),
            ],
        )

        def checkpoint(transaction: Transaction) -> None:
            if transaction.history[-1].event is HistoryEvent.SKILL_SKIPPED:
                checkpoints.append([skill.status for skill in transaction.skills])

        Engine().run(_ctx(tx), checkpoint=checkpoint)

        assert checkpoints == [[Status.FAILED, Status.SKIPPED, Status.SKIPPED]]

    def test_in_memory_run_still_works_without_checkpoint(self) -> None:
        tx = Transaction(reference="T1", skills=[SuccessSkill("a", 1)])

        Engine().run(_ctx(tx))

        assert tx.status is Status.SUCCESSFUL
        assert tx.state == {"a": "done"}

    def test_subprocess_exit_after_successful_skill_leaves_checkpoint(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")
        script = textwrap.dedent(
            """
            import os
            import sys

            from rpacore.context import ProcessContext
            from rpacore.engine import Engine
            from rpacore.persistence import save_transaction
            from rpacore.skill import Skill
            from rpacore.transaction import Transaction

            class FirstSkill(Skill):
                def execute(self, ctx):
                    ctx.state["first"] = "done"

            class CrashSkill(Skill):
                def execute(self, ctx):
                    os._exit(7)

            tx = Transaction(
                reference="crash",
                id="crash-tx",
                skills=[FirstSkill("first", 1), CrashSkill("crash", 2)],
            )
            Engine().run(
                ProcessContext(transaction=tx),
                checkpoint=lambda transaction: save_transaction(transaction, sys.argv[1]),
            )
            """
        )

        result = subprocess.run([sys.executable, "-c", script, db_path], check=False)

        assert result.returncode == 7
        loaded = load_transaction("crash-tx", db_path)
        assert loaded.skills[0].status is Status.SUCCESSFUL
        assert loaded.skills[1].status is Status.IN_PROGRESS
        assert loaded.state == {"first": "done"}
        assert HistoryEvent.SKILL_SUCCEEDED in [entry.event for entry in loaded.history]

    def test_subprocess_resume_after_crash_does_not_repeat_successful_skill(self, tmp_path) -> None:
        db_path = str(tmp_path / "transactions.db")
        log_path = str(tmp_path / "runs.txt")
        first_script = textwrap.dedent(
            """
            import os
            import sys
            from pathlib import Path

            from rpacore.context import ProcessContext
            from rpacore.engine import Engine
            from rpacore.persistence import save_transaction
            from rpacore.skill import Skill
            from rpacore.transaction import Transaction

            class FirstSkill(Skill):
                def execute(self, ctx):
                    with Path(sys.argv[2]).open("a", encoding="utf-8") as log:
                        log.write("first\\n")
                    ctx.state["first"] = "done"

            class CrashSkill(Skill):
                def execute(self, ctx):
                    os._exit(7)

            tx = Transaction(
                reference="crash",
                id="crash-tx",
                skills=[FirstSkill("first", 1), CrashSkill("crash", 2)],
            )
            Engine().run(
                ProcessContext(transaction=tx),
                checkpoint=lambda transaction: save_transaction(transaction, sys.argv[1]),
            )
            """
        )
        resume_script = textwrap.dedent(
            """
            import sys
            from pathlib import Path

            from rpacore.context import ProcessContext
            from rpacore.engine import Engine
            from rpacore.persistence import save_transaction
            from rpacore.recovery import resume_transaction
            from rpacore.skill import Skill

            class FirstSkill(Skill):
                def execute(self, ctx):
                    with Path(sys.argv[2]).open("a", encoding="utf-8") as log:
                        log.write("first\\n")
                    ctx.state["first"] = "rerun"

            class CompleteSkill(Skill):
                def execute(self, ctx):
                    with Path(sys.argv[2]).open("a", encoding="utf-8") as log:
                        log.write("second\\n")
                    ctx.state["second"] = "done"

            tx = resume_transaction(
                "crash-tx",
                [FirstSkill("first", 1), CompleteSkill("crash", 2)],
                db_path=sys.argv[1],
            )
            Engine().run(
                ProcessContext(transaction=tx),
                checkpoint=lambda transaction: save_transaction(transaction, sys.argv[1]),
            )
            """
        )

        first = subprocess.run(
            [sys.executable, "-c", first_script, db_path, log_path],
            check=False,
        )
        assert first.returncode == 7
        assert open(log_path, encoding="utf-8").read().splitlines() == ["first"]

        resumed = subprocess.run(
            [sys.executable, "-c", resume_script, db_path, log_path],
            check=False,
        )

        assert resumed.returncode == 0
        assert open(log_path, encoding="utf-8").read().splitlines() == [
            "first",
            "second",
        ]
        loaded = load_transaction("crash-tx", db_path)
        assert loaded.status is Status.SUCCESSFUL
        assert loaded.state == {"first": "done", "second": "done"}
        first_skill_succeeded = [
            entry
            for entry in loaded.history
            if (
                entry.event is HistoryEvent.SKILL_SUCCEEDED
                and entry.skill_name == "first"
                and entry.skill_execution_order == 1
            )
        ]
        first_skill_started = [
            entry
            for entry in loaded.history
            if (
                entry.event is HistoryEvent.SKILL_STARTED
                and entry.skill_name == "first"
                and entry.skill_execution_order == 1
            )
        ]
        assert len(first_skill_succeeded) == 1
        assert len(first_skill_started) == 1


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

    def test_memory_error_propagates(self) -> None:
        class MemoryFailSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise MemoryError("out of memory")

        skill = MemoryFailSkill("allocate", 1)
        tx = Transaction(reference="T1", skills=[skill])

        with pytest.raises(MemoryError, match="out of memory"):
            Engine().run(_ctx(tx))

        assert skill.exceptions == []
        assert skill.status is Status.FAILED
        assert tx.status is Status.FAILED
        assert tx.finished_at is not None
        assert [entry.event for entry in tx.history] == [
            HistoryEvent.TRANSACTION_STARTED,
            HistoryEvent.SKILL_STARTED,
            HistoryEvent.SKILL_INTERRUPTED,
            HistoryEvent.TRANSACTION_COMPLETED,
        ]
        assert tx.history[-1].status is Status.FAILED

    def test_screenshot_memory_error_preserves_business_failure_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail_screenshot(directory: str) -> str:
            raise MemoryError("out of memory")

        skill = BusinessFailSkill("validate", 1)
        tx = Transaction(reference="T1", skills=[skill])
        monkeypatch.setattr("rpacore.engine.capture_screenshot", _fail_screenshot)

        with pytest.raises(MemoryError, match="out of memory"):
            Engine(screenshot_dir="screenshots").run(_ctx(tx))

        assert tx.status is Status.FAILED
        assert tx.finished_at is not None
        assert skill.status is Status.FAILED
        assert len(skill.exceptions) == 1
        assert isinstance(skill.exceptions[0], BusinessException)
        assert tx.history[-1].event is HistoryEvent.TRANSACTION_COMPLETED
        assert tx.history[-1].status is Status.FAILED

    def test_captured_screenshot_is_registered_as_artifact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _capture_screenshot(directory: str) -> str:
            return "screenshots/shot.png"

        skill = BusinessFailSkill("validate", 1)
        tx = Transaction(reference="T1", skills=[skill])
        monkeypatch.setattr("rpacore.engine.capture_screenshot", _capture_screenshot)

        Engine(screenshot_dir="screenshots").run(_ctx(tx))

        assert skill.exceptions[0].screenshot_path == "screenshots/shot.png"
        assert len(tx.artifacts) == 1
        artifact = tx.artifacts[0]
        assert artifact.name == "validate screenshot"
        assert artifact.path == "screenshots/shot.png"
        assert artifact.kind == "screenshot"
        assert artifact.metadata == {
            "skill_name": "validate",
            "skill_execution_order": 1,
        }

    def test_system_exception_screenshot_is_registered_as_artifact(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _capture_screenshot(directory: str) -> str:
            return "screenshots/system.png"

        skill = SystemFailSkill("connect", 1)
        tx = Transaction(reference="T1", skills=[skill])
        monkeypatch.setattr("rpacore.engine.capture_screenshot", _capture_screenshot)

        Engine(screenshot_dir="screenshots").run(_ctx(tx))

        assert skill.exceptions[0].screenshot_path == "screenshots/system.png"
        assert [(artifact.kind, artifact.path) for artifact in tx.artifacts] == [
            ("screenshot", "screenshots/system.png")
        ]

    def test_generic_exception_screenshot_is_registered_as_artifact(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _capture_screenshot(directory: str) -> str:
            return "screenshots/generic.png"

        class GenericFailSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise RuntimeError("boom")

        skill = GenericFailSkill("generic", 1)
        tx = Transaction(reference="T1", skills=[skill])
        monkeypatch.setattr("rpacore.engine.capture_screenshot", _capture_screenshot)

        Engine(screenshot_dir="screenshots").run(_ctx(tx))

        assert skill.exceptions[0].screenshot_path == "screenshots/generic.png"
        assert [(artifact.kind, artifact.path) for artifact in tx.artifacts] == [
            ("screenshot", "screenshots/generic.png")
        ]


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
        assert HistoryEvent.RETRY_SCHEDULED in [entry.event for entry in tx.history]

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
        assert tx.outcome_category is OutcomeCategory.SYSTEM_FAILED
        assert tx.retry_disposition is RetryDisposition.RETRY_EXHAUSTED

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
