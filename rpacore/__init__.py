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
from rpacore.exceptions import BusinessException, SystemException
from rpacore.logger import configure_logger, get_logger
from rpacore.notify import EmailNotifier, Notifier, WebhookNotifier, build_notifiers, dispatch
from rpacore.paths import resolve_config_path, resolve_config_paths
from rpacore.persistence import list_transactions, load_transaction, save_transaction
from rpacore.queue import QueueItem, QueueProvider, QueueStatus, SqliteQueue
from rpacore.recovery import resume_transaction
from rpacore.report import SkillReport, TransactionReport, generate_report, render_html, render_text
from rpacore.runner import QueueRunSummary, run_queue_loop
from rpacore.skill import Skill
from rpacore.status import Status
from rpacore.transaction import Transaction

__all__ = [
    "build_notifiers",
    "build_credential_provider",
    "BusinessException",
    "configure_logger",
    "CredentialNotFoundError",
    "CredentialProvider",
    "dispatch",
    "Engine",
    "EnvCredentialProvider",
    "EmailNotifier",
    "generate_report",
    "get_logger",
    "KeyringCredentialProvider",
    "Notifier",
    "list_transactions",
    "load_config",
    "load_transaction",
    "optional_config",
    "ProcessContext",
    "QueueItem",
    "QueueProvider",
    "QueueRunSummary",
    "QueueStatus",
    "require_config",
    "require_section",
    "resolve_config_path",
    "resolve_config_paths",
    "resume_transaction",
    "WebhookNotifier",
    "render_html",
    "render_text",
    "run_queue_loop",
    "save_transaction",
    "SkillReport",
    "SqliteQueue",
    "SystemException",
    "Skill",
    "Status",
    "Transaction",
    "TransactionReport",
]
