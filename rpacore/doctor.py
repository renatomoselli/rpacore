"""Read-only operator diagnostics for the ``rpacore doctor`` command."""

from __future__ import annotations

import platform
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from rpacore import __version__
from rpacore._doctor_check import (
    FAIL, NOT_APPLICABLE, PASS, WARNING, DoctorCheck, input_failure,
)
from rpacore._doctor_config import discover_config
from rpacore._sqlite import (
    QUEUE_SCHEMA_VERSION,
    TRANSACTION_SCHEMA_VERSION,
    connect_sqlite,
    database_header_journal_mode,
    ensure_database_compatibility,
    require_current_schema,
)
from rpacore._schema_structure import SchemaComponent, missing_schema_objects
from rpacore.manifest import ProjectManifest, find_project_manifest, load_project_manifest


DOCTOR_FORMAT_VERSION = 1
_MISSING_SCHEMA_OBJECTS = "missing_schema_objects"


@dataclass(frozen=True)
class DoctorResult:
    """Versioned result returned by the doctor command implementation."""

    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        return 1 if any(check.status == FAIL for check in self.checks) else 0

    def to_dict(self) -> dict[str, object]:
        return {
            "doctor_format_version": DOCTOR_FORMAT_VERSION,
            "framework_version": __version__,
            "checks": [check.to_dict() for check in self.checks],
        }


def collect_doctor_result(
    *,
    transaction_db_path: str | None = None,
    queue_db_path: str | None = None,
    config_path: str | None = None,
) -> DoctorResult:
    """Collect deterministic read-only diagnostics without importing project code."""
    checks = [
        DoctorCheck(
            "runtime.python",
            PASS,
            "Python runtime is available",
            {"version": platform.python_version()},
        ),
        DoctorCheck(
            "runtime.sqlite",
            PASS,
            "SQLite runtime is available",
            {"version": sqlite3.sqlite_version},
        ),
    ]
    manifest, manifest_checks = _discover_manifest()
    checks.extend(manifest_checks)

    config, config_checks = discover_config(
        config_path=config_path,
        manifest=manifest,
    )
    checks.extend(config_checks)

    selected_transaction_path = (
        transaction_db_path
        if transaction_db_path is not None
        else None if manifest is None else manifest.transaction_db_path
    )
    checks.extend(
        _database_checks(
            component="transactions",
            label="Transaction",
            db_path=selected_transaction_path,
            supported_version=TRANSACTION_SCHEMA_VERSION,
            explicit=transaction_db_path is not None,
        )
    )

    selected_queue_path = queue_db_path
    if selected_queue_path is None and config is not None:
        queue = config.get("queue")
        if isinstance(queue, dict):
            candidate = queue.get("db_path")
            if isinstance(candidate, str):
                selected_queue_path = candidate
    queue_checks = _database_checks(
        component="queue",
        label="Queue",
        db_path=selected_queue_path,
        supported_version=QUEUE_SCHEMA_VERSION,
        explicit=queue_db_path is not None,
    )
    checks.extend(queue_checks)
    queue_schema = next(check for check in queue_checks if check.id == "queue.schema")
    if selected_queue_path is None:
        checks.append(DoctorCheck("queue.health", NOT_APPLICABLE, "No queue database selected"))
    elif queue_schema.details.get("reason") == _MISSING_SCHEMA_OBJECTS:
        # Counts beside a failed structural check would read as partly healthy.
        checks.append(
            DoctorCheck("queue.health", NOT_APPLICABLE, "Queue health requires a current readable schema")
        )
    else:
        checks.append(_queue_health_check(selected_queue_path))
    return DoctorResult(tuple(checks))


def _discover_manifest() -> tuple[ProjectManifest | None, list[DoctorCheck]]:
    try:
        candidate = find_project_manifest()
    except FileNotFoundError:
        return None, [DoctorCheck("project.manifest", NOT_APPLICABLE, "No project manifest found")]
    except Exception as exc:
        return None, [input_failure("project.manifest", "Project manifest", exc)]
    try:
        manifest = load_project_manifest(candidate)
    except Exception as exc:
        return None, [input_failure("project.manifest", "Project manifest", exc)]
    return manifest, [DoctorCheck("project.manifest", PASS, "Project manifest is valid")]


