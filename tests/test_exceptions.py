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
        assert exc.halts_remaining_steps is False
        assert exc.code == ""
        assert isinstance(exc.occurred_at, datetime)

    def test_custom_fields(self) -> None:
        dt = datetime(2026, 4, 3, tzinfo=timezone.utc)
        exc = BusinessException(
            "bad data",
            action="validate_invoice",
            retry_number=2,
            occurred_at=dt,
            screenshot_path="/tmp/shot.png",
            halts_remaining_steps=True,
        )
        assert exc.action == "validate_invoice"
        assert exc.retry_number == 2
        assert exc.occurred_at == dt
        assert exc.screenshot_path == "/tmp/shot.png"
        assert exc.halts_remaining_steps is True

    def test_accepts_namespaced_code(self) -> None:
        exc = BusinessException("bad invoice", code="acme.invoice.missing_number")

        assert exc.code == "acme.invoice.missing_number"

    @pytest.mark.parametrize("code", ["missing", "Acme.invoice", "acme..invoice", "acme.invoice-"])
    def test_rejects_invalid_code(self, code: str) -> None:
        with pytest.raises(ValueError, match="code must be empty"):
            BusinessException("bad invoice", code=code)

    def test_halts_remaining_steps_is_false(self) -> None:
        exc = BusinessException("test")
        assert exc.halts_remaining_steps is False

    def test_halts_remaining_steps_reflects_stop(self) -> None:
        exc = BusinessException("test", halts_remaining_steps=True)
        assert exc.halts_remaining_steps is True

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
        assert exc.code == ""
        assert isinstance(exc.occurred_at, datetime)

    def test_custom_fields(self) -> None:
        dt = datetime(2026, 4, 3, tzinfo=timezone.utc)
        exc = SystemException(
            "db crash",
            action="save_record",
            retry_number=1,
            occurred_at=dt,
            screenshot_path="/tmp/crash.png",
        )
        assert exc.action == "save_record"
        assert exc.retry_number == 1
        assert exc.occurred_at == dt
        assert exc.screenshot_path == "/tmp/crash.png"

    def test_rejects_non_string_code(self) -> None:
        with pytest.raises(TypeError, match="code must be a str"):
            SystemException("crash", code=1)  # type: ignore[arg-type]

    def test_halts_remaining_steps_is_true(self) -> None:
        exc = SystemException("test")
        assert exc.halts_remaining_steps is True

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
