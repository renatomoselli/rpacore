"""rpacore — RPA Core."""

from importlib import metadata


def _resolve_version() -> str:
    try:
        return metadata.version("rpacore")
    except metadata.PackageNotFoundError:
        return "0.0.0+local"


__version__ = _resolve_version()

from rpacore.config import load_config
from rpacore.config_validation import optional_config, require_config, require_section
from rpacore.context import ProcessContext
from rpacore.credentials import CredentialNotFoundError, CredentialProvider, EnvCredentialProvider, KeyringCredentialProvider, build_credential_provider
from rpacore.engine import Engine
from rpacore.exceptions import BusinessException, ExecutionValidationError, SystemException
from rpacore.logger import configure_logger, get_logger
from rpacore.manifest import (
    ProjectManifest,
    find_project_manifest,
    load_project_manifest,
    resolve_project_entrypoint,
)
from rpacore.notify import EmailNotifier, Notifier, WebhookNotifier, build_notifiers, dispatch
from rpacore.paths import resolve_config_path, resolve_config_paths
from rpacore.persistence import list_transactions, load_transaction, save_transaction
from rpacore.queue import QueueItem, QueueLeaseLostError, QueueProvider, QueueStatus, SqliteQueue
from rpacore.recovery import resume_transaction
from rpacore.report import ArtifactReport, SkillReport, TransactionReport, generate_report, render_html, render_text
from rpacore.runner import QueueRunSummary, run_queue_loop
from rpacore.serialization import TRANSACTION_FORMAT_VERSION, serialize_transaction
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Artifact, HistoryEntry, HistoryEvent, Transaction

__all__ = [
    "build_notifiers",
    "build_credential_provider",
    "Artifact",
    "ArtifactReport",
    "BusinessException",
    "configure_logger",
    "CredentialNotFoundError",
    "CredentialProvider",
    "dispatch",
    "Engine",
    "EnvCredentialProvider",
    "EmailNotifier",
    "ExecutionValidationError",
    "find_project_manifest",
    "generate_report",
    "get_logger",
    "HistoryEntry",
    "HistoryEvent",
    "KeyringCredentialProvider",
    "Notifier",
    "list_transactions",
    "load_config",
    "load_project_manifest",
    "load_transaction",
    "optional_config",
    "ProcessContext",
    "ProjectManifest",
    "QueueItem",
    "QueueLeaseLostError",
    "QueueProvider",
    "QueueRunSummary",
    "QueueStatus",
    "require_config",
    "require_section",
    "resolve_config_path",
    "resolve_config_paths",
    "resolve_project_entrypoint",
    "resume_transaction",
    "WebhookNotifier",
    "render_html",
    "render_text",
    "run_queue_loop",
    "save_transaction",
    "serialize_transaction",
    "SkillReport",
    "SqliteQueue",
    "SystemException",
    "Skill",
    "Status",
    "Transaction",
    "TRANSACTION_FORMAT_VERSION",
    "TransactionReport",
]
