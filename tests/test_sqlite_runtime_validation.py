"""Fail-closed orchestration and retained SQLite proof metadata."""

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "validate_sqlite_runtime.py"
    spec = importlib.util.spec_from_file_location("validate_sqlite_runtime", path)
    module = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(path.parent), *sys.path]):
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failure", [False, True])
def test_gate_retains_runtime_and_wheel_identity_even_when_tests_fail(tmp_path, monkeypatch, failure):
    module = _load_script()
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    names = ["test_queue.py", "test_queue_retry.py", "test_persistence.py", "test_sqlite_runtime.py"]
    for name in names:
        (repo / "tests" / name).write_text("# synthetic gate fixture\n", encoding="utf-8")
    wheel = tmp_path / "rpacore-test.whl"
    wheel.write_bytes(b"synthetic wheel identity")
    output = tmp_path / "evidence"
    output.mkdir()
    (output / "sqlite-runtime.json").write_text('{"status":"pass"}', encoding="utf-8")
    (output / "sqlite-runtime.xml").write_text("stale report", encoding="utf-8")
    (output / "keep.txt").write_text("caller-owned", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", "untrusted-checkout")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert [path.name for path in sorted(kwargs["cwd"].glob("test_*.py"))] == sorted(names)
        if failure:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module, "_run_tests", run)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            module.validate_sqlite_runtime(repo_root=repo, wheel=wheel, output_dir=output)
    else:
        module.validate_sqlite_runtime(repo_root=repo, wheel=wheel, output_dir=output)
    result = json.loads((output / "sqlite-runtime.json").read_text(encoding="utf-8"))
    assert result["status"] == ("fail" if failure else "pass")
    assert result["wheel"]["sha256"] == module.hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert result["runtime"]["sqlite"] == module.sqlite3.sqlite_version
    assert result["journal_modes"] == {"transactions": "delete", "queue": "delete"}
    assert not (output / "sqlite-runtime.xml").exists()
    assert (output / "keep.txt").read_text(encoding="utf-8") == "caller-owned"
    command, kwargs = calls[0]
    assert command[1:3] == ["-m", "pytest"]
    assert command[3:7] == names
    assert command[command.index("--basetemp") + 1] == str(kwargs["cwd"] / "tmp")
    assert command[-2:] == ["--junitxml", str(output / "sqlite-runtime.xml")]
    assert "PYTHONPATH" not in kwargs["env"]
    assert not kwargs["cwd"].is_relative_to(repo)
    assert not kwargs["cwd"].exists()  # Suite removed; evidence remains.


@pytest.mark.parametrize("failure_kind", ["checkout", "import"])
def test_import_precheck_discards_previous_reports(tmp_path, monkeypatch, failure_kind):
    module = _load_script()
    output = tmp_path / "evidence"
    output.mkdir()
    for name in ("sqlite-runtime.json", "sqlite-runtime.xml"):
        (output / name).write_text("stale pass", encoding="utf-8")
    (output / "keep.txt").write_text("caller-owned", encoding="utf-8")
    error_type, message = RuntimeError, "imported rpacore from checkout"
    if failure_kind == "import":
        original_import = __import__
        def fail_import(name, *args, **kwargs):
            if name == "rpacore":
                raise ImportError("controlled missing package")
            return original_import(name, *args, **kwargs)
        monkeypatch.setattr("builtins.__import__", fail_import)
        error_type, message = ImportError, "controlled missing package"
    with pytest.raises(error_type, match=message):
        module.validate_sqlite_runtime(
            repo_root=Path(__file__).resolve().parents[1],
            wheel=tmp_path / "unused.whl", output_dir=output,
        )
    assert sorted(path.name for path in output.iterdir()) == ["keep.txt"]
    assert (output / "keep.txt").read_text(encoding="utf-8") == "caller-owned"


@pytest.mark.parametrize("exit_code", [None, 7, 0], ids=["timeout", "failure", "success"])
def test_runner_terminates_owned_descendant_before_directory_cleanup(tmp_path, exit_code):
    module = _load_script()
    ready, survived = tmp_path / "ready", tmp_path / "survived"
    child_directory = tmp_path / "child-work"
    child_directory.mkdir()
    child_code = (
        "from pathlib import Path; import time; "
        f"Path({str(ready)!r}).touch(); time.sleep(4); Path({str(survived)!r}).touch()"
    )
    parent_code = (
        "import subprocess, sys, time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], cwd={str(child_directory)!r});\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        + ("time.sleep(30)" if exit_code is None else f"raise SystemExit({exit_code})")
    )
    if exit_code is None:
        with pytest.raises(subprocess.TimeoutExpired):
            module._run_tests([sys.executable, "-c", parent_code], cwd=tmp_path, env=module.os.environ.copy(), timeout=2)
    elif exit_code:
        with pytest.raises(subprocess.CalledProcessError) as failure:
            module._run_tests([sys.executable, "-c", parent_code], cwd=tmp_path, env=module.os.environ.copy(), timeout=2)
        assert failure.value.returncode == exit_code
    else:
        module._run_tests([sys.executable, "-c", parent_code], cwd=tmp_path, env=module.os.environ.copy(), timeout=2)
    assert ready.exists(), "descendant never started; tree cleanup was not exercised"
    child_directory.rmdir()  # A surviving Windows child would keep its cwd locked.
    # A leaked child exits itself after this marker, including on assertion failure.
    time.sleep(4.25)
    assert not survived.exists(), "owned child survived the test runner"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job assignment")
def test_failed_job_assignment_does_not_launch_the_command(tmp_path, monkeypatch):
    import ctypes

    module = _load_script()
    original_dll = ctypes.WinDLL
    def fail_assignment(*args, **kwargs):
        kernel = original_dll(*args, **kwargs)
        def deny(*args):
            ctypes.set_last_error(5)
            return 0
        kernel.AssignProcessToJobObject = deny
        return kernel
    monkeypatch.setattr(ctypes, "WinDLL", fail_assignment)
    marker = tmp_path / "should-not-run"
    with pytest.raises(OSError):
        module._run_tests(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            cwd=tmp_path, env=module.os.environ.copy(), timeout=2,
        )
    assert not marker.exists()
