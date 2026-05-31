"""Tests for rpacore.screenshot."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from rpacore.screenshot import capture_screenshot


class TestCaptureScreenshot:
    def test_returns_empty_when_mss_not_installed(self) -> None:
        with patch.dict("sys.modules", {"mss": None}):
            result = capture_screenshot("screenshots")
        assert result == ""

    def test_returns_filepath_when_mss_available(self, tmp_path: object) -> None:
        mock_sct_instance = MagicMock()
        mock_sct_instance.__enter__ = MagicMock(return_value=mock_sct_instance)
        mock_sct_instance.__exit__ = MagicMock(return_value=False)

        mock_mss_module = MagicMock()
        mock_mss_module.mss.return_value = mock_sct_instance

        with patch.dict("sys.modules", {"mss": mock_mss_module}):
            result = capture_screenshot(str(tmp_path))

        assert result != ""
        assert "screenshot_" in result
        assert result.endswith(".png")
        mock_sct_instance.shot.assert_called_once()

    def test_creates_directory_if_missing(self, tmp_path: object) -> None:
        target = str(tmp_path) + "/nested/dir"
        mock_sct_instance = MagicMock()
        mock_sct_instance.__enter__ = MagicMock(return_value=mock_sct_instance)
        mock_sct_instance.__exit__ = MagicMock(return_value=False)

        mock_mss_module = MagicMock()
        mock_mss_module.mss.return_value = mock_sct_instance

        with patch.dict("sys.modules", {"mss": mock_mss_module}):
            result = capture_screenshot(target)

        assert result != ""
        from pathlib import Path
        assert Path(target).is_dir()

    def test_returns_empty_on_capture_failure(self, tmp_path: object) -> None:
        mock_sct_instance = MagicMock()
        mock_sct_instance.__enter__ = MagicMock(return_value=mock_sct_instance)
        mock_sct_instance.__exit__ = MagicMock(return_value=False)
        mock_sct_instance.shot.side_effect = RuntimeError("display not available")

        mock_mss_module = MagicMock()
        mock_mss_module.mss.return_value = mock_sct_instance

        with patch.dict("sys.modules", {"mss": mock_mss_module}):
            result = capture_screenshot(str(tmp_path))

        assert result == ""


class TestEngineScreenshotIntegration:
    """Verify the engine sets screenshot_path on exceptions when screenshot_dir is configured."""

    def test_engine_captures_screenshot_on_business_exception(self, tmp_path: object) -> None:
        from rpacore import Engine, Transaction
        from rpacore.context import ProcessContext
        from rpacore.exceptions import BusinessException
        from rpacore.skill import Skill
        from rpacore.status import Status

        class FailSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise BusinessException("bad data", action="fail")

        tx = Transaction(reference="test")
        tx.skills = [FailSkill(name="fail", execution_order=1)]

        with patch("rpacore.engine.capture_screenshot", return_value="/tmp/shot.png") as mock_cap:
            engine = Engine(screenshot_dir="screenshots")
            engine.run(ProcessContext(transaction=tx))

        assert tx.skills[0].status == Status.FAILED
        assert tx.skills[0].exceptions[0].screenshot_path == "/tmp/shot.png"
        mock_cap.assert_called_once_with("screenshots")

    def test_engine_captures_screenshot_on_system_exception(self, tmp_path: object) -> None:
        from rpacore import Engine, Transaction
        from rpacore.context import ProcessContext
        from rpacore.exceptions import SystemException
        from rpacore.skill import Skill
        from rpacore.status import Status

        class FailSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise SystemException("crash", action="fail")

        tx = Transaction(reference="test")
        tx.skills = [FailSkill(name="fail", execution_order=1)]

        with patch("rpacore.engine.capture_screenshot", return_value="/tmp/crash.png") as mock_cap:
            engine = Engine(screenshot_dir="screenshots")
            engine.run(ProcessContext(transaction=tx))

        assert tx.skills[0].exceptions[0].screenshot_path == "/tmp/crash.png"
        mock_cap.assert_called_once_with("screenshots")

    def test_engine_captures_screenshot_on_unhandled_exception(self) -> None:
        from rpacore import Engine, Transaction
        from rpacore.context import ProcessContext
        from rpacore.skill import Skill
        from rpacore.status import Status

        class FailSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise RuntimeError("unexpected")

        tx = Transaction(reference="test")
        tx.skills = [FailSkill(name="fail", execution_order=1)]

        with patch("rpacore.engine.capture_screenshot", return_value="/tmp/unhandled.png"):
            engine = Engine(screenshot_dir="screenshots")
            engine.run(ProcessContext(transaction=tx))

        assert tx.skills[0].exceptions[0].screenshot_path == "/tmp/unhandled.png"

    def test_engine_skips_screenshot_when_dir_empty(self) -> None:
        from rpacore import Engine, Transaction
        from rpacore.context import ProcessContext
        from rpacore.exceptions import BusinessException
        from rpacore.skill import Skill

        class FailSkill(Skill):
            def execute(self, ctx: ProcessContext) -> None:
                raise BusinessException("bad data", action="fail")

        tx = Transaction(reference="test")
        tx.skills = [FailSkill(name="fail", execution_order=1)]

        with patch("rpacore.engine.capture_screenshot") as mock_cap:
            engine = Engine(screenshot_dir="")
            engine.run(ProcessContext(transaction=tx))

        mock_cap.assert_not_called()
        assert tx.skills[0].exceptions[0].screenshot_path == ""
