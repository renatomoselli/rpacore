"""Tests for the release-candidate validation script."""

from __future__ import annotations

import importlib.util
import errno
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from rpacore._validation import ValidationError as SharedValidationError
from rpacore._validation import ValidationFailure


def _load_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "validate_release_candidate.py"
    spec = importlib.util.spec_from_file_location("validate_release_candidate", script_path)
    assert spec is not None, f"Could not load module from {script_path}"
    assert spec.loader is not None, f"Could not load module from {script_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestReleaseCandidateValidationScript:
    def test_copy_ignore_excludes_generated_and_vcs_files(self) -> None:
        module = _load_script()

        ignored = module._copy_ignore(
            "repo",
            [
                ".git",
                ".internal",
                ".pytest_cache",
                ".rpiv",
                "Aux",
                "con",
                "NUL",
                "nul",
                "Prn",
                "rpacore.egg-info",
                "rpacore",
                "README.md",
            ],
        )

        assert ignored == {
            ".git",
            ".internal",
            ".pytest_cache",
            ".rpiv",
            "Aux",
            "con",
            "NUL",
            "nul",
            "Prn",
            "rpacore.egg-info",
        }

    def test_parser_defaults_write_to_public_validation_artifacts_dir(self) -> None:
        module = _load_script()

        args = module.build_parser().parse_args([])

        assert args.output_dir == Path("validation-artifacts/release-candidate-validation")
        assert ".rpiv" not in args.output_dir.parts
        assert args.work_dir is None

    def test_copy_tree_skips_symlinks(self, tmp_path: Path) -> None:
        module = _load_script()
        source = tmp_path / "source"
        destination = tmp_path / "destination"
        target = tmp_path / "outside.txt"
        source.mkdir()
        target.write_text("secret", encoding="utf-8")
        (source / "real.txt").write_text("real", encoding="utf-8")
        try:
            (source / "linked.txt").symlink_to(target)
        except OSError as exc:
            if exc.errno in {errno.EPERM, errno.EACCES, errno.ENOTSUP}:
                return
            raise

        module._copy_tree(source, destination)

        assert (destination / "real.txt").read_text(encoding="utf-8") == "real"
        assert not (destination / "linked.txt").exists()

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

    def test_parse_json_output_requires_object(self) -> None:
        module = _load_script()
        command_record = module.CommandRecord(
            name="json",
            command=["cmd"],
            cwd=".",
            exit_code=0,
            duration_seconds=0.0,
            stdout='{"ok": true}',
        )

        assert module._parse_json_output(command_record) == {"ok": True}

        command_record.stdout = "[]"
        try:
            module._parse_json_output(command_record)
        except module.ValidationError as exc:
            assert "JSON object" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_parse_json_output_uses_untruncated_stdout(self) -> None:
        module = _load_script()
        raw_stdout = json.dumps({
            "transactions": [{"id": str(index)} for index in range(1500)]
        })
        command_record = module.CommandRecord(
            name="json",
            command=["cmd"],
            cwd=".",
            exit_code=0,
            duration_seconds=0.0,
            stdout=raw_stdout[:12_000] + "\n...[truncated]...",
            raw_stdout=raw_stdout,
        )

        assert len(module._parse_json_output(command_record)["transactions"]) == 1500

    def test_parse_json_output_preserves_explicit_empty_raw_stdout(self) -> None:
        module = _load_script()
        command_record = module.CommandRecord(
            name="json",
            command=["cmd"],
            cwd=".",
            exit_code=0,
            duration_seconds=0.0,
            stdout='{"ok": true}',
            raw_stdout="",
        )

        try:
            module._parse_json_output(command_record)
        except module.ValidationError as exc:
            assert "valid JSON" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_parse_ndjson_output_parses_every_line(self) -> None:
        module = _load_script()
        command_record = module.CommandRecord(
            name="ndjson",
            command=["cmd"],
            cwd=".",
            exit_code=0,
            duration_seconds=0.0,
            stdout='{"id": 1}\n{"id": 2}\n',
        )

        assert module._parse_ndjson_output(command_record) == [{"id": 1}, {"id": 2}]

    def test_parse_ndjson_output_uses_untruncated_stdout(self) -> None:
        module = _load_script()
        raw_stdout = "".join(
            json.dumps({"id": str(index)}) + "\n"
            for index in range(1500)
        )
        command_record = module.CommandRecord(
            name="ndjson",
            command=["cmd"],
            cwd=".",
            exit_code=0,
            duration_seconds=0.0,
            stdout=raw_stdout[:12_000] + "\n...[truncated]...",
            raw_stdout=raw_stdout,
        )

        assert len(module._parse_ndjson_output(command_record)) == 1500

    def test_command_record_omits_raw_output(self) -> None:
        module = _load_script()
        command_record = module.CommandRecord(
            name="json",
            command=["cmd"],
            cwd=".",
            exit_code=0,
            duration_seconds=0.0,
            stdout="short",
            raw_stdout="full",
            raw_stderr="full err",
        )

        record = module._command_record(command_record)

        assert record["stdout"] == "short"
        assert "raw_stdout" not in record
        assert "raw_stderr" not in record

    def test_run_raises_validation_error_on_failed_command(self, tmp_path: Path) -> None:
        module = _load_script()

        try:
            module._run(
                "bad_command",
                [sys.executable, "-c", "import sys; sys.exit(7)"],
                cwd=tmp_path,
                allowed_roots=(tmp_path,),
            )
        except module.ValidationError as exc:
            assert "bad_command failed with exit code 7" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_run_check_false_returns_failed_command_record(self, tmp_path: Path) -> None:
        module = _load_script()

        command_record = module._run(
            "allowed_failure",
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            cwd=tmp_path,
            allowed_roots=(tmp_path,),
            check=False,
        )

        assert command_record.name == "allowed_failure"
        assert command_record.exit_code == 7

    def test_validate_contained_path_rejects_escaped_paths(self, tmp_path: Path) -> None:
        module = _load_script()
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()

        assert module._validate_contained_path(
            root / "child",
            allowed_roots=(root,),
            label="path",
        ) == (root / "child").resolve()

        try:
            module._validate_contained_path(outside, allowed_roots=(root,), label="path")
        except module.ValidationError as exc:
            assert "outside allowed roots" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_relative_test_path_rejects_escape(self, tmp_path: Path) -> None:
        module = _load_script()
        examples_root = tmp_path / "examples"
        examples_root.mkdir()

        assert module._validate_relative_test_path(
            "examples/demo/tests",
            examples_root=examples_root,
        ) == "examples/demo/tests"

        try:
            module._validate_relative_test_path("../outside", examples_root=examples_root)
        except module.ValidationError as exc:
            assert "outside allowed roots" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_example_project_dir_validates_example_project(self, tmp_path: Path) -> None:
        module = _load_script()
        examples_root = tmp_path / "rpacore-examples"
        project_dir = examples_root / "examples" / "demo"
        project_dir.mkdir(parents=True)

        assert module._example_project_dir(
            "examples/demo",
            examples_root=examples_root,
        ) == project_dir.resolve()

    def test_example_project_dir_rejects_invalid_paths(self, tmp_path: Path) -> None:
        module = _load_script()
        examples_root = tmp_path / "rpacore-examples"
        (examples_root / "examples" / "demo").mkdir(parents=True)

        cases = [
            ("../outside", "outside allowed roots"),
            ("examples", "inside examples/"),
            ("docs/demo", "inside examples/"),
            ("examples/missing", "does not exist"),
        ]
        for project_path, expected_message in cases:
            try:
                module._example_project_dir(project_path, examples_root=examples_root)
            except module.ValidationError as exc:
                assert expected_message in str(exc)
            else:
                raise AssertionError(f"Expected ValidationError for {project_path}")

    def test_example_db_path_rejects_escaped_paths(self, tmp_path: Path) -> None:
        module = _load_script()
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        assert module._example_db_path("rpacore.db", project_dir=project_dir) == (
            project_dir / "rpacore.db"
        ).resolve()
        try:
            module._example_db_path("../outside.db", project_dir=project_dir)
        except module.ValidationError as exc:
            assert "outside allowed roots" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_example_pytest_target_uses_standalone_project_cwd(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        examples_root = tmp_path / "rpacore-examples"
        (examples_root / "examples" / "demo" / "tests").mkdir(parents=True)

        target = module._example_pytest_target(
            "examples/demo/tests",
            examples_root=examples_root,
        )

        assert target.manifest_path == "examples/demo/tests"
        assert target.project_dir == examples_root / "examples" / "demo"
        assert target.pytest_path == "tests"

    def test_example_pytest_target_rejects_root_level_paths(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        examples_root = tmp_path / "rpacore-examples"

        try:
            module._example_pytest_target("tests", examples_root=examples_root)
        except module.ValidationError as exc:
            assert "inside one example project" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_transactions_payload_validation_reports_shape_errors(self) -> None:
        module = _load_script()

        try:
            module._transactions_from_list_payload({"transactions": "tx-1"})
        except module.ValidationError as exc:
            assert "must be a list" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

        try:
            module._transaction_ids([{"reference": "missing-id"}])
        except module.ValidationError as exc:
            assert "string 'id'" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

        assert module._transaction_ids([{"id": "not a uuid"}]) == ["not a uuid"]
        assert module._transaction_ids([{"id": "abc123-000"}]) == ["abc123-000"]

    def test_transaction_command_adds_db_only_when_explicit(self, tmp_path: Path) -> None:
        module = _load_script()
        rpacore_cli = tmp_path / "venv" / "Scripts" / "rpacore.exe"
        db_path = tmp_path / "project" / "rpacore.db"

        assert module._transaction_command(rpacore_cli, "list", db_path=None) == [
            str(rpacore_cli),
            "transaction",
            "list",
        ]
        assert module._transaction_command(
            rpacore_cli,
            "export",
            "--format",
            "json",
            db_path=db_path,
        ) == [
            str(rpacore_cli),
            "transaction",
            "export",
            "--format",
            "json",
            "--db",
            str(db_path),
        ]

    def test_append_cli_transaction_inspection_appends_only_after_success(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        commands: list[object] = []

        def fake_run(name, command, *, cwd, allowed_roots, env=None, check=True):
            stdout = ""
            if name == "demo_transaction_list_json":
                stdout = '{"transactions": []}'
            return module.CommandRecord(
                name=name,
                command=command,
                cwd=str(cwd),
                exit_code=0,
                duration_seconds=0.01,
                stdout=stdout,
            )

        with patch.object(module, "_run", side_effect=fake_run):
            try:
                module._append_cli_transaction_inspection(
                    commands,  # type: ignore[arg-type]
                    name_prefix="demo",
                    rpacore_cli=tmp_path / "rpacore",
                    cwd=tmp_path,
                    db_path=None,
                    allowed_roots=(tmp_path,),
                    env={},
                )
            except module.ValidationError as exc:
                assert "created no transactions" in str(exc)
            else:
                raise AssertionError("Expected ValidationError")

        assert commands == []

    def test_validation_error_uses_shared_base(self) -> None:
        module = _load_script()

        assert issubclass(module.ValidationError, ValidationFailure)
        assert module.ValidationError is SharedValidationError

    def test_installed_smoke_code_allows_owned_venv_inside_repo_and_rejects_source_copy(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        source_copy = tmp_path / "source-copy" / "rpacore"
        install_root = repo_root / ".rpiv" / "work" / "venv"
        site_packages = (
            install_root
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        system_site_packages = tmp_path / "system-site-packages"
        installed_package = site_packages / "rpacore"
        system_installed_package = system_site_packages / "rpacore"
        source_package = source_copy / "rpacore"
        installed_package.mkdir(parents=True)
        system_installed_package.mkdir(parents=True)
        source_package.mkdir(parents=True)
        package_text = "__version__ = '0.1.0'\n__all__ = []\n"
        (installed_package / "__init__.py").write_text(package_text, encoding="utf-8")
        (system_installed_package / "__init__.py").write_text(package_text, encoding="utf-8")
        (source_package / "__init__.py").write_text(package_text, encoding="utf-8")
        code = module._installed_smoke_code(
            repo_root=repo_root,
            source_copy=source_copy,
            allowed_install_root=install_root,
        )

        env = {**os.environ, "PYTHONPATH": str(site_packages)}
        installed_result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        )

        assert installed_result.returncode == 0

        env["PYTHONPATH"] = str(system_site_packages)
        system_result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        )

        assert system_result.returncode == 0

        env["PYTHONPATH"] = str(source_copy)
        source_result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            check=False,
        )

        assert source_result.returncode == 1
        assert "imported rpacore from checkout" in source_result.stderr

    def test_pytest_counts_handles_common_summary_forms(self) -> None:
        module = _load_script()

        assert module._pytest_counts("1 passed, 2 skipped in 0.01s") == {
            "passed": 1,
            "skipped": 2,
        }
        assert module._pytest_counts("1 error, 2 failed in 0.01s") == {
            "errors": 1,
            "failed": 2,
        }
        assert module._pytest_counts("1 passedenough in 0.01s") == {}

    def test_aggregate_pytest_counts_uses_command_parsed_counts(self) -> None:
        module = _load_script()
        commands = [
            module.CommandRecord(
                name="framework_tests",
                command=["pytest"],
                cwd=".",
                exit_code=0,
                duration_seconds=0.01,
                parsed={"passed": 2, "skipped": 1, "transaction_count": 99},
            ),
            module.CommandRecord(
                name="example_pytest:examples/demo/tests",
                command=["pytest"],
                cwd=".",
                exit_code=0,
                duration_seconds=0.01,
                parsed={"passed": 3, "errors": 1, "note": "ignored"},
            ),
        ]

        assert module._aggregate_pytest_counts(commands) == {
            "errors": 1,
            "passed": 5,
            "skipped": 1,
        }

    def test_manifest_result_counts_commands_and_dirty_repositories(self) -> None:
        module = _load_script()
        commands = [
            module.CommandRecord(
                name="passed",
                command=["cmd"],
                cwd=".",
                exit_code=0,
                duration_seconds=0.01,
            ),
            module.CommandRecord(
                name="failed",
                command=["cmd"],
                cwd=".",
                exit_code=2,
                duration_seconds=0.01,
            ),
        ]
        repos = [
            module.RepoState("rpacore", ".", "abc", "main", False, []),
            module.RepoState("rpacore-examples", ".", "def", "main", True, ["M file"]),
        ]

        assert module._manifest_result(commands=commands, repos=repos) == {
            "status": "fail",
            "command_count": 2,
            "failed_command_count": 1,
            "dirty_repository_count": 1,
        }

    def test_validation_index_maps_findings_to_present_commands(self) -> None:
        module = _load_script()
        commands = [
            module.CommandRecord(
                name="framework_tests",
                command=["pytest"],
                cwd=".",
                exit_code=0,
                duration_seconds=0.01,
            ),
            module.CommandRecord(
                name="example_cli_transaction_export_json",
                command=["rpacore"],
                cwd=".",
                exit_code=0,
                duration_seconds=0.01,
            ),
            module.CommandRecord(
                name="example_cli_transaction_export_ndjson",
                command=["rpacore"],
                cwd=".",
                exit_code=0,
                duration_seconds=0.01,
            ),
            module.CommandRecord(
                name="example_pytest:examples/demo/tests",
                command=["pytest"],
                cwd=".",
                exit_code=0,
                duration_seconds=0.01,
            ),
        ]

        assert module._validation_index(commands) == {
            "G2-001": ["framework_tests"],
            "G2-002": [],
            "G2-004": [
                "example_cli_transaction_export_json",
                "example_cli_transaction_export_ndjson",
            ],
            "G2-007": [],
            "G2-008": [],
            "G2-013": ["example_pytest:examples/demo/tests"],
        }

    def test_sha256_hashes_file_contents(self, tmp_path: Path) -> None:
        module = _load_script()
        path = tmp_path / "payload.txt"
        path.write_text("payload", encoding="utf-8")

        assert module._sha256(path) == sha256(b"payload").hexdigest()

    def test_artifact_records_include_supply_chain_metadata(self, tmp_path: Path) -> None:
        module = _load_script()
        wheel = tmp_path / "rpacore-0.1.0-py3-none-any.whl"
        sdist = tmp_path / "rpacore-0.1.0.tar.gz"

        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("rpacore/__init__.py", "")
            archive.writestr("rpacore-0.1.0.dist-info/METADATA", "")
            archive.writestr("rpacore-0.1.0.dist-info/RECORD", "")
            archive.writestr("rpacore-0.1.0.dist-info/entry_points.txt", "")
            archive.writestr("rpacore-0.1.0.dist-info/licenses/LICENSE", "")
            archive.writestr("rpacore-0.1.0.dist-info/licenses/NOTICE", "")
        sdist_root = tmp_path / "sdist" / "rpacore-0.1.0"
        sdist_root.mkdir(parents=True)
        (sdist_root / "PKG-INFO").write_text("", encoding="utf-8")
        (sdist_root / "LICENSE").write_text("", encoding="utf-8")
        (sdist_root / "NOTICE").write_text("", encoding="utf-8")
        sdist_egg_info = sdist_root / "rpacore.egg-info"
        sdist_egg_info.mkdir()
        (sdist_egg_info / "PKG-INFO").write_text("", encoding="utf-8")
        with tarfile.open(sdist, "w:gz") as archive:
            archive.add(sdist_root, arcname="rpacore-0.1.0")

        records = {record["name"]: record for record in module._artifact_records(tmp_path)}

        assert records[wheel.name]["contains_metadata"] is True
        assert records[wheel.name]["contains_record"] is True
        assert records[wheel.name]["contains_entry_points"] is True
        assert records[wheel.name]["contains_license"] is True
        assert records[wheel.name]["contains_notice"] is True
        assert records[wheel.name]["contains_private_paths"] is False
        assert records[wheel.name]["private_paths"] == []
        assert records[sdist.name]["contains_metadata"] is True
        assert records[sdist.name]["contains_license"] is True
        assert records[sdist.name]["contains_notice"] is True
        assert records[sdist.name]["contains_private_paths"] is False
        assert records[sdist.name]["private_paths"] == []

    def test_private_archive_path_detection_matches_copy_ignore_names(self) -> None:
        module = _load_script()

        for private_name in module.COPY_IGNORE_NAMES:
            assert module._is_private_archive_path(f"rpacore-0.1.0/{private_name}/file.txt") is True
            assert (
                module._private_archive_path_match(
                    f"rpacore-0.1.0/{private_name}/file.txt",
                    is_wheel=True,
                )
                == private_name
            )
        for private_pattern in module.ARCHIVE_PRIVATE_PATTERNS:
            private_name = private_pattern.replace("*", "module")
            assert module._is_private_archive_path(f"rpacore-0.1.0/{private_name}") is True
            assert (
                module._private_archive_path_match(
                    f"rpacore-0.1.0/{private_name}",
                    is_wheel=True,
                )
                == private_name
            )
        assert module._is_private_archive_path("rpacore-0.1.0/rpacore.egg-info/PKG-INFO") is True
        assert (
            module._private_archive_path_match(
                "rpacore-0.1.0/rpacore.egg-info/PKG-INFO",
                is_wheel=True,
            )
            == "rpacore.egg-info"
        )
        assert (
            module._private_archive_path_match(
                "rpacore-0.1.0/rpacore.egg-info/PKG-INFO",
                is_wheel=False,
            )
            is None
        )
        assert module._is_private_archive_path("rpacore-0.1.0/rpacore/__init__.py") is False
        assert (
            module._private_archive_path_match(
                "rpacore-0.1.0/rpacore/__init__.py",
                is_wheel=True,
            )
            is None
        )

    def test_artifact_records_reject_wheel_egg_info_metadata(self, tmp_path: Path) -> None:
        module = _load_script()
        wheel = tmp_path / "rpacore-0.1.0-py3-none-any.whl"

        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("rpacore/__init__.py", "")
            archive.writestr("rpacore-0.1.0.dist-info/METADATA", "")
            archive.writestr("rpacore-0.1.0.dist-info/RECORD", "")
            archive.writestr("rpacore-0.1.0.dist-info/entry_points.txt", "")
            archive.writestr("rpacore-0.1.0.dist-info/licenses/LICENSE", "")
            archive.writestr("rpacore-0.1.0.dist-info/licenses/NOTICE", "")
            archive.writestr("rpacore.egg-info/PKG-INFO", "")

        records = module._artifact_records(tmp_path)

        assert records[0]["contains_private_paths"] is True
        assert records[0]["private_paths"] == ["rpacore.egg-info/PKG-INFO"]
        try:
            module._validate_artifact_records(records)
        except module.ValidationError as exc:
            assert "release artifact contains private paths" in str(exc)
            assert "rpacore.egg-info/PKG-INFO" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_artifact_records_reject_unsupported_artifact_types(self, tmp_path: Path) -> None:
        module = _load_script()
        (tmp_path / "rpacore-0.1.0.zip").write_text("zip", encoding="utf-8")

        try:
            module._artifact_records(tmp_path)
        except module.ValidationError as exc:
            assert str(exc) == "unsupported release artifact type: rpacore-0.1.0.zip"
        else:
            raise AssertionError("Expected ValidationError")

    def test_contains_package_metadata_detects_wheel_and_sdist_metadata(self) -> None:
        module = _load_script()

        assert module._contains_package_metadata(
            ["rpacore-0.1.0.dist-info/METADATA"],
            is_wheel=True,
        ) is True
        assert module._contains_package_metadata(
            ["rpacore-0.1.0/PKG-INFO"],
            is_wheel=False,
        ) is True
        assert module._contains_package_metadata(["rpacore/__init__.py"], is_wheel=True) is False
        assert module._contains_package_metadata(["rpacore/__init__.py"], is_wheel=False) is False

    def test_validate_artifact_records_rejects_private_paths(self) -> None:
        module = _load_script()

        try:
            module._validate_artifact_records(
                [
                    {
                        "name": "rpacore-0.1.0.tar.gz",
                        "contains_private_paths": True,
                        "contains_license": True,
                        "contains_notice": True,
                        "contains_metadata": True,
                        "contains_examples": False,
                        "private_paths": ["rpacore-0.1.0/__pycache__/module.pyc"],
                    }
                ]
            )
        except module.ValidationError as exc:
            assert str(exc) == (
                "release artifact contains private paths: "
                "rpacore-0.1.0.tar.gz: rpacore-0.1.0/__pycache__/module.pyc"
            )
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_artifact_records_rejects_wheel_without_entry_points(self) -> None:
        module = _load_script()

        try:
            module._validate_artifact_records(
                [
                    {
                        "name": "rpacore-0.1.0-py3-none-any.whl",
                        "contains_private_paths": False,
                        "contains_license": True,
                        "contains_notice": True,
                        "contains_metadata": True,
                        "contains_examples": False,
                        "contains_record": True,
                        "contains_entry_points": False,
                    }
                ]
            )
        except module.ValidationError as exc:
            assert str(exc) == "wheel missing console entry point metadata: rpacore-0.1.0-py3-none-any.whl"
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_artifact_records_rejects_missing_notice(self) -> None:
        module = _load_script()

        try:
            module._validate_artifact_records(
                [
                    {
                        "name": "rpacore-0.1.0.tar.gz",
                        "contains_private_paths": False,
                        "contains_license": True,
                        "contains_notice": False,
                        "contains_metadata": True,
                        "contains_examples": False,
                    }
                ]
            )
        except module.ValidationError as exc:
            assert str(exc) == "release artifact missing notice file: rpacore-0.1.0.tar.gz"
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_artifact_records_rejects_examples_directory(self) -> None:
        module = _load_script()

        try:
            module._validate_artifact_records(
                [
                    {
                        "name": "rpacore-0.1.0.tar.gz",
                        "contains_private_paths": False,
                        "contains_license": True,
                        "contains_notice": True,
                        "contains_metadata": True,
                        "contains_examples": True,
                    }
                ]
            )
        except module.ValidationError as exc:
            assert str(exc) == "release artifact contains examples directory: rpacore-0.1.0.tar.gz"
        else:
            raise AssertionError("Expected ValidationError")

    def test_dependency_inventory_reads_pyproject(self, tmp_path: Path) -> None:
        module = _load_script()
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "demo"
dependencies = ["runtime-b>=1", "runtime-a"]

[project.optional-dependencies]
dev = ["pytest"]
screenshots = ["mss"]
""",
            encoding="utf-8",
        )

        assert module._dependency_inventory(tmp_path) == {
            "runtime_dependencies": ["runtime-a", "runtime-b>=1"],
            "optional_dependencies": {
                "dev": ["pytest"],
                "screenshots": ["mss"],
            },
        }

    def test_dependency_inventory_reports_missing_pyproject_as_validation_error(self, tmp_path: Path) -> None:
        module = _load_script()

        try:
            module._dependency_inventory(tmp_path)
        except module.ValidationError as exc:
            assert str(exc).startswith("cannot read dependency inventory from")
            assert exc.__cause__ is not None
        else:
            raise AssertionError("Expected ValidationError")

    def test_dependency_inventory_reports_invalid_toml_as_validation_error(self, tmp_path: Path) -> None:
        module = _load_script()
        (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")

        try:
            module._dependency_inventory(tmp_path)
        except module.ValidationError as exc:
            assert str(exc).startswith("cannot parse dependency inventory from")
            assert exc.__cause__ is not None
        else:
            raise AssertionError("Expected ValidationError")

    def test_dependency_inventory_rejects_missing_project_section(self, tmp_path: Path) -> None:
        module = _load_script()
        (tmp_path / "pyproject.toml").write_text("[build-system]\nrequires = []\n", encoding="utf-8")

        try:
            module._dependency_inventory(tmp_path)
        except module.ValidationError as exc:
            assert str(exc) == "pyproject missing [project] section"
        else:
            raise AssertionError("Expected ValidationError")

    def test_dependency_inventory_rejects_non_list_optional_group(self, tmp_path: Path) -> None:
        module = _load_script()
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "demo"

[project.optional-dependencies]
dev = "pytest"
""",
            encoding="utf-8",
        )

        try:
            module._dependency_inventory(tmp_path)
        except module.ValidationError as exc:
            assert str(exc) == "pyproject optional dependency group must be a list: dev"
        else:
            raise AssertionError("Expected ValidationError")

    def test_dependency_inventory_supports_empty_optional_dependencies(self, tmp_path: Path) -> None:
        module = _load_script()
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "demo"

[project.optional-dependencies]
""",
            encoding="utf-8",
        )

        assert module._dependency_inventory(tmp_path) == {
            "runtime_dependencies": [],
            "optional_dependencies": {},
        }

    def test_python_version_info_uses_requested_interpreter(self, tmp_path: Path) -> None:
        module = _load_script()
        python = tmp_path / "venv" / "Scripts" / "python.exe"
        calls: list[list[str]] = []

        def fake_run(command, *, capture_output, text, check):
            calls.append(command)
            if command[1:3] == ["-c", "import sys; print(sys.version)"]:
                return subprocess.CompletedProcess(command, 0, stdout="venv python\n")
            if command[1:3] == ["-m", "pip"]:
                return subprocess.CompletedProcess(command, 0, stdout="pip from venv\n")
            raise AssertionError(f"Unexpected command: {command}")

        with patch.object(module.subprocess, "run", side_effect=fake_run):
            info = module._python_version_info(python)

        assert calls == [
            [str(python), "-c", "import sys; print(sys.version)"],
            [str(python), "-m", "pip", "--version"],
        ]
        assert info == {
            "executable": str(python),
            "version": "venv python",
            "pip": "pip from venv",
        }

    def test_write_manifest_writes_json_and_summary(self, tmp_path: Path) -> None:
        module = _load_script()
        manifest = {
            "generated_at": "2026-06-26T00:00:00+00:00",
            "finding_ids": ["G2-001"],
            "platform": {"system": "Windows", "release": "10", "machine": "AMD64"},
            "repositories": [
                {
                    "name": "rpacore",
                    "commit": "abcdef123456",
                    "branch": "main",
                    "dirty": False,
                }
            ],
            "commands": [
                {"name": "framework_tests", "exit_code": 0, "duration_seconds": 1.2}
            ],
            "result": {
                "status": "pass",
                "command_count": 1,
                "failed_command_count": 0,
                "dirty_repository_count": 0,
            },
            "pytest_totals": {"passed": 10, "skipped": 2},
            "validation_index": {"G2-001": ["framework_tests"]},
            "artifacts": [{"name": "rpacore.whl", "sha256": "abc"}],
            "dependency_inventory": {
                "runtime_dependencies": [],
                "optional_dependencies": {"dev": ["pytest"]},
            },
        }

        module._write_manifest(manifest, tmp_path)

        written = json.loads((tmp_path / "release-candidate-validation-results.json").read_text())
        assert written["finding_ids"] == ["G2-001"]
        assert written["result"]["status"] == "pass"
        assert written["pytest_totals"] == {"passed": 10, "skipped": 2}
        summary = (tmp_path / "release-candidate-summary.md").read_text()
        assert "Result: `pass`" in summary
        assert "Commands: `1` total, `0` failed" in summary
        assert "Dirty repositories: `0`" in summary
        assert "## Pytest Totals" in summary
        assert "passed=10, skipped=2" in summary
        assert "## Validation Index" in summary
        assert "`G2-001`: `framework_tests`" in summary
        assert "framework_tests" in summary
        assert "rpacore.whl" in summary
        assert "## Dependency Inventory" in summary
        assert "Runtime dependencies: (none)" in summary
        assert "`dev` extra: `pytest`" in summary

    def test_write_manifest_includes_empty_pytest_totals_when_present(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        manifest = {
            "generated_at": "2026-06-26T00:00:00+00:00",
            "finding_ids": ["G2-001"],
            "platform": {"system": "Windows", "release": "10", "machine": "AMD64"},
            "repositories": [],
            "commands": [],
            "result": {
                "status": "pass",
                "command_count": 0,
                "failed_command_count": 0,
                "dirty_repository_count": 0,
            },
            "pytest_totals": {},
            "validation_index": {},
            "artifacts": [],
        }

        module._write_manifest(manifest, tmp_path)

        summary = (tmp_path / "release-candidate-summary.md").read_text()
        assert "## Pytest Totals" in summary
        assert "- (none)" in summary

    def test_write_manifest_replaces_manifest_before_summary(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        manifest = {
            "generated_at": "2026-06-26T00:00:00+00:00",
            "finding_ids": ["G2-001"],
            "platform": {"system": "Windows", "release": "10", "machine": "AMD64"},
            "repositories": [],
            "commands": [],
            "result": {
                "status": "pass",
                "command_count": 0,
                "failed_command_count": 0,
                "dirty_repository_count": 0,
            },
            "validation_index": {},
            "artifacts": [],
        }
        real_replace = Path.replace
        replacements: list[str] = []

        def record_replace(path: Path, target: Path):
            replacements.append(target.name)
            return real_replace(path, target)

        with patch.object(Path, "replace", record_replace):
            module._write_manifest(manifest, tmp_path)

        assert replacements == [
            "release-candidate-validation-results.json",
            "release-candidate-summary.md",
        ]

    def test_write_manifest_removes_manifest_when_summary_replace_fails(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        manifest = {
            "generated_at": "2026-06-26T00:00:00+00:00",
            "finding_ids": ["G2-001"],
            "platform": {"system": "Windows", "release": "10", "machine": "AMD64"},
            "repositories": [],
            "commands": [],
            "result": {
                "status": "pass",
                "command_count": 0,
                "failed_command_count": 0,
                "dirty_repository_count": 0,
            },
            "validation_index": {},
            "artifacts": [],
        }
        real_replace = Path.replace
        replacements: list[str] = []

        def fail_summary_replace(path: Path, target: Path):
            replacements.append(target.name)
            if target.name == "release-candidate-summary.md":
                raise OSError("summary locked")
            return real_replace(path, target)

        try:
            with patch.object(Path, "replace", fail_summary_replace):
                module._write_manifest(manifest, tmp_path)
        except OSError as exc:
            assert "summary locked" in str(exc)
        else:
            raise AssertionError("Expected OSError")

        assert replacements == [
            "release-candidate-validation-results.json",
            "release-candidate-summary.md",
        ]
        assert not (tmp_path / "release-candidate-validation-results.json").exists()
        assert not (tmp_path / ".release-candidate-validation-results.json.tmp").exists()
        assert not (tmp_path / ".release-candidate-summary.md.tmp").exists()

    def test_write_manifest_preserves_existing_summary_when_manifest_replace_fails(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        manifest = {
            "generated_at": "2026-06-26T00:00:00+00:00",
            "finding_ids": ["G2-001"],
            "platform": {"system": "Windows", "release": "10", "machine": "AMD64"},
            "repositories": [],
            "commands": [],
            "result": {
                "status": "pass",
                "command_count": 0,
                "failed_command_count": 0,
                "dirty_repository_count": 0,
            },
            "validation_index": {},
            "artifacts": [],
        }
        summary_path = tmp_path / "release-candidate-summary.md"
        summary_path.write_text("old summary", encoding="utf-8")
        replacements: list[str] = []

        def fail_manifest_replace(path: Path, target: Path):
            replacements.append(target.name)
            if target.name == "release-candidate-validation-results.json":
                raise OSError("manifest locked")
            raise AssertionError("summary replace should not run when manifest replace fails")

        try:
            with patch.object(Path, "replace", fail_manifest_replace):
                module._write_manifest(manifest, tmp_path)
        except OSError as exc:
            assert "manifest locked" in str(exc)
        else:
            raise AssertionError("Expected OSError")

        assert replacements == ["release-candidate-validation-results.json"]
        assert summary_path.read_text(encoding="utf-8") == "old summary"
        assert not (tmp_path / ".release-candidate-validation-results.json.tmp").exists()
        assert not (tmp_path / ".release-candidate-summary.md.tmp").exists()

    def test_latest_wheel_prefers_stable_when_mtimes_match(self, tmp_path: Path) -> None:
        module = _load_script()
        stable = tmp_path / "rpacore-1.0.0-py3-none-any.whl"
        prerelease = tmp_path / "rpacore-1.0.0rc1-py3-none-any.whl"
        stable.write_text("stable", encoding="utf-8")
        prerelease.write_text("prerelease", encoding="utf-8")
        mtime = 1_800_000_000
        os.utime(stable, (mtime, mtime))
        os.utime(prerelease, (mtime, mtime))

        assert module._latest_wheel(tmp_path) == stable

    def test_validate_release_candidate_records_core_steps(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        examples_repo = tmp_path / "examples"
        work_dir = tmp_path / "work"
        output_dir = tmp_path / "validation-results"
        repo_root.mkdir()
        examples_repo.mkdir()
        commands: list[str] = []
        command_details: dict[str, tuple[list[str], Path]] = {}

        def fake_copy_tree(source: Path, destination: Path) -> None:
            if source == examples_repo:
                (destination / "examples" / "demo" / "tests").mkdir(parents=True)
                (destination / "examples" / "demo" / "main.py").write_text("", encoding="utf-8")
            else:
                destination.mkdir(parents=True)
                (destination / "pyproject.toml").write_text(
                    """
[project]
name = "rpacore"

[project.optional-dependencies]
dev = ["pytest"]
""",
                    encoding="utf-8",
                )

        transaction_list_json = '{"transactions": [{"id": "12345678-1234-1234-1234-123456789abc"}]}'
        transaction_show_json = '{"transaction": {"id": "12345678-1234-1234-1234-123456789abc"}}'
        transaction_export_ndjson = '{"id": "12345678-1234-1234-1234-123456789abc"}\n'
        stdout_by_name = {
            "installed_import_smoke": '{"version": "0.1.0", "module_file": "venv/rpacore/__init__.py", "exports": []}',
            "framework_tests": "1 passed in 0.01s",
            "cli_transaction_list_json": transaction_list_json,
            "cli_transaction_show_json": transaction_show_json,
            "cli_transaction_export_json": transaction_list_json,
            "cli_transaction_export_ndjson": transaction_export_ndjson,
            "example_cli_transaction_list_json": transaction_list_json,
            "example_cli_transaction_show_json": transaction_show_json,
            "example_cli_transaction_export_json": transaction_list_json,
            "example_cli_transaction_export_ndjson": transaction_export_ndjson,
            "example_pytest:examples/demo/tests": "1 passed in 0.01s",
        }

        def fake_run(name, command, *, cwd, allowed_roots, env=None, check=True):
            commands.append(name)
            command_details[name] = (command, cwd)
            if name == "example_cli_run":
                (cwd / "rpacore.db").write_text("", encoding="utf-8")
            return module.CommandRecord(
                name=name,
                command=command,
                cwd=str(cwd),
                exit_code=0,
                duration_seconds=0.01,
                stdout=stdout_by_name.get(name, ""),
            )

        def fake_artifacts(wheelhouse: Path):
            wheelhouse.mkdir(parents=True, exist_ok=True)
            wheel = wheelhouse / "rpacore-0.1.0-py3-none-any.whl"
            sdist = wheelhouse / "rpacore-0.1.0.tar.gz"
            wheel.write_text("wheel", encoding="utf-8")
            sdist.write_text("sdist", encoding="utf-8")
            return [
                {
                    "name": wheel.name,
                    "path": str(wheel),
                    "sha256": "wheel-sha",
                    "size_bytes": 5,
                    "contains_license": True,
                    "contains_notice": True,
                    "contains_metadata": True,
                    "contains_record": True,
                    "contains_entry_points": True,
                    "contains_examples": False,
                    "contains_private_paths": False,
                    "private_paths": [],
                },
                {
                    "name": sdist.name,
                    "path": str(sdist),
                    "sha256": "sdist-sha",
                    "size_bytes": 5,
                    "contains_license": True,
                    "contains_notice": True,
                    "contains_metadata": True,
                    "contains_record": False,
                    "contains_entry_points": False,
                    "contains_examples": False,
                    "contains_private_paths": False,
                    "private_paths": [],
                },
            ]

        with patch.object(module, "_repo_state") as repo_state:
            repo_state.side_effect = [
                module.RepoState("rpacore", str(repo_root), "abc", "main", False, []),
                module.RepoState("rpacore-examples", str(examples_repo), "def", "main", False, []),
            ]
            with patch.object(module, "_copy_tree", side_effect=fake_copy_tree):
                with patch.object(module, "_run", side_effect=fake_run):
                    with patch.object(module, "_artifact_records", side_effect=fake_artifacts):
                        with patch.object(module, "_venv_python", return_value=work_dir / "venv" / "python"):
                            with patch.object(module, "_venv_script", return_value=work_dir / "venv" / "rpacore"):
                                with patch.object(
                                    module,
                                    "_python_version_info",
                                    return_value={
                                        "executable": "python",
                                        "version": "3.12",
                                        "pip": "pip 25",
                                    },
                                ):
                                    manifest = module.validate_release_candidate(
                                        repo_root=repo_root,
                                        examples_repo=examples_repo,
                                        work_dir=work_dir,
                                        output_dir=output_dir,
                                        examples_pytest=["examples/demo/tests"],
                                        example_cli_project="examples/demo",
                                        example_cli_db="rpacore.db",
                                    )

        assert commands == [
            "framework_tests",
            "build_artifacts",
            "twine_check",
            "create_venv",
            "install_wheel",
            "installed_import_smoke",
            "cli_version",
            "cli_init",
            "cli_run_generated_project",
            "cli_transaction_list",
            "cli_transaction_list_json",
            "cli_transaction_show",
            "cli_transaction_show_json",
            "cli_transaction_export_json",
            "cli_transaction_export_ndjson",
            "example_cli_run",
            "example_cli_transaction_list",
            "example_cli_transaction_list_json",
            "example_cli_transaction_show",
            "example_cli_transaction_show_json",
            "example_cli_transaction_export_json",
            "example_cli_transaction_export_ndjson",
            "install_pytest_for_examples",
            "example_pytest:examples/demo/tests",
        ]
        assert manifest["commands"][10]["parsed"] == {"transaction_count": 1}
        assert manifest["pytest_totals"] == {"passed": 2}
        assert manifest["validation_index"]["G2-004"] == [
            "example_cli_transaction_list_json",
            "example_cli_transaction_show_json",
            "example_cli_transaction_export_json",
            "example_cli_transaction_export_ndjson",
        ]
        assert manifest["validation_index"]["G2-013"] == [
            "example_pytest:examples/demo/tests"
        ]
        assert manifest["artifacts"][0]["contains_examples"] is False
        assert manifest["dependency_inventory"]["runtime_dependencies"] == []
        assert "dev" in manifest["dependency_inventory"]["optional_dependencies"]
        assert "--db" not in command_details["cli_transaction_export_json"][0]
        example_cli_command, example_cli_cwd = command_details["example_cli_transaction_export_json"]
        assert example_cli_command[-2:] == ["--db", str(example_cli_cwd / "rpacore.db")]
        assert example_cli_cwd == work_dir / "source" / "rpacore-examples" / "examples" / "demo"
        example_command, example_cwd = command_details["example_pytest:examples/demo/tests"]
        assert example_command[-2:] == ["tests", "-q"]
        assert example_cwd == work_dir / "source" / "rpacore-examples" / "examples" / "demo"
        assert (output_dir / "release-candidate-validation-results.json").exists()

    def test_validate_release_candidate_rejects_invalid_artifact_records(self, tmp_path: Path) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        output_dir = tmp_path / "validation-results"
        repo_root.mkdir()

        def fake_copy_tree(source: Path, destination: Path) -> None:
            destination.mkdir(parents=True)
            (destination / "pyproject.toml").write_text(
                """
[project]
name = "rpacore"
""",
                encoding="utf-8",
            )

        def fake_run(name, command, *, cwd, allowed_roots, env=None, check=True):
            return module.CommandRecord(
                name=name,
                command=command,
                cwd=str(cwd),
                exit_code=0,
                duration_seconds=0.01,
            )

        def fake_artifacts(wheelhouse: Path):
            wheel = wheelhouse / "rpacore-0.1.0-py3-none-any.whl"
            wheel.write_text("wheel", encoding="utf-8")
            return [
                {
                    "name": wheel.name,
                    "path": str(wheel),
                    "sha256": "wheel-sha",
                    "size_bytes": 5,
                    "contains_license": False,
                    "contains_notice": True,
                    "contains_metadata": True,
                    "contains_record": True,
                    "contains_entry_points": True,
                    "contains_examples": False,
                    "contains_private_paths": False,
                    "private_paths": [],
                }
            ]

        with patch.object(
            module,
            "_repo_state",
            return_value=module.RepoState("rpacore", str(repo_root), "abc", "main", False, []),
        ):
            with patch.object(module, "_copy_tree", side_effect=fake_copy_tree):
                with patch.object(module, "_run", side_effect=fake_run):
                    with patch.object(module, "_artifact_records", side_effect=fake_artifacts):
                        try:
                            module.validate_release_candidate(
                                repo_root=repo_root,
                                examples_repo=None,
                                work_dir=work_dir,
                                output_dir=output_dir,
                                examples_pytest=[],
                                example_cli_project=None,
                                example_cli_db="rpacore.db",
                            )
                        except module.ValidationError as exc:
                            assert str(exc) == "release artifact missing license file: rpacore-0.1.0-py3-none-any.whl"
                        else:
                            raise AssertionError("Expected ValidationError")

    def test_validate_release_candidate_cleans_generated_dirs_after_failure(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        output_dir = tmp_path / "validation-results"
        repo_root.mkdir()

        def fake_copy_tree(source: Path, destination: Path) -> None:
            destination.mkdir(parents=True)
            (destination / "copied.txt").write_text("copied", encoding="utf-8")

        with patch.object(
            module,
            "_repo_state",
            return_value=module.RepoState("rpacore", str(repo_root), "abc", "main", False, []),
        ):
            with patch.object(module, "_copy_tree", side_effect=fake_copy_tree):
                with patch.object(module, "_run", side_effect=module.ValidationError("boom")):
                    try:
                        module.validate_release_candidate(
                            repo_root=repo_root,
                            examples_repo=None,
                            work_dir=work_dir,
                            output_dir=output_dir,
                            examples_pytest=[],
                            example_cli_project=None,
                            example_cli_db="rpacore.db",
                        )
                    except module.ValidationError as exc:
                        assert "boom" in str(exc)
                    else:
                        raise AssertionError("Expected ValidationError")

        assert not (work_dir / "source").exists()
        assert not (work_dir / "wheelhouse").exists()
        assert not (work_dir / "outside").exists()

    def test_validate_release_candidate_requires_examples_pytest_with_examples_repo(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        output_dir = tmp_path / "validation-results"
        examples_repo = tmp_path / "examples"
        repo_root.mkdir()
        examples_repo.mkdir()

        try:
            module.validate_release_candidate(
                repo_root=repo_root,
                examples_repo=examples_repo,
                work_dir=work_dir,
                output_dir=output_dir,
                examples_pytest=[],
                example_cli_project=None,
                example_cli_db="rpacore.db",
            )
        except module.ValidationError as exc:
            assert str(exc) == "--examples-pytest or --example-cli-project is required when --examples-repo is provided"
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_release_candidate_requires_examples_repo_with_example_cli_project(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()

        try:
            module.validate_release_candidate(
                repo_root=tmp_path,
                examples_repo=None,
                work_dir=tmp_path / "work",
                output_dir=tmp_path / "validation-results",
                examples_pytest=[],
                example_cli_project="examples/demo",
                example_cli_db="rpacore.db",
            )
        except module.ValidationError as exc:
            assert str(exc) == "--examples-repo is required when --example-cli-project is used"
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_release_candidate_preserves_failure_when_cleanup_fails(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        output_dir = tmp_path / "validation-results"
        repo_root.mkdir()
        failure = module.ValidationError("validation failed")

        with patch.object(
            module,
            "_repo_state",
            return_value=module.RepoState("rpacore", str(repo_root), "abc", "main", False, []),
        ):
            with patch.object(module, "_copy_tree"):
                with patch.object(module, "_run", side_effect=failure):
                    with patch.object(module, "_remove_tree", side_effect=OSError("locked")):
                        try:
                            module.validate_release_candidate(
                                repo_root=repo_root,
                                examples_repo=None,
                                work_dir=work_dir,
                                output_dir=output_dir,
                                examples_pytest=[],
                                example_cli_project=None,
                                example_cli_db="rpacore.db",
                            )
                        except module.ValidationError as exc:
                            assert exc is failure
                        else:
                            raise AssertionError("Expected ValidationError")

    def test_validate_release_candidate_preserves_keyboard_interrupt_when_cleanup_fails(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        output_dir = tmp_path / "validation-results"
        repo_root.mkdir()
        interrupt = KeyboardInterrupt()

        with patch.object(
            module,
            "_repo_state",
            return_value=module.RepoState("rpacore", str(repo_root), "abc", "main", False, []),
        ):
            with patch.object(module, "_copy_tree"):
                with patch.object(module, "_run", side_effect=interrupt):
                    with patch.object(module, "_remove_tree", side_effect=OSError("locked")):
                        try:
                            module.validate_release_candidate(
                                repo_root=repo_root,
                                examples_repo=None,
                                work_dir=work_dir,
                                output_dir=output_dir,
                                examples_pytest=[],
                                example_cli_project=None,
                                example_cli_db="rpacore.db",
                            )
                        except KeyboardInterrupt as exc:
                            assert exc is interrupt
                        else:
                            raise AssertionError("Expected KeyboardInterrupt")

    def test_main_cleans_owned_work_dir_when_output_dir_is_external(self, tmp_path: Path) -> None:
        module = _load_script()
        work_dir = tmp_path / "owned-work"
        output_dir = tmp_path / "validation-results"
        work_dir.mkdir()

        with patch.object(
            module,
            "validate_release_candidate",
            return_value={"ok": True},
        ) as validate:
            result = module.main(
                [
                    "--work-dir",
                    str(work_dir),
                    "--output-dir",
                    str(output_dir),
                    "--repo-root",
                    str(tmp_path),
                ]
            )

        assert result == 0
        validate.assert_called_once()
        assert work_dir.exists()

    def test_main_defaults_output_to_public_validation_artifacts_dir(self, tmp_path: Path) -> None:
        module = _load_script()
        work_dir = tmp_path / "owned-work"

        with patch.object(module.tempfile, "mkdtemp", return_value=str(work_dir)):
            with patch.object(
                module,
                "validate_release_candidate",
                return_value={"ok": True},
            ) as validate:
                result = module.main(["--repo-root", str(tmp_path)])

        assert result == 0
        validate.assert_called_once()
        assert validate.call_args.kwargs["output_dir"] == (
            Path("validation-artifacts/release-candidate-validation").resolve()
        )
        assert ".rpiv" not in validate.call_args.kwargs["output_dir"].parts
        assert not work_dir.exists()

    def test_main_removes_owned_temp_work_dir_when_output_dir_is_external(self, tmp_path: Path) -> None:
        module = _load_script()
        work_dir = tmp_path / "owned-work"
        output_dir = tmp_path / "validation-results"

        with patch.object(module.tempfile, "mkdtemp", return_value=str(work_dir)):
            with patch.object(module, "validate_release_candidate", return_value={"ok": True}):
                result = module.main(["--output-dir", str(output_dir), "--repo-root", str(tmp_path)])

        assert result == 0
        assert not work_dir.exists()
