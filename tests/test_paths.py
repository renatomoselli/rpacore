"""Tests for public configuration path helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import rpacore
from rpacore.exceptions import SystemException
from rpacore.paths import resolve_config_path, resolve_config_paths


class TestResolveConfigPath:
    def test_relative_path_resolves_against_base_dir(self, tmp_path: Path) -> None:
        result = resolve_config_path("data/input.csv", base_dir=tmp_path)

        assert result == str((tmp_path / "data" / "input.csv").resolve())

    def test_absolute_path_remains_absolute(self, tmp_path: Path) -> None:
        absolute = (tmp_path / "data" / "input.csv").resolve()

        result = resolve_config_path(absolute, base_dir=tmp_path / "other")

        assert result == str(absolute)

    def test_pathlike_value_is_supported(self, tmp_path: Path) -> None:
        value = Path("data") / "input.csv"

        result = resolve_config_path(value, base_dir=tmp_path)

        assert result == str((tmp_path / value).resolve())

    def test_relative_root_resolves_against_base_dir(self, tmp_path: Path) -> None:
        result = resolve_config_path(
            "output/report.csv",
            base_dir=tmp_path,
            root="output",
        )

        assert result == str((tmp_path / "output" / "report.csv").resolve())

    def test_escaped_path_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SystemException) as exc_info:
            resolve_config_path(
                "../outside.txt",
                base_dir=tmp_path / "project",
                root=tmp_path / "project",
                key="output.path",
            )

        assert str(exc_info.value) == (
            f"output.path resolves outside root: {(tmp_path / 'outside.txt').resolve()} "
            f"(root: {(tmp_path / 'project').resolve()})"
        )
        assert exc_info.value.action == "output.path"

    def test_root_itself_is_allowed(self, tmp_path: Path) -> None:
        root = tmp_path / "project"

        assert resolve_config_path(".", base_dir=root, root=root) == str(root.resolve())

    def test_root_with_trailing_separator_is_allowed(self, tmp_path: Path) -> None:
        root = tmp_path / "project"

        result = resolve_config_path(
            "output.txt",
            base_dir=root,
            root=str(root) + os.sep,
        )

        assert result == str((root / "output.txt").resolve())

    def test_symlink_target_outside_root_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        link = root / "external"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlinks unavailable: {exc}")

        with pytest.raises(SystemException, match="resolves outside root"):
            resolve_config_path("external/report.txt", base_dir=root, root=root)

    def test_symlink_target_inside_root_is_allowed(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        target = root / "reports"
        root.mkdir()
        target.mkdir()
        link = root / "current"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlinks unavailable: {exc}")

        result = resolve_config_path("current/report.txt", base_dir=root, root=root)

        assert result == str((target / "report.txt").resolve())

    @pytest.mark.skipif(os.name != "nt", reason="Windows drive containment behavior")
    def test_path_on_different_windows_drive_is_rejected(self, tmp_path: Path) -> None:
        current_drive = tmp_path.drive.upper()
        other_drive = "Z:" if current_drive != "Z:" else "Y:"
        root = tmp_path / "project"

        with pytest.raises(SystemException, match="resolves outside root"):
            resolve_config_path(f"{other_drive}\\outside.txt", base_dir=root, root=root)

    def test_invalid_value_type_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError) as exc_info:
            resolve_config_path(123, base_dir=tmp_path)  # type: ignore[arg-type]

        assert str(exc_info.value) == "path expected str | PathLike[str]; got int value=123"

    def test_invalid_base_dir_type_raises(self) -> None:
        with pytest.raises(TypeError) as exc_info:
            resolve_config_path("input.txt", base_dir=None)  # type: ignore[arg-type]

        assert str(exc_info.value) == "base_dir expected str | PathLike[str]; got NoneType value=None"

    def test_invalid_root_type_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TypeError) as exc_info:
            resolve_config_path("input.txt", base_dir=tmp_path, root=123)  # type: ignore[arg-type]

        assert str(exc_info.value) == "root expected str | PathLike[str]; got int value=123"


class TestResolveConfigPaths:
    def test_dotted_keys_resolve_in_nested_sections(self, tmp_path: Path) -> None:
        config: dict[str, object] = {
            "queue": {"db_path": "data/queue.db"},
            "transaction_db_path": "data/transactions.db",
        }

        result = resolve_config_paths(
            config,
            ["queue.db_path", "transaction_db_path"],
            base_dir=tmp_path,
        )

        assert result["queue"] == {
            "db_path": str((tmp_path / "data" / "queue.db").resolve())
        }
        assert result["transaction_db_path"] == str(
            (tmp_path / "data" / "transactions.db").resolve()
        )

    def test_original_config_is_not_mutated(self, tmp_path: Path) -> None:
        config: dict[str, object] = {
            "queue": {"db_path": "queue.db"},
            "other": {"values": ["unchanged"]},
        }

        result = resolve_config_paths(config, ["queue.db_path"], base_dir=tmp_path)

        assert config == {
            "queue": {"db_path": "queue.db"},
            "other": {"values": ["unchanged"]},
        }
        assert result is not config
        assert result["queue"] is not config["queue"]
        assert result["other"] is not config["other"]

    def test_missing_dotted_key_raises(self, tmp_path: Path) -> None:
        config: dict[str, object] = {"queue": {}}

        with pytest.raises(KeyError, match="queue.db_path"):
            resolve_config_paths(config, ["queue.db_path"], base_dir=tmp_path)

    def test_non_section_dotted_parent_raises(self, tmp_path: Path) -> None:
        config: dict[str, object] = {"queue": "queue.db"}

        with pytest.raises(TypeError) as exc_info:
            resolve_config_paths(config, ["queue.db_path"], base_dir=tmp_path)

        assert str(exc_info.value) == "queue expected dict; got str value='queue.db'"

    def test_escaped_dotted_path_is_rejected(self, tmp_path: Path) -> None:
        config: dict[str, object] = {"output": {"path": "../outside.txt"}}
        root = tmp_path / "project"

        with pytest.raises(SystemException, match="output.path resolves outside root"):
            resolve_config_paths(
                config,
                ["output.path"],
                base_dir=root,
                root=root,
            )

    def test_empty_keys_returns_deep_copy(self, tmp_path: Path) -> None:
        config: dict[str, object] = {"nested": {"value": 1}}

        result = resolve_config_paths(config, [], base_dir=tmp_path)

        assert result == config
        assert result is not config
        assert result["nested"] is not config["nested"]


class TestPackageExports:
    def test_path_helpers_are_reexported(self) -> None:
        assert rpacore.resolve_config_path is resolve_config_path
        assert rpacore.resolve_config_paths is resolve_config_paths
