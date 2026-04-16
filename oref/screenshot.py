"""Screenshot capture for exception evidence.

Uses mss (optional dependency) to capture the screen when an exception
occurs during skill execution. If mss is not installed, logs a warning
and returns an empty string — the framework continues without screenshots.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def capture_screenshot(directory: str) -> str:
    """Capture a screenshot and save it as a PNG file.

    Returns the file path on success, or "" if mss is not installed
    or the capture fails.
    """
    try:
        import mss  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("mss is not installed — screenshot skipped. Install with: pip install oref[screenshots]")
        return ""

    try:
        dir_path = Path(directory)
        dir_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        filename = f"screenshot_{timestamp}.png"
        filepath = dir_path / filename

        with mss.mss() as sct:
            sct.shot(output=str(filepath))

        return str(filepath)
    except Exception:
        logger.warning("Screenshot capture failed", exc_info=True)
        return ""
