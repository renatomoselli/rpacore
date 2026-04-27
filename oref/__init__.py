"""OREF — Open Robotic Enterprise Framework."""

__version__ = "0.1.0"

from oref.config import load_config
from oref.context import ProcessContext
from oref.credentials import CredentialNotFoundError, CredentialProvider, EnvCredentialProvider, KeyringCredentialProvider, build_credential_provider
from oref.engine import Engine
from oref.exceptions import BusinessException, SystemException
from oref.logger import configure_logger, get_logger
from oref.notify import EmailNotifier, Notifier, WebhookNotifier, build_notifiers, dispatch
from oref.persistence import list_transactions, load_transaction, save_transaction
from oref.queue import QueueItem, QueueProvider, QueueStatus, SqliteQueue
from oref.recovery import resume_transaction
from oref.report import SkillReport, TransactionReport, generate_report, render_html, render_text
from oref.runner import QueueRunSummary, run_queue_loop
from oref.skill import Skill
from oref.status import Status
from oref.transaction import Transaction

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
    "ProcessContext",
    "QueueItem",
    "QueueProvider",
    "QueueRunSummary",
    "QueueStatus",
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
