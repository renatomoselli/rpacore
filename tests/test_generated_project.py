"""Generated-project setup, receipts, logging, and durability boundaries."""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

import rpacore.cli as cli_module
from rpacore import HistoryEvent, ProcessContext, Status, Transaction, list_transactions
from tests.cli_helpers import REPO_ROOT, run_cli


def parse_generated_receipt(output: str, project: Path, label: str) -> str:
    match = re.search(rf"^Transaction ([\w-]+): {re.escape(label)}$", output, re.MULTILINE)
    assert match is not None, output
    transaction_id = match.group(1)
    lines = output.splitlines()
    assert f"Database: {project / 'rpacore.db'}" in lines
    assert f"Inspect from project directory: {project}" in lines
    command = next((line.split() for line in lines if line.startswith("rpacore ")), [])
    assert command == ["rpacore", "transaction", "show", transaction_id]
    return transaction_id


class TestGeneratedProject:
    @pytest.mark.parametrize("setting,provider_class", [("env", "EnvCredentialProvider"), ("keyring", "KeyringCredentialProvider")])
    def test_selected_provider_reaches_step_context_without_secret_access(self, tmp_path, setting, provider_class):
        project = tmp_path / "demo"
        assert run_cli("init", str(project), cwd=tmp_path).returncode == 0
        (project / "config.toml").write_text(f'credential_provider="{setting}"\n', encoding="utf-8")
        probe = textwrap.dedent('''
            import runpy, sys
            from pathlib import Path
            import rpacore
            project = Path(sys.argv[1])
            expected = getattr(rpacore, sys.argv[2])
            def forbidden(*args, **kwargs):
                raise AssertionError("Credentials must not be read")
            rpacore.EnvCredentialProvider.get = forbidden
            rpacore.KeyringCredentialProvider.get = forbidden
            class NoKeyring:
                def find_spec(self, fullname, *args):
                    if fullname == "keyring":
                        forbidden()
            sys.meta_path.insert(0, NoKeyring())
            sentinel = expected()
            factory = rpacore.build_credential_provider
            def build(name):
                assert isinstance(factory(name), expected)
                assert not (project / "rpacore.db").exists()
                return sentinel
            rpacore.build_credential_provider = build
            sys.path.insert(0, str(project))
            from steps.greeting import WriteGreeting
            original = WriteGreeting.execute
            seen = []
            def inspect(self, ctx):
                assert ctx.credentials is sentinel
                seen.append(ctx.credentials)
                original(self, ctx)
            WriteGreeting.execute = inspect
            application = runpy.run_path(str(project / "main.py"))
            assert application["main"]() == 0
            assert seen == [sentinel]
            assert "keyring" not in sys.modules
        ''')
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        result = subprocess.run([sys.executable, "-c", probe, str(project), provider_class],
                                cwd=tmp_path, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert (project / "greeting.txt").read_bytes() == b"Hello, Alice\n"

    @pytest.mark.parametrize("name", [
        "my project", ".demo", "demo.", "_demo", "demo_", "-demo", "demo-",
        'my"project', "my'project", "démø", "Kelvin", "demo\n", "demo\t", "demo\x01",
    ])
    def test_init_invalid_name_does_not_create_parents(self, tmp_path: Path, name: str) -> None:
        parent = tmp_path / "missing" / "nested"

        result = run_cli("init", str(parent / name), cwd=tmp_path)

        assert result.returncode == 2
        assert result.stdout == ""
        assert "project basename" in result.stderr
        assert "my-project" in result.stderr
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("target", ["", ".", "..", "missing/.."])
    def test_init_directory_references_are_usage_errors(self, tmp_path: Path, target: str) -> None:
        result = run_cli("init", target, cwd=tmp_path)

        assert result.returncode == 2
        assert result.stdout == ""
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("name", ["a", "1", "Demo.Project-1", "demo_project", "demo-project", "a..__--b"])
    def test_init_preserves_valid_name_with_spaces_in_parent(self, tmp_path: Path, name: str) -> None:
        project = tmp_path / "parent with spaces" / "nested" / name

        result = run_cli("init", str(project), cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
        assert metadata["project"]["name"] == name

    def test_init_creates_project_that_runs_without_edits(self, tmp_path: Path) -> None:
        result = run_cli("init", "demo_project", cwd=tmp_path)

        assert result.returncode == 0
        assert "Created RPA Core project: demo_project" in result.stdout
        project = tmp_path / "demo_project"
        assert (project / "pyproject.toml").exists()
        assert (project / "rpacore.toml").exists()
        assert (project / "config.toml").exists()
        assert (project / "main.py").exists()
        assert (project / "steps" / "__init__.py").exists()
        assert (project / "steps" / "greeting.py").exists()
        assert (project / "tests" / "test_greeting_step.py").exists()
        assert (project / ".gitignore").exists()
        assert not (project / "LICENSE").exists()
        assert not (project / "NOTICE").exists()
        assert "transaction_db_path" not in (project / "config.toml").read_text(
            encoding="utf-8"
        )

        run_result = run_cli("run", cwd=project)

        assert run_result.returncode == 0
        assert "Transaction completed" in run_result.stderr
        parse_generated_receipt(run_result.stdout, project, "successful")
        assert (project / "greeting.txt").read_text(encoding="utf-8") == "Hello, Alice\n"
        assert (project / "rpacore.db").exists()
        assert "execute_transaction(" in (
            project / "main.py"
        ).read_text(encoding="utf-8")
        assert "load_project_manifest" in (project / "main.py").read_text(encoding="utf-8")
        transactions = list_transactions(str(project / "rpacore.db"))
        assert len(transactions) == 1
        assert transactions[0].status is Status.SUCCESSFUL
        assert transactions[0].definition_identity == "generated-greeting/v2"
        assert HistoryEvent.STEP_SUCCEEDED in [
            entry.event for entry in transactions[0].history
        ]

    @pytest.mark.parametrize("level,fmt", [("INFO", "text"), ("INFO", "json"), ("ERROR", "text")])
    def test_generated_receipts_logging_and_new_run_identity(
        self, tmp_path: Path, level: str, fmt: str,
    ) -> None:
        project = tmp_path / "parent with spaces" / "demo"
        assert run_cli("init", str(project), cwd=tmp_path).returncode == 0
        config_path = project / "config.toml"
        config_source = config_path.read_text(encoding="utf-8")
        assert 'log_level = "INFO"' in config_source
        assert 'log_format = "text"' in config_source
        config_path.write_text(
            config_source
            .replace('log_level = "INFO"', f'log_level = "{level}"')
            .replace('log_format = "text"', f'log_format = "{fmt}"'),
            encoding="utf-8",
        )
        nested = project / "nested"
        nested.mkdir()
        ids = []
        for cwd in (project, nested):
            result = run_cli("run", cwd=cwd)
            assert result.returncode == 0, result.stderr
            transaction_id = parse_generated_receipt(result.stdout, project, "successful")
            inspection = run_cli("transaction", "show", transaction_id, "--json", cwd=project)
            assert inspection.returncode == 0, inspection.stderr
            saved = json.loads(inspection.stdout)["transaction"]
            assert saved["id"] == transaction_id and saved["status"] == "successful"
            destination = str(project / "greeting.txt")
            assert saved["definition_identity"] == "generated-greeting/v2"
            assert saved["steps"][0]["arguments"]["output_path"] == destination
            assert saved["state"]["greeting_path"] == destination
            assert saved["artifacts"][0]["path"] == destination
            assert Path(saved["artifacts"][0]["path"]).read_bytes() == b"Hello, Alice\n"
            assert not (nested / "greeting.txt").exists()
            ids.append(transaction_id)
            assert "Alice" not in result.stdout and "greeting.txt" not in result.stdout
            if level == "ERROR":
                assert result.stderr == ""
            elif fmt == "json":
                logs = [json.loads(line) for line in result.stderr.splitlines()]
                assert all(log["log_format_version"] == 3 for log in logs)
                assert "rpacore.transaction.completed" in {log["event"] for log in logs}
                assert all(log["attributes"]["transaction_id"] == transaction_id for log in logs)
            else:
                assert "Transaction completed" in result.stderr
            for args in (("transaction", "list", "--json"), ("doctor", "--json"),
                         ("transaction", "export", "--format", "json")):
                inspection = run_cli(*args, cwd=cwd)
                assert inspection.returncode == 0, inspection.stderr
                assert inspection.stderr == ""
                json.loads(inspection.stdout)
            ndjson = run_cli("transaction", "export", "--format", "ndjson", cwd=cwd)
            assert ndjson.returncode == 0 and ndjson.stderr == "" and ndjson.stdout
            for line in ndjson.stdout.splitlines():
                json.loads(line)
        assert len(set(ids)) == 2
        assert {tx.id for tx in list_transactions(str(project / "rpacore.db"))} == set(ids)

    @pytest.mark.parametrize("relative", [False, True])
    def test_generated_writer_failure_preserves_destination_and_metadata(self, tmp_path, monkeypatch, relative):
        project = tmp_path / "parent with spaces" / "demo"
        assert run_cli("init", str(project), cwd=tmp_path).returncode == 0
        nested = project / "nested"
        nested.mkdir()
        monkeypatch.chdir(nested)
        greeting = runpy.run_path(str(project / "steps" / "greeting.py"))["WriteGreeting"]
        output = project / "greeting.txt"
        output.write_bytes(b"previous greeting")
        step = greeting(name="write_greeting", execution_order=1, arguments={
            "name": "Alice", "output_path": "greeting.txt" if relative else str(output),
        })
        transaction = Transaction(reference="writer-failure", steps=[step])
        real_write = Path.write_text
        def partial_write(path, content, **kwargs):
            real_write(path, "partial", **kwargs)
            raise OSError("controlled-writer-failure")
        with monkeypatch.context() as patch:
            patch.setattr(Path, "write_text", partial_write)
            with pytest.raises(OSError, match="controlled-writer-failure"):
                step.execute(ProcessContext(transaction=transaction))
        assert output.read_bytes() == b"previous greeting"
        assert transaction.state == {} and transaction.artifacts == []
        assert not list(project.glob(".greeting.txt.*.tmp"))
        assert not (nested / "greeting.txt").exists()
        step.execute(ProcessContext(transaction=transaction))
        assert output.read_bytes() == b"Hello, Alice\n"
        assert transaction.state["greeting_path"] == str(output)
        assert transaction.artifacts[0].path == str(output)

    def test_generated_post_publication_failure_and_same_id_replay(self, tmp_path):
        project = tmp_path / "parent with spaces" / "demo"
        assert run_cli("init", str(project), cwd=tmp_path).returncode == 0
        step_path = project / "steps" / "greeting.py"
        source = step_path.read_text(encoding="utf-8")
        step_path.write_text(source + textwrap.dedent('''
            from contextlib import contextmanager
            real_atomic_output_path = atomic_output_path
            @contextmanager
            def atomic_output_path(destination):
                with real_atomic_output_path(destination) as temporary:
                    yield temporary
                raise RuntimeError("controlled-post-publication-failure")
            '''), encoding="utf-8")
        failed = run_cli("run", cwd=project)
        assert failed.returncode == 1
        transaction_id = parse_generated_receipt(failed.stdout, project, "failed")
        inspection = run_cli("transaction", "show", transaction_id, "--json", cwd=project)
        assert inspection.returncode == 0, inspection.stderr
        saved = json.loads(inspection.stdout)["transaction"]
        assert saved["status"] == "failed" and saved["steps"][0]["status"] == "failed"
        assert saved["steps"][0]["exceptions"][-1]["type"] == "system"
        assert saved["steps"][0]["exceptions"][-1]["message"] == "controlled-post-publication-failure"
        assert saved["state"] == {} and saved["artifacts"] == []
        destination = project / "greeting.txt"
        before = destination.read_bytes()
        assert before == b"Hello, Alice\n"
        # Fresh process imports the original generated step, without the injected fault.
        step_path.write_text(source, encoding="utf-8")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        replay = textwrap.dedent('''
            import sys
            from pathlib import Path
            from rpacore import Engine, execute_transaction, resume_transaction, Status
            project = Path(sys.argv[1])
            sys.path.insert(0, str(project))
            from steps.greeting import WriteGreeting
            tx = resume_transaction(sys.argv[2], [WriteGreeting(
                name="write_greeting", execution_order=1,
                arguments={"name": "different", "output_path": str(Path.cwd() / "wrong.txt")},
            )], db_path=str(project / "rpacore.db"), definition_identity="generated-greeting/v2")
            assert tx.id == sys.argv[2]
            assert tx.steps[0].arguments == {"name": "Alice", "output_path": str(project / "greeting.txt")}
            execute_transaction(tx, engine=Engine(), transaction_db_path=str(project / "rpacore.db"))
            assert tx.status is Status.SUCCESSFUL
            assert not Path("wrong.txt").exists()
            ''')
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT)
        result = subprocess.run([sys.executable, "-c", replay, str(project), transaction_id],
                                cwd=elsewhere, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert destination.read_bytes() == before
        inspection = run_cli("transaction", "show", transaction_id, "--db",
                             str(project / "rpacore.db"), "--json", cwd=elsewhere)
        assert inspection.returncode == 0, inspection.stderr
        final = json.loads(inspection.stdout)["transaction"]
        assert final["id"] == transaction_id and final["status"] == "successful"
        assert final["steps"][0]["status"] == "successful"
        assert final["state"]["greeting_path"] == str(destination)
        assert final["artifacts"][0]["path"] == str(destination)
        assert len(list_transactions(str(project / "rpacore.db"))) == 1
        assert list(project.rglob("greeting.txt")) == [destination]
        assert not list(project.glob(".greeting.txt.*.tmp"))
        assert not list(elsewhere.iterdir())

    @pytest.mark.parametrize("failure", ["business", "system"])
    def test_generated_failure_receipt_matches_saved_outcome(self, tmp_path: Path, failure: str) -> None:
        assert run_cli("init", "demo", cwd=tmp_path).returncode == 0
        project = tmp_path / "demo"
        main_path = project / "main.py"
        source = main_path.read_text(encoding="utf-8")
        if failure == "business":
            needle = '"name": "Alice"'
            assert needle in source
            source = source.replace(needle, '"name": ""')
        else:
            needle = "from steps.greeting import WriteGreeting"
            assert needle in source
            source = source.replace(
                needle,
                "from steps.greeting import WriteGreeting\n"
                "from rpacore import SystemException\n"
                "def fail_step(self, ctx):\n"
                "    raise SystemException('controlled-system-failure', action=self.name)\n"
                "WriteGreeting.execute = fail_step",
            )
        main_path.write_text(source, encoding="utf-8")
        result = run_cli("run", cwd=project)
        assert result.returncode == 1
        transaction_id = parse_generated_receipt(result.stdout, project, "failed")
        inspection = run_cli("transaction", "show", transaction_id, "--json", cwd=project)
        assert inspection.returncode == 0, inspection.stderr
        saved_record = json.loads(inspection.stdout)["transaction"]
        assert saved_record["id"] == transaction_id and saved_record["status"] == "failed"
        failure_message = "'name' must be a non-empty string" if failure == "business" else "controlled-system-failure"
        assert f"Step failed: {failure_message}" in result.stderr
        assert transaction_id in result.stderr
        assert failure_message not in result.stdout
        assert "greeting" not in result.stdout and "Alice" not in result.stdout
        assert "controlled-system-failure" not in result.stdout
        saved = list_transactions(str(project / "rpacore.db"))
        assert len(saved) == 1 and saved[0].outcome_category == f"{failure}_failed"

    @pytest.mark.parametrize("boundary", ["initial", "final"])
    def test_generated_checkpoint_abort_never_claims_saved_success(
        self, tmp_path: Path, boundary: str,
    ) -> None:
        assert run_cli("init", "demo", cwd=tmp_path).returncode == 0
        project = tmp_path / "demo"
        main_path = project / "main.py"
        source = main_path.read_text(encoding="utf-8")
        needle = "def main() -> int:"
        assert needle in source
        # Keep the final-boundary semantics aligned with the standalone wheel smoke.
        injection = textwrap.dedent(f"""\
            from rpacore import save_transaction
            real_execute_transaction = execute_transaction
            def execute_transaction(transaction, **kwargs):
                db_path = kwargs.pop("transaction_db_path")
                def checkpoint(tx):
                    if {boundary!r} == "initial" or tx.status is Status.SUCCESSFUL:
                        raise RuntimeError("controlled-{boundary}-checkpoint-failure")
                    save_transaction(tx, db_path=db_path)
                real_execute_transaction(transaction, checkpoint=checkpoint, **kwargs)
            """)
        main_path.write_text(source.replace(needle, injection + "\n" + needle), encoding="utf-8")
        result = run_cli("run", cwd=project)
        assert result.returncode == 1
        assert result.stdout == ""
        transaction_id = parse_generated_receipt(result.stderr, project, "aborted; durable outcome is unconfirmed")
        assert "durable outcome is unconfirmed" in result.stderr
        assert f"Project execution failed: controlled-{boundary}-checkpoint-failure" in result.stderr
        inspection = run_cli("transaction", "show", transaction_id, "--json", cwd=project)
        if boundary == "initial":
            assert inspection.returncode != 0
            assert not (project / "rpacore.db").exists()
            assert not (project / "greeting.txt").exists()
        else:
            assert "Transaction completed" in result.stderr
            assert (project / "greeting.txt").read_text(encoding="utf-8") == "Hello, Alice\n"
            assert inspection.returncode == 0, inspection.stderr
            saved = json.loads(inspection.stdout)["transaction"]
            assert saved["status"] == "in_progress"
            assert saved["steps"][0]["status"] == "successful"

    @pytest.mark.parametrize("config", ['log_format = "invalid"\n', 'retry_backoff = 0.5\n'])
    def test_generated_setup_error_keeps_existing_cli_diagnostic(self, tmp_path: Path, config: str) -> None:
        assert run_cli("init", "demo", cwd=tmp_path).returncode == 0
        project = tmp_path / "demo"
        (project / "config.toml").write_text(config, encoding="utf-8")
        result = run_cli("run", cwd=project)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "Project execution failed" in result.stderr
        assert "durable outcome" not in result.stderr
        assert not (project / "rpacore.db").exists()

    @pytest.mark.parametrize("config", ["", 'log_level = "debug"\n', 'log_level = "NOTSET"\n'])
    def test_generated_logging_defaults_normalized_level_and_notset(self, tmp_path: Path, config: str) -> None:
        assert run_cli("init", "demo", cwd=tmp_path).returncode == 0
        project = tmp_path / "demo"
        (project / "config.toml").write_text(config, encoding="utf-8")
        step_path = project / "steps" / "greeting.py"
        step_source = step_path.read_text(encoding="utf-8")
        needle = '        name = self.arguments.get("name")'
        assert needle in step_source
        step_path.write_text(
            step_source.replace(
                needle,
                '        from rpacore import get_logger\n'
                '        get_logger().debug("debug-level-probe")\n'
                '        name = self.arguments.get("name")',
            ), encoding="utf-8",
        )
        result = run_cli("run", cwd=project)
        assert result.returncode == 0, result.stderr
        transaction_id = parse_generated_receipt(result.stdout, project, "successful")
        assert "Transaction completed" in result.stderr and transaction_id in result.stderr
        if config:
            assert "DEBUG | " in result.stderr and "debug-level-probe" in result.stderr
        else:
            assert "debug-level-probe" not in result.stderr
            assert all(line.startswith("INFO | ") for line in result.stderr.splitlines())

    def test_generated_receipt_omits_application_and_configuration_contents(self, tmp_path: Path) -> None:
        assert run_cli("init", "demo", cwd=tmp_path).returncode == 0
        project = tmp_path / "demo"
        (project / "config.toml").write_text(
            'max_retries = 13\nretry_delay = 0.123456\nretry_backoff = 1.234567\n'
            'screenshot_dir = "config-screenshot-marker"\nlog_level = "WARNING"\n'
            'custom_value = "custom-config-marker"\n', encoding="utf-8",
        )
        main_path = project / "main.py"
        source = main_path.read_text(encoding="utf-8")
        needle = '"name": "Alice"'
        assert needle in source
        main_path.write_text(
            source.replace(needle, '"name": "argument-marker"'),
            encoding="utf-8",
        )
        result = run_cli("run", cwd=project)
        assert result.returncode == 0, result.stderr
        transaction_id = parse_generated_receipt(result.stdout, project, "successful")
        assert result.stderr == ""
        assert (project / "greeting.txt").read_text(encoding="utf-8") == "Hello, argument-marker\n"
        for value in ("0.123456", "1.234567", "config-screenshot-marker", "WARNING",
                      "custom-config-marker", "argument-marker", "greeting.txt"):
            assert value not in result.stdout
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        inspection = run_cli("transaction", "show", transaction_id, "--json", cwd=unrelated)
        assert inspection.returncode == 1 and inspection.stdout == ""
        assert "pass --db" in inspection.stderr
        assert not (unrelated / "rpacore.db").exists()

    @pytest.mark.parametrize("exception", ["SystemExit(7)", "KeyboardInterrupt()"])
    def test_generated_termination_propagates_without_ordinary_abort_receipt(
        self, tmp_path: Path, exception: str,
    ) -> None:
        assert run_cli("init", "demo", cwd=tmp_path).returncode == 0
        project = tmp_path / "demo"
        main_path = project / "main.py"
        source = main_path.read_text(encoding="utf-8")
        needle = "from steps.greeting import WriteGreeting"
        assert needle in source
        main_path.write_text(
            source.replace(
                needle,
                "from steps.greeting import WriteGreeting\n"
                "def interrupt_step(self, ctx):\n"
                f"    raise {exception}\n"
                "WriteGreeting.execute = interrupt_step",
            ), encoding="utf-8",
        )
        result = run_cli("run", cwd=project)
        expected_exit = 7 if exception.startswith("SystemExit") else (0xC000013A if os.name == "nt" else -2)
        assert result.returncode == expected_exit, result.stderr
        assert result.stdout == ""
        assert "durable outcome" not in result.stderr and "rpacore transaction show" not in result.stderr
        if exception == "KeyboardInterrupt()":
            assert "KeyboardInterrupt" in result.stderr
        saved = list_transactions(str(project / "rpacore.db"))
        assert len(saved) == 1 and saved[0].status is Status.IN_PROGRESS
        assert saved[0].steps[0].status is Status.IN_PROGRESS

    def test_generated_project_step_test_passes(self, tmp_path: Path) -> None:
        init_result = run_cli("init", "demo_project", cwd=tmp_path)
        assert init_result.returncode == 0
        project = tmp_path / "demo_project"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + str(project)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--basetemp",
                str(tmp_path / "inner-pytest-tmp"),
                "tests/test_greeting_step.py",
            ],
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

    def test_init_concurrent_destination_is_preserved(self, tmp_path: Path, capsys, monkeypatch) -> None:
        project = tmp_path / "demo_project"
        original_mkdir = Path.mkdir

        def collide(path: Path, *args, **kwargs) -> None:
            if path == project:
                original_mkdir(path)
                original_mkdir(path / "nested")
                (path / "nested" / "sentinel.bin").write_bytes(b"\x00\xffowner")
            original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", collide)
        result = cli_module.main(["init", str(project)])
        captured = capsys.readouterr()

        assert result == 2
        assert captured.out == ""
        assert f"Project path already exists: {project}" in captured.err
        assert sorted(str(path.relative_to(project)) for path in project.rglob("*")) == [
            "nested", str(Path("nested") / "sentinel.bin")
        ]
        assert (project / "nested" / "sentinel.bin").read_bytes() == b"\x00\xffowner"

    def test_init_invalid_name_precedes_existing_path_error(self, tmp_path: Path) -> None:
        project = tmp_path / "my project"
        project.mkdir()
        existing_file = project / "keep.txt"
        existing_file.write_text("preserve me", encoding="utf-8")

        result = run_cli("init", str(project), cwd=tmp_path)

        assert result.returncode == 2
        assert result.stdout == ""
        assert "Invalid project basename" in result.stderr
        assert "Project path already exists" not in result.stderr
        assert list(tmp_path.iterdir()) == [project]
        assert list(project.iterdir()) == [existing_file]
        assert existing_file.read_text(encoding="utf-8") == "preserve me"

    def test_init_write_failure_exits_one_and_reports_cleanup(
        self,
        tmp_path: Path,
        capsys,
        monkeypatch,
    ) -> None:
        def fail_write(project_dir: Path) -> None:
            (project_dir / "partial.txt").write_text("partial", encoding="utf-8")
            raise FileExistsError("write collision")

        monkeypatch.setattr(cli_module, "_write_project_files", fail_write)

        result = cli_module.main(["init", str(tmp_path / "demo_project")])

        captured = capsys.readouterr()
        assert result == 1
        assert "Could not create project" in captured.err
        assert "write collision" in captured.err
        assert "Project path already exists" not in captured.err
        assert list(tmp_path.iterdir()) == []

    def test_init_acquisition_failure_does_not_clean_target(self, tmp_path: Path, capsys, monkeypatch) -> None:
        parent = tmp_path / "parent"
        parent.mkdir()
        sentinel = parent / "sentinel.bin"
        sentinel.write_bytes(b"owner")
        project = parent / "demo_project"
        project_sentinel = project / "sentinel.bin"
        original_mkdir = Path.mkdir
        cleanup_calls = []

        def fail_acquisition(path: Path, *args, **kwargs) -> None:
            if path == project:
                original_mkdir(path)
                project_sentinel.write_bytes(b"concurrent owner")
                raise PermissionError("acquisition denied")
            original_mkdir(path, *args, **kwargs)

        def fail_cleanup(path: Path) -> None:
            cleanup_calls.append(path)

        monkeypatch.setattr(Path, "mkdir", fail_acquisition)
        monkeypatch.setattr(cli_module.shutil, "rmtree", fail_cleanup)
        result = cli_module.main(["init", str(project)])
        captured = capsys.readouterr()

        assert result == 1
        assert captured.out == ""
        assert "Could not create project" in captured.err
        assert "acquisition denied" in captured.err
        assert cleanup_calls == []
        assert set(parent.iterdir()) == {sentinel, project}
        assert sentinel.read_bytes() == b"owner"
        assert list(project.iterdir()) == [project_sentinel]
        assert project_sentinel.read_bytes() == b"concurrent owner"

    def test_init_reports_write_and_cleanup_failures(self, tmp_path: Path, capsys, monkeypatch) -> None:
        def fail_write(project_dir: Path) -> None:
            raise OSError("write failed")

        def fail_cleanup(path: Path) -> None:
            raise OSError("cleanup failed")

        monkeypatch.setattr(cli_module, "_write_project_files", fail_write)
        monkeypatch.setattr(cli_module.shutil, "rmtree", fail_cleanup)
        result = cli_module.main(["init", str(tmp_path / "demo_project")])
        captured = capsys.readouterr()

        assert result == 1
        assert "Could not create project" in captured.err
        assert "write failed" in captured.err
        assert "Could not remove partial project" in captured.err
        assert "cleanup failed" in captured.err
