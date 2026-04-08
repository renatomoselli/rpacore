"""Tests for oref.persistence."""

import sqlite3

import pytest

from oref.exceptions import BusinessException, SystemException
from oref.persistence import load_transaction, save_transaction
from oref.skill import Skill
from oref.status import Status
from oref.transaction import Transaction


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def make_transaction(**kwargs) -> Transaction:
    return Transaction(reference="REF-001", **kwargs)


class TestSaveAndLoad:
    def test_roundtrip_empty_transaction(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.id == tx.id
        assert loaded.reference == tx.reference
        assert loaded.status is Status.PENDING
        assert loaded.retry_count == 0
        assert loaded.skills == []

    def test_roundtrip_preserves_status(self, db_path) -> None:
        tx = make_transaction(status=Status.SUCCESSFUL)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.status is Status.SUCCESSFUL

    def test_roundtrip_preserves_retry_count(self, db_path) -> None:
        tx = make_transaction(retry_count=3)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.retry_count == 3

    def test_roundtrip_preserves_skills(self, db_path) -> None:
        s1 = Skill("login", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Skill("fetch", 2)
        s2.status = Status.FAILED
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.skills) == 2
        assert loaded.skills[0].name == "login"
        assert loaded.skills[0].status is Status.SUCCESSFUL
        assert loaded.skills[1].name == "fetch"
        assert loaded.skills[1].status is Status.FAILED

    def test_roundtrip_preserves_skill_execution_order(self, db_path) -> None:
        s1 = Skill("a", 3)
        s2 = Skill("b", 1)
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].name == "b"
        assert loaded.skills[0].execution_order == 1
        assert loaded.skills[1].name == "a"
        assert loaded.skills[1].execution_order == 3

    def test_roundtrip_preserves_business_exception(self, db_path) -> None:
        skill = Skill("validate", 1)
        exc = BusinessException("bad data", action="validate", retry_number=0)
        skill.exceptions.append(exc)
        skill.status = Status.FAILED
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.skills[0].exceptions) == 1
        loaded_exc = loaded.skills[0].exceptions[0]
        assert isinstance(loaded_exc, BusinessException)
        assert str(loaded_exc) == "bad data"
        assert loaded_exc.action == "validate"
        assert loaded_exc.retry_number == 0

    def test_roundtrip_preserves_system_exception(self, db_path) -> None:
        skill = Skill("connect", 1)
        exc = SystemException("timeout", action="connect", retry_number=1)
        skill.exceptions.append(exc)
        skill.status = Status.FAILED
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        loaded_exc = loaded.skills[0].exceptions[0]
        assert isinstance(loaded_exc, SystemException)
        assert str(loaded_exc) == "timeout"
        assert loaded_exc.retry_number == 1

    def test_roundtrip_preserves_multiple_exceptions(self, db_path) -> None:
        skill = Skill("connect", 1)
        skill.exceptions.append(SystemException("timeout", action="connect", retry_number=0))
        skill.exceptions.append(SystemException("timeout", action="connect", retry_number=1))
        skill.status = Status.FAILED
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.skills[0].exceptions) == 2
        assert loaded.skills[0].exceptions[0].retry_number == 0
        assert loaded.skills[0].exceptions[1].retry_number == 1

    def test_not_found_raises_key_error(self, db_path) -> None:
        with pytest.raises(KeyError, match="not-a-real-id"):
            load_transaction("not-a-real-id", db_path)

    def test_save_is_idempotent(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.id == tx.id

    def test_save_updates_existing_transaction(self, db_path) -> None:
        tx = make_transaction()
        save_transaction(tx, db_path)
        tx.status = Status.SUCCESSFUL
        tx.retry_count = 2
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.status is Status.SUCCESSFUL
        assert loaded.retry_count == 2

    def test_removed_skill_deleted_on_resave(self, db_path) -> None:
        s1 = Skill("a", 1)
        s2 = Skill("b", 2)
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        tx.skills.remove(s2)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert len(loaded.skills) == 1
        assert loaded.skills[0].name == "a"

    def test_roundtrip_preserves_arguments(self, db_path) -> None:
        skill = Skill("login", 1, arguments={"user": "admin", "timeout": 30})
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].arguments == {"user": "admin", "timeout": 30}

    def test_duplicate_skill_name_and_order_raises(self, db_path) -> None:
        s1 = Skill("dup", 1)
        s2 = Skill("dup", 1)
        tx = make_transaction(skills=[s1, s2])
        with pytest.raises(sqlite3.IntegrityError):
            save_transaction(tx, db_path)


class TestCrashRecovery:
    def test_in_progress_skill_loaded_as_failed(self, db_path) -> None:
        skill = Skill("process", 1)
        skill.status = Status.IN_PROGRESS
        tx = make_transaction(skills=[skill])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].status is Status.FAILED

    def test_in_progress_transaction_loaded_as_failed(self, db_path) -> None:
        tx = make_transaction(status=Status.IN_PROGRESS)
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.status is Status.FAILED

    def test_successful_skills_not_affected_by_crash_recovery(self, db_path) -> None:
        s1 = Skill("login", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Skill("process", 2)
        s2.status = Status.IN_PROGRESS
        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)
        loaded = load_transaction(tx.id, db_path)
        assert loaded.skills[0].status is Status.SUCCESSFUL
        assert loaded.skills[1].status is Status.FAILED


class TestResumeScenario:
    def test_resume_skips_successful_skills(self, db_path) -> None:
        from oref.engine import Engine

        # Save a transaction where skill "a" already succeeded and "b" is pending.
        s1 = Skill("a", 1)
        s1.status = Status.SUCCESSFUL
        s2 = Skill("b", 2)
        s2.status = Status.PENDING

        tx = make_transaction(skills=[s1, s2])
        save_transaction(tx, db_path)

        # Reload and run with a concrete subclass wired to the same names.
        counts: dict[str, int] = {"a": 0, "b": 0}

        class TrackSkill(Skill):
            def execute(self, context: dict[str, object]) -> None:
                counts[self.name] += 1

        loaded = load_transaction(tx.id, db_path)
        # Replace plain Skill instances with executable ones, preserving status.
        for i, skill in enumerate(loaded.skills):
            track = TrackSkill(skill.name, skill.execution_order)
            track.status = skill.status
            track.exceptions = skill.exceptions
            loaded.skills[i] = track

        Engine().run(loaded)

        assert counts["a"] == 0  # already successful, engine skips it
        assert counts["b"] == 1
        assert loaded.status is Status.SUCCESSFUL
