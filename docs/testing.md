# Testing RPA Core Skills

RPA Core v0.1.0 does not provide `SkillTestCase`, a custom test runner, or a
framework-specific pytest plugin. Skill tests should be normal pytest tests that
instantiate a skill, a `Transaction`, and a `ProcessContext` directly.

This keeps test execution deterministic and visible. A custom base class would
need rules for hidden setup, exception wrapping, transaction mutation,
artifact cleanup, and engine ordering. The current examples do not show enough
repeated boilerplate to justify that public API before v0.1.0.

## Basic Pattern

Test skill behavior at the smallest useful boundary:

```python
from pathlib import Path

from rpacore import ProcessContext, Transaction
from skills.greeting import WriteGreeting


def test_write_greeting(tmp_path: Path) -> None:
    output = tmp_path / "greeting.txt"
    skill = WriteGreeting(
        name="write_greeting",
        execution_order=1,
        arguments={"name": "Alice", "output_path": str(output)},
    )
    transaction = Transaction(reference="test", skills=[skill])

    skill.execute(ProcessContext(transaction=transaction))

    assert output.read_text(encoding="utf-8") == "Hello, Alice\n"
    assert transaction.state["greeting_path"] == str(output)
```

Use `Engine.run()` in tests only when the engine's ordering, retry,
classification, screenshot, checkpoint, or status behavior is part of the
assertion.

## Business Exceptions

Business failures are expected domain outcomes. Assert the exception directly
when testing a skill method, and assert that no unintended state or artifacts
were produced:

```python
import pytest

from rpacore import BusinessException, ProcessContext, Transaction


def test_skill_rejects_invalid_input(skill) -> None:
    transaction = Transaction(reference="test", skills=[skill])

    with pytest.raises(BusinessException, match="invalid input"):
        skill.execute(ProcessContext(transaction=transaction))

    assert transaction.state == {}
    assert transaction.artifacts == []
```

Use an engine-level test when you need to verify that a `BusinessException`
sets skill status, lets later skills continue, or stops execution with
`stop=True`.

## System Exceptions

System failures are technical failures. If the skill raises `SystemException`
itself, assert that exception directly. If the skill raises another unexpected
exception, use an engine-level test to verify the framework wraps it as a
`SystemException` and applies retry behavior.

```python
import pytest

from rpacore import ProcessContext, SystemException, Transaction


def test_skill_reports_service_timeout(skill) -> None:
    transaction = Transaction(reference="test", skills=[skill])

    with pytest.raises(SystemException, match="service timeout"):
        skill.execute(ProcessContext(transaction=transaction))
```

## State and Artifacts

`ctx.state` is durable JSON-safe transaction state. Assert exact keys and
values that later skills or persisted records depend on.

Use `ctx.add_artifact()` to register generated file paths. Tests should assert
the artifact record, not read artifact contents through the reporting or
notification layer. Reports, notifications, and serialization use artifact
metadata and paths; they do not automatically attach arbitrary artifact file
contents.

## Decision

Do not add `SkillTestCase` for the `0.1.x` release line. Plain pytest tests with direct
`ProcessContext` construction cover the demonstrated skill-testing needs:
business exceptions, system exceptions, durable state, artifact records, and
engine-level retry/status behavior where needed.

If repeated real projects later show meaningful boilerplate that cannot be
removed with ordinary pytest fixtures, add a narrow helper for that workflow.
Any helper must document its setup timing, mutation behavior, exception
disposition, and whether it runs a single skill directly or delegates to
`Engine.run()`.
