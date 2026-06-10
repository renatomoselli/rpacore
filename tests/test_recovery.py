"""Tests for rpacore.recovery."""

import pytest

from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, SystemException
from rpacore.persistence import load_transaction, save_transaction
from rpacore.recovery import resume_transaction
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import HistoryEvent, Transaction


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


class _TrackingSkill(Skill):
    def __init__(self, name: str, execution_order: int, counts: dict[str, int]) -> None:
        super().__init__(name, execution_order)
        self._counts = counts

    def execute(self, ctx: ProcessContext) -> None:
        self._counts[self.name] = self._counts.get(self.name, 0) + 1


def test_resume_reruns_only_non_successful_skills(db_path) -> None:
    counts: dict[str, int] = {}

    first = Skill("first", 1)
    first.status = Status.SUCCESSFUL
    second = Skill("second", 2)
    second.status = Status.FAILED
    second.exceptions.append(SystemException("timeout", action="second"))
    third = Skill("third", 3)
    third.status = Status.PENDING

    tx = Transaction(
        reference="REF-001",
        status=Status.FAILED,
        skills=[first, second, third],
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingSkill("first", 1, counts),
            _TrackingSkill("second", 2, counts),
            _TrackingSkill("third", 3, counts),
        ],
        db_path=db_path,
    )

    assert resumed.status is Status.PENDING
    assert resumed.started_at is None
    assert resumed.finished_at is None
    assert resumed.history[-1].event is HistoryEvent.TRANSACTION_RESUMED
    assert [skill.status for skill in resumed.skills] == [
        Status.SUCCESSFUL,
        Status.PENDING,
        Status.PENDING,
    ]
    assert len(resumed.skills[1].exceptions) == 1

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"second": 1, "third": 1}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_resets_retry_count_for_new_engine_run(db_path) -> None:
    failed = Skill("failed", 1)
    failed.status = Status.FAILED
    failed.exceptions.append(SystemException("timeout", action="failed"))
    tx = Transaction(
        reference="REF-001",
        status=Status.FAILED,
        retry_count=3,
        skills=[failed],
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [_TrackingSkill("failed", 1, {})],
        db_path=db_path,
    )

    assert resumed.retry_count == 0


def test_load_shows_interrupted_state_before_explicit_resume(db_path) -> None:
    first = Skill("first", 1)
    first.status = Status.SUCCESSFUL
    second = Skill("second", 2)
    second.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        skills=[first, second],
        state={"first": "done"},
    )
    save_transaction(tx, db_path)

    loaded = load_transaction(tx.id, db_path)

    assert loaded.status is Status.IN_PROGRESS
    assert [skill.status for skill in loaded.skills] == [
        Status.SUCCESSFUL,
        Status.IN_PROGRESS,
    ]
    assert loaded.state == {"first": "done"}


def test_resume_interrupted_transaction_skips_successful_and_preserves_state(db_path) -> None:
    counts: dict[str, int] = {}
    first = Skill("first", 1)
    first.status = Status.SUCCESSFUL
    second = Skill("second", 2)
    second.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        skills=[first, second],
        state={"first": "done"},
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingSkill("first", 1, counts),
            _TrackingSkill("second", 2, counts),
        ],
        db_path=db_path,
    )

    assert resumed.status is Status.PENDING
    assert resumed.state == {"first": "done"}
    assert [skill.status for skill in resumed.skills] == [
        Status.SUCCESSFUL,
        Status.PENDING,
    ]

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"second": 1}
    assert resumed.state == {"first": "done"}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_interrupted_transaction_preserves_skipped_skills(db_path) -> None:
    counts: dict[str, int] = {}
    skipped = Skill("optional", 1)
    skipped.status = Status.SKIPPED
    running = Skill("main", 2)
    running.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        skills=[skipped, running],
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingSkill("optional", 1, counts),
            _TrackingSkill("main", 2, counts),
        ],
        db_path=db_path,
    )

    assert [skill.status for skill in resumed.skills] == [
        Status.SKIPPED,
        Status.PENDING,
    ]

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"main": 1}


def test_repeated_resume_does_not_duplicate_resume_history(db_path) -> None:
    running = Skill("main", 1)
    running.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        skills=[running],
    )
    save_transaction(tx, db_path)

    first = resume_transaction(
        tx.id,
        [_TrackingSkill("main", 1, {})],
        db_path=db_path,
    )
    save_transaction(first, db_path)
    second = resume_transaction(
        tx.id,
        [_TrackingSkill("main", 1, {})],
        db_path=db_path,
    )

    assert [entry.event for entry in second.history].count(
        HistoryEvent.TRANSACTION_RESUMED
    ) == 1