def _database_checks(
    *,
    component: SchemaComponent,
    label: str,
    db_path: str | None,
    supported_version: int,
    explicit: bool,
) -> list[DoctorCheck]:
    prefix = component
    if db_path is None:
        return _not_applicable_database_checks(prefix, f"No {label.lower()} database selected")
    try:
        header_journal_mode = database_header_journal_mode(
            db_path,
            field=f"doctor.{component}_db_path",
        )
    except FileNotFoundError:
        if not explicit:
            summary = (
                f"{label} database is absent and unchecked: it may not yet be created or may have been removed; "
                "verify the configured location and recovery/backup for established deployments before running work"
            )
            checks = _not_applicable_database_checks(prefix, summary)
            checks[0] = DoctorCheck(
                f"{prefix}.schema", NOT_APPLICABLE, summary,
                {"reason": "missing_discovered_database"},
            )
            return checks
        return _failed_database_checks(prefix, f"{label} database is unavailable")
    except Exception:
        return _failed_database_checks(prefix, f"{label} database is unavailable")
    if header_journal_mode == "not_sqlite":
        return _failed_database_checks(prefix, f"{label} database is not an SQLite database")
    if header_journal_mode == "wal":
        return _wal_database_checks(prefix, label)
    try:
        connection = connect_sqlite(db_path, field=f"doctor.{component}_db_path", readonly=True)
    except Exception:
        return _failed_database_checks(prefix, f"{label} database is unavailable")
    try:
        try:
            ensure_database_compatibility(connection)
            require_current_schema(
                connection,
                component=component,
                supported_version=supported_version,
                label=label.lower(),
            )
        except Exception:
            return _failed_database_checks(prefix, f"{label} database schema is incompatible")
        try:
            structurally_incomplete = missing_schema_objects(connection, component)
        except sqlite3.Error:
            return _failed_database_checks(prefix, f"{label} database schema is incompatible")
        if structurally_incomplete:
            return _failed_database_checks(
                prefix,
                f"{label} database schema is structurally incomplete",
                {"version": supported_version, "reason": _MISSING_SCHEMA_OBJECTS},
            )

        try:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            journal_check = DoctorCheck(
                f"{prefix}.journal",
                PASS if journal_mode == "delete" else WARNING,
                "Rollback journal is active" if journal_mode == "delete" else "Journal mode is not rollback journal",
                {"mode": journal_mode},
            )
        except sqlite3.Error:
            journal_check = DoctorCheck(f"{prefix}.journal", FAIL, "SQLite journal inspection failed")
        try:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0]).lower()
            quick_check_result = DoctorCheck(
                f"{prefix}.quick_check",
                PASS if quick_check == "ok" else FAIL,
                "SQLite quick check passed" if quick_check == "ok" else "SQLite quick check reported an error",
            )
        except sqlite3.Error:
            quick_check_result = DoctorCheck(f"{prefix}.quick_check", FAIL, "SQLite quick check failed")
        try:
            foreign_keys_ok = connection.execute("PRAGMA foreign_key_check").fetchone() is None
            foreign_key_result = DoctorCheck(
                f"{prefix}.foreign_keys",
                PASS if foreign_keys_ok else FAIL,
                "Foreign-key check passed" if foreign_keys_ok else "Foreign-key check reported violations",
            )
        except sqlite3.Error:
            foreign_key_result = DoctorCheck(
                f"{prefix}.foreign_keys", FAIL, "Foreign-key check failed"
            )
        return [
            DoctorCheck(
                f"{prefix}.schema",
                PASS,
                f"{label} database schema is current",
                {"version": supported_version},
            ),
            journal_check,
            quick_check_result,
            foreign_key_result,
        ]
    finally:
        connection.close()


def _not_applicable_database_checks(prefix: str, summary: str) -> list[DoctorCheck]:
    return [
        DoctorCheck(f"{prefix}.schema", NOT_APPLICABLE, summary),
        DoctorCheck(f"{prefix}.journal", NOT_APPLICABLE, summary),
        DoctorCheck(f"{prefix}.quick_check", NOT_APPLICABLE, summary),
        DoctorCheck(f"{prefix}.foreign_keys", NOT_APPLICABLE, summary),
    ]


