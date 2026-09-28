"""
email_notify.py - SMTP Email Notification Delivery

Two delivery paths share this module:

  * ``send_smtp_email`` — notifications configured in ``[SMTP]`` of
    config.ini (STARTTLS, HTML + plain text, retries on transient errors
    but not on authentication failures).
  * ``send_email`` — recipient notifications and archive-password delivery
    configured in ``config/email_config.ini`` (implicit TLS, attachments).

Every TLS connection verifies the server certificate.
"""

import contextlib
import html as html_lib
import os
import smtplib
import ssl
import time
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .paths import CONFIG_DIR


def _build_html_body(subject, body):
    """Build a styled HTML version of the notification email."""
    # Determine status color based on content
    if "failed" in body.lower() or "error" in body.lower():
        status_color = "#dc3545"
    elif "completed" in body.lower() or "success" in body.lower():
        status_color = "#28a745"
    else:
        status_color = "#007bff"

    # Subject/body can contain file paths and error text — escape them.
    subject = html_lib.escape(subject)
    body = html_lib.escape(body)

    return f"""\
<html>
<body style="font-family: Arial, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5;">
  <div style="max-width: 600px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
    <div style="background: {status_color}; color: #fff; padding: 16px 24px;">
      <h2 style="margin: 0; font-size: 18px;">{subject}</h2>
    </div>
    <div style="padding: 24px;">
      <p style="color: #333; line-height: 1.6; white-space: pre-wrap;">{body}</p>
    </div>
    <div style="padding: 12px 24px; background: #f9f9f9; color: #999; font-size: 12px; text-align: center;">
      Sent by Backup Handler
    </div>
  </div>
</body>
</html>"""


def send_smtp_email(
    logger,
    smtp_host,
    smtp_port,
    smtp_user,
    smtp_password,
    from_addr,
    to_addrs,
    subject,
    body,
    use_tls=True,
    html=True,
):
    """
    Send an email notification via SMTP.

    Parameters:
    - logger: Logger instance.
    - smtp_host (str): SMTP server hostname.
    - smtp_port (int): SMTP server port.
    - smtp_user (str): SMTP authentication username.
    - smtp_password (str): SMTP authentication password.
    - from_addr (str): Sender email address.
    - to_addrs (list of str): List of recipient email addresses.
    - subject (str): Email subject line.
    - body (str): Email body text.
    - use_tls (bool): Whether to use STARTTLS (default True).
    - html (bool): Whether to include an HTML version (default True).

    Returns:
    - bool: True if email sent successfully, False otherwise.
    """
    if not to_addrs:
        logger.warning("No recipient email addresses provided. Skipping SMTP notification.")
        return False

    # Build the MIME message with both plain text and HTML
    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    if html:
        html_body = _build_html_body(subject, body)
        msg.attach(MIMEText(html_body, "html"))

    # Retry loop — handles transient network failures (up to 3 attempts)
    retries = 3
    for attempt in range(1, retries + 1):
        server = None
        try:
            # Establish SMTP connection with a 30-second timeout
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            if use_tls:
                # Explicit context: smtplib's default does not verify the
                # server certificate, which would expose credentials to MITM.
                server.starttls(context=ssl.create_default_context())
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.sendmail(from_addr, to_addrs, msg.as_string())
            server.quit()
            logger.info(f"SMTP email sent to {', '.join(to_addrs)}: {subject}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            # Authentication errors are permanent — do not retry
            logger.error(f"SMTP authentication failed: {e}")
            if server:
                with contextlib.suppress(Exception):
                    server.quit()
            return False
        except Exception as e:
            # Close the connection to prevent socket leaks before retrying
            if server:
                with contextlib.suppress(Exception):
                    server.quit()
            logger.warning(f"SMTP send attempt {attempt}/{retries} failed: {e}")
            if attempt == retries:
                logger.error(f"Failed to send SMTP email after {retries} attempts: {e}")
                return False
    return False


# ─── email_config.ini delivery (recipient notifications, attachments) ───────

_EMAIL_CONFIG_PATH = CONFIG_DIR / "email_config.ini"

# Cached email config with TTL (reloads after 5 minutes for long-running scheduled processes)
_cached_email_config = None
_cache_timestamp = 0.0
_CACHE_TTL = 300  # seconds

_SMTP_MAX_RETRIES = 2
_SMTP_RETRY_DELAY = 3  # seconds


def _load_email_config():
    global _cached_email_config, _cache_timestamp
    if _cached_email_config is not None and (time.time() - _cache_timestamp) < _CACHE_TTL:
        return _cached_email_config

    import configparser

    config_path = _EMAIL_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"Email config not found at '{config_path}'. "
            "Copy config/email_config.ini.example to config/email_config.ini and fill in your values."
        )

    config = configparser.ConfigParser()
    config.read(str(config_path))

    if "EMAIL" not in config:
        raise KeyError("Missing [EMAIL] section in config/email_config.ini")

    email_section = config["EMAIL"]
    sender_email = email_section.get("sender_email", "").strip()
    app_password = email_section.get("app_password", "").strip()

    if not sender_email or sender_email == "your_email@gmail.com":
        raise ValueError("Config error: 'sender_email' is not set in config/email_config.ini [EMAIL]")
    _placeholder = "YOUR_APP_PASSWORD"  # the .example value, not a credential
    if not app_password or app_password == _placeholder:
        raise ValueError("Config error: 'app_password' is not set in config/email_config.ini [EMAIL]")

    smtp_host = email_section.get("smtp_host", "smtp.gmail.com").strip()
    smtp_port = email_section.getint("smtp_port", 465)

    _cached_email_config = (sender_email, app_password, smtp_host, smtp_port)
    _cache_timestamp = time.time()
    return _cached_email_config


