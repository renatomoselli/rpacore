"""Tests for rpacore project manifest handling."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def remove_module_tree(root_name: str) -> None:
    for module_name in list(sys.modules):
        if module_name == root_name or module_name.startswith(f"{root_name}."):
            sys.modules.pop(module_name, None)


def write_package_entrypoint(
    project_dir: Path,
    *,
    root_name: str,
    source: str,
) -> None:
    package = project_dir / root_name
    jobs = package / "jobs"
    jobs.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (jobs / "__init__.py").write_text("", encoding="utf-8")
    (jobs / "run.py").write_text(source, encoding="utf-8")
    write_manifest(project_dir, entrypoint=f"{root_name}.jobs.run:main")


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

    @pytest.mark.parametrize("db_path", ["", "   ", ":memory:", " :memory: "])
    def test_transient_transaction_db_path_rejected(
        self,
        tmp_path: Path,
        db_path: str,
    ) -> None:
        write_manifest(tmp_path, db_path=db_path)

        with pytest.raises(ValueError, match="storage.transaction_db_path"):
            load_project_manifest(tmp_path)

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
            "[pipeline]\nsteps = []\n",
            encoding="utf-8",
        )

        with pytest.raises(KeyError, match="manifest.pipeline"):
            load_project_manifest(manifest)

    def test_unknown_project_key_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / "rpacore.toml"
        manifest.write_text(
            "[project]\nentrypoint = \"main:main\"\nsteps = []\n"
            "[storage]\ntransaction_db_path = \"rpacore.db\"\n",
            encoding="utf-8",
        )

        with pytest.raises(KeyError, match="project.steps"):
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

    def test_successful_nonconflicting_import_preserves_existing_module_cache(
        self,
        tmp_path: Path,
    ) -> None:
        module_name = "rpacore_patch007_no_conflict"
        write_manifest(tmp_path, entrypoint=f"{module_name}:main")
        (tmp_path / f"{module_name}.py").write_text(
            "def main():\n    return 'project'\n",
            encoding="utf-8",
        )
        remove_module_tree(module_name)
        modules_before = dict(sys.modules)

        try:
            entrypoint = resolve_project_entrypoint(
                load_project_manifest(tmp_path)
            )

            assert entrypoint() == "project"
            assert set(sys.modules) - set(modules_before) == {module_name}
            assert all(
                sys.modules.get(name) is module
                for name, module in modules_before.items()
            )
        finally:
            remove_module_tree(module_name)

    def test_partial_import_marker_is_replaced_by_project_module(
        self,
        tmp_path: Path,
    ) -> None:
        module_name = "rpacore_patch007_partial_import"
        write_manifest(tmp_path, entrypoint=f"{module_name}:main")
        (tmp_path / f"{module_name}.py").write_text(
            "def main():\n    return 'project'\n",
            encoding="utf-8",
        )
        remove_module_tree(module_name)
        sys.modules[module_name] = None  # type: ignore[assignment]

        try:
            entrypoint = resolve_project_entrypoint(
                load_project_manifest(tmp_path)
            )

            assert entrypoint() == "project"
            assert sys.modules[module_name] is not None
        finally:
            remove_module_tree(module_name)

    def test_project_path_takes_temporary_precedence_when_already_on_sys_path(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        module_name = "rpacore_patch007_path_order"
        project = tmp_path / "project"
        decoy = tmp_path / "decoy"
        project.mkdir()
        decoy.mkdir()
        write_manifest(project, entrypoint=f"{module_name}:main")
        (project / f"{module_name}.py").write_text(
            "def main():\n    return 'project'\n",
            encoding="utf-8",
        )
        (decoy / f"{module_name}.py").write_text(
            "def main():\n    return 'decoy'\n",
            encoding="utf-8",
        )
        remove_module_tree(module_name)
        monkeypatch.syspath_prepend(str(decoy))
        sys.path.append(str(project.resolve()))
        original_project_index = sys.path.index(str(project.resolve()))

        try:
            entrypoint = resolve_project_entrypoint(
                load_project_manifest(project)
            )

            assert entrypoint() == "project"
            assert sys.path.index(str(project.resolve())) == original_project_index
        finally:
            remove_module_tree(module_name)
            try:
                sys.path.remove(str(project.resolve()))
            except ValueError:
                pass

    def test_sequential_same_name_nested_packages_resolve_from_each_project(
        self,
        tmp_path: Path,
    ) -> None:
        root_name = "rpacore_patch007_shared"
        unrelated_name = "rpacore_patch007_unrelated"
        unrelated_module = SimpleNamespace(marker="keep")
        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"
        project_a.mkdir()
        project_b.mkdir()
        write_package_entrypoint(
            project_a,
            root_name=root_name,
            source="def main():\n    return 'project-a'\n",
        )
        write_package_entrypoint(
            project_b,
            root_name=root_name,
            source="def main():\n    return 'project-b'\n",
        )
        remove_module_tree(root_name)
        sys.modules[unrelated_name] = unrelated_module

        try:
            entrypoint_a = resolve_project_entrypoint(
                load_project_manifest(project_a)
            )
            entrypoint_b = resolve_project_entrypoint(
                load_project_manifest(project_b)
            )

            assert entrypoint_a() == "project-a"
            assert entrypoint_b() == "project-b"
            assert sys.modules[unrelated_name] is unrelated_module
        finally:
            remove_module_tree(root_name)
            sys.modules.pop(unrelated_name, None)

    @pytest.mark.parametrize(
        ("failing_source", "error_type", "message"),
        [
            (
                "raise RuntimeError('project-b import failed')\n",
                RuntimeError,
                "project-b import failed",
            ),
            (
                "other = 1\n",
                AttributeError,
                "Project entrypoint attribute not found",
            ),
            (
                "raise SystemExit('project-b stopped')\n",
                SystemExit,
                "project-b stopped",
            ),
        ],
    )
    def test_failed_conflicting_resolution_restores_previous_project_modules(
        self,
        tmp_path: Path,
        failing_source: str,
        error_type: type[BaseException],
        message: str,
    ) -> None:
        root_name = "rpacore_patch007_restore"
        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"
        project_a.mkdir()
        project_b.mkdir()
        write_package_entrypoint(
            project_a,
            root_name=root_name,
            source="def main():\n    return 'project-a'\n",
        )
        write_package_entrypoint(
            project_b,
            root_name=root_name,
            source=failing_source,
        )
        remove_module_tree(root_name)

        try:
            entrypoint_a = resolve_project_entrypoint(
                load_project_manifest(project_a)
            )
            previous_modules = {
                name: module
                for name, module in sys.modules.items()
                if name == root_name or name.startswith(f"{root_name}.")
            }

            with pytest.raises(error_type, match=message):
                resolve_project_entrypoint(load_project_manifest(project_b))

            assert entrypoint_a() == "project-a"
            assert {
                name: module
                for name, module in sys.modules.items()
                if name == root_name or name.startswith(f"{root_name}.")
            } == previous_modules
        finally:
            remove_module_tree(root_name)

    def test_failed_deep_conflict_removes_new_sibling_modules_and_attributes(
        self,
        tmp_path: Path,
    ) -> None:
        root_name = "rpacore_patch007_deep_restore"
        project = tmp_path / "project"
        project.mkdir()
        write_package_entrypoint(
            project,
            root_name=root_name,
            source=(
                f"import {root_name}.other\n"
                "raise RuntimeError('entrypoint import failed')\n"
            ),
        )
        (project / root_name / "other.py").write_text(
            "VALUE = 'new sibling'\n",
            encoding="utf-8",
        )
        remove_module_tree(root_name)
        sys.path.insert(0, str(project))
        try:
            root_module = importlib.import_module(root_name)
        finally:
            sys.path.remove(str(project))
        stale_submodule = ModuleType(f"{root_name}.jobs")
        stale_submodule.__file__ = str(
            tmp_path / "stale" / "jobs" / "__init__.py"
        )
        sys.modules[f"{root_name}.jobs"] = stale_submodule
        setattr(root_module, "jobs", stale_submodule)
        modules_before = {
            name: module
            for name, module in sys.modules.items()
            if name == root_name or name.startswith(f"{root_name}.")
        }

        try:
            with pytest.raises(RuntimeError, match="entrypoint import failed"):
                resolve_project_entrypoint(load_project_manifest(project))

            assert {
                name: module
                for name, module in sys.modules.items()
                if name == root_name or name.startswith(f"{root_name}.")
            } == modules_before
            assert root_module.jobs is stale_submodule
            assert not hasattr(root_module, "other")
        finally:
            remove_module_tree(root_name)

    def test_failed_import_keeps_external_dependency_cache_side_effect(
        self,
        tmp_path: Path,
    ) -> None:
        root_name = "rpacore_patch007_external_failure"
        dependency_name = "rpacore_patch007_external_dependency"
        project = tmp_path / "project"
        project.mkdir()
        write_package_entrypoint(
            project,
            root_name=root_name,
            source=(
                f"import {dependency_name}\n"
                "raise RuntimeError('entrypoint import failed')\n"
            ),
        )
        (project / f"{dependency_name}.py").write_text(
            "VALUE = 'normal import side effect'\n",
            encoding="utf-8",
        )
        remove_module_tree(root_name)
        remove_module_tree(dependency_name)

        try:
            with pytest.raises(RuntimeError, match="entrypoint import failed"):
                resolve_project_entrypoint(load_project_manifest(project))

            dependency = sys.modules[dependency_name]
            assert dependency is not None
            assert dependency.VALUE == "normal import side effect"  # type: ignore[attr-defined]
        finally:
            remove_module_tree(root_name)
            remove_module_tree(dependency_name)

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
