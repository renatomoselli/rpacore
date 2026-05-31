"""Tests for rpacore.status."""

from rpacore.status import Status


class TestStatusValues:
    def test_pending(self) -> None:
        assert Status.PENDING == "pending"

    def test_in_progress(self) -> None:
        assert Status.IN_PROGRESS == "in_progress"

    def test_successful(self) -> None:
        assert Status.SUCCESSFUL == "successful"

    def test_failed(self) -> None:
        assert Status.FAILED == "failed"

    def test_skipped(self) -> None:
        assert Status.SKIPPED == "skipped"

    def test_total_count(self) -> None:
        assert len(Status) == 5


class TestStatusBehavior:
    def test_is_string(self) -> None:
        assert isinstance(Status.PENDING, str)

    def test_string_comparison(self) -> None:
        assert Status.SUCCESSFUL == "successful"
        assert Status.FAILED != "successful"

    def test_from_string(self) -> None:
        assert Status("pending") is Status.PENDING
        assert Status("failed") is Status.FAILED

    def test_invalid_value_raises(self) -> None:
        import pytest
        with pytest.raises(ValueError):
            Status("invalid")

    def test_usable_in_dict_keys(self) -> None:
        d = {Status.PENDING: "waiting", Status.FAILED: "error"}
        assert d[Status.PENDING] == "waiting"
        assert d["pending"] == "waiting"

    def test_json_serializable(self) -> None:
        import json
        data = {"status": Status.SUCCESSFUL}
        result = json.dumps(data)
        assert '"successful"' in result
