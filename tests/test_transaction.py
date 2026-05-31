"""Tests for rpacore.transaction."""

import uuid

import pytest

from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction


class TestTransactionFreshState:
    """A freshly created Transaction has predictable defaults."""

    def test_reference(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.reference == "INV-001"

    def test_id_is_auto_generated_uuid(self) -> None:
        tx = Transaction(reference="INV-001")
        parsed = uuid.UUID(tx.id)
        assert str(parsed) == tx.id

    def test_id_is_unique_per_instance(self) -> None:
        a = Transaction(reference="A")
        b = Transaction(reference="B")
        assert a.id != b.id

    def test_status_defaults_to_pending(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.status is Status.PENDING

    def test_retry_count_defaults_to_zero(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.retry_count == 0

    def test_skills_defaults_to_empty_list(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.skills == []

    def test_skills_not_shared_between_instances(self) -> None:
        a = Transaction(reference="A")
        b = Transaction(reference="B")
        a.skills.append(Skill("login", 1))
        assert len(b.skills) == 0

    def test_negative_retry_count_raises(self) -> None:
        with pytest.raises(ValueError, match="retry_count must be >= 0"):
            Transaction(reference="INV-001", retry_count=-1)


class TestTransactionCustomValues:
    def test_custom_id(self) -> None:
        tx = Transaction(reference="INV-001", id="custom-id")
        assert tx.id == "custom-id"

    def test_custom_retry_count(self) -> None:
        tx = Transaction(reference="INV-001", retry_count=3)
        assert tx.retry_count == 3

    def test_custom_status(self) -> None:
        tx = Transaction(reference="INV-001", status=Status.IN_PROGRESS)
        assert tx.status is Status.IN_PROGRESS

    def test_custom_skills(self) -> None:
        skills = [Skill("a", 1), Skill("b", 2)]
        tx = Transaction(reference="INV-001", skills=skills)
        assert len(tx.skills) == 2

    def test_custom_skills_does_not_keep_reference(self) -> None:
        skills = [Skill("a", 1)]
        tx = Transaction(reference="INV-001", skills=skills)
        skills.append(Skill("b", 2))
        assert len(tx.skills) == 1


class TestTransactionOrderedSkills:
    def test_returns_skills_sorted_by_execution_order(self) -> None:
        tx = Transaction(
            reference="INV-001",
            skills=[Skill("c", 3), Skill("a", 1), Skill("b", 2)],
        )
        ordered = tx.ordered_skills()
        assert [s.name for s in ordered] == ["a", "b", "c"]

    def test_does_not_mutate_original_list(self) -> None:
        tx = Transaction(
            reference="INV-001",
            skills=[Skill("c", 3), Skill("a", 1)],
        )
        tx.ordered_skills()
        assert tx.skills[0].name == "c"

    def test_empty_skills_returns_empty(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.ordered_skills() == []


class TestTransactionFailedSkills:
    def test_returns_only_failed_skills(self) -> None:
        s1 = Skill("a", 1)
        s2 = Skill("b", 2)
        s3 = Skill("c", 3)
        s1.status = Status.SUCCESSFUL
        s2.status = Status.FAILED
        s3.status = Status.FAILED
        tx = Transaction(reference="INV-001", skills=[s1, s2, s3])
        failed = tx.failed_skills()
        assert [s.name for s in failed] == ["b", "c"]

    def test_no_failures_returns_empty(self) -> None:
        s1 = Skill("a", 1)
        s1.status = Status.SUCCESSFUL
        tx = Transaction(reference="INV-001", skills=[s1])
        assert tx.failed_skills() == []

    def test_empty_skills_returns_empty(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.failed_skills() == []


class TestTransactionStatusTransitions:
    def test_status_can_be_set_to_in_progress(self) -> None:
        tx = Transaction(reference="INV-001")
        tx.status = Status.IN_PROGRESS
        assert tx.status is Status.IN_PROGRESS

    def test_status_can_be_set_to_successful(self) -> None:
        tx = Transaction(reference="INV-001")
        tx.status = Status.SUCCESSFUL
        assert tx.status is Status.SUCCESSFUL

    def test_status_can_be_set_to_failed(self) -> None:
        tx = Transaction(reference="INV-001")
        tx.status = Status.FAILED
        assert tx.status is Status.FAILED
