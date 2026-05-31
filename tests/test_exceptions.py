"""Tests for rpacore.exceptions."""

from datetime import datetime, timezone

import pytest

from rpacore.exceptions import BusinessException, SystemException


class TestBusinessException:
    def test_is_exception(self) -> None:
        exc = BusinessException("rule violated")
        assert isinstance(exc, Exception)

    def test_message(self) -> None:
        exc = BusinessException("invalid amount")
        assert str(exc) == "invalid amount"

    def test_defaults(self) -> None:
        exc = BusinessException("test")
        assert exc.action == ""
        assert exc.retry_number == 0
        assert exc.screenshot_path == ""
        assert isinstance(exc.datetime_occurred, datetime)

    def test_custom_fields(self) -> None:
        dt = datetime(2026, 4, 3, tzinfo=timezone.utc)
        exc = BusinessException(
            "bad data",
            action="validate_invoice",
            retry_number=2,
            datetime_occurred=dt,
            screenshot_path="/tmp/shot.png",
        )
        assert exc.action == "validate_invoice"
        assert exc.retry_number == 2
        assert exc.datetime_occurred == dt
        assert exc.screenshot_path == "/tmp/shot.png"

    def test_stops_execution_is_false(self) -> None:
        exc = BusinessException("test")
        assert exc.stops_execution is False

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(BusinessException, match="rule failed"):
            raise BusinessException("rule failed")


class TestSystemException:
    def test_is_exception(self) -> None:
        exc = SystemException("connection lost")
        assert isinstance(exc, Exception)

    def test_message(self) -> None:
        exc = SystemException("timeout")
        assert str(exc) == "timeout"

    def test_defaults(self) -> None:
        exc = SystemException("test")
        assert exc.action == ""
        assert exc.retry_number == 0
        assert exc.screenshot_path == ""
        assert isinstance(exc.datetime_occurred, datetime)

    def test_custom_fields(self) -> None:
        dt = datetime(2026, 4, 3, tzinfo=timezone.utc)
        exc = SystemException(
            "db crash",
            action="save_record",
            retry_number=1,
            datetime_occurred=dt,
            screenshot_path="/tmp/crash.png",
        )
        assert exc.action == "save_record"
        assert exc.retry_number == 1
        assert exc.datetime_occurred == dt
        assert exc.screenshot_path == "/tmp/crash.png"

    def test_stops_execution_is_true(self) -> None:
        exc = SystemException("test")
        assert exc.stops_execution is True

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(SystemException, match="crash"):
            raise SystemException("crash")


class TestExceptionHierarchy:
    def test_business_not_system(self) -> None:
        exc = BusinessException("test")
        assert not isinstance(exc, SystemException)

    def test_system_not_business(self) -> None:
        exc = SystemException("test")
        assert not isinstance(exc, BusinessException)

    def test_both_are_exceptions(self) -> None:
        assert issubclass(BusinessException, Exception)
        assert issubclass(SystemException, Exception)