def test_repeated_resume_preserves_skipped_recovery_decision(db_path) -> None:
    skipped = Skill("optional", 1)
    skipped.status = Status.SKIPPED
    running = Skill("main", 2)
    running.status = Status.IN_PROGRESS
    tx = Transaction(
        reference="REF-001",
        status=Status.IN_PROGRESS,
        skills=[skipped, running],
    )
    save_transaction(tx, db_path)

    first = resume_transaction(
        tx.id,
        [
            _TrackingSkill("optional", 1, {}),
            _TrackingSkill("main", 2, {}),
        ],
        db_path=db_path,
    )
    save_transaction(first, db_path)
    second = resume_transaction(
        tx.id,
        [
            _TrackingSkill("optional", 1, {}),
            _TrackingSkill("main", 2, {}),
        ],
        db_path=db_path,
    )

    assert [skill.status for skill in second.skills] == [
        Status.SKIPPED,
        Status.PENDING,
    ]


def test_resume_does_not_rerun_business_failed_skills(db_path) -> None:
    counts: dict[str, int] = {}

    failed = Skill("failed", 1)
    failed.status = Status.FAILED
    failed.exceptions.append(BusinessException("bad data", action="failed"))
    next_skill = Skill("next", 2)
    next_skill.status = Status.PENDING

    tx = Transaction(
        reference="REF-001",
        status=Status.FAILED,
        skills=[failed, next_skill],
    )
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingSkill("failed", 1, counts),
            _TrackingSkill("next", 2, counts),
        ],
        db_path=db_path,
    )

    assert resumed.skills[0].status is Status.FAILED
    assert resumed.skills[1].status is Status.PENDING

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"next": 1}
    assert resumed.skills[0].status is Status.FAILED
    assert resumed.status is Status.FAILED


def test_resume_can_retry_business_failed_skills_when_policy_allows_it(db_path) -> None:
    counts: dict[str, int] = {}
    failed = Skill("failed", 1)
    failed.status = Status.FAILED
    failed.exceptions.append(BusinessException("bad data", action="failed"))
    tx = Transaction(reference="REF-001", status=Status.FAILED, skills=[failed])
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [_TrackingSkill("failed", 1, counts)],
        db_path=db_path,
        retry_business_failures=True,
    )

    assert resumed.skills[0].status is Status.PENDING

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"failed": 1}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_successful_transaction_is_effective_no_op(db_path) -> None:
    counts: dict[str, int] = {}

    skill = Skill("done", 1)
    skill.status = Status.SUCCESSFUL
    tx = Transaction(reference="REF-001", status=Status.SUCCESSFUL, skills=[skill])
    tx.started_at = tx.created_at
    tx.finished_at = tx.created_at
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [_TrackingSkill("done", 1, counts)],
        db_path=db_path,
    )

    assert resumed.status is Status.SUCCESSFUL
    assert resumed.skills[0].status is Status.SUCCESSFUL
    assert resumed.started_at == tx.started_at
    assert resumed.finished_at == tx.finished_at

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_missing_skill_mapping_raises_clear_error(db_path) -> None:
    tx = Transaction(reference="REF-001", skills=[Skill("only", 1)])
    save_transaction(tx, db_path)

    with pytest.raises(KeyError, match="Missing recovery skill"):
        resume_transaction(tx.id, [], db_path=db_path)


def test_resume_duplicate_skill_mapping_raises_clear_error(db_path) -> None:
    tx = Transaction(reference="REF-001", skills=[Skill("dup", 1)])
    save_transaction(tx, db_path)

    with pytest.raises(ValueError, match="Duplicate recovery skill provided"):
        resume_transaction(
            tx.id,
            [Skill("dup", 1), Skill("dup", 1)],
            db_path=db_path,
        )


def test_resume_extra_skill_mapping_raises_clear_error(db_path) -> None:
    tx = Transaction(reference="REF-001", skills=[Skill("persisted", 1)])
    save_transaction(tx, db_path)

    with pytest.raises(KeyError, match="did not match persisted transaction"):
        resume_transaction(
            tx.id,
            [Skill("persisted", 1), Skill("extra", 2)],
            db_path=db_path,
        )


def test_resume_resets_skipped_skills_to_pending(db_path) -> None:
    counts: dict[str, int] = {}

    skipped = Skill("optional", 1)
    skipped.status = Status.SKIPPED
    pending = Skill("main", 2)
    pending.status = Status.PENDING

    tx = Transaction(reference="REF-001", status=Status.FAILED, skills=[skipped, pending])
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [
            _TrackingSkill("optional", 1, counts),
            _TrackingSkill("main", 2, counts),
        ],
        db_path=db_path,
    )

    assert resumed.skills[0].status is Status.PENDING
    assert resumed.skills[1].status is Status.PENDING

    Engine().run(ProcessContext(transaction=resumed))

    assert counts.get("optional", 0) == 1
    assert counts.get("main", 0) == 1
    assert resumed.status is Status.SUCCESSFUL


def test_resume_copies_exception_objects(db_path) -> None:
    failed = Skill("failed", 1)
    failed.status = Status.FAILED
    original = SystemException("timeout", action="failed")
    failed.exceptions.append(original)

    tx = Transaction(reference="REF-001", status=Status.FAILED, skills=[failed])
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [Skill("failed", 1)],
        db_path=db_path,
    )

    recovered = resumed.skills[0].exceptions[0]
    assert recovered is not original
    assert str(recovered) == str(original)
    assert recovered.action == original.action

    recovered.action = "mutated"
    assert original.action == "failed"
