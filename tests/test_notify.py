"""Tests for rpacore/notify.py."""

from __future__ import annotations

import json
import logging
import os
import smtplib
import socket
import ssl
from datetime import datetime, timezone
from email import message_from_string
from html.parser import HTMLParser
from io import BytesIO, StringIO
from pathlib import PureWindowsPath
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import pytest

from rpacore.credentials import EnvCredentialProvider
from rpacore.exceptions import BusinessException, SystemException
from rpacore.logger import configure_logger
from rpacore.notify import (
    EmailNotifier,
    Notifier,
    WebhookNotifier,
    build_notifiers,
    dispatch,
)
from rpacore.report import (
    ArtifactReport,
    ReportRecord,
    StepReport,
    TransactionReport,
    generate_report,
    render_json,
)
from rpacore.status import Status
from rpacore.transaction import Transaction


class DomainRejection(BusinessException):
    """Application-level business rejection used by category tests."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_report(reference: str = "ref-test", status: Status = Status.SUCCESSFUL) -> TransactionReport:
    return TransactionReport(
        transaction_id="tx-001",
        reference=reference,
        status=status,
        retry_count=0,
        steps=[],
        metadata={"customer": "acme"},
        artifacts=[
            ArtifactReport(
                id="artifact-001",
                name="invoice",
                path="/tmp/invoice.pdf",
                kind="pdf",
                metadata={"invoice_id": 42},
                created_at=datetime(2026, 4, 21, 11, 0, 0, tzinfo=timezone.utc),
            )
        ],
        transaction_record={
            "transaction_format_version": 3,
            "id": "tx-001",
            "reference": reference,
            "status": str(status),
            "metadata": {"customer": "acme"},
            "artifacts": [
                {
                    "id": "artifact-001",
                    "name": "invoice",
                    "path": "/tmp/invoice.pdf",
                    "kind": "pdf",
                    "metadata": {"invoice_id": 42},
                    "created_at": "2026-04-21T11:00:00+00:00",
                }
            ],
        },
        generated_at=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
    )


def _business_subclass_report() -> TransactionReport:
    """A failed report whose only exception is a ``BusinessException`` subclass."""
    report = _make_report(status=Status.FAILED)
    report.steps = [StepReport(
        name="reject", execution_order=1, status=Status.FAILED, icon="✗",
        exceptions=[DomainRejection("bad row")],
    )]
    return report


def _email_config(host: str = "smtp.example.com", port: int = 587) -> dict:
    return {
        "notification": {
            "email": {
                "host": host,
                "port": port,
                "from_addr": "rpacore@example.com",
                "to_addrs": ["admin@example.com"],
            }
        }
    }


def _webhook_config(
    url: str = "https://hooks.example.com/notify",
    *,
    include_transaction: bool = False,
    include_report: bool = False,
) -> dict:
    return {
        "notification": {
            "webhook": {
                "url": url,
                "include_transaction": include_transaction,
                "include_report": include_report,
            }
        }
    }


def _creds(password: str = "s3cr3t") -> EnvCredentialProvider:
    provider = MagicMock()
    provider.get = MagicMock(return_value=password)
    return provider


# ---------------------------------------------------------------------------
# TestNotifierProtocol
# ---------------------------------------------------------------------------

class TestNotifierProtocol:
    def test_email_notifier_satisfies_protocol(self):
        notifier = EmailNotifier(_email_config(), _creds())
        assert isinstance(notifier, Notifier)

    def test_webhook_notifier_satisfies_protocol(self):
        notifier = WebhookNotifier(_webhook_config())
        assert isinstance(notifier, Notifier)


# ---------------------------------------------------------------------------
# TestEmailNotifierConfig
# ---------------------------------------------------------------------------

class TestEmailNotifierConfig:
    def test_valid_config(self):
        n = EmailNotifier(_email_config(), _creds())
        assert n.host == "smtp.example.com"
        assert n.port == 587
        assert n.from_addr == "rpacore@example.com"
        assert n.to_addrs == ["admin@example.com"]
        assert n.attach_screenshots is True

    def test_to_addrs_as_comma_string(self):
        cfg = {"notification": {"email": {
            "host": "smtp.example.com",
            "from_addr": "a@b.com",
            "to_addrs": "x@b.com, y@b.com",
        }}}
        n = EmailNotifier(cfg, _creds())
        assert n.to_addrs == ["x@b.com", "y@b.com"]

    def test_missing_host_raises(self):
        cfg = {"notification": {"email": {"from_addr": "a@b.com", "to_addrs": ["x@b.com"]}}}
        with pytest.raises(ValueError) as exc_info:
            EmailNotifier(cfg, _creds())

        assert str(exc_info.value) == (
            "notification.email.host expected non-empty str; got str value=''"
        )

    def test_bad_host_type_raises(self):
        cfg = {"notification": {"email": {"host": 123, "from_addr": "a@b.com", "to_addrs": ["x@b.com"]}}}
        with pytest.raises(TypeError, match="host"):
            EmailNotifier(cfg, _creds())

    def test_missing_from_addr_raises(self):
        cfg = {"notification": {"email": {"host": "smtp.example.com", "to_addrs": ["x@b.com"]}}}
        with pytest.raises(ValueError, match="from_addr"):
            EmailNotifier(cfg, _creds())

    def test_bad_from_addr_type_raises(self):
        cfg = {"notification": {"email": {"host": "h", "from_addr": 99, "to_addrs": ["x@b.com"]}}}
        with pytest.raises(TypeError, match="from_addr"):
            EmailNotifier(cfg, _creds())

    def test_missing_to_addrs_raises(self):
        cfg = {"notification": {"email": {"host": "h", "from_addr": "a@b.com"}}}
        with pytest.raises(ValueError, match="to_addrs"):
            EmailNotifier(cfg, _creds())

    def test_bad_to_addrs_type_raises(self):
        cfg = {"notification": {"email": {"host": "h", "from_addr": "a@b.com", "to_addrs": 42}}}
        with pytest.raises(TypeError, match="to_addrs"):
            EmailNotifier(cfg, _creds())

    def test_non_string_list_entries_rejected(self):
        cfg = {"notification": {"email": {
            "host": "h", "from_addr": "a@b.com",
            "to_addrs": ["ok@b.com", 123],
        }}}
        with pytest.raises(TypeError, match="to_addrs"):
            EmailNotifier(cfg, _creds())

    def test_port_bool_rejected(self):
        cfg = {"notification": {"email": {
            "host": "h", "from_addr": "a@b.com", "to_addrs": ["x@b.com"], "port": True,
        }}}
        with pytest.raises(TypeError) as exc_info:
            EmailNotifier(cfg, _creds())

        assert str(exc_info.value) == (
            "notification.email.port expected int; got bool value=True"
        )

    def test_bad_notification_section_type(self):
        with pytest.raises(TypeError) as exc_info:
            EmailNotifier({"notification": "bad"}, _creds())

        assert str(exc_info.value) == "notification expected dict; got str value='bad'"

    def test_bad_email_section_type(self):
        with pytest.raises(TypeError, match="email"):
            EmailNotifier({"notification": {"email": "bad"}}, _creds())

    def test_default_timeout(self):
        n = EmailNotifier(_email_config(), _creds())
        assert n.timeout == 30

    def test_custom_timeout(self):
        cfg = _email_config()
        cfg["notification"]["email"]["timeout"] = 10
        n = EmailNotifier(cfg, _creds())
        assert n.timeout == 10

    def test_timeout_bool_rejected(self):
        cfg = _email_config()
        cfg["notification"]["email"]["timeout"] = True
        with pytest.raises(TypeError, match="timeout"):
            EmailNotifier(cfg, _creds())

    def test_timeout_zero_rejected(self):
        cfg = _email_config()
        cfg["notification"]["email"]["timeout"] = 0
        with pytest.raises(ValueError, match="timeout"):
            EmailNotifier(cfg, _creds())

    def test_timeout_negative_rejected(self):
        cfg = _email_config()
        cfg["notification"]["email"]["timeout"] = -5
        with pytest.raises(ValueError, match="timeout"):
            EmailNotifier(cfg, _creds())

    def test_attach_screenshots_can_be_disabled(self):
        cfg = _email_config()
        cfg["notification"]["email"]["attach_screenshots"] = False
        n = EmailNotifier(cfg, _creds())
        assert n.attach_screenshots is False

    def test_attach_screenshots_bad_type_rejected(self):
        cfg = _email_config()
        cfg["notification"]["email"]["attach_screenshots"] = "yes"
        with pytest.raises(TypeError) as exc_info:
            EmailNotifier(cfg, _creds())

        assert str(exc_info.value) == (
            "notification.email.attach_screenshots expected bool; "
            "got str value='yes'"
        )


# ---------------------------------------------------------------------------
# TestEmailNotifierSend
# ---------------------------------------------------------------------------

class TestEmailNotifierSend:
    def test_email_html_uses_safe_screenshot_links(self):
        report = _make_report()
        report.steps = [StepReport(
            name="synthetic", execution_order=1, status=Status.FAILED, icon="x",
            exceptions=[
                SystemException("unsafe", screenshot_path="javascript:alert(1)"),
                BusinessException("local", screenshot_path="shots/err.png"),
            ],
        )]
        with patch("rpacore.notify.smtplib.SMTP") as smtp_cls:
            EmailNotifier(_email_config(), _creds("synthetic")).send(report)
        smtp = smtp_cls.return_value.__enter__.return_value
        message = message_from_string(smtp.sendmail.call_args.args[2])
        html_part = next(part for part in message.walk() if part.get_content_type() == "text/html")
        text_part = next(part for part in message.walk() if part.get_content_type() == "text/plain")

        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    links.append(dict(attrs))

        links = []
        html_body = html_part.get_payload(decode=True).decode("utf-8")
        Links().feed(html_body)
        assert links == [{"href": "shots/err.png"}]
        assert "javascript:alert(1)" in html_body
        assert "javascript:alert(1)" in text_part.get_payload(decode=True).decode("utf-8")

    def _send(self, report: TransactionReport | None = None, password: str = "pw") -> MagicMock:
        """Call send() with a mocked SMTP and return the mock instance."""
        notifier = EmailNotifier(_email_config(), _creds(password))
        if report is None:
            report = _make_report()
        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier.send(report)
        return mock_smtp

    def test_starttls_called(self):
        smtp = self._send()
        smtp.starttls.assert_called_once()
        context = smtp.starttls.call_args.kwargs["context"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_starttls_completes_before_login(self):
        smtp = self._send(password="mysecret")
        method_names = [call[0] for call in smtp.method_calls]

        assert method_names.index("starttls") < method_names.index("login")

    def test_certificate_failure_prevents_login_and_send(self):
        notifier = EmailNotifier(_email_config(), _creds("mysecret"))

        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp.starttls.side_effect = ssl.SSLCertVerificationError(
                "certificate verify failed"
            )
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(ssl.SSLCertVerificationError):
                notifier.send(_make_report())

        mock_smtp.login.assert_not_called()
        mock_smtp.sendmail.assert_not_called()

    def test_certificate_failure_is_contained_without_logging_credentials(self, caplog):
        password = "smtp-password-must-stay-secret"
        notifier = EmailNotifier(_email_config(), _creds(password))
        logger = logging.getLogger("test.notify.tls")

        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp.starttls.side_effect = ssl.SSLCertVerificationError(
                "certificate verify failed"
            )
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            with caplog.at_level(logging.ERROR, logger=logger.name):
                dispatch([notifier], _make_report(), logger=logger)

        mock_smtp.login.assert_not_called()
        mock_smtp.sendmail.assert_not_called()
        assert password not in caplog.text
        assert any(
            record.__dict__.get("event") == "notifier_error"
            for record in caplog.records
        )

    def test_smtp_constructed_with_timeout(self):
        notifier = EmailNotifier(_email_config(), _creds())
        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier.send(_make_report())
        _, kwargs = mock_smtp_cls.call_args
        assert kwargs.get("timeout") == 30

    def test_login_uses_credential(self):
        smtp = self._send(password="mysecret")
        smtp.login.assert_called_once_with("rpacore@example.com", "mysecret")

    def test_sendmail_called_with_recipients(self):
        smtp = self._send()
        smtp.sendmail.assert_called_once()
        _, recipients, _ = smtp.sendmail.call_args.args
        assert recipients == ["admin@example.com"]

    def test_subject_contains_status_and_reference(self):
        smtp = self._send(_make_report(reference="inv-99", status=Status.FAILED))
        _, _, raw_msg = smtp.sendmail.call_args.args
        assert "failed" in raw_msg.lower()
        assert "inv-99" in raw_msg

    def test_incomplete_record_keeps_subject_transaction_header(self):
        transaction = Transaction(reference="incomplete-email")
        transaction.state = {"runtime": object()}

        smtp = self._send(generate_report(transaction))

        _, _, raw_msg = smtp.sendmail.call_args.args
        assert "pending" in raw_msg.lower()
        assert "incomplete-email" in raw_msg

    def test_credentials_get_called_for_password(self):
        creds = _creds("pw")
        notifier = EmailNotifier(_email_config(), creds)
        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier.send(_make_report())
        creds.get.assert_called_once_with("smtp_password")

    def test_screenshot_attached_if_file_exists(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"PNG")

        step_report = StepReport(
            name="s", execution_order=1, status=Status.FAILED, icon="✗",
            exceptions=[BusinessException("err", screenshot_path=str(img))],
        )
        report = TransactionReport(
            transaction_id="tx", reference="r", status=Status.FAILED,
            retry_count=0, steps=[step_report],
            generated_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
        )
        notifier = EmailNotifier(_email_config(), _creds())
        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier.send(report)
        _, _, raw_msg = mock_smtp.sendmail.call_args.args
        message = message_from_string(raw_msg)
        attachments = [
            part for part in message.walk()
            if part.get_content_disposition() == "attachment"
        ]
        assert [part.get_filename() for part in attachments] == ["shot.png"]
        assert attachments[0].get_payload(decode=True) == b"PNG"
        html_part = next(
            part for part in message.walk() if part.get_content_type() == "text/html"
        )

        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    links.append(dict(attrs))

        links: list[dict[str, str]] = []
        Links().feed(html_part.get_payload(decode=True).decode("utf-8"))
        # Drive paths become file:/// targets; single-root POSIX paths stay absolute.
        expected_href = (
            PureWindowsPath(img).as_uri()
            if os.name == "nt"
            else quote(str(img), safe="/")
        )
        assert links == [{"href": expected_href}]

    def test_attachment_filename_not_double_quoted(self, tmp_path):
        """Content-Disposition filename must not contain embedded quotes."""
        img = tmp_path / "shot.png"
        img.write_bytes(b"PNG")

        step_report = StepReport(
            name="s", execution_order=1, status=Status.FAILED, icon="✗",
            exceptions=[BusinessException("err", screenshot_path=str(img))],
        )
        report = TransactionReport(
            transaction_id="tx", reference="r", status=Status.FAILED,
            retry_count=0, steps=[step_report],
            generated_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
        )
        notifier = EmailNotifier(_email_config(), _creds())
        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier.send(report)
        _, _, raw_msg = mock_smtp.sendmail.call_args.args
        # A well-formed header looks like: filename="shot.png"
        # A double-quoted one would be: filename=""shot.png""
        assert 'filename=""shot.png""' not in raw_msg
        assert 'filename="shot.png"' in raw_msg

    def test_missing_screenshot_file_does_not_raise(self):
        step_report = StepReport(
            name="s", execution_order=1, status=Status.FAILED, icon="✗",
            exceptions=[BusinessException("err", screenshot_path="/nonexistent/shot.png")],
        )
        report = TransactionReport(
            transaction_id="tx", reference="r", status=Status.FAILED,
            retry_count=0, steps=[step_report],
            generated_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
        )
        notifier = EmailNotifier(_email_config(), _creds())
        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier.send(report)  # must not raise
        mock_smtp.sendmail.assert_called_once()

    def test_missing_record_screenshot_field_skips_attachment(self):
        report = _make_report()
        payload = json.loads(render_json(report))
        payload["steps"] = [
            {
                "name": "s",
                "execution_order": 1,
                "status": "failed",
                "exceptions": [
                    {
                        "type": "SystemException",
                        "message": "err",
                        "retry_number": 0,
                        "action": "",
                        "halts_remaining_steps": False,
                    }
                ],
            }
        ]
        report.record = ReportRecord(1, json.dumps(payload))
        notifier = EmailNotifier(_email_config(), _creds())

        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier.send(report)

        mock_smtp.sendmail.assert_called_once()

    def test_screenshot_attachment_can_be_disabled(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"PNG")

        step_report = StepReport(
            name="s", execution_order=1, status=Status.FAILED, icon="✗",
            exceptions=[BusinessException("err", screenshot_path=str(img))],
        )
        report = TransactionReport(
            transaction_id="tx", reference="r", status=Status.FAILED,
            retry_count=0, steps=[step_report],
            generated_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
        )
        cfg = _email_config()
        cfg["notification"]["email"]["attach_screenshots"] = False
        notifier = EmailNotifier(cfg, _creds())
        with patch("rpacore.notify.smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)
            notifier.send(report)
        _, _, raw_msg = mock_smtp.sendmail.call_args.args
        message = message_from_string(raw_msg)
        assert not any(
            part.get_content_disposition() == "attachment" for part in message.walk()
        )


# ---------------------------------------------------------------------------
# TestWebhookNotifierConfig
# ---------------------------------------------------------------------------

class TestWebhookNotifierConfig:
    def test_valid_config(self):
        n = WebhookNotifier(_webhook_config())
        assert n.url == "https://hooks.example.com/notify"
        assert n.include_transaction is False
        assert n.include_report is False

    def test_missing_url_raises(self):
        with pytest.raises(ValueError) as exc_info:
            WebhookNotifier({"notification": {"webhook": {}}})

        assert str(exc_info.value) == (
            "notification.webhook.url expected non-empty str; got str value=''"
        )

    def test_bad_url_type_raises(self):
        with pytest.raises(TypeError, match="url"):
            WebhookNotifier({"notification": {"webhook": {"url": 42}}})

    def test_non_http_url_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            WebhookNotifier(_webhook_config("file:///tmp/hook"))

        assert str(exc_info.value) == (
            "notification.webhook.url expected http or https URL; "
            "got str value='file:///tmp/hook'"
        )

    def test_url_without_host_rejected(self):
        with pytest.raises(ValueError, match="http or https URL"):
            WebhookNotifier(_webhook_config("https:///hook"))

    def test_url_with_embedded_credentials_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            WebhookNotifier(_webhook_config("https://user:pass@hooks.example.com/path"))

        assert str(exc_info.value) == (
            "notification.webhook.url expected URL without embedded credentials; "
            "got str value='https://<credentials>@hooks.example.com/path'"
        )
        assert "user:pass" not in str(exc_info.value)

    def test_bad_notification_section_type(self):
        with pytest.raises(TypeError, match="notification"):
            WebhookNotifier({"notification": "bad"})

    def test_bad_webhook_section_type(self):
        with pytest.raises(TypeError, match="webhook"):
            WebhookNotifier({"notification": {"webhook": "bad"}})

    def test_default_timeout(self):
        n = WebhookNotifier(_webhook_config())
        assert n.timeout == 30

    def test_custom_timeout(self):
        cfg = _webhook_config()
        cfg["notification"]["webhook"]["timeout"] = 5
        n = WebhookNotifier(cfg)
        assert n.timeout == 5

    def test_include_transaction_enabled(self):
        n = WebhookNotifier(_webhook_config(include_transaction=True))
        assert n.include_transaction is True

    def test_include_transaction_bad_type_rejected(self):
        cfg = _webhook_config()
        cfg["notification"]["webhook"]["include_transaction"] = "yes"
        with pytest.raises(TypeError) as exc_info:
            WebhookNotifier(cfg)

        assert str(exc_info.value) == (
            "notification.webhook.include_transaction expected bool; "
            "got str value='yes'"
        )

    def test_include_report_bad_type_rejected(self):
        cfg = _webhook_config()
        cfg["notification"]["webhook"]["include_report"] = "yes"

        with pytest.raises(TypeError) as exc_info:
            WebhookNotifier(cfg)

        assert str(exc_info.value) == (
            "notification.webhook.include_report expected bool; got str value='yes'"
        )

    def test_timeout_bool_rejected(self):
        cfg = _webhook_config()
        cfg["notification"]["webhook"]["timeout"] = True
        with pytest.raises(TypeError) as exc_info:
            WebhookNotifier(cfg)

        assert str(exc_info.value) == (
            "notification.webhook.timeout expected int; got bool value=True"
        )

    def test_timeout_zero_rejected(self):
        cfg = _webhook_config()
        cfg["notification"]["webhook"]["timeout"] = 0
        with pytest.raises(ValueError, match="timeout"):
            WebhookNotifier(cfg)

    def test_timeout_negative_rejected(self):
        cfg = _webhook_config()
        cfg["notification"]["webhook"]["timeout"] = -1
        with pytest.raises(ValueError, match="timeout"):
            WebhookNotifier(cfg)


# ---------------------------------------------------------------------------
# TestWebhookNotifierSend
# ---------------------------------------------------------------------------

class TestWebhookNotifierSend:
    def _send(
        self,
        report: TransactionReport | None = None,
        *,
        include_transaction: bool = False,
        include_report: bool = False,
    ) -> bytes:
        """Call send() with a mocked urlopen, return the posted body."""
        if report is None:
            report = _make_report()
        notifier = WebhookNotifier(
            _webhook_config(
                include_transaction=include_transaction,
                include_report=include_report,
            )
        )
        posted: list[bytes] = []

        def fake_urlopen(req, timeout=None):
            posted.append(req.data)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = MagicMock(return_value=b"ok")
            return resp

        with patch("rpacore.notify.urllib.request.urlopen", side_effect=fake_urlopen):
            notifier.send(report)

        return posted[0]

    def test_posts_json(self):
        body = self._send()
        payload = json.loads(body)
        assert payload["transaction_id"] == "tx-001"
        assert payload["reference"] == "ref-test"
        assert payload["status"] == "successful"

    def test_json_contains_text_render(self):
        body = self._send()
        payload = json.loads(body)
        assert "ref-test" in payload["text"]

    def test_json_contains_generated_at(self):
        body = self._send()
        payload = json.loads(body)
        assert "2026-04-21" in payload["generated_at"]

    def test_json_preserves_legacy_shape_by_default(self):
        body = self._send()
        payload = json.loads(body)
        assert "transaction" not in payload
        assert "report" not in payload
        assert payload["metadata"] == {"customer": "acme"}
        assert payload["artifacts"][0]["metadata"] == {"invoice_id": 42}

    def test_json_contains_canonical_transaction_record_when_enabled(self):
        body = self._send(include_transaction=True)
        payload = json.loads(body)
        assert payload["transaction"]["transaction_format_version"] == 3
        assert payload["transaction"]["id"] == "tx-001"
        assert payload["transaction"]["reference"] == "ref-test"
        assert payload["transaction"]["metadata"] == {"customer": "acme"}
        assert payload["transaction"]["artifacts"][0]["metadata"] == {"invoice_id": 42}

    def test_empty_transaction_record_is_omitted_when_enabled(self):
        report = _make_report()
        report.transaction_record = {}

        body = self._send(report, include_transaction=True)

        payload = json.loads(body)
        assert "transaction" not in payload

    def test_json_contains_opt_in_report_v3(self):
        body = self._send(_business_subclass_report(), include_report=True)
        payload = json.loads(body)

        assert payload["report"]["report_format_version"] == 3
        assert payload["report"]["transaction"]["id"] == "tx-001"
        entry = payload["report"]["steps"][0]["exceptions"][0]
        assert entry["category"] == "business"
        assert entry["type"] == "DomainRejection"

    def test_incomplete_record_is_disclosed_only_in_opt_in_report(self):
        transaction = Transaction(reference="incomplete-webhook")
        transaction.state = {"runtime": object()}

        body = self._send(
            generate_report(transaction),
            include_transaction=True,
            include_report=True,
        )
        payload = json.loads(body)

        assert "transaction" not in payload
        assert payload["report"]["complete"] is False
        assert payload["report"]["errors"] == [
            {
                "code": "rpacore.report.transaction_serialization_failed",
                "scope": "transaction_record",
            }
        ]

    def test_json_uses_existing_report_record(self):
        report = _make_report()
        report.record = ReportRecord(1, render_json(report))
        report.reference = "mutated-after-record"
        report.metadata["customer"] = "mutated"
        report.artifacts.clear()

        body = self._send(report)
        payload = json.loads(body)

        assert payload["reference"] == "ref-test"
        assert payload["metadata"] == {"customer": "acme"}
        assert payload["artifacts"][0]["name"] == "invoice"

    def test_posts_to_correct_url(self):
        notifier = WebhookNotifier(_webhook_config("https://custom.url/hook"))
        called_urls: list[str] = []

        def fake_urlopen(req, timeout=None):
            called_urls.append(req.full_url)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = MagicMock(return_value=b"")
            return resp

        with patch("rpacore.notify.urllib.request.urlopen", side_effect=fake_urlopen):
            notifier.send(_make_report())

        assert called_urls == ["https://custom.url/hook"]

    def test_content_type_header(self):
        notifier = WebhookNotifier(_webhook_config())
        captured: list = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = MagicMock(return_value=b"")
            return resp

        with patch("rpacore.notify.urllib.request.urlopen", side_effect=fake_urlopen):
            notifier.send(_make_report())

        assert captured[0].get_header("Content-type") == "application/json"

    def test_urlopen_called_with_timeout(self):
        notifier = WebhookNotifier(_webhook_config())
        calls: list[dict] = []

        def fake_urlopen(req, timeout=None):
            calls.append({"timeout": timeout})
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = MagicMock(return_value=b"")
            return resp

        with patch("rpacore.notify.urllib.request.urlopen", side_effect=fake_urlopen):
            notifier.send(_make_report())

        assert calls[0]["timeout"] == 30


# ---------------------------------------------------------------------------
# TestDispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    @pytest.mark.parametrize(
        ("error", "retryable"),
        [
            pytest.param(
                HTTPError("https://example.invalid", code, "synthetic", {}, None),
                code in {408, 429, 500, 502, 503, 504},
                id=f"http-{code}",
            )
            for code in [400, 401, 403, 404, 408, 429, 500, 501, 502, 503, 504, 505]
        ] + [
            (smtplib.SMTPDataError(451, b"temporary"), True),
            (smtplib.SMTPConnectError(421, b"temporary"), True),
            (smtplib.SMTPAuthenticationError(535, b"permanent"), False),
            (smtplib.SMTPDataError(550, b"permanent"), False),
            (smtplib.SMTPResponseException(399, b"unknown"), False),
            (smtplib.SMTPResponseException(400, b"temporary"), True),
            (smtplib.SMTPResponseException(499, b"temporary"), True),
            (smtplib.SMTPResponseException(500, b"permanent"), False),
            (smtplib.SMTPServerDisconnected("disconnected"), True),
            (smtplib.SMTPNotSupportedError("unsupported"), False),
            (smtplib.SMTPRecipientsRefused({"a": (450, b"temporary")}), True),
            (smtplib.SMTPRecipientsRefused({"a": (450, b"temporary"), "b": (550, b"permanent")}), False),
            (smtplib.SMTPRecipientsRefused({}), False),
            (TimeoutError("timeout"), True),
            (ConnectionResetError("reset"), True),
            (ConnectionRefusedError("refused"), True),
            (socket.gaierror(socket.EAI_AGAIN, "temporary DNS"), True),
            (socket.gaierror(socket.EAI_NONAME, "permanent DNS"), False),
            (URLError(TimeoutError("timeout")), True),
            (URLError(ConnectionResetError("reset")), True),
            (URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS")), True),
            (URLError(socket.gaierror(socket.EAI_NONAME, "permanent DNS")), False),
            (URLError("timed out"), False),
            (URLError(ssl.SSLCertVerificationError("untrusted")), False),
            (ssl.SSLCertVerificationError("untrusted"), False),
            (OSError("unknown OS failure"), False),
            (ValueError("invalid configuration"), False),
        ],
    )
    def test_transport_retry_boundary(self, error, retryable, monkeypatch):
        sleep = MagicMock()
        monkeypatch.setattr("time.sleep", sleep)
        notifier = MagicMock(spec=Notifier)
        notifier.send.side_effect = [error, None]
        failed = []

        dispatch([notifier], _make_report(), on_failure=failed.append)

        assert notifier.send.call_count == (2 if retryable else 1)
        assert failed == ([] if retryable else ["MagicMock"])
        assert [call.args for call in sleep.call_args_list] == ([(1,)] if retryable else [])

    @pytest.mark.parametrize("successful_attempt", [1, 2, 3, None])
    def test_attempt_bound_waits_and_structured_terminal_accounting(
        self, successful_attempt, monkeypatch
    ):
        sleep = MagicMock()
        monkeypatch.setattr("time.sleep", sleep)
        notifier = MagicMock(spec=Notifier)
        notifier.send.side_effect = (
            [TimeoutError("synthetic timeout")] * (successful_attempt - 1) + [None]
            if successful_attempt is not None else TimeoutError("synthetic timeout")
        )
        following = MagicMock(spec=Notifier)
        failed = []
        stream = StringIO()
        logger = configure_logger(name="rpacore.test.retry", fmt="json", stream=stream)

        dispatch([notifier, following], _make_report(), logger=logger, on_failure=failed.append)

        attempts = successful_attempt or 3
        assert notifier.send.call_count == attempts
        assert [c.args[0] for c in sleep.call_args_list] == list(range(1, attempts))
        following.send.assert_called_once()
        assert failed == ([] if successful_attempt else ["MagicMock"])
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        retries = [r for r in records if r["event"] == "rpacore.notifier.retry"]
        assert [r["attributes"]["attempt"] for r in retries] == list(range(1, attempts))
        assert [r["attributes"]["wait_seconds"] for r in retries] == list(range(1, attempts))
        assert all(r["attributes"]["max_attempts"] == 3 for r in records)
        assert all(r["severity"] == "warning" and "exception" not in r for r in retries)
        terminal = [r for r in records if r["event"] == "rpacore.notifier.error"]
        assert len(terminal) == (0 if successful_attempt else 1)
        if terminal:
            assert terminal[0]["attributes"]["attempt"] == 3
            assert terminal[0]["exception"]["type"] == "TimeoutError"

    def test_permanent_failure_after_retry_stops_early(self, monkeypatch):
        sleep = MagicMock()
        monkeypatch.setattr("time.sleep", sleep)
        notifier = MagicMock(spec=Notifier)
        notifier.send.side_effect = [TimeoutError(), ValueError("permanent"), None]
        callback = MagicMock(side_effect=RuntimeError("callback failure"))
        following = MagicMock(spec=Notifier)
        log = MagicMock(spec=logging.Logger)

        dispatch([notifier, following], _make_report(), logger=log, on_failure=callback)

        assert notifier.send.call_count == 2
        sleep.assert_called_once_with(1)
        callback.assert_called_once_with("MagicMock")
        following.send.assert_called_once()
        assert log.exception.call_args_list[0].kwargs["extra"]["attempt"] == 2

    def test_ordinary_wait_failure_is_contained(self, monkeypatch):
        sleep = MagicMock(side_effect=OSError("wait failed"))
        monkeypatch.setattr("time.sleep", sleep)
        notifier = MagicMock(spec=Notifier)
        notifier.send.side_effect = TimeoutError()
        following = MagicMock(spec=Notifier)
        callback = MagicMock()

        dispatch([notifier, following], _make_report(), on_failure=callback)

        notifier.send.assert_called_once()
        sleep.assert_called_once_with(1)
        callback.assert_called_once_with("MagicMock")
        following.send.assert_called_once()

    @pytest.mark.parametrize("fatal_type", [MemoryError, KeyboardInterrupt, SystemExit])
    @pytest.mark.parametrize("source", ["later_attempt", "wait", "callback"])
    def test_fatal_signal_propagates(self, fatal_type, source, monkeypatch):
        sleep = MagicMock(side_effect=fatal_type() if source == "wait" else None)
        monkeypatch.setattr("time.sleep", sleep)
        notifier = MagicMock(spec=Notifier)
        notifier.send.side_effect = (
            RuntimeError("permanent") if source == "callback"
            else [TimeoutError(), fatal_type()]
        )
        callback = MagicMock(side_effect=fatal_type() if source == "callback" else None)
        following = MagicMock(spec=Notifier)

        with pytest.raises(fatal_type):
            dispatch([notifier, following], _make_report(), on_failure=callback)

        following.send.assert_not_called()
        assert notifier.send.call_count == (2 if source == "later_attempt" else 1)
        assert callback.call_count == (1 if source == "callback" else 0)

    @pytest.mark.parametrize("backend", ["webhook", "email"])
    def test_real_adapter_recovery_and_direct_send_single_attempt(self, backend, monkeypatch):
        monkeypatch.setattr("time.sleep", MagicMock())
        notifier = WebhookNotifier(_webhook_config()) if backend == "webhook" else EmailNotifier(_email_config(), _creds())
        target = "rpacore.notify.urllib.request.urlopen" if backend == "webhook" else "rpacore.notify.smtplib.SMTP"
        with patch(target) as transport:
            transport.side_effect = [TimeoutError(), MagicMock()]
            callback = MagicMock()
            dispatch([notifier], _make_report(), on_failure=callback)
            assert transport.call_count == 2
            callback.assert_not_called()

            transport.reset_mock(side_effect=True)
            transport.side_effect = TimeoutError()
            with pytest.raises(TimeoutError):
                notifier.send(_make_report())
            transport.assert_called_once()

    @pytest.mark.parametrize("status", [400, 503])
    def test_direct_webhook_http_error_preserves_readable_body(self, status, monkeypatch):
        body = BytesIO(b"synthetic endpoint diagnostic")
        error = HTTPError("https://example.invalid", status, "synthetic", {}, body)
        sleep = MagicMock()
        monkeypatch.setattr("time.sleep", sleep)
        notifier = WebhookNotifier(_webhook_config())

        try:
            with patch("rpacore.notify.urllib.request.urlopen", side_effect=error) as transport:
                with pytest.raises(HTTPError) as caught:
                    notifier.send(_make_report())
            assert caught.value is error
            transport.assert_called_once()
            sleep.assert_not_called()
            assert not body.closed
            assert caught.value.read() == b"synthetic endpoint diagnostic"
        finally:
            error.close()

    @pytest.mark.parametrize("status", [400, 503])
    def test_dispatch_closes_webhook_error_before_retry_or_return(self, status, monkeypatch):
        body = BytesIO(b"synthetic error response")
        error = HTTPError("https://example.invalid", status, "synthetic", {}, body)
        notifier = WebhookNotifier(_webhook_config())

        def wait(_seconds):
            assert body.closed

        monkeypatch.setattr("time.sleep", wait)
        with patch("rpacore.notify.urllib.request.urlopen", side_effect=[error, MagicMock()]) as transport:
            callback = MagicMock()
            dispatch([notifier], _make_report(), on_failure=callback)

        assert body.closed
        assert transport.call_count == (2 if status == 503 else 1)
        assert callback.call_count == int(status == 400)

    @pytest.mark.parametrize("error", [
        smtplib.SMTPDataError(451, b"temporary"),
        smtplib.SMTPRecipientsRefused({"admin@example.com": (450, b"temporary")}),
        smtplib.SMTPServerDisconnected("disconnected"),
    ])
    def test_email_sendmail_transient_failure_recovers(self, error, monkeypatch):
        sleep = MagicMock()
        monkeypatch.setattr("time.sleep", sleep)
        notifier = EmailNotifier(_email_config(), _creds())
        callback = MagicMock()
        with patch("rpacore.notify.smtplib.SMTP") as transport:
            smtp = transport.return_value.__enter__.return_value
            smtp.sendmail.side_effect = [error, {}]

            dispatch([notifier], _make_report(), on_failure=callback)

        assert transport.call_count == 2
        assert smtp.sendmail.call_count == 2
        assert smtp.starttls.call_count == smtp.login.call_count == 2
        sleep.assert_called_once_with(1)
        callback.assert_not_called()

    @pytest.mark.parametrize("backend", ["webhook", "email"])
    def test_real_adapter_terminal_identity_and_first_attempt_log(self, backend, monkeypatch):
        sleep = MagicMock()
        monkeypatch.setattr("time.sleep", sleep)
        failed = []
        stream = StringIO()
        log = configure_logger(name=f"rpacore.test.terminal.{backend}", fmt="json", stream=stream)
        if backend == "webhook":
            notifier = WebhookNotifier(_webhook_config())
            error = HTTPError("https://example.invalid", 400, "permanent", {}, BytesIO())
            with patch("rpacore.notify.urllib.request.urlopen", side_effect=error) as transport:
                dispatch([notifier], _make_report(), logger=log, on_failure=failed.append)
        else:
            notifier = EmailNotifier(_email_config(), _creds())
            with patch("rpacore.notify.smtplib.SMTP") as transport:
                smtp = transport.return_value.__enter__.return_value
                smtp.sendmail.side_effect = smtplib.SMTPDataError(550, b"permanent")
                dispatch([notifier], _make_report(), logger=log, on_failure=failed.append)
            smtp.sendmail.assert_called_once()

        transport.assert_called_once()
        sleep.assert_not_called()
        name = "WebhookNotifier" if backend == "webhook" else "EmailNotifier"
        assert failed == [name]
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert len(records) == 1
        assert records[0]["event"] == "rpacore.notifier.error"
        assert records[0]["attributes"] == {"notifier": name, "attempt": 1, "max_attempts": 3}

    def test_all_notifiers_called(self):
        n1, n2 = MagicMock(spec=Notifier), MagicMock(spec=Notifier)
        report = _make_report()
        dispatch([n1, n2], report)
        n1.send.assert_called_once()
        n2.send.assert_called_once()
        first_report = n1.send.call_args.args[0]
        second_report = n2.send.call_args.args[0]
        assert first_report == report
        assert second_report == report
        assert first_report is not report
        assert second_report is not report
        assert first_report is not second_report

    def test_dispatch_bodies_label_business_subclass_as_biz(self):
        report = _business_subclass_report()
        posted: list[bytes] = []

        def fake_urlopen(req, timeout=None):
            posted.append(req.data)
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = MagicMock(return_value=b"ok")
            return resp

        with patch("rpacore.notify.smtplib.SMTP") as smtp_cls, patch(
            "rpacore.notify.urllib.request.urlopen", side_effect=fake_urlopen,
        ):
            dispatch([
                EmailNotifier(_email_config(), _creds()),
                WebhookNotifier(_webhook_config()),
            ], report)

        smtp = smtp_cls.return_value.__enter__.return_value
        message = message_from_string(smtp.sendmail.call_args.args[2])
        html_part = next(
            part for part in message.walk() if part.get_content_type() == "text/html"
        )
        text_part = next(
            part for part in message.walk() if part.get_content_type() == "text/plain"
        )
        text_body = text_part.get_payload(decode=True).decode("utf-8")
        html_body = html_part.get_payload(decode=True).decode("utf-8")
        webhook_text = json.loads(posted[0])["text"]

        assert "[BIZ] retry=0: bad row" in text_body
        assert '<div class="exc biz"><strong>[BIZ]</strong> retry=0: bad row' in html_body
        assert "[BIZ] retry=0: bad row" in webhook_text
        assert "[SYS] retry=0: bad row" not in text_body
        assert "[SYS] retry=0: bad row" not in html_body
        assert "[SYS] retry=0: bad row" not in webhook_text

    def test_notifier_exception_swallowed(self):
        bad = MagicMock(spec=Notifier)
        bad.send.side_effect = RuntimeError("boom")
        good = MagicMock(spec=Notifier)
        report = _make_report()
        dispatch([bad, good], report)  # must not raise
        good.send.assert_called_once()
        assert good.send.call_args.args[0] == report
        assert good.send.call_args.args[0] is not report

    def test_notifier_mutation_cannot_reach_retry_later_views_or_source_report(self, monkeypatch):
        monkeypatch.setattr("time.sleep", MagicMock())
        report = _make_report()
        report.metadata = {"nested": {"values": ["original"]}}
        report.artifacts[0].metadata = {
            "nested": {"values": ["original"]}
        }
        report.steps = [
            StepReport(
                name="validate",
                execution_order=1,
                status=Status.FAILED,
                icon="✗",
                exceptions=[BusinessException("invalid", action="original")],
            )
        ]
        observed: list[TransactionReport] = []
        attempts: list[TransactionReport] = []

        class _MutatingNotifier:
            def send(self, view: TransactionReport) -> None:
                assert view.metadata == {"nested": {"values": ["original"]}}
                assert view.artifacts[0].metadata == {"nested": {"values": ["original"]}}
                assert view.steps[0].exceptions[0].action == "original"
                assert view.transaction_record["metadata"] == {"customer": "acme"}
                attempts.append(view)
                view.metadata["nested"]["values"].append("mutated")
                view.artifacts[0].metadata["nested"]["values"].append("mutated")
                view.steps[0].exceptions[0].action = "mutated"
                view.transaction_record["metadata"]["customer"] = "mutated"
                if len(attempts) == 1:
                    raise TimeoutError("retry after mutation")

        class _CapturingNotifier:
            def send(self, view: TransactionReport) -> None:
                observed.append(view)

        dispatch([_MutatingNotifier(), _CapturingNotifier()], report)

        assert len(attempts) == 2
        assert attempts[0] is not attempts[1]
        assert report.metadata == {"nested": {"values": ["original"]}}
        assert report.artifacts[0].metadata == {
            "nested": {"values": ["original"]}
        }
        assert report.steps[0].exceptions[0].action == "original"
        assert report.transaction_record["metadata"] == {"customer": "acme"}
        assert observed[0].metadata == {"nested": {"values": ["original"]}}
        assert observed[0].artifacts[0].metadata == {
            "nested": {"values": ["original"]}
        }
        assert observed[0].steps[0].exceptions[0].action == "original"
        assert observed[0].transaction_record["metadata"] == {
            "customer": "acme"
        }

    def test_empty_notifiers_is_noop(self):
        assert dispatch([], _make_report()) is None

    def test_notifier_exception_calls_failure_callback(self):
        bad = MagicMock(spec=Notifier)
        bad.send.side_effect = RuntimeError("boom")
        failed: list[str] = []

        result = dispatch([bad], _make_report(), on_failure=failed.append)

        assert result is None
        assert failed == ["MagicMock"]

    def test_failure_callback_exception_swallowed(self):
        bad = MagicMock(spec=Notifier)
        bad.send.side_effect = RuntimeError("boom")

        def callback(_notifier: str) -> None:
            raise RuntimeError("callback failed")

        dispatch([bad], _make_report(), on_failure=callback)

    def test_exception_logged(self):
        bad = MagicMock(spec=Notifier)
        bad.send.side_effect = RuntimeError("fail")

        class _Cap(logging.Handler):
            records: list[logging.LogRecord] = []
            def emit(self, r): self.records.append(r)

        cap = _Cap()
        log = logging.getLogger("rpacore.test.dispatch")
        log.addHandler(cap)
        log.setLevel(logging.ERROR)
        log.propagate = False

        dispatch([bad], _make_report(), logger=log)
        assert cap.records, "Expected an error log record"

    def test_swallowed_notifier_error_retains_json_exception_evidence(self):
        bad = MagicMock(spec=Notifier)
        bad.send.side_effect = RuntimeError("notification diagnostic")
        stream = StringIO()
        logger = configure_logger(
            name="rpacore.test.dispatch.exception",
            fmt="json",
            stream=stream,
        )

        dispatch([bad], _make_report(), logger=logger)

        payload = json.loads(stream.getvalue())
        assert payload["event"] == "rpacore.notifier.error"
        assert payload["exception"]["type"] == "RuntimeError"
        assert payload["exception"]["message"] == "notification diagnostic"
        assert "RuntimeError: notification diagnostic" in (
            payload["exception"]["stacktrace"]
        )

    def test_memory_error_propagates(self):
        bad = MagicMock(spec=Notifier)
        bad.send.side_effect = MemoryError("out of memory")

        with pytest.raises(MemoryError, match="out of memory"):
            dispatch([bad], _make_report())


# ---------------------------------------------------------------------------
# TestBuildNotifiers
# ---------------------------------------------------------------------------

class TestBuildNotifiers:
    def test_no_notification_section_returns_empty(self):
        assert build_notifiers({}, _creds()) == []

    def test_email_section_builds_email_notifier(self):
        result = build_notifiers(_email_config(), _creds())
        assert len(result) == 1
        assert isinstance(result[0], EmailNotifier)

    def test_webhook_section_builds_webhook_notifier(self):
        result = build_notifiers(_webhook_config(), _creds())
        assert len(result) == 1
        assert isinstance(result[0], WebhookNotifier)

    def test_both_sections_build_two_notifiers(self):
        cfg = {
            "notification": {
                "email": {
                    "host": "smtp.example.com",
                    "from_addr": "a@b.com",
                    "to_addrs": ["x@b.com"],
                },
                "webhook": {"url": "https://hook.example.com"},
            }
        }
        result = build_notifiers(cfg, _creds())
        assert len(result) == 2
        types = {type(n) for n in result}
        assert EmailNotifier in types
        assert WebhookNotifier in types

    def test_bad_notification_type_returns_empty(self):
        result = build_notifiers({"notification": "bad"}, _creds())
        assert result == []
