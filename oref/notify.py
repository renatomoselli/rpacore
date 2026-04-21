"""Notification dispatch for OREF transactions."""

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

from oref.credentials import CredentialProvider
from oref.exceptions import BusinessException
from oref.logger import get_logger
from oref.report import TransactionReport, render_html, render_text


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
            raise TypeError("config['notification'] must be a dict")
        cfg = raw.get("email", {})
        if not isinstance(cfg, dict):
            raise TypeError("config['notification']['email'] must be a dict")

        host = cfg.get("host", "")
        if not isinstance(host, str):
            raise TypeError("notification.email.host must be a str")
        if not host:
            raise ValueError("notification.email.host is required")

        port = cfg.get("port", 587)
        if isinstance(port, bool) or not isinstance(port, int):
            raise TypeError("notification.email.port must be an int")

        from_addr = cfg.get("from_addr", "")
        if not isinstance(from_addr, str):
            raise TypeError("notification.email.from_addr must be a str")
        if not from_addr:
            raise ValueError("notification.email.from_addr is required")

        to_addrs_raw = cfg.get("to_addrs", "")
        if isinstance(to_addrs_raw, list):
            bad = [a for a in to_addrs_raw if not isinstance(a, str)]
            if bad:
                raise TypeError(
                    f"notification.email.to_addrs entries must all be str, got: {bad!r}"
                )
            to_addrs = [a for a in to_addrs_raw]
        elif isinstance(to_addrs_raw, str):
            to_addrs = [a.strip() for a in to_addrs_raw.split(",") if a.strip()]
        else:
            raise TypeError("notification.email.to_addrs must be a list or comma-separated str")
        if not to_addrs:
            raise ValueError("notification.email.to_addrs is required")

        timeout = cfg.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise TypeError("notification.email.timeout must be an int")
        if timeout <= 0:
            raise ValueError("notification.email.timeout must be a positive int")

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
        msg["Subject"] = f"OREF [{report.status}] {report.reference}"
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
      url (str, required)
    """

    def __init__(self, config: dict[str, object]) -> None:
        raw = config.get("notification", {})
        if not isinstance(raw, dict):
            raise TypeError("config['notification'] must be a dict")
        cfg = raw.get("webhook", {})
        if not isinstance(cfg, dict):
            raise TypeError("config['notification']['webhook'] must be a dict")

        url = cfg.get("url", "")
        if not isinstance(url, str):
            raise TypeError("notification.webhook.url must be a str")
        if not url:
            raise ValueError("notification.webhook.url is required")

        timeout = cfg.get("timeout", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise TypeError("notification.webhook.timeout must be an int")
        if timeout <= 0:
            raise ValueError("notification.webhook.timeout must be a positive int")

        self.url: str = url
        self.timeout: int = timeout

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
