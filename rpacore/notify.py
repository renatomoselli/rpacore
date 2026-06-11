"""Notification dispatch for rpacore transactions."""

from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.request
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Protocol, runtime_checkable

from rpacore._validation import type_error, value_error
from rpacore.credentials import CredentialProvider
from rpacore.exceptions import BusinessException
from rpacore.logger import get_logger
from rpacore.report import TransactionReport, render_html, render_text


@runtime_checkable
class Notifier(Protocol):
    """Protocol for notification backends."""

    def send(self, report: TransactionReport) -> None:
        """Send a notification for the given report."""
        ...


class EmailNotifier:
    """Sends transaction reports via SMTP.

    The SMTP password is read from the CredentialProvider via
    credentials.get("smtp_password") — never stored in config.

    Config section: [notification.email]
      host      (str, required)
      port      (int, default 587)
      from_addr (str, required)
      to_addrs  (list[str] or comma-separated str, required)

    Screenshots referenced in the report are attached as files if they
    exist on disk.
    """

    def __init__(
        self,
        config: dict[str, object],
        credentials: CredentialProvider,
    ) -> None:
        raw = config.get("notification", {})
        if not isinstance(raw, dict):
            raise type_error("notification", "dict", raw)
        cfg = raw.get("email", {})
        if not isinstance(cfg, dict):
            raise type_error("notification.email", "dict", cfg)

        host = cfg.get("host", "")
        if not isinstance(host, str):
            raise type_error("notification.email.host", "str", host)
        if not host:
            raise value_error("notification.email.host", "non-empty str", host)

        port = cfg.get("port", 587)
        if isinstance(port, bool) or not isinstance(port, int):
            raise type_error("notification.email.port", "int", port)

        from_addr = cfg.get("from_addr", "")
        if not isinstance(from_addr, str):
            raise type_error("notification.email.from_addr", "str", from_addr)
        if not from_addr:
            raise value_error(
                "notification.email.from_addr", "non-empty str", from_addr
            )

        to_addrs_raw = cfg.get("to_addrs", "")
        if isinstance(to_addrs_raw, list):
            if any(not isinstance(a, str) for a in to_addrs_raw):
                raise type_error("notification.email.to_addrs", "list[str]", to_addrs_raw)
            to_addrs = [a for a in to_addrs_raw]
        elif isinstance(to_addrs_raw, str):
            to_addrs = [a.strip() for a in to_addrs_raw.split(",") if a.strip()]
        else:
            raise type_error(
                "notification.email.to_addrs",
                "list[str] or comma-separated str",
                to_addrs_raw,
            )
        if not to_addrs:
            raise value_error(
                "notification.email.to_addrs", "non-empty list[str]", to_addrs_raw
            )

        timeout = cfg.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise type_error("notification.email.timeout", "int", timeout)
        if timeout <= 0:
            raise value_error("notification.email.timeout", "int > 0", timeout)

        self.host: str = host
        self.port: int = port
        self.timeout: int = timeout
        self.from_addr: str = from_addr
        self.to_addrs: list[str] = to_addrs
        self._credentials: CredentialProvider = credentials

    def send(self, report: TransactionReport) -> None:
        """Build and send an HTML email with optional screenshot attachments."""
        password = self._credentials.get("smtp_password")

        html_body = render_html(report)
        text_body = render_text(report)

        msg = MIMEMultipart("mixed")
        msg["Subject"] = f"rpacore [{report.status}] {report.reference}"
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text_body, "plain", "utf-8"))
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        # Attach screenshots referenced in exception reports.
        seen: set[str] = set()
        for sr in report.skills:
            for exc in sr.exceptions:
                path = exc.screenshot_path
                if path and path not in seen:
                    seen.add(path)
                    try:
                        filename = os.path.basename(path)
                        with open(path, "rb") as fh:
                            part = MIMEApplication(fh.read())
                        part["Content-Disposition"] = f'attachment; filename="{filename}"'
                        msg.attach(part)
                    except OSError:
                        pass  # Screenshot missing on disk — skip attachment silently.

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            smtp.starttls()
            smtp.login(self.from_addr, password)
            smtp.sendmail(self.from_addr, self.to_addrs, msg.as_string())


class WebhookNotifier:
    """Posts transaction reports as JSON to a webhook URL.

    Compatible with Slack incoming webhooks, Teams, Discord, or any custom
    endpoint that accepts a JSON POST.

    Config section: [notification.webhook]
      url                 (str, required)
      include_transaction (bool, default false)
    """

    def __init__(self, config: dict[str, object]) -> None:
        raw = config.get("notification", {})
        if not isinstance(raw, dict):
            raise type_error("notification", "dict", raw)
        cfg = raw.get("webhook", {})
        if not isinstance(cfg, dict):
            raise type_error("notification.webhook", "dict", cfg)

        url = cfg.get("url", "")
        if not isinstance(url, str):
            raise type_error("notification.webhook.url", "str", url)
        if not url:
            raise value_error("notification.webhook.url", "non-empty str", url)

        timeout = cfg.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise type_error("notification.webhook.timeout", "int", timeout)
        if timeout <= 0:
            raise value_error("notification.webhook.timeout", "int > 0", timeout)

        include_transaction = cfg.get("include_transaction", False)
        if not isinstance(include_transaction, bool):
            raise type_error(
                "notification.webhook.include_transaction",
                "bool",
                include_transaction,
            )

        self.url: str = url
        self.timeout: int = timeout
        self.include_transaction: bool = include_transaction

    def send(self, report: TransactionReport) -> None:
        """POST a JSON payload to the configured webhook URL."""
        payload = {
            "transaction_id": report.transaction_id,
            "reference": report.reference,
            "status": str(report.status),
            "retry_count": report.retry_count,
            "generated_at": report.generated_at.isoformat(),
            "text": render_text(report),
        }
        if self.include_transaction and report.transaction_record:
            payload["transaction"] = report.transaction_record
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            resp.read()


def dispatch(
    notifiers: list[Notifier],
    report: TransactionReport,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """Send a report through all configured notifiers.

    If a notifier raises, the error is logged and swallowed.
    The transaction outcome is never affected by notification failures.
    """
    log = logger if logger is not None else get_logger()
    for notifier in notifiers:
        try:
            notifier.send(report)
        except MemoryError:
            raise
        except Exception:
            log.exception(
                "Notifier %s failed; swallowing to protect transaction outcome",
                type(notifier).__name__,
                extra={"event": "notifier_error", "notifier": type(notifier).__name__},
            )


def build_notifiers(
    config: dict[str, object],
    credentials: CredentialProvider,
) -> list[Notifier]:
    """Construct notifiers from config. Missing sections produce no notifier.

    Returns a list containing whichever notifiers are configured.
    An absent [notification.email] or [notification.webhook] section is a no-op.
    """
    notifiers: list[Notifier] = []
    notification = config.get("notification", {})
    if not isinstance(notification, dict):
        return notifiers

    if "email" in notification:
        notifiers.append(EmailNotifier(config, credentials))
    if "webhook" in notification:
        notifiers.append(WebhookNotifier(config))

    return notifiers
