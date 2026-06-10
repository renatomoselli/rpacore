"""Tests for the installed-wheel validation script."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch


def _load_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "validate_installed_wheel.py"
    spec = importlib.util.spec_from_file_location("validate_installed_wheel", script_path)
    assert spec is not None, f"Could not load module from {script_path}"
    assert spec.loader is not None, f"Could not load module from {script_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestInstalledWheelValidationScript:
    def test_latest_wheel_picks_newest_rpacore_wheel(self, tmp_path: Path) -> None:
        module = _load_script()
        old_wheel = tmp_path / "rpacore-0.1.0-py3-none-any.whl"
        new_wheel = tmp_path / "rpacore-0.2.0-py3-none-any.whl"
        old_wheel.write_text("", encoding="utf-8")
        new_wheel.write_text("", encoding="utf-8")

        assert module._latest_wheel(tmp_path) == new_wheel

    def test_parser_defaults_examples_repo_to_sibling(self) -> None:
        module = _load_script()

        args = module.build_parser().parse_args([])

        assert args.examples_repo is None

    def test_smoke_code_checks_import_is_not_from_checkout(self) -> None:
        module = _load_script()

        code = module._smoke_code(Path("D:/repos/oref"))

        assert "imported rpacore from checkout" in code
        assert "Engine().run(ctx)" in code
        assert "ctx.state" in code

    def test_run_prints_command_and_checks_result(self, tmp_path: Path, capsys) -> None:
        module = _load_script()
        command = ["python", "--version"]

        with patch.object(module.subprocess, "run") as run:
            module._run(command, cwd=tmp_path)

        run.assert_called_once_with(command, cwd=tmp_path, check=True)
        assert capsys.readouterr().out.strip() == "+ python --version"

    def test_run_propagates_subprocess_errors(self, tmp_path: Path) -> None:
        module = _load_script()
        error = subprocess.CalledProcessError(1, ["bad"])

        with patch.object(module.subprocess, "run", side_effect=error):
            try:
                module._run(["bad"], cwd=tmp_path)
            except subprocess.CalledProcessError as exc:
                assert exc is error
            else:
                raise AssertionError("Expected CalledProcessError")

    def test_validate_installed_wheel_runs_expected_steps(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        repo_root.mkdir()
        work_dir.mkdir()
        calls: list[tuple[list[str], Path]] = []

        def fake_run(command: list[str], *, cwd: Path) -> None:
            calls.append((command, cwd))
            if command[1:3] == ["-m", "build"]:
                wheelhouse = Path(command[-1])
                wheelhouse.mkdir(parents=True, exist_ok=True)
                (wheelhouse / "rpacore-0.1.0-py3-none-any.whl").write_text("", encoding="utf-8")

        with patch.object(module, "_run", side_effect=fake_run):
            with patch.object(module, "_venv_python", return_value=work_dir / "venv" / "Scripts" / "python.exe"):
                with patch.object(module, "_venv_script", return_value=work_dir / "venv" / "Scripts" / "rpacore.exe"):
                    module.validate_installed_wheel(
                        repo_root=repo_root,
                        work_dir=work_dir,
                        examples_repo=None,
                        examples_pytest=[],
                    )

        assert len(calls) == 7
        assert calls[0][0][1:3] == ["-m", "build"]
        assert calls[1][0][1:3] == ["-m", "venv"]
        assert calls[2][0][1:4] == ["-m", "pip", "install"]
        assert calls[3][0][1] == "-c"
        assert calls[4][0][-1] == "version"
        assert calls[5][0][-2:] == ["init", "installed_project"]
        assert calls[6][0][-1] == "run"
        assert calls[6][1] == work_dir / "outside" / "installed_project"

    def test_validate_installed_wheel_removes_existing_generated_project(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        generated_project = work_dir / "outside" / "installed_project"
        repo_root.mkdir()
        generated_project.mkdir(parents=True)

        def fake_run(command: list[str], *, cwd: Path) -> None:
            if command[1:3] == ["-m", "build"]:
                wheelhouse = Path(command[-1])
                wheelhouse.mkdir(parents=True, exist_ok=True)
                (wheelhouse / "rpacore-0.1.0-py3-none-any.whl").write_text("", encoding="utf-8")

        with patch.object(module, "_run", side_effect=fake_run):
            with patch.object(module, "_venv_python", return_value=work_dir / "venv" / "Scripts" / "python.exe"):
                with patch.object(module, "_venv_script", return_value=work_dir / "venv" / "Scripts" / "rpacore.exe"):
                    with patch.object(module.shutil, "rmtree") as rmtree:
                        module.validate_installed_wheel(
                            repo_root=repo_root,
                            work_dir=work_dir,
                            examples_repo=None,
                            examples_pytest=[],
                        )

        rmtree.assert_called_once_with(generated_project, ignore_errors=True)

    def test_validate_installed_wheel_runs_examples_when_paths_are_explicit(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        examples_repo = tmp_path / "examples"
        repo_root.mkdir()
        work_dir.mkdir()
        examples_repo.mkdir()
        calls: list[tuple[list[str], Path]] = []

        def fake_run(command: list[str], *, cwd: Path) -> None:
            calls.append((command, cwd))
            if command[1:3] == ["-m", "build"]:
                wheelhouse = Path(command[-1])
                wheelhouse.mkdir(parents=True, exist_ok=True)
                (wheelhouse / "rpacore-0.1.0-py3-none-any.whl").write_text("", encoding="utf-8")

        with patch.object(module, "_run", side_effect=fake_run):
            with patch.object(module, "_venv_python", return_value=work_dir / "venv" / "Scripts" / "python.exe"):
                with patch.object(module, "_venv_script", return_value=work_dir / "venv" / "Scripts" / "rpacore.exe"):
                    module.validate_installed_wheel(
                        repo_root=repo_root,
                        work_dir=work_dir,
                        examples_repo=examples_repo,
                        examples_pytest=["examples/rest_api_batch/tests"],
                    )

        assert len(calls) == 9
        assert calls[-2][0][-2:] == ["install", "pytest"]
        assert calls[-2][1] == work_dir / "outside"
        assert calls[-1][0][-2:] == ["pytest", "examples/rest_api_batch/tests"]
        assert calls[-1][1] == examples_repo

    def test_validate_installed_wheel_requires_examples_pytest_with_examples_repo(self, tmp_path: Path) -> None:
        module = _load_script()

        try:
            module.validate_installed_wheel(
                repo_root=tmp_path,
                work_dir=tmp_path / "work",
                examples_repo=tmp_path / "examples",
                examples_pytest=[],
            )
        except ValueError as exc:
            assert str(exc) == "--examples-pytest is required when --examples-repo is provided"
        else:
            raise AssertionError("Expected ValueError")

    def test_validate_installed_wheel_requires_examples_repo_with_examples_pytest(self, tmp_path: Path) -> None:
        module = _load_script()

        try:
            module.validate_installed_wheel(
                repo_root=tmp_path,
                work_dir=tmp_path / "work",
                examples_repo=None,
                examples_pytest=["tests"],
            )
        except ValueError as exc:
            assert str(exc) == "--examples-repo is required when --examples-pytest is used"
        else:
            raise AssertionError("Expected ValueError")

    def test_main_cleans_owned_work_dir_on_failure(self, tmp_path: Path) -> None:
        module = _load_script()
        work_dir = tmp_path / "owned"
        failure = RuntimeError("boom")

        with patch.object(module.tempfile, "mkdtemp", return_value=str(work_dir)):
            with patch.object(module, "validate_installed_wheel", side_effect=failure):
                with patch.object(module.shutil, "rmtree") as rmtree:
                    try:
                        module.main([])
                    except RuntimeError as exc:
                        assert exc is failure
                    else:
                        raise AssertionError("Expected RuntimeError")

        rmtree.assert_called_once_with(work_dir)
