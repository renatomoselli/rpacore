"""rpacore — RPA Core."""

from importlib import metadata


def _resolve_version() -> str:
    try:
        return metadata.version("rpacore")
    except metadata.PackageNotFoundError:
        return "0.0.0+local"


__version__ = _resolve_version()

from rpacore.config import load_config
from rpacore.config_validation import ConfigField, optional_config, require_config, require_section, validate_config
from rpacore.context import ProcessContext
from rpacore.credentials import CredentialNotFoundError, CredentialProvider, EnvCredentialProvider, KeyringCredentialProvider, build_credential_provider
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, DefinitionIdentityError, ExecutionValidationError, SystemException
from rpacore.execution import execute_transaction
from rpacore.logger import bind_log_context, configure_logger, get_logger
from rpacore.manifest import (
    ProjectManifest,
    find_project_manifest,
    load_project_manifest,
    resolve_project_entrypoint,
)
from rpacore.notify import EmailNotifier, Notifier, WebhookNotifier, build_notifiers, dispatch
from rpacore.outcome import OutcomeCategory, RetryDisposition
from rpacore.paths import atomic_output_path, resolve_config_path, resolve_config_paths
from rpacore.persistence import (
    TransactionFenceError,
    TransactionPage,
    TransactionSummary,
    list_transactions,
    load_transaction,
    query_transactions,
    save_transaction,
)
from rpacore.queue import (
    QueueAdminEvent,
    QueueAttempt,
    QueueAttemptOutcome,
    QueueItem,
    QueueLeaseLostError,
    QueuePoisonEvent,
    QueueProvider,
    QueueStatus,
    SqliteQueue,
)
from rpacore.recovery import resume_transaction
from rpacore.report import ArtifactReport, OutcomeReport, ReportRecord, StepReport, TransactionReport, generate_report, render_html, render_json, render_text
from rpacore.runner import QueueRunSummary, run_queue_loop
from rpacore.serialization import TRANSACTION_FORMAT_VERSION, serialize_transaction
from rpacore.step import Step
from rpacore.status import Status
from rpacore.transaction import Artifact, HistoryEntry, HistoryEvent, Transaction
from rpacore.transition import ExecutionTransition

__all__ = [
    "Artifact",
    "ArtifactReport",
    "BusinessException",
    "ConfigField",
    "CredentialNotFoundError",
    "CredentialProvider",
    "DefinitionIdentityError",
    "EmailNotifier",
    "Engine",
    "EnvCredentialProvider",
    "ExecutionTransition",
    "ExecutionValidationError",
    "HistoryEntry",
    "HistoryEvent",
    "KeyringCredentialProvider",
    "Notifier",
    "OutcomeCategory",
    "OutcomeReport",
    "ProcessContext",
    "ProjectManifest",
    "QueueAdminEvent",
    "QueueAttempt",
    "QueueAttemptOutcome",
    "QueueItem",
    "QueueLeaseLostError",
    "QueuePoisonEvent",
    "QueueProvider",
    "QueueRunSummary",
    "QueueStatus",
    "ReportRecord",
    "RetryDisposition",
    "SqliteQueue",
    "Status",
    "Step",
    "StepReport",
    "SystemException",
    "TRANSACTION_FORMAT_VERSION",
    "Transaction",
    "TransactionFenceError",
    "TransactionPage",
    "TransactionReport",
    "TransactionSummary",
    "WebhookNotifier",
    "atomic_output_path",
    "bind_log_context",
    "build_credential_provider",
    "build_notifiers",
    "configure_logger",
    "dispatch",
    "execute_transaction",
    "find_project_manifest",
    "generate_report",
    "get_logger",
    "list_transactions",
    "load_config",
    "load_project_manifest",
    "load_transaction",
    "optional_config",
    "query_transactions",
    "render_html",
    "render_json",
    "render_text",
    "require_config",
    "require_section",
    "resolve_config_path",
    "resolve_config_paths",
    "resolve_project_entrypoint",
    "resume_transaction",
    "run_queue_loop",
    "save_transaction",
    "serialize_transaction",
    "validate_config",
]
