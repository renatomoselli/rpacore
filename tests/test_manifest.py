"""Tests for rpacore project manifest handling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import rpacore
from rpacore.manifest import (
    ProjectManifest,
    _is_dotted_identifier,
    _module_belongs_to_project,
    find_project_manifest,
    load_project_manifest,
    resolve_project_entrypoint,
)


def write_manifest(project_dir: Path, *, entrypoint: str = "main:main", db_path: str = "rpacore.db") -> Path:
    manifest = project_dir / "rpacore.toml"
    manifest.write_text(
        "[project]\n"
        f'entrypoint = "{entrypoint}"\n'
        "\n"
        "[storage]\n"
        f'transaction_db_path = "{db_path}"\n',
        encoding="utf-8",
    )
    return manifest


class TestFindProjectManifest:
    def test_finds_manifest_in_start_directory(self, tmp_path: Path) -> None:
        manifest = write_manifest(tmp_path)

        assert find_project_manifest(tmp_path) == manifest

    def test_finds_manifest_from_nested_directory(self, tmp_path: Path) -> None:
        manifest = write_manifest(tmp_path)
        nested = tmp_path / "src" / "jobs"
        nested.mkdir(parents=True)

        assert find_project_manifest(nested) == manifest

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="rpacore.toml not found"):
            find_project_manifest(tmp_path)


class TestLoadProjectManifest:
    def test_loads_manifest_and_resolves_storage_path(self, tmp_path: Path) -> None:
        manifest_path = write_manifest(tmp_path, db_path="data/rpacore.db")

        manifest = load_project_manifest(manifest_path)

        assert manifest == ProjectManifest(
            manifest_path=manifest_path.resolve(),
            project_dir=tmp_path.resolve(),
            entrypoint="main:main",
            transaction_db_path=str((tmp_path / "data" / "rpacore.db").resolve()),
        )

    def test_directory_argument_loads_rpacore_toml(self, tmp_path: Path) -> None:
        write_manifest(tmp_path)

        manifest = load_project_manifest(tmp_path)

        assert manifest.manifest_path == (tmp_path / "rpacore.toml").resolve()

    def test_none_uses_discovery_from_current_directory(self, monkeypatch, tmp_path: Path) -> None:
        write_manifest(tmp_path)
        nested = tmp_path / "nested"
        nested.mkdir()
        monkeypatch.chdir(nested)

        manifest = load_project_manifest()

        assert manifest.project_dir == tmp_path.resolve()

    def test_absolute_transaction_db_path_is_preserved(self, tmp_path: Path) -> None:
        absolute = str((tmp_path / "absolute.db").resolve())
        write_manifest(tmp_path, db_path=absolute.replace("\\", "\\\\"))

        manifest = load_project_manifest(tmp_path)

        assert manifest.transaction_db_path == absolute

    def test_relative_transaction_db_path_matches_config_path_style(self, tmp_path: Path) -> None:
        manifest_path = write_manifest(tmp_path, db_path="data/rpacore.db")

        manifest = load_project_manifest(manifest_path)

        assert manifest.transaction_db_path == str(manifest.project_dir / "data" / "rpacore.db")

    def test_missing_manifest_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Project manifest not found"):
            load_project_manifest(tmp_path / "missing.toml")

    def test_missing_project_section_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "rpacore.toml"
        manifest.write_text('[storage]\ntransaction_db_path = "rpacore.db"\n', encoding="utf-8")

        with pytest.raises(KeyError, match="project"):
            load_project_manifest(manifest)

    def test_missing_entrypoint_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "rpacore.toml"
        manifest.write_text(
            "[project]\n[storage]\ntransaction_db_path = \"rpacore.db\"\n",
            encoding="utf-8",
        )

        with pytest.raises(KeyError, match="project.entrypoint"):
            load_project_manifest(manifest)

    def test_bad_entrypoint_type_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "rpacore.toml"
        manifest.write_text(
            "[project]\nentrypoint = 123\n[storage]\ntransaction_db_path = \"rpacore.db\"\n",
            encoding="utf-8",
        )

        with pytest.raises(TypeError) as exc_info:
            load_project_manifest(manifest)

        assert str(exc_info.value) == "project.entrypoint expected str; got int value=123"

    def test_bad_entrypoint_shape_raises(self, tmp_path: Path) -> None:
        write_manifest(tmp_path, entrypoint="main")

        with pytest.raises(ValueError, match="module:callable"):
            load_project_manifest(tmp_path)

    def test_unknown_manifest_key_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "rpacore.toml"
        manifest.write_text(
            "[project]\nentrypoint = \"main:main\"\n"
            "[storage]\ntransaction_db_path = \"rpacore.db\"\n"
            "[pipeline]\nskills = []\n",
            encoding="utf-8",
        )

        with pytest.raises(KeyError, match="manifest.pipeline"):
            load_project_manifest(manifest)

    def test_unknown_project_key_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "rpacore.toml"
        manifest.write_text(
            "[project]\nentrypoint = \"main:main\"\nskills = []\n"
            "[storage]\ntransaction_db_path = \"rpacore.db\"\n",
            encoding="utf-8",
        )

        with pytest.raises(KeyError, match="project.skills"):
            load_project_manifest(manifest)


class TestResolveProjectEntrypoint:
    def test_resolves_entrypoint_from_project_dir_outside_cwd(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        project = tmp_path / "project"
        other = tmp_path / "other"
        project.mkdir()
        other.mkdir()
        write_manifest(project)
        (project / "main.py").write_text("def main():\n    return 7\n", encoding="utf-8")
        monkeypatch.chdir(other)

        entrypoint = resolve_project_entrypoint(load_project_manifest(project))

        assert entrypoint() == 7

    def test_resolves_dotted_attribute(self, tmp_path: Path) -> None:
        write_manifest(tmp_path, entrypoint="main:app.main")
        (tmp_path / "main.py").write_text(
            "class App:\n"
            "    def main(self):\n"
            "        return 0\n"
            "app = App()\n",
            encoding="utf-8",
        )

        entrypoint = resolve_project_entrypoint(load_project_manifest(tmp_path))

        assert entrypoint() == 0

    def test_non_callable_entrypoint_raises(self, tmp_path: Path) -> None:
        write_manifest(tmp_path)
        (tmp_path / "main.py").write_text("main = 123\n", encoding="utf-8")

        with pytest.raises(TypeError, match="project.entrypoint expected callable"):
            resolve_project_entrypoint(load_project_manifest(tmp_path))

    def test_required_argument_entrypoint_raises(self, tmp_path: Path) -> None:
        write_manifest(tmp_path)
        (tmp_path / "main.py").write_text("def main(argv):\n    return 0\n", encoding="utf-8")

        with pytest.raises(TypeError, match="callable without arguments"):
            resolve_project_entrypoint(load_project_manifest(tmp_path))

    def test_missing_attribute_raises(self, tmp_path: Path) -> None:
        write_manifest(tmp_path, entrypoint="main:missing")
        (tmp_path / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")

        with pytest.raises(AttributeError, match="Project entrypoint attribute not found"):
            resolve_project_entrypoint(load_project_manifest(tmp_path))

    def test_import_error_is_not_masked_when_project_path_was_removed(self, tmp_path: Path) -> None:
        write_manifest(tmp_path)
        project_dir = str(tmp_path.resolve())
        (tmp_path / "main.py").write_text(
            f"import sys\nsys.path.remove({project_dir!r})\nraise RuntimeError('import failed')\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="import failed"):
            resolve_project_entrypoint(load_project_manifest(tmp_path))

    def test_class_entrypoint_raises_specific_error(self, tmp_path: Path) -> None:
        write_manifest(tmp_path, entrypoint="main:Job")
        (tmp_path / "main.py").write_text("class Job:\n    pass\n", encoding="utf-8")

        with pytest.raises(TypeError, match="not a class"):
            resolve_project_entrypoint(load_project_manifest(tmp_path))


class TestPackageExports:
    def test_manifest_helpers_are_reexported(self) -> None:
        assert rpacore.ProjectManifest is ProjectManifest
        assert rpacore.find_project_manifest is find_project_manifest
        assert rpacore.load_project_manifest is load_project_manifest
        assert rpacore.resolve_project_entrypoint is resolve_project_entrypoint


class TestManifestPrivateHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("main", True),
            ("package.module", True),
            ("app.factory.main", True),
            ("", False),
            (".main", False),
            ("main.", False),
            ("main-function", False),
        ],
    )
    def test_is_dotted_identifier(self, value: str, expected: bool) -> None:
        assert _is_dotted_identifier(value) is expected

    def test_module_without_file_is_treated_as_project_module(self, tmp_path: Path) -> None:
        module = SimpleNamespace()

        assert _module_belongs_to_project(module, tmp_path) is True

    def test_module_file_inside_project_belongs_to_project(self, tmp_path: Path) -> None:
        module_file = tmp_path / "main.py"
        module_file.write_text("", encoding="utf-8")
        module = SimpleNamespace(__file__=str(module_file))

        assert _module_belongs_to_project(module, tmp_path) is True

    def test_module_file_outside_project_does_not_belong_to_project(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        outside = tmp_path / "outside"
        project.mkdir()
        outside.mkdir()
        module_file = outside / "main.py"
        module_file.write_text("", encoding="utf-8")
        module = SimpleNamespace(__file__=str(module_file))

        assert _module_belongs_to_project(module, project) is False
