"""Tests for internal validation helpers."""

from __future__ import annotations

from pathlib import Path

from rpacore._validation import (
    ValidationError,
    ValidationFailure,
    assert_relative_path,
    example_pytest_target,
    type_error,
    validate_contained_path,
    value_error,
)


class TestValidationHelpers:
    def test_validation_error_uses_runtime_base(self) -> None:
        assert issubclass(ValidationError, ValidationFailure)
        assert issubclass(ValidationError, ValueError)

    def test_type_error_formats_expected_message(self) -> None:
        error = type_error("retry_count", "int", "bad")

        assert isinstance(error, TypeError)
        assert str(error) == "retry_count expected int; got str value='bad'"

    def test_value_error_formats_expected_message(self) -> None:
        error = value_error("timeout", "positive number", -1)

        assert isinstance(error, ValueError)
        assert str(error) == "timeout expected positive number; got int value=-1"

    def test_validate_contained_path_rejects_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()

        assert validate_contained_path(
            root,
            allowed_roots=(root,),
            label="path",
        ) == root.resolve()
        assert validate_contained_path(
            root / "child",
            allowed_roots=(root,),
            label="path",
        ) == (root / "child").resolve()

        try:
            validate_contained_path(outside, allowed_roots=(root,), label="path")
        except ValidationError as exc:
            assert "outside allowed roots" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_contained_path_rejects_non_path_inputs(self, tmp_path: Path) -> None:
        try:
            validate_contained_path("child", allowed_roots=(tmp_path,), label="path")  # type: ignore[arg-type]
        except ValidationError as exc:
            assert "must be a Path" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

        try:
            validate_contained_path(tmp_path, allowed_roots=("root",), label="path")  # type: ignore[arg-type]
        except ValidationError as exc:
            assert "allowed root must be a Path" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_relative_path_rejects_absolute_path(self, tmp_path: Path) -> None:
        try:
            assert_relative_path(str(tmp_path.resolve()), root=tmp_path, label="test path")
        except ValidationError as exc:
            assert "must be relative" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_validate_relative_path_returns_valid_relative_path(self, tmp_path: Path) -> None:
        assert assert_relative_path("relative/path", root=tmp_path, label="test path") == "relative/path"

    def test_validate_relative_path_rejects_non_string_path(self, tmp_path: Path) -> None:
        try:
            assert_relative_path(tmp_path, root=tmp_path, label="test path")  # type: ignore[arg-type]
        except ValidationError as exc:
            assert "must be a string" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_example_pytest_target_uses_standalone_project_cwd(self, tmp_path: Path) -> None:
        examples_root = tmp_path / "rpacore-examples"
        tests_dir = examples_root / "examples" / "demo" / "tests"
        tests_dir.mkdir(parents=True)

        target = example_pytest_target(
            "examples/demo/tests",
            examples_root=examples_root,
        )

        assert target.manifest_path == "examples/demo/tests"
        assert target.project_dir == examples_root / "examples" / "demo"
        assert target.pytest_path == "tests"

    def test_example_pytest_target_normalizes_relative_components(
        self,
        tmp_path: Path,
    ) -> None:
        examples_root = tmp_path / "rpacore-examples"
        tests_dir = examples_root / "examples" / "other" / "tests"
        tests_dir.mkdir(parents=True)

        target = example_pytest_target(
            "examples/demo/../other/tests",
            examples_root=examples_root,
        )

        assert target.manifest_path == "examples/other/tests"
        assert target.project_dir == examples_root / "examples" / "other"
        assert target.pytest_path == "tests"

    def test_example_pytest_target_supports_deeper_targets(self, tmp_path: Path) -> None:
        examples_root = tmp_path / "rpacore-examples"
        tests_dir = examples_root / "examples" / "demo" / "nested" / "tests"
        tests_dir.mkdir(parents=True)

        target = example_pytest_target(
            "examples/demo/nested/tests",
            examples_root=examples_root,
        )

        assert target.manifest_path == "examples/demo/nested/tests"
        assert target.project_dir == examples_root / "examples" / "demo"
        assert target.pytest_path == "nested/tests"

    def test_example_pytest_target_rejects_non_example_paths(self, tmp_path: Path) -> None:
        examples_root = tmp_path / "rpacore-examples"
        tests_dir = examples_root / "docs" / "demo" / "tests"
        tests_dir.mkdir(parents=True)

        try:
            example_pytest_target("docs/demo/tests", examples_root=examples_root)
        except ValidationError as exc:
            assert "inside one example project" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_example_pytest_target_rejects_project_without_target(
        self,
        tmp_path: Path,
    ) -> None:
        examples_root = tmp_path / "rpacore-examples"
        project_dir = examples_root / "examples" / "demo"
        project_dir.mkdir(parents=True)

        try:
            example_pytest_target("examples/demo", examples_root=examples_root)
        except ValidationError as exc:
            assert "must include a target" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")

    def test_example_pytest_target_rejects_missing_target(self, tmp_path: Path) -> None:
        examples_root = tmp_path / "rpacore-examples"
        project_dir = examples_root / "examples" / "demo"
        project_dir.mkdir(parents=True)

        try:
            example_pytest_target("examples/demo/tests", examples_root=examples_root)
        except ValidationError as exc:
            assert "does not exist" in str(exc)
        else:
            raise AssertionError("Expected ValidationError")
