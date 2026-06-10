"""Subprocess tests for the rpacore command line interface."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import rpacore.cli as cli_module


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing_python_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT)
        if existing_python_path is None
        else str(REPO_ROOT) + os.pathsep + existing_python_path
    )
    return subprocess.run(
        [sys.executable, "-m", "rpacore.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def write_project(tmp_path: Path, main_source: str, *, manifest: str | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "rpacore.toml").write_text(
        manifest
        if manifest is not None
        else textwrap.dedent("""\
            [project]
            entrypoint = "main:main"

            [storage]
            transaction_db_path = "rpacore.db"
            """),
        encoding="utf-8",
    )
    (project / "main.py").write_text(textwrap.dedent(main_source), encoding="utf-8")
    return project


class TestCliRun:
    def test_none_entrypoint_result_exits_zero(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                return None
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 0
        assert result.stderr == ""

    def test_integer_entrypoint_result_is_process_exit_code(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                return 7
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 7
        assert result.stderr == ""

    def test_entrypoint_can_import_project_modules_during_execution(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                from local_module import exit_code
                return exit_code()
            """,
        )
        (project / "local_module.py").write_text(
            "def exit_code():\n    return 3\n",
            encoding="utf-8",
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 3
        assert result.stderr == ""

    def test_entrypoint_exception_exits_one_with_stderr(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                raise OSError("database open failed")
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 1
        assert "Project execution failed" in result.stderr
        assert "database open failed" in result.stderr

    def test_invalid_manifest_exits_two_with_stderr(self, tmp_path: Path) -> None:
        project = write_project(tmp_path, "def main():\n    return 0\n", manifest="[project]\n")

        result = run_cli("run", cwd=project)

        assert result.returncode == 2
        assert "Project manifest or entrypoint is invalid" in result.stderr

    def test_entrypoint_resolution_failure_exits_two_with_stderr(self, tmp_path: Path) -> None:
        project = write_project(tmp_path, "def other():\n    return 0\n")

        result = run_cli("run", cwd=project)

        assert result.returncode == 2
        assert "Project manifest or entrypoint is invalid" in result.stderr

    def test_invalid_entrypoint_return_value_exits_two(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                return "ok"
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 2
        assert "must return None or an integer exit code" in result.stderr

    def test_out_of_range_entrypoint_return_value_exits_two(self, tmp_path: Path) -> None:
        project = write_project(
            tmp_path,
            """\
            def main():
                return 256
            """,
        )

        result = run_cli("run", cwd=project)

        assert result.returncode == 2
        assert "out-of-range exit code" in result.stderr

    def test_usage_error_exits_two(self, tmp_path: Path) -> None:
        result = run_cli(cwd=tmp_path)

        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_unknown_command_exits_two(self, tmp_path: Path) -> None:
        result = run_cli("bogus", cwd=tmp_path)

        assert result.returncode == 2
        assert "invalid choice" in result.stderr

    def test_empty_pythonpath_is_preserved_for_subprocess(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("PYTHONPATH", "")

        result = run_cli("version", cwd=tmp_path)

        assert result.returncode == 0
        assert result.stderr == ""


class TestCliInit:
    def test_init_creates_project_that_runs_without_edits(self, tmp_path: Path) -> None:
        result = run_cli("init", "demo_project", cwd=tmp_path)

        assert result.returncode == 0
        assert "Created RPA Core project: demo_project" in result.stdout
        project = tmp_path / "demo_project"
        assert (project / "pyproject.toml").exists()
        assert (project / "rpacore.toml").exists()
        assert (project / "config.toml").exists()
        assert (project / "main.py").exists()
        assert (project / "skills" / "__init__.py").exists()
        assert (project / "skills" / "greeting.py").exists()
        assert (project / "tests" / "test_greeting_skill.py").exists()
        assert (project / ".gitignore").exists()

        run_result = run_cli("run", cwd=project)

        assert run_result.returncode == 0
        assert run_result.stderr == ""
        assert (project / "greeting.txt").read_text(encoding="utf-8") == "Hello, Alice\n"
        assert (project / "rpacore.db").exists()

    def test_generated_project_skill_test_passes(self, tmp_path: Path) -> None:
        init_result = run_cli("init", "demo_project", cwd=tmp_path)
        assert init_result.returncode == 0
        project = tmp_path / "demo_project"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + str(project)

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_greeting_skill.py"],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0

    def test_init_existing_path_exits_two(self, tmp_path: Path) -> None:
        (tmp_path / "demo_project").mkdir()

        result = run_cli("init", "demo_project", cwd=tmp_path)

        assert result.returncode == 2
        assert "Project path already exists" in result.stderr

    def test_init_write_failure_exits_one_and_reports_cleanup(
        self,
        tmp_path: Path,
        capsys,
        monkeypatch,
    ) -> None:
        def fail_write(project_dir: Path) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(cli_module, "_write_project_files", fail_write)

        result = cli_module.main(["init", str(tmp_path / "demo_project")])

        captured = capsys.readouterr()
        assert result == 1
        assert "Could not create project" in captured.err
        assert "disk full" in captured.err
        assert not (tmp_path / "demo_project").exists()
