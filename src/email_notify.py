"""
email_notify.py - SMTP Email Notification Delivery

Sends plain-text email notifications for backup events (start, completion,
failure) via SMTP with STARTTLS encryption. Includes automatic retry logic
for transient connection failures while avoiding retries on permanent errors
such as authentication failures.
"""

import contextlib
import html as html_lib
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


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
