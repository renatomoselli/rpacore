"""Exception types for rpacore.

Two categories:
- BusinessException: expected rule violation (does not halt remaining steps by default)
- SystemException: technical failure (halts the current execution pass)
"""

from datetime import datetime, timezone

from rpacore.outcome import validate_failure_code


class ExecutionValidationError(ValueError):
    """Permanent invalid transaction or step wiring detected before execution."""


class DefinitionIdentityError(ExecutionValidationError):
    """Missing, invalid, changed, or incompatible automation definition identity."""


class BusinessException(Exception):
    """Expected business rule violation.

    Represents a known, anticipated error — e.g., invalid data,
    missing required field, rule not met. Does not halt remaining steps by
    default.
    """

    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        retry_number: int = 0,
        occurred_at: datetime | None = None,
        screenshot_path: str = "",
        halts_remaining_steps: bool = False,
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.action: str = action
        self.retry_number: int = retry_number
        self.occurred_at: datetime = occurred_at or datetime.now(timezone.utc)
        self.screenshot_path: str = screenshot_path
        self.halts_remaining_steps: bool = halts_remaining_steps
        self.code: str = validate_failure_code(code)


class SystemException(Exception):
    """Unexpected technical failure.

    Represents a system-level error — e.g., network timeout,
    application crash, file not found. Always halts the current execution pass.
    """

    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        retry_number: int = 0,
        occurred_at: datetime | None = None,
        screenshot_path: str = "",
        code: str = "",
    ) -> None:
        super().__init__(message)
        self.action: str = action
        self.retry_number: int = retry_number
        self.occurred_at: datetime = occurred_at or datetime.now(timezone.utc)
        self.screenshot_path: str = screenshot_path
        self.code: str = validate_failure_code(code)

    @property
    def halts_remaining_steps(self) -> bool:
        return True
