"""Tests for the release-candidate validation script."""

from __future__ import annotations

import importlib.util
import errno
import json
import os
import sys
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
            [".git", ".internal", ".pytest_cache", "NUL", "rpacore.egg-info", "rpacore", "README.md"],
        )

        assert ignored == {".git", ".internal", ".pytest_cache", "NUL", "rpacore.egg-info"}

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
        evidence = module.CommandEvidence(
            name="json",
            command=["cmd"],
            cwd=".",
            exit_code=0,
            duration_seconds=0.0,
            stdout='{"ok": true}',
        )

        assert module._parse_json_output(evidence) == {"ok": True}

        evidence.stdout = "[]"
        try:
            module._parse_json_output(evidence)
        except module.ValidationError as exc:
            assert "JSON object" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_parse_ndjson_output_parses_every_line(self) -> None:
        module = _load_script()
        evidence = module.CommandEvidence(
            name="ndjson",
            command=["cmd"],
            cwd=".",
            exit_code=0,
            duration_seconds=0.0,
            stdout='{"id": 1}\n{"id": 2}\n',
        )

        assert module._parse_ndjson_output(evidence) == [{"id": 1}, {"id": 2}]

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

    def test_run_check_false_returns_failed_command_evidence(self, tmp_path: Path) -> None:
        module = _load_script()

        evidence = module._run(
            "allowed_failure",
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            cwd=tmp_path,
            allowed_roots=(tmp_path,),
            check=False,
        )

        assert evidence.name == "allowed_failure"
        assert evidence.exit_code == 7

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

    def test_validation_error_uses_shared_base(self) -> None:
        module = _load_script()

        assert issubclass(module.ValidationError, ValidationFailure)
        assert module.ValidationError is SharedValidationError

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

    def test_sha256_hashes_file_contents(self, tmp_path: Path) -> None:
        module = _load_script()
        path = tmp_path / "payload.txt"
        path.write_text("payload", encoding="utf-8")

        assert module._sha256(path) == sha256(b"payload").hexdigest()

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
            "artifacts": [{"name": "rpacore.whl", "sha256": "abc"}],
        }

        module._write_manifest(manifest, tmp_path)

        written = json.loads((tmp_path / "release-candidate-evidence.json").read_text())
        assert written["finding_ids"] == ["G2-001"]
        summary = (tmp_path / "release-candidate-summary.md").read_text()
        assert "framework_tests" in summary
        assert "rpacore.whl" in summary

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
            "release-candidate-evidence.json",
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
            "release-candidate-evidence.json",
            "release-candidate-summary.md",
        ]
        assert not (tmp_path / "release-candidate-evidence.json").exists()
        assert not (tmp_path / ".release-candidate-evidence.json.tmp").exists()
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
            "artifacts": [],
        }
        summary_path = tmp_path / "release-candidate-summary.md"
        summary_path.write_text("old summary", encoding="utf-8")
        replacements: list[str] = []

        def fail_manifest_replace(path: Path, target: Path):
            replacements.append(target.name)
            if target.name == "release-candidate-evidence.json":
                raise OSError("manifest locked")
            raise AssertionError("summary replace should not run when manifest replace fails")

        try:
            with patch.object(Path, "replace", fail_manifest_replace):
                module._write_manifest(manifest, tmp_path)
        except OSError as exc:
            assert "manifest locked" in str(exc)
        else:
            raise AssertionError("Expected OSError")

        assert replacements == ["release-candidate-evidence.json"]
        assert summary_path.read_text(encoding="utf-8") == "old summary"
        assert not (tmp_path / ".release-candidate-evidence.json.tmp").exists()
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
        output_dir = tmp_path / "evidence"
        repo_root.mkdir()
        examples_repo.mkdir()
        commands: list[str] = []

        def fake_run(name, command, *, cwd, allowed_roots, env=None, check=True):
            commands.append(name)
            stdout = ""
            if name == "installed_import_smoke":
                stdout = '{"version": "0.1.0", "module_file": "venv/rpacore/__init__.py", "exports": []}'
            elif name == "cli_transaction_list_json":
                stdout = '{"transactions": [{"id": "12345678-1234-1234-1234-123456789abc"}]}'
            elif name == "cli_transaction_show_json":
                stdout = '{"transaction": {"id": "12345678-1234-1234-1234-123456789abc"}}'
            elif name == "cli_transaction_export_json":
                stdout = '{"transactions": [{"id": "12345678-1234-1234-1234-123456789abc"}]}'
            elif name == "cli_transaction_export_ndjson":
                stdout = '{"id": "12345678-1234-1234-1234-123456789abc"}\n'
            elif name in {"framework_tests", "example_pytest:examples/demo/tests"}:
                stdout = "1 passed in 0.01s"
            return module.CommandEvidence(
                name=name,
                command=command,
                cwd=str(cwd),
                exit_code=0,
                duration_seconds=0.01,
                stdout=stdout,
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
                    "contains_examples": False,
                },
                {
                    "name": sdist.name,
                    "path": str(sdist),
                    "sha256": "sdist-sha",
                    "size_bytes": 5,
                    "contains_license": True,
                    "contains_examples": False,
                },
            ]

        with patch.object(module, "_repo_evidence") as repo_evidence:
            repo_evidence.side_effect = [
                module.RepoEvidence("rpacore", str(repo_root), "abc", "main", False, []),
                module.RepoEvidence("rpacore-examples", str(examples_repo), "def", "main", False, []),
            ]
            with patch.object(module, "_copy_tree"):
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
            "install_pytest_for_examples",
            "example_pytest:examples/demo/tests",
        ]
        assert manifest["commands"][10]["parsed"] == {"transaction_count": 1}
        assert manifest["artifacts"][0]["contains_examples"] is False
        assert (output_dir / "release-candidate-evidence.json").exists()

    def test_validate_release_candidate_cleans_generated_dirs_after_failure(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        output_dir = tmp_path / "evidence"
        repo_root.mkdir()

        def fake_copy_tree(source: Path, destination: Path) -> None:
            destination.mkdir(parents=True)
            (destination / "copied.txt").write_text("copied", encoding="utf-8")

        with patch.object(
            module,
            "_repo_evidence",
            return_value=module.RepoEvidence("rpacore", str(repo_root), "abc", "main", False, []),
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
        output_dir = tmp_path / "evidence"
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
            )
        except module.ValidationError as exc:
            assert str(exc) == "--examples-pytest is required when --examples-repo is provided"
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_release_candidate_preserves_failure_when_cleanup_fails(
        self,
        tmp_path: Path,
    ) -> None:
        module = _load_script()
        repo_root = tmp_path / "repo"
        work_dir = tmp_path / "work"
        output_dir = tmp_path / "evidence"
        repo_root.mkdir()
        failure = module.ValidationError("validation failed")

        with patch.object(
            module,
            "_repo_evidence",
            return_value=module.RepoEvidence("rpacore", str(repo_root), "abc", "main", False, []),
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
        output_dir = tmp_path / "evidence"
        repo_root.mkdir()
        interrupt = KeyboardInterrupt()

        with patch.object(
            module,
            "_repo_evidence",
            return_value=module.RepoEvidence("rpacore", str(repo_root), "abc", "main", False, []),
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
                            )
                        except KeyboardInterrupt as exc:
                            assert exc is interrupt
                        else:
                            raise AssertionError("Expected KeyboardInterrupt")

    def test_main_cleans_owned_work_dir_when_output_dir_is_external(self, tmp_path: Path) -> None:
        module = _load_script()
        work_dir = tmp_path / "owned-work"
        output_dir = tmp_path / "evidence"
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

    def test_main_removes_owned_temp_work_dir_when_output_dir_is_external(self, tmp_path: Path) -> None:
        module = _load_script()
        work_dir = tmp_path / "owned-work"
        output_dir = tmp_path / "evidence"

        with patch.object(module.tempfile, "mkdtemp", return_value=str(work_dir)):
            with patch.object(module, "validate_release_candidate", return_value={"ok": True}):
                result = module.main(["--output-dir", str(output_dir), "--repo-root", str(tmp_path)])

        assert result == 0
        assert not work_dir.exists()
