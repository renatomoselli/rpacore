"""Tests for validating external examples against a built wheel."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch


def _load_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "validate_examples_against_wheel.py"
    spec = importlib.util.spec_from_file_location("validate_examples_against_wheel", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclass decoration resolves postponed annotations through sys.modules.
    previous_module = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_module is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous_module
    return module


class TestExamplesWheelValidationScript:
    def test_example_dirs_discovers_only_project_directories(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_root = tmp_path / "rpacore-examples"
        examples_dir = examples_root / "examples"
        (examples_dir / "json_event_log_processor").mkdir(parents=True)
        (examples_dir / ".pytest_cache").mkdir()
        (examples_dir / "README.md").write_text("not a directory", encoding="utf-8")

        discovered = script._example_dirs(examples_root)

        assert [path.name for path in discovered] == ["json_event_log_processor"]

    def test_example_dirs_logs_generated_directory_skip(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_root = tmp_path / "rpacore-examples"
        examples_dir = examples_root / "examples"
        (examples_dir / "build").mkdir(parents=True)

        with patch.object(script.logger, "warning") as warning:
            assert script._example_dirs(examples_root) == []

        warning.assert_called_once()

    def test_example_dirs_requires_examples_directory(self, tmp_path: Path) -> None:
        script = _load_script()

        try:
            script._example_dirs(tmp_path / "rpacore-examples")
        except script.ValidationError as exc:
            assert "examples directory does not exist" in str(exc)
        else:
            raise AssertionError("expected ValidationError")

    def test_filter_example_dirs_includes_and_excludes_by_name(self, tmp_path: Path) -> None:
        script = _load_script()
        example_dirs = [
            tmp_path / "checkpoint_resume",
            tmp_path / "json_event_log_processor",
            tmp_path / "rpa_challenge",
        ]

        filtered = script._filter_example_dirs(
            example_dirs,
            include_examples={"checkpoint_resume", "json_event_log_processor"},
            exclude_examples={"checkpoint_resume"},
        )

        assert [path.name for path in filtered] == ["json_event_log_processor"]

    def test_filter_example_dirs_rejects_unknown_includes(self, tmp_path: Path) -> None:
        script = _load_script()

        try:
            script._filter_example_dirs(
                [tmp_path / "checkpoint_resume"],
                include_examples={"missing_example"},
                exclude_examples=set(),
            )
        except script.ValidationError as exc:
            assert "requested examples do not exist" in str(exc)
        else:
            raise AssertionError("expected ValidationError")

    def test_filter_example_dirs_ignores_unknown_excludes(self, tmp_path: Path) -> None:
        script = _load_script()
        example_dirs = [tmp_path / "checkpoint_resume"]

        filtered = script._filter_example_dirs(
            example_dirs,
            include_examples=set(),
            exclude_examples={"renamed_example"},
        )

        assert filtered == example_dirs

    def test_validated_examples_repo_requires_examples_directory(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        examples_repo.mkdir()

        try:
            script._validated_examples_repo(examples_repo)
        except script.ValidationError as exc:
            assert "examples directory does not exist" in str(exc)
        else:
            raise AssertionError("expected ValidationError")

    def test_classify_example_skips_manual_main_by_default(self) -> None:
        script = _load_script()

        assert script._classify_example("json_event_log_processor", False) == ("deterministic", None)

        category, reason = script._classify_example("rpa_challenge", False)
        assert category == "manual"
        assert reason == "requires browser automation setup"

        assert script._classify_example("rpa_challenge", True) == ("manual-included", None)

    def test_unknown_example_uses_manual_fallback(self) -> None:
        script = _load_script()

        assert script._classify_example("new_external_example", False) == (
            "manual",
            "not in deterministic main allowlist",
        )

    def test_test_targets_include_only_directories_with_pytest_files(self, tmp_path: Path) -> None:
        script = _load_script()
        example_dir = tmp_path / "windows_calculator"
        (example_dir / "tests").mkdir(parents=True)
        (example_dir / "test").mkdir()
        (example_dir / "tests" / "test_main_helpers.py").write_text("def test_ok(): pass", encoding="utf-8")

        assert script._test_targets(example_dir) == ["tests"]

    def test_expected_nonzero_exit_codes_do_not_fail_example(self) -> None:
        script = _load_script()
        result = script.ExampleResult(
            name="database_reconciliation",
            path="example",
            venv_path="example/.venv",
            category="deterministic",
            commands=[
                script.CommandRecord(
                    name="run_main",
                    command=["python", "main.py"],
                    cwd="example",
                    exit_code=1,
                    duration_seconds=0.1,
                    stdout="",
                    stderr="expected business discrepancies",
                    expected_exit_codes=[0, 1],
                )
            ],
        )

        assert result.status == "pass"

    def test_requires_playwright_detects_requirement(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "acme_work_items"
        example_dir.mkdir(parents=True)
        (example_dir / "requirements.txt").write_text(
            "rpacore==0.1.0\nplaywright>=1.59.0\nplaywright-extra>=1.0\n",
            encoding="utf-8",
        )

        assert script._requires_playwright(example_dir, examples_repo) is True

    def test_requires_playwright_rejects_prefix_packages(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "acme_work_items"
        example_dir.mkdir(parents=True)
        (example_dir / "requirements.txt").write_text("playwright-extra>=1.0\n", encoding="utf-8")

        assert script._requires_playwright(example_dir, examples_repo) is False

    def test_example_venv_path_honors_mode(self, tmp_path: Path) -> None:
        script = _load_script()
        example_dir = tmp_path / "examples" / "json_event_log_processor"
        venv_root = tmp_path / "work" / "venvs"

        assert script._example_venv_path(example_dir, venv_mode="in-place", venv_root=venv_root) == example_dir / ".venv"
        assert script._example_venv_path(example_dir, venv_mode="work-dir", venv_root=venv_root) == venv_root / "json_event_log_processor"

    def test_sha256_hashes_file_contents(self, tmp_path: Path) -> None:
        script = _load_script()
        sample = tmp_path / "sample.txt"
        sample.write_text("hello", encoding="utf-8")

        assert script._sha256(sample) == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    def test_truncate_preserves_short_text_and_marks_long_text(self) -> None:
        script = _load_script()

        assert script._truncate("short") == "short"

        truncated = script._truncate("x" * (script.MAX_CAPTURE_CHARS + 3))
        assert truncated.startswith("x" * script.MAX_CAPTURE_CHARS)
        assert truncated.endswith("...<truncated 3 chars>")

    def test_timeout_stderr_keeps_timeout_note_after_truncation(self) -> None:
        script = _load_script()

        stderr = script._timeout_stderr("x" * (script.MAX_CAPTURE_CHARS + 3), timeout_seconds=12)

        assert "...<truncated 3 chars>" in stderr
        assert stderr.endswith("command timed out after 12 seconds")

    def test_skipped_command_is_passing_with_reason(self, tmp_path: Path) -> None:
        script = _load_script()

        command = script._skipped("pytest", cwd=tmp_path, reason="dependency setup failed")

        assert command.passed is True
        assert command.skipped is True
        assert command.skip_reason == "dependency setup failed"

    def test_remove_tree_raises_when_directory_remains(self, tmp_path: Path) -> None:
        script = _load_script()
        target = tmp_path / "venv"
        target.mkdir()

        with patch.object(script.shutil, "rmtree", return_value=None):
            try:
                script._remove_tree(target)
            except OSError as exc:
                assert "cleanup left directory behind" in str(exc)
            else:
                raise AssertionError("expected OSError")

    def test_copy_example_workspace_ignores_generated_directories(self, tmp_path: Path) -> None:
        script = _load_script()
        source = tmp_path / "source"
        destination = tmp_path / "workspace"
        source.mkdir()
        (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (source / ".venv").mkdir()
        (source / ".venv" / "pyvenv.cfg").write_text("stale\n", encoding="utf-8")
        (source / "build").mkdir()
        (source / "build" / "artifact.txt").write_text("stale\n", encoding="utf-8")

        copied = script._copy_example_workspace(source, destination)

        assert copied == destination
        assert (destination / "main.py").exists()
        assert not (destination / ".venv").exists()
        assert not (destination / "build").exists()

    def test_copy_example_workspace_removes_partial_destination_on_failure(self, tmp_path: Path) -> None:
        script = _load_script()
        source = tmp_path / "source"
        destination = tmp_path / "workspace"
        source.mkdir()
        (source / "main.py").write_text("print('ok')\n", encoding="utf-8")

        def fail_copytree(*args, **kwargs):
            destination.mkdir(parents=True)
            (destination / "partial.txt").write_text("partial\n", encoding="utf-8")
            raise OSError("copy failed")

        with patch.object(script.shutil, "copytree", side_effect=fail_copytree):
            try:
                script._copy_example_workspace(source, destination)
            except OSError as exc:
                assert str(exc) == "copy failed"
            else:
                raise AssertionError("expected OSError")

        assert not destination.exists()

    def test_copy_example_workspace_reports_cleanup_failure_after_copy_failure(self, tmp_path: Path) -> None:
        script = _load_script()
        source = tmp_path / "source"
        destination = tmp_path / "workspace"
        source.mkdir()
        (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
        destination.mkdir()

        def fail_remove_tree(path: Path) -> None:
            if path == destination and getattr(fail_remove_tree, "calls", 0) > 0:
                raise OSError("cleanup locked")
            fail_remove_tree.calls = getattr(fail_remove_tree, "calls", 0) + 1

        def fail_copytree(*args, **kwargs):
            raise OSError("copy failed")

        with patch.object(script, "_remove_tree", side_effect=fail_remove_tree):
            with patch.object(script.shutil, "copytree", side_effect=fail_copytree):
                try:
                    script._copy_example_workspace(source, destination)
                except OSError as exc:
                    assert str(exc).startswith("copy failed and partial workspace cleanup failed:")
                    assert isinstance(exc.__cause__, OSError)
                    assert exc.__cause__.__notes__ == ["original copy failure: copy failed"]
                else:
                    raise AssertionError("expected OSError")

    def test_cleanup_failed_setup_venv_logs_cleanup_failure(self, tmp_path: Path) -> None:
        script = _load_script()
        venv_path = tmp_path / ".venv"

        with patch.object(script, "_remove_tree", side_effect=OSError("locked")):
            with patch.object(script.logger, "warning") as warning:
                script._cleanup_failed_setup_venv(venv_path)

        warning.assert_called()
        assert (venv_path / script.BROKEN_VENV_SENTINEL).exists()

    def test_cleanup_failed_setup_venv_raises_when_sentinel_cannot_be_written(self, tmp_path: Path) -> None:
        script = _load_script()
        venv_path = tmp_path / ".venv"
        sentinel = venv_path / script.BROKEN_VENV_SENTINEL
        original_write_text = Path.write_text

        def fail_sentinel_write(path: Path, text: str, *args, **kwargs):
            if path == sentinel:
                raise OSError("read only")
            return original_write_text(path, text, *args, **kwargs)

        with patch.object(script, "_remove_tree", side_effect=OSError("locked")):
            with patch.object(Path, "write_text", autospec=True, side_effect=fail_sentinel_write):
                try:
                    script._cleanup_failed_setup_venv(venv_path)
                except OSError as exc:
                    assert "failed to mark invalid example venv" in str(exc)
                else:
                    raise AssertionError("expected OSError")

    def test_build_wheel_failure_raises_validation_error(self, tmp_path: Path) -> None:
        script = _load_script()
        wheelhouse = tmp_path / "wheelhouse"

        def fake_run(*_args, **_kwargs):
            (wheelhouse / "partial.whl").write_text("partial", encoding="utf-8")
            return script.CommandRecord(
                name="build_wheel",
                command=["python", "-m", "build"],
                cwd=str(tmp_path),
                exit_code=1,
                duration_seconds=0.1,
                stdout="",
                stderr="build failed",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            try:
                script._build_wheel(tmp_path, wheelhouse, timeout_seconds=300)
            except script.ValidationError as exc:
                assert "wheel build failed" in str(exc)
            else:
                raise AssertionError("expected ValidationError")
        assert not wheelhouse.exists()

    def test_build_wheel_success_without_wheel_raises_validation_error(self, tmp_path: Path) -> None:
        script = _load_script()
        wheelhouse = tmp_path / "wheelhouse"

        def fake_run(*_args, **_kwargs):
            return script.CommandRecord(
                name="build_wheel",
                command=["python", "-m", "build"],
                cwd=str(tmp_path),
                exit_code=0,
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            try:
                script._build_wheel(tmp_path, wheelhouse, timeout_seconds=300)
            except script.ValidationError as exc:
                assert "no wheel was created" in str(exc)
            else:
                raise AssertionError("expected ValidationError")
        assert not wheelhouse.exists()

    def test_build_wheel_multiple_wheels_raises_validation_error(self, tmp_path: Path) -> None:
        script = _load_script()
        wheelhouse = tmp_path / "wheelhouse"

        def fake_run(*_args, **_kwargs):
            (wheelhouse / "rpacore-0.1.0-py3-none-any.whl").write_text("wheel", encoding="utf-8")
            (wheelhouse / "rpacore-0.1.0-cp311-cp311-win_amd64.whl").write_text("wheel", encoding="utf-8")
            return script.CommandRecord(
                name="build_wheel",
                command=["python", "-m", "build"],
                cwd=str(tmp_path),
                exit_code=0,
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            try:
                script._build_wheel(tmp_path, wheelhouse, timeout_seconds=300)
            except script.BuildValidationError as exc:
                assert "multiple wheels" in str(exc)
            else:
                raise AssertionError("expected BuildValidationError")
        assert not wheelhouse.exists()

    def test_build_wheel_success_returns_created_wheel(self, tmp_path: Path) -> None:
        script = _load_script()
        wheelhouse = tmp_path / "wheelhouse"
        wheel = wheelhouse / "rpacore-0.1.0-py3-none-any.whl"

        def fake_run(*_args, **_kwargs):
            wheel.write_text("wheel", encoding="utf-8")
            return script.CommandRecord(
                name="build_wheel",
                command=["python", "-m", "build"],
                cwd=str(tmp_path),
                exit_code=0,
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            built_wheel, command_record = script._build_wheel(tmp_path, wheelhouse, timeout_seconds=300)

        assert built_wheel == wheel
        assert command_record.passed is True

    def test_build_failure_manifest_records_failed_result(self, tmp_path: Path) -> None:
        script = _load_script()
        repo_root = tmp_path / "rpacore"
        examples_repo = tmp_path / "rpacore-examples"
        (examples_repo / "examples").mkdir(parents=True)
        output_dir = tmp_path / "out"
        repo_root.mkdir()

        build_command_record = script.CommandRecord(
            name="build_wheel",
            command=["python", "-m", "build"],
            cwd=str(repo_root),
            exit_code=2,
            duration_seconds=1.2,
            stdout="build stdout",
            stderr="build stderr",
        )

        def fail_build(*_args, **_kwargs):
            raise script.BuildValidationError("wheel build failed with exit code 2", build_command_record)

        with patch.object(script, "_build_wheel", side_effect=fail_build):
            manifest = script.validate_examples_against_wheel(
                repo_root=repo_root,
                examples_repo=examples_repo,
                output_dir=output_dir,
                work_dir=tmp_path / "work",
                venv_mode="work-dir",
                recreate_venvs=True,
                run_main="deterministic",
                install_playwright_browsers=False,
                timeout_seconds=300,
            )

        assert manifest["result"]["status"] == "fail"
        assert manifest["result"]["example_count"] == 0
        assert manifest["build"]["exit_code"] == 2
        assert manifest["build"]["stdout"] == "build stdout"
        assert manifest["build"]["stderr"] == "build stderr"
        assert manifest["wheel"] == {
            "build_failed": True,
            "name": None,
            "path": None,
            "sha256": None,
        }
        assert not (tmp_path / "work" / "wheelhouse" / ".rpacore-build-failed").match("rpacore-*.whl")

    def test_upgrade_pip_failure_skips_remaining_setup(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "json_event_log_processor"
        venv_path = example_dir / ".venv"
        python = script._venv_python(venv_path)
        example_dir.mkdir(parents=True, exist_ok=True)

        def fake_run(name, cmd, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            if name == "create_venv":
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in cmd],
                cwd=str(cwd),
                exit_code=1 if name == "upgrade_pip" else 0,
                duration_seconds=0.1,
                stdout="",
                stderr="upgrade failed" if name == "upgrade_pip" else "",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=venv_path,
                recreate_venv=True,
                run_main="deterministic",
                install_playwright_browsers=False,
                timeout_seconds=300,
            )

        assert [command.name for command in result.commands] == [
            "create_venv",
            "upgrade_pip",
            "uninstall_rpacore",
            "install_rpacore_wheel",
            "installed_import",
            "pytest",
            "run_main",
        ]
        assert all(command.skipped for command in result.commands[2:])

    def test_uninstall_rpacore_failure_skips_wheel_install(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "json_event_log_processor"
        venv_path = example_dir / ".venv"
        python = script._venv_python(venv_path)
        example_dir.mkdir(parents=True, exist_ok=True)

        def fake_run(name, cmd, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            if name == "create_venv":
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in cmd],
                cwd=str(cwd),
                exit_code=1 if name == "uninstall_rpacore" else 0,
                duration_seconds=0.1,
                stdout="",
                stderr="uninstall failed" if name == "uninstall_rpacore" else "",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=venv_path,
                recreate_venv=True,
                run_main="deterministic",
                install_playwright_browsers=False,
                timeout_seconds=300,
            )

        assert [command.name for command in result.commands] == [
            "create_venv",
            "upgrade_pip",
            "uninstall_rpacore",
            "install_rpacore_wheel",
            "installed_import",
            "pytest",
            "run_main",
        ]
        assert all(command.skipped for command in result.commands[3:])

    def test_installed_import_failure_skips_requirements_tests_and_main(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "json_event_log_processor"
        venv_path = example_dir / ".venv"
        python = script._venv_python(venv_path)
        example_dir.mkdir(parents=True, exist_ok=True)
        (example_dir / "requirements.txt").write_text("example-package==1\n", encoding="utf-8")
        (example_dir / "tests").mkdir()
        (example_dir / "tests" / "test_example.py").write_text("def test_ok(): pass", encoding="utf-8")

        def fake_run(name, cmd, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            if name == "create_venv":
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in cmd],
                cwd=str(cwd),
                exit_code=1 if name == "installed_import" else 0,
                duration_seconds=0.1,
                stdout="",
                stderr="import failed" if name == "installed_import" else "",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=venv_path,
                recreate_venv=True,
                run_main="deterministic",
                install_playwright_browsers=False,
                timeout_seconds=300,
            )

        command_names = [command.name for command in result.commands]
        assert "install_requirements:requirements.txt" not in command_names
        assert command_names[-2:] == ["pytest", "run_main"]
        assert all(command.skipped for command in result.commands[-2:])

    def test_broken_venv_sentinel_blocks_reuse_when_cleanup_fails(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "json_event_log_processor"
        venv_path = example_dir / ".venv"
        example_dir.mkdir(parents=True, exist_ok=True)
        venv_path.mkdir()
        (venv_path / script.BROKEN_VENV_SENTINEL).write_text("cleanup failed\n", encoding="utf-8")

        with patch.object(script, "_remove_tree", side_effect=OSError("locked")):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=venv_path,
                recreate_venv=False,
                run_main="deterministic",
                install_playwright_browsers=False,
                timeout_seconds=300,
            )

        assert result.status == "fail"
        assert result.commands[0].name == "venv_cleanup_required"
        assert all(command.skipped for command in result.commands[1:])

    def test_dependency_failure_skips_import_tests_and_main(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "pdf_invoice_extraction"
        venv_path = example_dir / ".venv"
        python = script._venv_python(venv_path)
        example_dir.mkdir(parents=True, exist_ok=True)
        (example_dir / "requirements.txt").write_text("missing-package==0\n", encoding="utf-8")
        (example_dir / "tests").mkdir()
        (example_dir / "tests" / "test_example.py").write_text("def test_ok(): pass", encoding="utf-8")

        def fake_run(name, command, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            if name == "create_venv":
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in command],
                cwd=str(cwd),
                exit_code=1 if name == "install_requirements:example:requirements.txt" else 0,
                duration_seconds=0.1,
                stdout="",
                stderr="install failed" if name == "install_requirements:example:requirements.txt" else "",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=venv_path,
                recreate_venv=True,
                run_main="deterministic",
                install_playwright_browsers=False,
                timeout_seconds=300,
            )

        assert result.status == "fail"
        assert not venv_path.exists()
        command_names = [command.name for command in result.commands]
        assert "installed_import" in command_names
        assert command_names[-2:] == ["pytest", "run_main"]
        assert all(command.skipped for command in result.commands[-2:])

    def test_playwright_failure_keeps_installed_import_record(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "acme_work_items"
        venv_path = example_dir / ".venv"
        python = script._venv_python(venv_path)
        example_dir.mkdir(parents=True, exist_ok=True)
        (example_dir / "requirements.txt").write_text("playwright>=1.59.0\n", encoding="utf-8")
        (example_dir / "tests").mkdir()
        (example_dir / "tests" / "test_browser.py").write_text("def test_ok(): pass", encoding="utf-8")

        def fake_run(name, cmd, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            if name == "create_venv":
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in cmd],
                cwd=str(cwd),
                exit_code=1 if name == "install_playwright_browsers" else 0,
                duration_seconds=0.1,
                stdout="",
                stderr="browser failed" if name == "install_playwright_browsers" else "",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=venv_path,
                recreate_venv=True,
                run_main="deterministic",
                install_playwright_browsers=True,
                timeout_seconds=300,
            )

        command_names = [command.name for command in result.commands]
        assert "installed_import" in command_names
        assert command_names[-2:] == ["pytest", "run_main"]
        assert all(command.skipped for command in result.commands[-2:])

    def test_pytest_install_failure_cleans_venv_and_skips_main(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "json_event_log_processor"
        venv_path = example_dir / ".venv"
        python = script._venv_python(venv_path)
        example_dir.mkdir(parents=True, exist_ok=True)
        (example_dir / "tests").mkdir()
        (example_dir / "tests" / "test_example.py").write_text("def test_ok(): pass", encoding="utf-8")
        (example_dir / "main.py").write_text("print('ok')", encoding="utf-8")

        def fake_run(name, cmd, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            if name == "create_venv":
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in cmd],
                cwd=str(cwd),
                exit_code=1 if name == "install_pytest" else 0,
                duration_seconds=0.1,
                stdout="",
                stderr="pytest install failed" if name == "install_pytest" else "",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=venv_path,
                recreate_venv=True,
                run_main="deterministic",
                install_playwright_browsers=False,
                timeout_seconds=300,
            )

        pytest_command = next(command for command in result.commands if command.name == "pytest:tests")
        run_main = next(command for command in result.commands if command.name == "run_main")
        assert pytest_command.skipped is True
        assert run_main.skipped is True
        assert run_main.skip_reason == "pytest setup failed at install_pytest"
        assert not venv_path.exists()

    def test_create_venv_failure_skips_remaining_steps(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "json_event_log_processor"
        example_dir.mkdir(parents=True)

        def fake_run(name, command, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in command],
                cwd=str(cwd),
                exit_code=1 if name == "create_venv" else 0,
                duration_seconds=0.1,
                stdout="",
                stderr="venv failed" if name == "create_venv" else "",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=example_dir / ".venv",
                recreate_venv=True,
                run_main="deterministic",
                install_playwright_browsers=False,
                timeout_seconds=300,
            )

        assert result.status == "fail"
        assert [command.name for command in result.commands] == [
            "create_venv",
            "installed_import",
            "pytest",
            "run_main",
        ]
        assert all(command.skipped for command in result.commands[1:])

    def test_requirement_install_failure_skips_remaining_requirements(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "pdf_invoice_extraction"
        example_dir.mkdir(parents=True)
        (examples_repo / "requirements.txt").write_text("root-package==1\n", encoding="utf-8")
        (example_dir / "requirements.txt").write_text("example-package==1\n", encoding="utf-8")
        (example_dir / "requirements-test.txt").write_text("test-package==1\n", encoding="utf-8")
        result = script.ExampleResult(
            name="pdf_invoice_extraction",
            path=str(example_dir),
            venv_path=str(example_dir / ".venv"),
            category="manual",
        )

        def fake_run(name, command, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in command],
                cwd=str(cwd),
                exit_code=1 if name == "install_requirements:repo:requirements.txt" else 0,
                duration_seconds=0.1,
                stdout="",
                stderr="install failed" if name == "install_requirements:repo:requirements.txt" else "",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            script._pip_install_requirements(
                result,
                tmp_path / "python.exe",
                example_dir,
                examples_repo,
                timeout_seconds=300,
            )

        assert [command.name for command in result.commands] == [
            "install_requirements:repo:requirements.txt",
            "install_requirements:example:requirements.txt",
            "install_requirements:example:requirements-test.txt",
        ]
        assert result.commands[1].skipped is True
        assert result.commands[2].skipped is True
        assert result.commands[1].skip_reason == "dependency setup failed at install_requirements:repo:requirements.txt"

    def test_requirement_install_records_skip_when_no_requirements_exist(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "checkpoint_resume"
        example_dir.mkdir(parents=True)
        result = script.ExampleResult(
            name="checkpoint_resume",
            path=str(example_dir),
            venv_path=str(example_dir / ".venv"),
            category="deterministic",
        )

        with patch.object(script, "_run") as run:
            script._pip_install_requirements(
                result,
                tmp_path / "python.exe",
                example_dir,
                examples_repo,
                timeout_seconds=300,
            )

        run.assert_not_called()
        assert [command.name for command in result.commands] == ["install_requirements:skip:no_files"]
        assert result.commands[0].skipped is True
        assert result.commands[0].skip_reason == "no requirements files"

    def test_requirement_install_success_runs_all_requirement_files(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "json_event_log_processor"
        example_dir.mkdir(parents=True)
        (examples_repo / "requirements.txt").write_text("root-package==1\n", encoding="utf-8")
        (example_dir / "requirements.txt").write_text("example-package==1\n", encoding="utf-8")
        result = script.ExampleResult(
            name="json_event_log_processor",
            path=str(example_dir),
            venv_path=str(example_dir / ".venv"),
            category="deterministic",
        )

        def fake_run(name, command, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in command],
                cwd=str(cwd),
                exit_code=0,
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

        with patch.object(script, "_run", side_effect=fake_run):
            script._pip_install_requirements(
                result,
                tmp_path / "python.exe",
                example_dir,
                examples_repo,
                timeout_seconds=300,
            )

        assert [command.name for command in result.commands] == [
            "install_requirements:repo:requirements.txt",
            "install_requirements:example:requirements.txt",
        ]
        assert all(not command.skipped and command.passed for command in result.commands)

    def test_reused_manual_venv_runs_main_all_without_playwright_install(self, tmp_path: Path) -> None:
        script = _load_script()
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "rpa_challenge"
        venv_path = example_dir / ".venv"
        python = script._venv_python(venv_path)
        example_dir.mkdir(parents=True)
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        (example_dir / "requirements.txt").write_text("rpacore==0.1.0\n", encoding="utf-8")
        (example_dir / "main.py").write_text("print('run')", encoding="utf-8")

        def fake_run(name, command, *, cwd, env=None, timeout_seconds, allowed_roots=(), expected_exit_codes=None):
            return script.CommandRecord(
                name=name,
                command=[str(part) for part in command],
                cwd=str(cwd),
                exit_code=0,
                duration_seconds=0.1,
                stdout="",
                stderr="",
                expected_exit_codes=sorted(expected_exit_codes or {0}),
            )

        with patch.object(script, "_run", side_effect=fake_run):
            result = script._validate_example(
                example_dir,
                examples_repo=examples_repo,
                wheel=tmp_path / "rpacore.whl",
                venv_path=venv_path,
                recreate_venv=False,
                run_main="all",
                install_playwright_browsers=True,
                timeout_seconds=300,
            )

        command_names = [command.name for command in result.commands]
        assert "create_venv" not in command_names
        assert "install_playwright_browsers" not in command_names
        assert result.category == "manual-included"
        assert command_names[-2:] == ["pytest", "run_main"]
        assert result.commands[-1].skipped is False

    def test_run_rejects_cwd_outside_allowed_roots(self, tmp_path: Path) -> None:
        script = _load_script()
        allowed = tmp_path / "allowed"
        outside = tmp_path / "outside"
        allowed.mkdir()
        outside.mkdir()

        try:
            script._run(
                "outside",
                [sys.executable, "-c", "print('nope')"],
                cwd=outside,
                timeout_seconds=1,
                allowed_roots=(allowed,),
            )
        except script.ValidationError as exc:
            assert "resolves outside allowed roots" in str(exc)
        else:
            raise AssertionError("expected ValidationError")

    def test_write_outputs_removes_temp_files_when_summary_write_fails(self, tmp_path: Path) -> None:
        script = _load_script()
        manifest = {
            "generated_at": "now",
            "result": {
                "status": "pass",
                "example_count": 0,
                "failed_example_count": 0,
                "skipped_example_count": 0,
                "command_count": 1,
                "failed_command_count": 0,
                "skipped_command_count": 0,
            },
            "wheel": {"name": "rpacore.whl", "sha256": "abc"},
            "examples": [],
        }
        summary_tmp = tmp_path / "examples-wheel-validation.md.tmp"
        original_write_text = Path.write_text

        def fail_summary_write(path: Path, text: str, *args, **kwargs):
            if path == summary_tmp:
                raise OSError("summary failed")
            return original_write_text(path, text, *args, **kwargs)

        with patch.object(Path, "write_text", autospec=True, side_effect=fail_summary_write):
            try:
                script._write_outputs(manifest, tmp_path)
            except OSError as exc:
                assert "summary failed" in str(exc)
            else:
                raise AssertionError("expected OSError")

        assert not (tmp_path / "examples-wheel-validation.json.tmp").exists()
        assert not summary_tmp.exists()
        assert not (tmp_path / "examples-wheel-validation.json").exists()

    def test_write_outputs_removes_summary_when_manifest_replace_fails(self, tmp_path: Path) -> None:
        script = _load_script()
        manifest = {
            "generated_at": "now",
            "result": {
                "status": "pass",
                "example_count": 0,
                "failed_example_count": 0,
                "skipped_example_count": 0,
                "command_count": 1,
                "failed_command_count": 0,
                "skipped_command_count": 0,
            },
            "wheel": {"name": "rpacore.whl", "sha256": "abc"},
            "examples": [],
        }
        manifest_tmp = tmp_path / "examples-wheel-validation.json.tmp"
        original_replace = Path.replace

        def fail_manifest_replace(path: Path, target: Path):
            if path == manifest_tmp:
                raise OSError("manifest failed")
            return original_replace(path, target)

        with patch.object(Path, "replace", autospec=True, side_effect=fail_manifest_replace):
            try:
                script._write_outputs(manifest, tmp_path)
            except OSError as exc:
                assert "manifest failed" in str(exc)
            else:
                raise AssertionError("expected OSError")

        assert not (tmp_path / "examples-wheel-validation.md").exists()
        assert not (tmp_path / "examples-wheel-validation.json").exists()

    def test_write_outputs_removes_outputs_when_summary_replace_fails(self, tmp_path: Path) -> None:
        script = _load_script()
        manifest = {
            "generated_at": "now",
            "result": {
                "status": "pass",
                "example_count": 0,
                "failed_example_count": 0,
                "skipped_example_count": 0,
                "command_count": 1,
                "failed_command_count": 0,
                "skipped_command_count": 0,
            },
            "wheel": {"name": "rpacore.whl", "sha256": "abc"},
            "examples": [],
        }
        summary_path = tmp_path / "examples-wheel-validation.md"
        summary_tmp = tmp_path / "examples-wheel-validation.md.tmp"
        summary_path.write_text("old summary\n", encoding="utf-8")
        original_replace = Path.replace

        def fail_summary_replace(path: Path, target: Path):
            if path == summary_tmp:
                raise OSError("summary failed")
            return original_replace(path, target)

        with patch.object(Path, "replace", autospec=True, side_effect=fail_summary_replace):
            try:
                script._write_outputs(manifest, tmp_path)
            except OSError as exc:
                assert "summary failed" in str(exc)
            else:
                raise AssertionError("expected OSError")

        assert not (tmp_path / "examples-wheel-validation.json").exists()
        assert not summary_path.exists()
        assert not summary_tmp.exists()

    def test_write_outputs_publishes_summary_and_manifest(self, tmp_path: Path) -> None:
        script = _load_script()
        manifest = {
            "generated_at": "now",
            "result": {
                "status": "pass",
                "example_count": 0,
                "failed_example_count": 0,
                "skipped_example_count": 0,
                "command_count": 1,
                "failed_command_count": 0,
                "skipped_command_count": 0,
            },
            "wheel": {"name": "rpacore.whl", "sha256": "abc"},
            "examples": [],
        }

        script._write_outputs(manifest, tmp_path)

        assert json.loads((tmp_path / "examples-wheel-validation.json").read_text(encoding="utf-8")) == manifest
        assert "# Examples Wheel Validation" in (tmp_path / "examples-wheel-validation.md").read_text(encoding="utf-8")
        assert not (tmp_path / "examples-wheel-validation.json.tmp").exists()
        assert not (tmp_path / "examples-wheel-validation.md.tmp").exists()

    def test_validate_examples_against_wheel_writes_report(self, tmp_path: Path) -> None:
        script = _load_script()
        repo_root = tmp_path / "rpacore"
        examples_repo = tmp_path / "rpacore-examples"
        example_dir = examples_repo / "examples" / "json_event_log_processor"
        skipped_example_dir = examples_repo / "examples" / "rpa_challenge"
        output_dir = tmp_path / "out"
        work_dir = tmp_path / "work"
        repo_root.mkdir()
        example_dir.mkdir(parents=True)
        skipped_example_dir.mkdir(parents=True)
        (example_dir / "tests").mkdir()

        wheel = work_dir / "wheelhouse" / "rpacore-0.1.0-py3-none-any.whl"

        def fake_build_wheel(repo_root_arg: Path, wheelhouse: Path, *, timeout_seconds: int):
            wheelhouse.mkdir(parents=True, exist_ok=True)
            wheel.write_bytes(b"wheel")
            return wheel, script.CommandRecord(
                name="build_wheel",
                command=["python", "-m", "build"],
                cwd=str(repo_root_arg),
                exit_code=0,
                duration_seconds=0.1,
                stdout="",
                stderr="",
            )

        def fake_validate_example(example_dir_arg: Path, **kwargs):
            return script.ExampleResult(
                name=example_dir_arg.name,
                path=str(example_dir_arg),
                venv_path=str(kwargs["venv_path"]),
                category="deterministic",
                commands=[
                    script.CommandRecord(
                        name="pytest:tests",
                        command=["python", "-m", "pytest", "tests", "-q"],
                        cwd=str(example_dir_arg),
                        exit_code=0,
                        duration_seconds=0.2,
                        stdout="1 passed",
                        stderr="",
                    )
                ],
            )

        with patch.object(script, "_build_wheel", side_effect=fake_build_wheel):
            with patch.object(script, "_validate_example", side_effect=fake_validate_example):
                manifest = script.validate_examples_against_wheel(
                    repo_root=repo_root,
                    examples_repo=examples_repo,
                    output_dir=output_dir,
                    work_dir=work_dir,
                    venv_mode="work-dir",
                    recreate_venvs=True,
                    run_main="deterministic",
                    install_playwright_browsers=True,
                    include_examples={"json_event_log_processor"},
                    exclude_examples=set(),
                    timeout_seconds=300,
                )

        assert manifest["result"]["status"] == "pass"
        assert manifest["result"]["example_count"] == 1
        assert manifest["examples"][0]["venv_path"] == str(
            script._example_venv_path(example_dir, venv_mode="work-dir", venv_root=work_dir / "venvs")
        )
        assert manifest["examples"][0]["path"] == str(work_dir / "examples" / "json_event_log_processor")
        assert not (work_dir / "examples" / "rpa_challenge").exists()
        assert not (work_dir / "examples" / "json_event_log_processor" / ".venv").exists()
        assert (output_dir / "examples-wheel-validation.json").exists()
        assert (output_dir / "examples-wheel-validation.md").exists()

    def test_validate_examples_against_wheel_rejects_missing_examples_repo_before_build(self, tmp_path: Path) -> None:
        script = _load_script()
        repo_root = tmp_path / "rpacore"
        examples_repo = tmp_path / "rpacore-examples"
        repo_root.mkdir()
        examples_repo.mkdir()

        with patch.object(script, "_build_wheel") as build_wheel:
            try:
                script.validate_examples_against_wheel(
                    repo_root=repo_root,
                    examples_repo=examples_repo,
                    output_dir=tmp_path / "out",
                    work_dir=tmp_path / "work",
                    venv_mode="work-dir",
                    recreate_venvs=True,
                    run_main="deterministic",
                    install_playwright_browsers=False,
                    include_examples=None,
                    exclude_examples=None,
                    timeout_seconds=300,
                )
            except script.ValidationError as exc:
                assert "examples directory does not exist" in str(exc)
            else:
                raise AssertionError("expected ValidationError")

        build_wheel.assert_not_called()

    def test_run_records_timeout(self, tmp_path: Path) -> None:
        script = _load_script()

        command = script._run(
            "slow_command",
            [
                sys.executable,
                "-c",
                "import time; time.sleep(10)",
            ],
            cwd=tmp_path,
            timeout_seconds=1,
        )

        assert command.exit_code == 124
        assert command.timed_out is True
        assert "timed out" in command.stderr

    def test_parser_defaults_write_to_public_validation_artifacts_dir(self) -> None:
        script = _load_script()

        args = script.build_parser().parse_args([])

        assert args.output_dir == Path("validation-artifacts/examples-wheel-validation")
        assert ".rpiv" not in args.output_dir.parts

    def test_main_returns_failure_when_required_command_fails(self, tmp_path: Path) -> None:
        script = _load_script()

        def fake_validate_examples_against_wheel(**_kwargs):
            return {"result": {"status": "fail"}}

        with patch.object(script, "validate_examples_against_wheel", side_effect=fake_validate_examples_against_wheel):
            exit_code = script.main(
                [
                    "--repo-root",
                    str(tmp_path / "rpacore"),
                    "--examples-repo",
                    str(tmp_path / "rpacore-examples"),
                    "--output-dir",
                    str(tmp_path / "out"),
                ]
            )

        assert exit_code == 1

    def test_main_allows_failures_when_requested(self, tmp_path: Path) -> None:
        script = _load_script()

        def fake_validate_examples_against_wheel(**_kwargs):
            return {"result": {"status": "fail"}}

        with patch.object(script, "validate_examples_against_wheel", side_effect=fake_validate_examples_against_wheel):
            exit_code = script.main(
                [
                    "--repo-root",
                    str(tmp_path / "rpacore"),
                    "--examples-repo",
                    str(tmp_path / "rpacore-examples"),
                    "--output-dir",
                    str(tmp_path / "out"),
                    "--allow-failures",
                ]
            )

        assert exit_code == 0

    def test_main_returns_success_when_required_commands_pass(self, tmp_path: Path) -> None:
        script = _load_script()

        def fake_validate_examples_against_wheel(**_kwargs):
            return {"result": {"status": "pass"}}

        with patch.object(script, "validate_examples_against_wheel", side_effect=fake_validate_examples_against_wheel):
            exit_code = script.main(
                [
                    "--repo-root",
                    str(tmp_path / "rpacore"),
                    "--examples-repo",
                    str(tmp_path / "rpacore-examples"),
                    "--output-dir",
                    str(tmp_path / "out"),
                ]
            )

        assert exit_code == 0
