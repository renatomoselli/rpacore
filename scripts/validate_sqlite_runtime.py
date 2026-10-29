"""Run SQLite safety tests against an installed wheel outside its checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from _test_process import run_tests as _run_tests


def validate_sqlite_runtime(*, repo_root: Path, wheel: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    # Invalidate previous reports even if package import or identity checks fail.
    for name in ("sqlite-runtime.json", "sqlite-runtime.xml"):
        (output_dir / name).unlink(missing_ok=True)
    import rpacore
    from rpacore import SqliteQueue, Transaction, atomic_output_path, save_transaction

    imported_from = Path(rpacore.__file__).resolve()
    if imported_from.is_relative_to(repo_root.resolve() / "rpacore"):
        raise RuntimeError("SQLite safety gate imported rpacore from checkout")
    result = {
        "schema_version": 1,
        "wheel": {"name": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "runtime": {
            "os": platform.system(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "status": "fail",
        "tests": ["test_queue.py", "test_queue_retry.py", "test_persistence.py", "test_sqlite_runtime.py"],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="rpacore-sqlite-") as temporary:
            suite = Path(temporary)
            journal_modes = {}
            for component in ("transactions", "queue"):
                database = suite / f"{component}.db"
                if component == "transactions":
                    save_transaction(Transaction("sqlite-runtime-probe"), str(database))
                else:
                    SqliteQueue({"db_path": str(database)})
                with closing(sqlite3.connect(database)) as connection:
                    journal_modes[component] = connection.execute("PRAGMA journal_mode").fetchone()[0]
            result["journal_modes"] = journal_modes
            if set(journal_modes.values()) != {"delete"}:
                raise RuntimeError(f"SQLite safety gate requires DELETE mode: {journal_modes}")
            for name in result["tests"]:
                shutil.copyfile(repo_root / "tests" / name, suite / name)
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            _run_tests(
                [sys.executable, "-m", "pytest", *result["tests"], "-q",
                 "--basetemp", str(suite / "tmp"),
                 "--junitxml", str(output_dir / "sqlite-runtime.xml")],
                cwd=suite,
                env=env,
                timeout=300,
            )
        result["status"] = "pass"
    finally:
        # Keep bounded, synthetic evidence even when tests or suite cleanup fail.
        with atomic_output_path(output_dir / "sqlite-runtime.json") as temporary_report:
            temporary_report.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print("SQLite runtime safety: " + json.dumps(result, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    validate_sqlite_runtime(
        repo_root=args.repo_root.resolve(),
        wheel=args.wheel.resolve(),
        output_dir=args.output_dir.resolve(),
    )


if __name__ == "__main__":
    main()
