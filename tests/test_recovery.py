"""Tests for rpacore.recovery."""

import pytest

from rpacore.context import ProcessContext
from rpacore.engine import Engine
from rpacore.exceptions import SystemException
from rpacore.persistence import save_transaction
from rpacore.recovery import resume_transaction
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction


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
    assert [skill.status for skill in resumed.skills] == [
        Status.SUCCESSFUL,
        Status.PENDING,
        Status.PENDING,
    ]
    assert len(resumed.skills[1].exceptions) == 1

    Engine().run(ProcessContext(transaction=resumed))

    assert counts == {"second": 1, "third": 1}
    assert resumed.status is Status.SUCCESSFUL


def test_resume_successful_transaction_is_effective_no_op(db_path) -> None:
    counts: dict[str, int] = {}

    skill = Skill("done", 1)
    skill.status = Status.SUCCESSFUL
    tx = Transaction(reference="REF-001", status=Status.SUCCESSFUL, skills=[skill])
    save_transaction(tx, db_path)

    resumed = resume_transaction(
        tx.id,
        [_TrackingSkill("done", 1, counts)],
        db_path=db_path,
    )

    assert resumed.status is Status.SUCCESSFUL
    assert resumed.skills[0].status is Status.SUCCESSFUL

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
