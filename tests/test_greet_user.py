"""Integration and idempotency tests for the greet_user example skills."""

from __future__ import annotations

import pytest

from oref import Engine, Status, Transaction
from oref.exceptions import BusinessException, SystemException
from skills.greet_user import ConfirmOutput, ValidateInput, WriteGreeting


def _transaction(tmp_path, *, name: str | None = "Alice") -> Transaction:
    """Build a standard greet-user Transaction pointing at a tmp output file."""
    output_path = str(tmp_path / "greeting.txt")
    arguments: dict[str, object] = {"output_path": output_path}
    if name is not None:
        arguments["name"] = name

    tx = Transaction(reference="test-greet")
    tx.skills = [
        ValidateInput(name="validate_input", execution_order=1, arguments=arguments),
        WriteGreeting(name="write_greeting", execution_order=2, arguments=arguments),
        ConfirmOutput(name="confirm_output", execution_order=3, arguments=arguments),
    ]
    return tx


class TestHappyPath:
    def test_all_skills_succeed(self, tmp_path):
        tx = _transaction(tmp_path)
        Engine().run(tx)

        assert tx.status == Status.SUCCESSFUL
        for skill in tx.skills:
            assert skill.status == Status.SUCCESSFUL

    def test_greeting_file_contains_name(self, tmp_path):
        tx = _transaction(tmp_path)
        Engine().run(tx)

        output_path = tmp_path / "greeting.txt"
        assert output_path.read_text(encoding="utf-8") == "Hello, Alice\n"


class TestBusinessExceptionPath:
    def test_missing_name_fails_validate_input(self, tmp_path):
        tx = _transaction(tmp_path, name=None)
        Engine().run(tx)

        validate = tx.skills[0]
        assert validate.status == Status.FAILED
        assert isinstance(validate.exceptions[0], BusinessException)

    def test_execution_continues_after_business_exception(self, tmp_path):
        """Engine keeps running after BusinessException — downstream skills execute."""
        tx = _transaction(tmp_path, name=None)
        Engine().run(tx)

        # WriteGreeting ran (engine continued past ValidateInput's BusinessException)
        # but raised its own BusinessException because name is still missing.
        write = tx.skills[1]
        assert write.status == Status.FAILED
        assert isinstance(write.exceptions[0], BusinessException)

    def test_transaction_fails_when_any_skill_fails(self, tmp_path):
        tx = _transaction(tmp_path, name=None)
        Engine().run(tx)

        assert tx.status == Status.FAILED


class TestIdempotency:
    def test_write_greeting_is_idempotent(self, tmp_path):
        """Calling execute() twice produces the same file content without error."""
        output_path = tmp_path / "greeting.txt"
        skill = WriteGreeting(
            name="write_greeting",
            execution_order=1,
            arguments={"name": "Alice", "output_path": str(output_path)},
        )
        skill.execute({})
        skill.execute({})

        assert output_path.read_text(encoding="utf-8") == "Hello, Alice\n"

    def test_write_greeting_overwrites_stale_content(self, tmp_path):
        """Ensures desired state: a file with wrong content is overwritten."""
        output_path = tmp_path / "greeting.txt"
        output_path.write_text("stale\n", encoding="utf-8")

        skill = WriteGreeting(
            name="write_greeting",
            execution_order=1,
            arguments={"name": "Alice", "output_path": str(output_path)},
        )
        skill.execute({})

        assert output_path.read_text(encoding="utf-8") == "Hello, Alice\n"

    def test_write_greeting_skips_when_content_already_correct(self, tmp_path):
        """File already in desired state: execute() returns without error."""
        output_path = tmp_path / "greeting.txt"
        output_path.write_text("Hello, Alice\n", encoding="utf-8")

        skill = WriteGreeting(
            name="write_greeting",
            execution_order=1,
            arguments={"name": "Alice", "output_path": str(output_path)},
        )
        skill.execute({})

        assert output_path.read_text(encoding="utf-8") == "Hello, Alice\n"


class TestSystemExceptionPath:
    def test_confirm_output_raises_system_exception_when_file_missing(self, tmp_path):
        output_path = tmp_path / "missing.txt"
        skill = ConfirmOutput(
            name="confirm_output",
            execution_order=1,
            arguments={"output_path": str(output_path)},
        )
        with pytest.raises(SystemException):
            skill.execute({})

    def test_confirm_output_raises_system_exception_when_content_wrong(self, tmp_path):
        output_path = tmp_path / "greeting.txt"
        output_path.write_text("wrong content\n", encoding="utf-8")

        skill = ConfirmOutput(
            name="confirm_output",
            execution_order=1,
            arguments={"name": "Alice", "output_path": str(output_path)},
        )
        with pytest.raises(SystemException):
            skill.execute({})

    def test_transaction_fails_when_confirm_output_fails(self, tmp_path):
        """Transaction where WriteGreeting is replaced by a skill that does NOT
        create the file, so ConfirmOutput finds nothing."""
        output_path = str(tmp_path / "greeting.txt")
        arguments: dict[str, object] = {"name": "Alice", "output_path": output_path}

        tx = Transaction(reference="test-no-file")
        tx.skills = [
            ValidateInput(name="validate_input", execution_order=1, arguments=arguments),
            # WriteGreeting deliberately omitted — file will not exist
            ConfirmOutput(name="confirm_output", execution_order=2, arguments=arguments),
        ]
        Engine().run(tx)

        confirm = tx.skills[1]
        assert confirm.status == Status.FAILED
        assert isinstance(confirm.exceptions[0], SystemException)
        assert tx.status == Status.FAILED
