"""Tests for the installed-wheel validation script."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import rpacore

from rpacore._validation import ValidationError as SharedValidationError
from rpacore._validation import ValidationFailure


def _load_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "validate_installed_wheel.py"
    spec = importlib.util.spec_from_file_location("validate_installed_wheel", script_path)
    assert spec is not None, f"Could not load module from {script_path}"
    assert spec.loader is not None, f"Could not load module from {script_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_code_exercises_public_configuration_and_query_apis(tmp_path: Path) -> None:
    module = _load_script()

    smoke_code = module._smoke_code(tmp_path)

    assert "ConfigField" in smoke_code
    assert "validate_config" in smoke_code
    assert '"retry_count"' in smoke_code
    assert "query_transactions" in smoke_code
    assert '"query-smoke"' in smoke_code
    assert "missing py.typed" in smoke_code
    assert "expected_public_exports" in smoke_code
    assert "retired_name" in smoke_code
    assert "rpacore.skill" in smoke_code
    assert "frozen v0.3 contract" in smoke_code
    assert "ExecutionTransition" in smoke_code
    assert "transition_sink=transitions.append" in smoke_code
    assert 'transition_state_fields=("ran",)' in smoke_code
    compile(smoke_code, "<installed-wheel-smoke>", "exec")


def test_frozen_wheel_export_inventories_match_the_checkout_contract() -> None:
    module = _load_script()

    assert module._FROZEN_PUBLIC_EXPORTS == tuple(rpacore.__all__)
    assert not hasattr(rpacore, "Skill")
    assert not hasattr(rpacore, "SkillReport")


def test_typing_consumer_uses_scalar_accessor_types() -> None:
    module = _load_script()

    consumer_code = module._typing_consumer_code()

    assert 'retries: int = require_config(config, "retries", int)' in consumer_code
    assert 'state_label: str = context.require_state("label", str)' in consumer_code
    assert "transitions: list[ExecutionTransition] = []" in consumer_code
    assert "transition_sink=transitions.append" in consumer_code


class TestInstalledWheelValidationScript:
    def test_latest_wheel_picks_newest_rpacore_wheel(self, tmp_path: Path) -> None:
        module = _load_script()
        old_wheel = tmp_path / "rpacore-0.1.0-py3-none-any.whl"
        new_wheel = tmp_path / "rpacore-0.2.0-py3-none-any.whl"
        old_wheel.write_text("", encoding="utf-8")
        new_wheel.write_text("", encoding="utf-8")

        assert module._latest_wheel(tmp_path) == new_wheel

    def test_latest_wheel_prefers_stable_when_mtimes_match(self, tmp_path: Path) -> None:
        module = _load_script()
        stable = tmp_path / "rpacore-1.0.0-py3-none-any.whl"
        prerelease = tmp_path / "rpacore-1.0.0rc1-py3-none-any.whl"
        stable.write_text("", encoding="utf-8")
        prerelease.write_text("", encoding="utf-8")
        mtime = 1_800_000_000
        os.utime(stable, (mtime, mtime))
        os.utime(prerelease, (mtime, mtime))

        assert module._latest_wheel(tmp_path) == stable

    def test_parser_defaults_examples_repo_to_sibling(self) -> None:
        module = _load_script()

        args = module.build_parser().parse_args([])

        assert args.examples_repo is None
        assert args.wheel_dir is None

    def test_smoke_code_checks_import_is_not_from_checkout(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "rpacore-checkout"

        code = module._smoke_code(repo_root)

        assert "imported rpacore from checkout" in code
        assert repr(str(repo_root.resolve())) in code
        assert "transition_sink=transitions.append" in code
        assert "ctx.state" in code

    def test_run_prints_command_and_checks_result(self, tmp_path: Path, capsys) -> None:
        module = _load_script()
        command = ["python", "--version"]

        with patch.object(module.subprocess, "run") as run:
            module._run(command, cwd=tmp_path, allowed_roots=(tmp_path,))

        run.assert_called_once_with(command, cwd=tmp_path, check=True)
        assert capsys.readouterr().out.strip() == "+ python --version"

    def test_run_rejects_cwd_outside_allowed_roots(self, tmp_path: Path) -> None:
        module = _load_script()
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()

        try:
            module._run(["python", "--version"], cwd=outside, allowed_roots=(root,))
        except module.ValidationError as exc:
            assert "outside allowed roots" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_validation_error_uses_shared_base(self) -> None:
        module = _load_script()

        assert issubclass(module.ValidationError, ValidationFailure)
        assert module.ValidationError is SharedValidationError

    def test_run_converts_subprocess_errors(self, tmp_path: Path) -> None:
        module = _load_script()
        error = subprocess.CalledProcessError(1, ["bad"])

        with patch.object(module.subprocess, "run", side_effect=error):
            try:
                module._run(["bad"], cwd=tmp_path, allowed_roots=(tmp_path,))
            except module.ValidationError as exc:
                assert "command failed with exit code 1" in str(exc)
                assert exc.__cause__ is error
            else:
                raise AssertionError("Expected ValidationError")

    def test_remove_tree_raises_when_directory_remains(self, tmp_path: Path) -> None:
        module = _load_script()
        path = tmp_path / "locked"
        path.mkdir()

        with patch.object(module.shutil, "rmtree"):
            try:
                module._remove_tree(path)
            except OSError as exc:
                assert "left directory behind" in str(exc)
            else:
                raise AssertionError("Expected OSError")

    def test_remove_tree_logs_chmod_failure(self, tmp_path: Path) -> None:
        module = _load_script()
        path = tmp_path / "locked"
        path.mkdir()

        def fail_with_onerror(target: Path, *, onerror) -> None:
            onerror(module.os.unlink, target / "file.txt", (PermissionError, PermissionError("locked"), None))

        with patch.object(module.shutil, "rmtree", side_effect=fail_with_onerror):
            with patch.object(module.os, "chmod", side_effect=OSError("chmod failed")):
                with patch.object(module.logging, "warning") as warning:
                    try:
                        module._remove_tree(path)
                    except OSError as exc:
                        assert "left directory behind" in str(exc)
                    else:
                        raise AssertionError("Expected OSError")

        warning.assert_called_once()
        assert warning.call_args.args[:2] == (
            "Failed to clean %s after %s: %s",
            path / "file.txt",
        )

    def test_validate_installed_wheel_runs_expected_steps(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        repo_root.mkdir()
        work_dir.mkdir()
        calls: list[tuple[list[str], Path]] = []

        def fake_run(command: list[str], *, cwd: Path, allowed_roots: tuple[Path, ...]) -> None:
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
                        wheel_dir=None,
                        examples_repo=None,
                        examples_pytest=[],
                    )

        assert len(calls) == 13
        assert calls[0][0][1:3] == ["-m", "build"]
        assert calls[1][0][1:3] == ["-m", "venv"]
        assert calls[2][0][1:4] == ["-m", "pip", "install"]
        assert calls[3][0][1] == "-c"
        assert calls[4][0][-2:] == ["install", "mypy==2.1.0"]
        assert calls[5][0][1:4] == ["-m", "mypy", "--no-incremental"]
        assert calls[6][0][-1] == "version"
        assert calls[7][0][-2:] == ["init", "installed_project"]
        assert calls[8][0][1] == "-c"
        assert "steps/greeting.py" in calls[8][0][2]
        assert calls[9][0][-1] == "run"
        assert calls[9][1] == work_dir / "outside" / "installed_project"
        assert calls[10][0][-2:] == ["transaction", "list"]
        assert calls[11][0][-3:] == ["transaction", "list", "--json"]
        assert calls[12][0][-2:] == ["doctor", "--json"]

    def test_validate_installed_wheel_uses_prebuilt_wheel_dir(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        wheel_dir = tmp_path / "dist"
        repo_root.mkdir()
        wheel_dir.mkdir()
        wheel = wheel_dir / "rpacore-0.1.0-py3-none-any.whl"
        wheel.write_text("", encoding="utf-8")
        calls: list[tuple[list[str], Path]] = []

        def fake_run(command: list[str], *, cwd: Path, allowed_roots: tuple[Path, ...]) -> None:
            calls.append((command, cwd))

        with patch.object(module, "_run", side_effect=fake_run):
            with patch.object(module, "_venv_python", return_value=work_dir / "venv" / "Scripts" / "python.exe"):
                with patch.object(module, "_venv_script", return_value=work_dir / "venv" / "Scripts" / "rpacore.exe"):
                    module.validate_installed_wheel(
                        repo_root=repo_root,
                        work_dir=work_dir,
                        wheel_dir=wheel_dir,
                        examples_repo=None,
                        examples_pytest=[],
                    )

        assert len(calls) == 12
        assert all(command[1:3] != ["-m", "build"] for command, _cwd in calls)
        assert calls[0][0][1:3] == ["-m", "venv"]
        assert calls[1][0][1:4] == ["-m", "pip", "install"]
        assert calls[1][0][-1] == str(wheel)
        assert calls[3][0][-2:] == ["install", "mypy==2.1.0"]
        assert calls[4][0][1:4] == ["-m", "mypy", "--no-incremental"]
        assert calls[-1][0][-2:] == ["doctor", "--json"]

    def test_validate_installed_wheel_removes_existing_generated_project(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        generated_project = work_dir / "outside" / "installed_project"
        repo_root.mkdir()
        generated_project.mkdir(parents=True)

        def fake_run(command: list[str], *, cwd: Path, allowed_roots: tuple[Path, ...]) -> None:
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
                        wheel_dir=None,
                        examples_repo=None,
                        examples_pytest=[],
                    )

        assert not generated_project.exists()

    def test_validate_installed_wheel_runs_examples_when_paths_are_explicit(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        examples_repo = tmp_path / "examples"
        repo_root.mkdir()
        work_dir.mkdir()
        examples_repo.mkdir()
        (examples_repo / "examples" / "rest_api_batch" / "tests").mkdir(parents=True)
        calls: list[tuple[list[str], Path]] = []

        def fake_run(command: list[str], *, cwd: Path, allowed_roots: tuple[Path, ...]) -> None:
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
                        wheel_dir=None,
                        examples_repo=examples_repo,
                        examples_pytest=["examples/rest_api_batch/tests"],
                    )

        assert len(calls) == 15
        assert calls[-2][0][-2:] == ["install", "pytest"]
        assert calls[-2][1] == work_dir / "outside"
        assert calls[-1][0][-3:] == ["pytest", "tests", "-q"]
        assert calls[-1][1] == examples_repo / "examples" / "rest_api_batch"

    def test_validate_installed_wheel_rejects_escaped_example_test_path(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        examples_repo = tmp_path / "examples"
        repo_root.mkdir()
        work_dir.mkdir()
        examples_repo.mkdir()

        def fake_run(command: list[str], *, cwd: Path, allowed_roots: tuple[Path, ...]) -> None:
            if command[1:3] == ["-m", "build"]:
                wheelhouse = Path(command[-1])
                wheelhouse.mkdir(parents=True, exist_ok=True)
                (wheelhouse / "rpacore-0.1.0-py3-none-any.whl").write_text("", encoding="utf-8")

        with patch.object(module, "_run", side_effect=fake_run):
            with patch.object(module, "_venv_python", return_value=work_dir / "venv" / "Scripts" / "python.exe"):
                with patch.object(module, "_venv_script", return_value=work_dir / "venv" / "Scripts" / "rpacore.exe"):
                    try:
                        module.validate_installed_wheel(
                            repo_root=repo_root,
                            work_dir=work_dir,
                            wheel_dir=None,
                            examples_repo=examples_repo,
                            examples_pytest=["../outside"],
                        )
                    except module.ValidationError as exc:
                        assert "outside allowed roots" in str(exc)
                    else:
                        raise AssertionError("Expected ValidationError")

    def test_validate_installed_wheel_requires_examples_pytest_with_examples_repo(self, tmp_path: Path) -> None:
        module = _load_script()

        try:
            module.validate_installed_wheel(
                repo_root=tmp_path,
                work_dir=tmp_path / "work",
                wheel_dir=None,
                examples_repo=tmp_path / "examples",
                examples_pytest=[],
            )
        except module.ValidationError as exc:
            assert str(exc) == "--examples-pytest is required when --examples-repo is provided"
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_installed_wheel_requires_examples_repo_with_examples_pytest(self, tmp_path: Path) -> None:
        module = _load_script()

        try:
            module.validate_installed_wheel(
                repo_root=tmp_path,
                work_dir=tmp_path / "work",
                wheel_dir=None,
                examples_repo=None,
                examples_pytest=["tests"],
            )
        except module.ValidationError as exc:
            assert str(exc) == "--examples-repo is required when --examples-pytest is used"
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_installed_wheel_cleans_generated_dirs_on_failure(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        repo_root.mkdir()

        try:
            with patch.object(module, "_run", side_effect=module.ValidationError("boom")):
                module.validate_installed_wheel(
                    repo_root=repo_root,
                    work_dir=work_dir,
                    wheel_dir=None,
                    examples_repo=None,
                    examples_pytest=[],
                )
        except module.ValidationError as exc:
            assert "boom" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

        assert not (work_dir / "wheelhouse").exists()
        assert not (work_dir / "outside").exists()

    def test_validate_installed_wheel_preserves_keyboard_interrupt_when_cleanup_fails(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        repo_root.mkdir()
        interrupt = KeyboardInterrupt()

        with patch.object(module, "_run", side_effect=interrupt):
            with patch.object(module, "_remove_tree", side_effect=OSError("locked")):
                try:
                    module.validate_installed_wheel(
                        repo_root=repo_root,
                        work_dir=work_dir,
                        wheel_dir=None,
                        examples_repo=None,
                        examples_pytest=[],
                    )
                except KeyboardInterrupt as exc:
                    assert exc is interrupt
                else:
                    raise AssertionError("Expected KeyboardInterrupt")

    def test_validate_installed_wheel_reports_cleanup_residue(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        residue = work_dir / "outside"
        repo_root.mkdir()
        residue.mkdir(parents=True)

        def fake_remove_tree(path: Path) -> None:
            if path == residue:
                return
            if path.exists():
                module.shutil.rmtree(path)

        try:
            with patch.object(module.logging, "warning") as warning:
                with patch.object(module, "_run", side_effect=module.ValidationError("boom")):
                    with patch.object(module, "_remove_tree", side_effect=fake_remove_tree):
                        module.validate_installed_wheel(
                            repo_root=repo_root,
                            work_dir=work_dir,
                            wheel_dir=None,
                            examples_repo=None,
                            examples_pytest=[],
                        )
        except module.ValidationError as exc:
            assert "boom" in str(exc)
            warning.assert_called_once_with(
                "Installed-wheel cleanup left generated directories behind: %s",
                str(residue),
            )
        else:
            raise AssertionError("Expected ValidationError")

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

        rmtree.assert_called_once()
        assert rmtree.call_args.args == (work_dir,)
