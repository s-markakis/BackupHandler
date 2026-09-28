"""
heartbeat.py - Dead-man's-switch heartbeat ping.

On a successful backup run, we GET/POST a heartbeat URL (healthchecks.io,
Dead Man's Snitch, Uptime Kuma, etc). The external service expects a ping
on a schedule; a missed ping triggers their alert. That catches the silent
failures our own notifications cannot report — host off, systemd unit
disabled, network partition before the run started.

URL scheme is restricted to http/https to prevent SSRF and file:// leakage,
mirroring webhook_notify's validator.
"""

from __future__ import annotations

from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _redact_url(url: str) -> str:
    """Reduce a URL to scheme://host for logging; paths often embed secrets."""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname}/…" if parsed.hostname else "<invalid url>"
    except ValueError:
        return "<invalid url>"


def _validate_url(url: str) -> str | None:
    if not url or not isinstance(url, str):
        return "heartbeat URL is empty"
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return f"heartbeat URL could not be parsed: {exc}"
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"heartbeat scheme must be http or https, got {parsed.scheme!r}"
    if not parsed.netloc:
        return "heartbeat URL is missing a hostname"
    return None


def send_heartbeat(logger, url: str, timeout: int = 10) -> bool:
    """
    Ping the heartbeat URL. Returns True on 2xx, False otherwise.

    A failed heartbeat is non-fatal to the backup run itself — the backup
    has already succeeded. The caller should log and continue.
    """
    error = _validate_url(url)
    if error is not None:
        logger.error(f"Refusing to send heartbeat: {error}")
        return False

    try:
        import requests
    except ImportError:
        logger.error("requests is not installed; cannot send heartbeat.")
        return False

    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        logger.error(f"Heartbeat ping to {_redact_url(url)} failed: {type(exc).__name__}")
        return False

    if response.ok:
        logger.info(f"Heartbeat ping delivered to {_redact_url(url)}")
        return True

    logger.error(f"Heartbeat ping to {_redact_url(url)} returned {response.status_code}")
    return False
