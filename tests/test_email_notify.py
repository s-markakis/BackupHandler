"""Tests for SMTP email notifications."""

from __future__ import annotations

from unittest import mock

from src.email_notify import send_smtp_email


class TestSMTPEmail:
    @mock.patch("src.email_notify.smtplib.SMTP")
    def test_send_email_success(self, mock_smtp_class, logger):
        mock_server = mock.MagicMock()
        mock_smtp_class.return_value = mock_server

        result = send_smtp_email(
            logger,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="user@example.com",
            smtp_password="pass",
            from_addr="user@example.com",
            to_addrs=["recipient@example.com"],
            subject="Test",
            body="Test body",
        )

        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user@example.com", "pass")
        mock_server.sendmail.assert_called_once()
        mock_server.quit.assert_called_once()

    @mock.patch("src.email_notify.smtplib.SMTP")
    def test_send_email_no_tls(self, mock_smtp_class, logger):
        mock_server = mock.MagicMock()
        mock_smtp_class.return_value = mock_server

        result = send_smtp_email(
            logger,
            smtp_host="smtp.example.com",
            smtp_port=25,
            smtp_user="user",
            smtp_password="pass",
            from_addr="user@example.com",
            to_addrs=["recipient@example.com"],
            subject="Test",
            body="Test body",
            use_tls=False,
        )

        assert result is True
        mock_server.starttls.assert_not_called()

    def test_send_email_no_recipients(self, logger):
        result = send_smtp_email(
            logger,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="user",
            smtp_password="pass",
            from_addr="user@example.com",
            to_addrs=[],
            subject="Test",
            body="Test body",
        )
        assert result is False

    @mock.patch("src.email_notify.smtplib.SMTP")
    def test_send_email_auth_failure_no_retry(self, mock_smtp_class, logger):
        import smtplib

        mock_server = mock.MagicMock()
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth failed")
        mock_smtp_class.return_value = mock_server

        result = send_smtp_email(
            logger,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="user",
            smtp_password="wrongpass",
            from_addr="user@example.com",
            to_addrs=["recipient@example.com"],
            subject="Test",
            body="Test body",
        )
        assert result is False
        assert mock_smtp_class.call_count == 1

    @mock.patch("src.email_notify.smtplib.SMTP")
    def test_send_email_retries_on_connection_error(self, mock_smtp_class, logger):
        mock_smtp_class.side_effect = [
            ConnectionError("refused"),
            ConnectionError("refused"),
            mock.MagicMock(),
        ]

        result = send_smtp_email(
            logger,
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="user",
            smtp_password="pass",
            from_addr="user@example.com",
            to_addrs=["recipient@example.com"],
            subject="Test",
            body="Test body",
        )
        assert result is True
        assert mock_smtp_class.call_count == 3


def test_html_body_escapes_markup():
    from src.email_notify import _build_html_body

    html = _build_html_body("<b>x</b>", "file <script>alert(1)</script>.txt")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
