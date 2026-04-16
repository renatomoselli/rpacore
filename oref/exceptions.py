"""Exception types for OREF.

Two categories:
- BusinessException: expected rule violation (does not stop execution by default)
- SystemException: technical failure (stops execution by default)
"""

from datetime import datetime, timezone


class BusinessException(Exception):
    """Expected business rule violation.

    Represents a known, anticipated error — e.g., invalid data,
    missing required field, rule not met. Does not stop execution
    by default.
    """

    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        retry_number: int = 0,
        datetime_occurred: datetime | None = None,
        screenshot_path: str = "",
    ) -> None:
        super().__init__(message)
        self.action: str = action
        self.retry_number: int = retry_number
        self.datetime_occurred: datetime = datetime_occurred or datetime.now(timezone.utc)
        self.screenshot_path: str = screenshot_path

    @property
    def stops_execution(self) -> bool:
        return False


class SystemException(Exception):
    """Unexpected technical failure.

    Represents a system-level error — e.g., network timeout,
    application crash, file not found. Stops execution by default.
    """

    def __init__(
        self,
        message: str,
        *,
        action: str = "",
        retry_number: int = 0,
        datetime_occurred: datetime | None = None,
        screenshot_path: str = "",
    ) -> None:
        super().__init__(message)
        self.action: str = action
        self.retry_number: int = retry_number
        self.datetime_occurred: datetime = datetime_occurred or datetime.now(timezone.utc)
        self.screenshot_path: str = screenshot_path

    @property
    def stops_execution(self) -> bool:
        return True
