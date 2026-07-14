"""Tests for rpacore.transaction."""

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest

from rpacore.exceptions import ExecutionValidationError
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Artifact, HistoryEntry, HistoryEvent, Transaction


class TestTransactionFreshState:
    """A freshly created Transaction has predictable defaults."""

    def test_reference(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.reference == "INV-001"

    def test_id_is_auto_generated_uuid(self) -> None:
        tx = Transaction(reference="INV-001")
        parsed = uuid.UUID(tx.id)
        assert str(parsed) == tx.id
        assert tx.id == tx.id.lower()

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

    def test_state_defaults_to_empty_dict(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.state == {}

    def test_created_at_defaults_to_utc_timestamp(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.created_at is not None
        assert tx.created_at.tzinfo is not None

    def test_started_and_finished_default_to_unknown(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.started_at is None
        assert tx.finished_at is None

    def test_history_defaults_to_empty_list(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.history == []

    def test_metadata_defaults_to_empty_dict(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.metadata == {}

    def test_artifacts_defaults_to_empty_list(self) -> None:
        tx = Transaction(reference="INV-001")
        assert tx.artifacts == []

    def test_skills_not_shared_between_instances(self) -> None:
        a = Transaction(reference="A")
        b = Transaction(reference="B")
        a.skills.append(Skill("login", 1))
        assert len(b.skills) == 0

    def test_state_not_shared_between_instances(self) -> None:
        a = Transaction(reference="A")
        b = Transaction(reference="B")
        a.state["invoice"] = 42
        assert b.state == {}

    def test_history_not_shared_between_instances(self) -> None:
        a = Transaction(reference="A")
        b = Transaction(reference="B")
        a.append_history(HistoryEvent.TRANSACTION_STARTED)
        assert b.history == []

    def test_artifacts_not_shared_between_instances(self) -> None:
        a = Transaction(reference="A")
        b = Transaction(reference="B")
        a.artifacts.append(Artifact(name="invoice", path="missing.pdf"))
        assert b.artifacts == []

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

    def test_custom_state(self) -> None:
        tx = Transaction(reference="INV-001", state={"invoice": 42})
        assert tx.state == {"invoice": 42}

    def test_custom_state_does_not_keep_reference(self) -> None:
        state = {"invoice": 42}
        tx = Transaction(reference="INV-001", state=state)
        tx.state["status"] = "ready"
        assert state == {"invoice": 42}

    def test_custom_artifacts_does_not_keep_list_reference(self) -> None:
        artifacts = [Artifact(name="invoice", path="invoice.pdf")]
        tx = Transaction(reference="INV-001", artifacts=artifacts)

        artifacts.append(Artifact(name="receipt", path="receipt.pdf"))

        assert len(tx.artifacts) == 1

    def test_artifact_id_is_auto_generated_uuid(self) -> None:
        artifact = Artifact(name="invoice", path="invoice.pdf")

        parsed = uuid.UUID(artifact.id)

        assert str(parsed) == artifact.id

    def test_artifact_metadata_does_not_keep_reference(self) -> None:
        metadata = {"source": "skill"}

        artifact = Artifact(name="invoice", path="invoice.pdf", metadata=metadata)
        artifact.metadata["status"] = "created"

        assert metadata == {"source": "skill"}

    def test_artifact_metadata_accepts_none_as_empty_mapping(self) -> None:
        artifact = Artifact(name="invoice", path="invoice.pdf", metadata=None)  # type: ignore[arg-type]

        assert artifact.metadata == {}

    def test_append_history_uses_monotonic_transaction_sequence(self) -> None:
        tx = Transaction(reference="INV-001")
        first = tx.append_history(HistoryEvent.TRANSACTION_STARTED)
        second = tx.append_history(HistoryEvent.TRANSACTION_COMPLETED)

        assert [entry.sequence for entry in tx.history] == [1, 2]
        assert first.event is HistoryEvent.TRANSACTION_STARTED
        assert second.event is HistoryEvent.TRANSACTION_COMPLETED
        assert first.status is Status.PENDING

    def test_append_history_uses_explicit_timestamp(self) -> None:
        tx = Transaction(reference="INV-001")
        fixed_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

        entry = tx.append_history(HistoryEvent.TRANSACTION_STARTED, timestamp=fixed_time)

        assert entry.timestamp is fixed_time

    def test_append_history_uses_explicit_status(self) -> None:
        tx = Transaction(reference="INV-001")

        entry = tx.append_history(HistoryEvent.TRANSACTION_STARTED, status=Status.FAILED)

        assert entry.status is Status.FAILED

    def test_history_entry_is_immutable(self) -> None:
        entry = HistoryEntry(
            sequence=1,
            timestamp=datetime.now(timezone.utc),
            event=HistoryEvent.TRANSACTION_STARTED,
            status=Status.PENDING,
            retry_number=0,
        )

        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.sequence = 2  # type: ignore[misc]

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


class TestTransactionExecutionValidation:
    def test_valid_transaction_passes(self) -> None:
        tx = Transaction(reference="INV-001", skills=[Skill("login", 1)])
        tx.validate_for_execution()

    @pytest.mark.parametrize("reference", ["", "   "])
    def test_blank_reference_raises(self, reference: str) -> None:
        tx = Transaction(reference=reference)
        with pytest.raises(ExecutionValidationError, match="transaction.reference"):
            tx.validate_for_execution()

    def test_non_string_reference_raises(self) -> None:
        tx = Transaction(reference=123)  # type: ignore[arg-type]
        with pytest.raises(ExecutionValidationError, match="transaction.reference"):
            tx.validate_for_execution()

    @pytest.mark.parametrize("name", ["", "   "])
    def test_blank_skill_name_raises(self, name: str) -> None:
        tx = Transaction(reference="INV-001", skills=[Skill(name, 1)])
        with pytest.raises(ExecutionValidationError, match="skill.name"):
            tx.validate_for_execution()

    def test_duplicate_skill_name_raises(self) -> None:
        tx = Transaction(reference="INV-001", skills=[Skill("login", 1), Skill("login", 2)])
        with pytest.raises(ExecutionValidationError, match="skill.name must be unique"):
            tx.validate_for_execution()

    @pytest.mark.parametrize("execution_order", [0, -1])
    def test_non_positive_execution_order_raises(self, execution_order: int) -> None:
        tx = Transaction(reference="INV-001", skills=[Skill("login", execution_order)])
        with pytest.raises(ExecutionValidationError, match="skill.execution_order"):
            tx.validate_for_execution()

    @pytest.mark.parametrize("execution_order", [True, "1"])
    def test_non_integer_execution_order_raises(self, execution_order: object) -> None:
        tx = Transaction(reference="INV-001", skills=[Skill("login", execution_order)])  # type: ignore[arg-type]
        with pytest.raises(ExecutionValidationError, match="skill.execution_order"):
            tx.validate_for_execution()

    def test_duplicate_execution_order_raises(self) -> None:
        tx = Transaction(reference="INV-001", skills=[Skill("login", 1), Skill("submit", 1)])
        with pytest.raises(ExecutionValidationError, match="skill.execution_order must be unique"):
            tx.validate_for_execution()

    def test_non_json_safe_skill_arguments_raise_before_execution(self) -> None:
        tx = Transaction(
            reference="INV-001",
            skills=[Skill("submit", 1, arguments={"ids": (1, 2)})],
        )

        with pytest.raises(
            TypeError,
            match=r"transaction\.skills\['submit'\]\.arguments\['ids'\]",
        ):
            tx.validate_for_execution()
