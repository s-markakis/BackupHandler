"""
notify.py - Notification fan-out (Telegram, webhook, SMTP, local mail).
"""

from ..email_notify import send_smtp_email
from ..preflight import (
    send_local_mail,
)
from ..webhook_notify import send_webhook


def _critical_alert(
    logger,
    config_values,
    telegram_bot,
    notifications,
    subject: str,
    body: str,
) -> None:
    """
    Emit a high-priority alert through every available channel.

    Used for failures that MUST reach an operator: preflight aborts, stale
    backups, lost destinations. Tries Telegram/SMTP/webhook (best-effort —
    these can fail silently when DNS is broken, which is exactly the
    scenario this guards), then always attempts a local-MTA mail to
    [PREFLIGHT] local_mail_to. Local mail does not need DNS or external
    network and survives the failure modes that disabled our other
    channels on 2026-04-16.
    """
    _notify(logger, telegram_bot, notifications, f"{subject}: {body}", config_values=config_values)
    if config_values:
        local_to = config_values.get("preflight_local_mail_to")
        if local_to:
            send_local_mail(logger, local_to, f"[backup-handler] {subject}", body)


def _notify(logger, telegram_bot, notifications, message, config_values=None):
    """
    Dispatch notifications via all configured channels (Telegram and SMTP).

    Telegram notifications require the ``--notifications`` flag and a valid bot.
    SMTP notifications are sent when ``[SMTP]`` host and recipients are configured
    in ``config_values``, regardless of the ``--notifications`` flag.
    """
    if notifications and telegram_bot:
        try:
            telegram_bot.send_notification(message)
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")

    # Webhook notification
    if config_values:
        webhook_url = config_values.get("webhook_url")
        if webhook_url:
            try:
                headers = {}
                auth_header = config_values.get("webhook_auth_header")
                if auth_header:
                    headers["Authorization"] = auth_header
                send_webhook(logger, webhook_url, message, headers=headers or None)
            except Exception as e:
                logger.error(f"Failed to send webhook notification: {e}")

    # SMTP email notification
    if config_values:
        smtp_host = config_values.get("smtp_host")
        smtp_to = config_values.get("smtp_to", [])
        if smtp_host and smtp_to:
            try:
                send_smtp_email(
                    logger,
                    smtp_host=smtp_host,
                    smtp_port=config_values.get("smtp_port", 587),
                    smtp_user=config_values.get("smtp_user"),
                    smtp_password=config_values.get("smtp_password"),
                    from_addr=config_values.get("smtp_from", config_values.get("smtp_user", "")),
                    to_addrs=smtp_to,
                    subject=f"Backup Handler: {message[:50]}",
                    body=message,
                    use_tls=config_values.get("smtp_tls", True),
                )
            except Exception as e:
                logger.error(f"Failed to send SMTP notification: {e}")
