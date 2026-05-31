"""Smoke tests for the main entry point.

These tests call main() with a controlled config_path so every code path
(config load → resolve db_path → engine run → persist) uses safe temp locations
and never touches the repo root or relies on a writable CWD.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import examples.sample_main as main_module


def _write_config(tmp_path: Path) -> tuple[str, str, str]:
    """Write a minimal config.toml into tmp_path and return (config_path, db_path, output_path)."""
    db = tmp_path / "rpacore.db"
    output = tmp_path / "greeting.txt"
    config = tmp_path / "config.toml"
    # Use forward slashes so TOML parses cleanly on Windows too.
    config.write_text(
        textwrap.dedent(f"""\
            max_retries = 0
            log_level = "INFO"
            db_path = "{db.as_posix()}"
        """),
        encoding="utf-8",
    )
    return str(config), str(db), str(output)


class TestMainSmoke:
    def test_main_completes_successfully(self, tmp_path):
        """Happy path via config-backed defaults: config drives db_path."""
        config_path, db, output = _write_config(tmp_path)
        main_module.main(config_path=config_path, output_path=output)

        assert Path(output).read_text(encoding="utf-8") == "Hello, Alice\n"
        assert Path(db).exists()

    def test_main_is_idempotent(self, tmp_path):
        """Running main() twice with the same paths must not crash."""
        config_path, db, output = _write_config(tmp_path)
        main_module.main(config_path=config_path, output_path=output)
        main_module.main(config_path=config_path, output_path=output)

        assert Path(output).read_text(encoding="utf-8") == "Hello, Alice\n"

    def test_main_db_path_override_takes_precedence(self, tmp_path):
        """An explicit db_path kwarg overrides the config value."""
        config_path, config_db, output = _write_config(tmp_path)
        override_db = str(tmp_path / "override.db")

        main_module.main(config_path=config_path, db_path=override_db, output_path=output)

        assert Path(override_db).exists()
        assert not Path(config_db).exists()