def send_email(receiver_emails, subject, body, attachment_paths=None, logger=None):
    """
    Send an email (with optional attachments) using ``config/email_config.ini``.

    Configuration errors propagate so callers know email is misconfigured;
    delivery errors are logged.
    """
    try:
        sender_email, app_password, smtp_host, smtp_port = _load_email_config()

        message = MIMEMultipart()
        message["From"] = sender_email
        message["To"] = ", ".join(receiver_emails)
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain"))

        if attachment_paths:
            attach_files_to_email(message, attachment_paths, logger)

        send_via_smtp(
            sender_email,
            app_password,
            receiver_emails,
            message,
            logger,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        )
    except (FileNotFoundError, KeyError, ValueError) as e:
        if logger:
            logger.error(f"Email configuration error: {e}")
        raise
    except Exception as e:
        if logger:
            logger.error(f"Failed to send email: {e}")
        else:
            print(f"Failed to send email: {e}")


def attach_files_to_email(message, attachment_paths, logger=None):
    """Attach files to ``message`` as ``application/octet-stream`` parts."""
    for attachment_path in attachment_paths:
        try:
            with open(attachment_path, "rb") as f:
                attachment = MIMEApplication(f.read(), _subtype="octet-stream")
            attachment.add_header(
                "Content-Disposition", "attachment", filename=os.path.basename(attachment_path)
            )
            message.attach(attachment)
            if logger:
                logger.info(f"Attached file: {attachment_path}")
        except Exception as e:
            if logger:
                logger.error(f"Error attaching file {attachment_path}: {e}")
            else:
                print(f"Error attaching file {attachment_path}: {e}")


def send_via_smtp(
    sender_email,
    app_password,
    receiver_emails,
    message,
    logger=None,
    smtp_host="smtp.gmail.com",
    smtp_port=465,
):
    """Send ``message`` over implicit TLS (certificate verified), retrying transient failures."""
    for attempt in range(1, _SMTP_MAX_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL(
                smtp_host, smtp_port, timeout=30, context=ssl.create_default_context()
            ) as server:
                server.login(sender_email, app_password)
                server.sendmail(sender_email, receiver_emails, message.as_string())
            if logger:
                logger.info(f"Email sent successfully to: {', '.join(receiver_emails)}")
            return
        except smtplib.SMTPAuthenticationError as e:
            if logger:
                logger.error(f"SMTP authentication failed: {e}")
            return
        except (smtplib.SMTPException, OSError) as e:
            if attempt < _SMTP_MAX_RETRIES:
                if logger:
                    logger.warning(
                        f"SMTP error on attempt {attempt}/{_SMTP_MAX_RETRIES}: {e}. "
                        f"Retrying in {_SMTP_RETRY_DELAY}s..."
                    )
                time.sleep(_SMTP_RETRY_DELAY)
            else:
                msg = f"SMTP error after {_SMTP_MAX_RETRIES} attempts: {e}"
                if logger:
                    logger.error(msg)
                else:
                    print(msg)