def _failed_database_checks(
    prefix: str, summary: str, details: dict[str, object] | None = None
) -> list[DoctorCheck]:
    return [
        DoctorCheck(f"{prefix}.schema", FAIL, summary, details or {}),
        DoctorCheck(f"{prefix}.journal", NOT_APPLICABLE, "Schema check did not permit journal inspection"),
        DoctorCheck(f"{prefix}.quick_check", NOT_APPLICABLE, "Schema check did not permit integrity inspection"),
        DoctorCheck(f"{prefix}.foreign_keys", NOT_APPLICABLE, "Schema check did not permit integrity inspection"),
    ]


def _wal_database_checks(prefix: str, label: str) -> list[DoctorCheck]:
    return [
        DoctorCheck(
            f"{prefix}.schema",
            NOT_APPLICABLE,
            f"{label} database is not opened while WAL is active",
        ),
        DoctorCheck(
            f"{prefix}.journal",
            WARNING,
            "WAL journal is active; deeper checks were skipped to preserve sidecars",
            {"mode": "wal"},
        ),
        DoctorCheck(f"{prefix}.quick_check", NOT_APPLICABLE, "WAL database was not opened"),
        DoctorCheck(f"{prefix}.foreign_keys", NOT_APPLICABLE, "WAL database was not opened"),
    ]


def _queue_health_check(db_path: str) -> DoctorCheck:
    try:
        header_journal_mode = database_header_journal_mode(
            db_path,
            field="doctor.queue_db_path",
        )
        if header_journal_mode == "not_sqlite":
            return DoctorCheck("queue.health", NOT_APPLICABLE, "Queue database is not an SQLite database")
        if header_journal_mode == "wal":
            return DoctorCheck("queue.health", NOT_APPLICABLE, "WAL queue database was not opened")
        connection = connect_sqlite(db_path, field="doctor.queue_db_path", readonly=True)
    except FileNotFoundError:
        return DoctorCheck("queue.health", NOT_APPLICABLE, "Queue database is absent and unchecked")
    except Exception:
        return DoctorCheck("queue.health", NOT_APPLICABLE, "Queue database is unavailable")
    try:
        try:
            ensure_database_compatibility(connection)
            require_current_schema(
                connection,
                component="queue",
                supported_version=QUEUE_SCHEMA_VERSION,
                label="queue",
            )
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM queue_items GROUP BY status ORDER BY status"
            ).fetchall()
            bound_items = int(
                connection.execute(
                    "SELECT COUNT(*) FROM queue_items WHERE transaction_id <> ''"
                ).fetchone()[0]
            )
            invalid_claims = int(
                connection.execute(
                    "SELECT COUNT(*) FROM queue_items "
                    "WHERE status = 'in_progress' "
                    "AND (claimed_by = '' OR claim_token = '' OR claimed_at IS NULL)"
                ).fetchone()[0]
            )
            oldest = connection.execute("SELECT MIN(created_at) FROM queue_items").fetchone()[0]
        except Exception:
            return DoctorCheck("queue.health", NOT_APPLICABLE, "Queue health requires a current readable schema")
    finally:
        connection.close()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    known_statuses = ("pending", "in_progress", "successful", "failed")
    details: dict[str, object] = {
        "pending_items": counts.get("pending", 0),
        "in_progress_items": counts.get("in_progress", 0),
        "successful_items": counts.get("successful", 0),
        "failed_items": counts.get("failed", 0),
        "unknown_status_items": sum(
            count for status, count in counts.items() if status not in known_statuses
        ),
        "bound_items": bound_items,
    }
    if oldest:
        try:
            created_at = datetime.fromisoformat(str(oldest))
            if created_at.tzinfo is not None:
                details["oldest_age_seconds"] = max(
                    0, int((datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds())
                )
        except ValueError:
            pass
    return DoctorCheck(
        "queue.health",
        FAIL if invalid_claims else PASS,
        "Queue items have complete claim bindings" if not invalid_claims else "Queue has incomplete in-progress claim bindings",
        details,
    )
