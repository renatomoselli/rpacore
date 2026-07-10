"""Tests for public configuration path helpers."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

import rpacore
from rpacore.exceptions import SystemException
from rpacore.paths import atomic_output_path, resolve_config_path, resolve_config_paths


class TestAtomicOutputPath:
    def test_success_replaces_destination_after_json_writer_completes(self, tmp_path: Path) -> None:
        destination = tmp_path / "report.json"
        destination.write_text('{"status": "old"}', encoding="utf-8")

        with atomic_output_path(destination) as temporary:
            assert temporary.parent == destination.parent
            assert temporary.name.startswith(f".{destination.name}.")
            assert temporary.name.endswith(".tmp")
            assert destination.read_text(encoding="utf-8") == '{"status": "old"}'
            temporary.write_text(json.dumps({"status": "new"}), encoding="utf-8")

        assert json.loads(destination.read_text(encoding="utf-8")) == {"status": "new"}
        assert not temporary.exists()

    def test_success_supports_csv_writer(self, tmp_path: Path) -> None:
        destination = tmp_path / "rows.csv"

        with atomic_output_path(destination) as temporary:
            with temporary.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["name", "count"])
                writer.writerow(["processed", "2"])

        assert destination.read_text(encoding="utf-8").splitlines() == [
            "name,count",
            "processed,2",
        ]

    def test_success_supports_workbook_like_binary_writer(self, tmp_path: Path) -> None:
        destination = tmp_path / "workbook.xlsx"
        payload = b"PK\x03\x04fake-xlsx-content"

        with atomic_output_path(destination) as temporary:
            temporary.write_bytes(payload)

        assert destination.read_bytes() == payload

    def test_success_without_writer_output_publishes_empty_file(self, tmp_path: Path) -> None:
        destination = tmp_path / "empty.txt"
        destination.write_text("old", encoding="utf-8")

        with atomic_output_path(destination) as temporary:
            assert temporary.read_bytes() == b""

        assert destination.read_bytes() == b""
        assert not temporary.exists()

    def test_exception_preserves_existing_destination_and_removes_temporary(self, tmp_path: Path) -> None:
        destination = tmp_path / "report.json"
        destination.write_text("old", encoding="utf-8")

        with pytest.raises(RuntimeError, match="writer failed"):
            with atomic_output_path(destination) as temporary:
                temporary.write_text("partial", encoding="utf-8")
                raise RuntimeError("writer failed")

        assert destination.read_text(encoding="utf-8") == "old"
        assert not temporary.exists()
        assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []

    def test_cleanup_failure_does_not_mask_writer_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        destination = tmp_path / "report.json"
        destination.write_text("old", encoding="utf-8")
        cleanup_attempts: list[Path] = []

        def fail_unlink(path: Path, missing_ok: bool = False) -> None:
            cleanup_attempts.append(path)
            raise OSError("cleanup failed")

        monkeypatch.setattr(Path, "unlink", fail_unlink)

        with pytest.raises(RuntimeError, match="writer failed"):
            with atomic_output_path(destination) as temporary:
                temporary.write_text("partial", encoding="utf-8")
                raise RuntimeError("writer failed")

        assert cleanup_attempts == [temporary]
        assert destination.read_text(encoding="utf-8") == "old"

    def test_replace_failure_preserves_existing_destination_and_removes_temporary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        destination = tmp_path / "report.json"
        destination.write_text("old", encoding="utf-8")

        def fail_replace(source: Path, target: Path) -> None:
            assert source.exists()
            assert target == destination
            raise OSError("replace denied")

        monkeypatch.setattr(os, "replace", fail_replace)

        with pytest.raises(OSError, match="replace denied"):
            with atomic_output_path(destination) as temporary:
                temporary.write_text("new", encoding="utf-8")

        assert destination.read_text(encoding="utf-8") == "old"
        assert not temporary.exists()

    def test_fsync_failure_preserves_existing_destination_and_removes_temporary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        destination = tmp_path / "report.json"
        destination.write_text("old", encoding="utf-8")
        replace_calls: list[tuple[Path, Path]] = []

        def fail_fsync(file_descriptor: int) -> None:
            raise OSError("fsync failed")

        def record_replace(source: Path, target: Path) -> None:
            replace_calls.append((source, target))

        monkeypatch.setattr(os, "fsync", fail_fsync)
        monkeypatch.setattr(os, "replace", record_replace)

        with pytest.raises(OSError, match="fsync failed"):
            with atomic_output_path(destination) as temporary:
                temporary.write_text("new", encoding="utf-8")

        assert destination.read_text(encoding="utf-8") == "old"
        assert not temporary.exists()
        assert replace_calls == []

    def test_missing_parent_directory_fails_before_yield(self, tmp_path: Path) -> None:
        destination = tmp_path / "missing" / "report.json"

        with pytest.raises(FileNotFoundError):
            with atomic_output_path(destination):
                raise AssertionError("context body should not run")

    def test_invalid_destination_type_raises(self) -> None:
        with pytest.raises(TypeError) as exc_info:
            with atomic_output_path(123):  # type: ignore[arg-type]
                raise AssertionError("context body should not run")

        assert str(exc_info.value) == "destination expected str | PathLike[str]; got int value=123"


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
