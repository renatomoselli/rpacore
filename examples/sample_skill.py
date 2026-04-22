"""Example automation: greet a user by writing their name to a file.

This module contains three Skill subclasses that form a complete automation
scenario. It is user-space code — not framework code — and is intended to show
the pattern a developer would follow when building their own automation on top
of OREF.

Scenario
--------
Given a name in the skill's arguments, the automation:
  1. Validates that the name is present and non-empty.
  2. Writes "Hello, {name}" to an output file (idempotent: ensures desired state).
  3. Confirms that the output file exists and contains the expected content.
"""

from __future__ import annotations

from pathlib import Path

from oref.context import ProcessContext
from oref.exceptions import BusinessException, SystemException
from oref.skill import Skill


class ValidateInput(Skill):
    """Ensure the 'name' argument is present and non-empty.

    Raises BusinessException if validation fails. Execution continues to the
    next skill because BusinessException does not stop the engine.
    """

    def execute(self, ctx: ProcessContext) -> None:
        name = self.arguments.get("name")
        if not name or not str(name).strip():
            raise BusinessException(
                message="'name' argument is missing or empty",
                action="ValidateInput",
            )


class WriteGreeting(Skill):
    """Write "Hello, {name}" to the output file.

    Idempotent: if the file already contains the correct greeting, the skill
    does nothing. If the file is absent or contains stale content, it is
    written. This demonstrates "ensure desired state" rather than "skip on
    existence."
    """

    def execute(self, ctx: ProcessContext) -> None:
        name = self.arguments.get("name")
        if not name or not str(name).strip():
            raise BusinessException(
                message="'name' argument is missing or empty",
                action="WriteGreeting",
            )

        output_path = Path(str(self.arguments.get("output_path", "greeting.txt")))
        desired_content = f"Hello, {name}\n"

        if output_path.exists() and output_path.read_text(encoding="utf-8") == desired_content:
            return  # Already in desired state — nothing to do.

        output_path.write_text(desired_content, encoding="utf-8")


class ConfirmOutput(Skill):
    """Confirm that the output file exists and contains the expected greeting.

    Raises SystemException if the file is missing or its content is wrong.
    SystemException stops execution immediately and is retryable by the engine.
    """

    def execute(self, ctx: ProcessContext) -> None:
        name = str(self.arguments.get("name", ""))
        output_path = Path(str(self.arguments.get("output_path", "greeting.txt")))
        desired_content = f"Hello, {name}\n"

        if not output_path.exists():
            raise SystemException(
                message=f"Output file not found: {output_path}",
                action="ConfirmOutput",
            )

        actual_content = output_path.read_text(encoding="utf-8")
        if actual_content != desired_content:
            raise SystemException(
                message=f"Output file has unexpected content: {actual_content!r}",
                action="ConfirmOutput",
            )
