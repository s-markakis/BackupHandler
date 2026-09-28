"""
webhook_notify.py - Webhook Notification Delivery

Sends backup event notifications to external services via HTTP webhooks.
Supports JSON payloads with configurable URLs and optional authentication
headers. Compatible with Slack, Discord, Microsoft Teams, and custom endpoints.

Security:
    URLs are validated against an allowlist of schemes (``http``/``https``)
    to prevent SSRF against local services or file:// leakage. Response body
    snippets in logs are truncated to 200 characters. URLs are logged as
    scheme://host only, since Slack/Discord/Teams URLs embed their token.
"""

from __future__ import annotations

from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MAX_BODY_SNIPPET = 200


def _redact_url(url: str) -> str:
    """Reduce a URL to scheme://host for logging; paths often embed secrets."""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname}/…" if parsed.hostname else "<invalid url>"
    except ValueError:
        return "<invalid url>"


def _validate_url(url: str) -> str | None:
    """
    Validate a webhook URL.

    Returns:
        None if the URL is valid, otherwise a human-readable error string.
    """
    if not url or not isinstance(url, str):
        return "webhook URL is empty"
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return f"webhook URL could not be parsed: {exc}"
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"webhook scheme must be http or https, got {parsed.scheme!r}"
    if not parsed.netloc:
        return "webhook URL is missing a hostname"
    return None


def send_webhook(
    logger,
    url: str,
    message: str,
    headers: dict | None = None,
    timeout: int = 30,
) -> bool:
    """
    Send a notification to a webhook URL.

    Parameters:
        logger: Logger instance.
        url: Webhook endpoint URL (must be ``http://`` or ``https://``).
        message: Notification message text.
        headers: Additional HTTP headers (e.g. auth tokens).
        timeout: Request timeout in seconds. Defaults to 30.

    Returns:
        True if the webhook responded with a 2xx status.
    """
    error = _validate_url(url)
    if error is not None:
        logger.error(f"Refusing to send webhook: {error}")
        return False

    try:
        import requests
    except ImportError:
        logger.error("requests is not installed. Install it with: pip install requests")
        return False

    payload = {"text": message, "content": message}
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)

    try:
        response = requests.post(
            url,
            json=payload,
            headers=request_headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.error(f"Failed to send webhook to {_redact_url(url)}: {type(exc).__name__}")
        return False

    if response.ok:
        logger.info(f"Webhook notification sent to {_redact_url(url)}")
        return True

    snippet = response.text[:_MAX_BODY_SNIPPET] if response.text else ""
    logger.error(f"Webhook returned {response.status_code}: {snippet}")
    return False
