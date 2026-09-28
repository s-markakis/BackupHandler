"""
tailscale.py - Tailscale VPN Integration for SSH Backups

Manages Tailscale connectivity for SSH backup operations using pre-auth keys.
Handles bringing Tailscale up before SSH connections and tearing it down after,
with status checks to avoid disrupting existing connections.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _run_cmd(cmd, logger=None, timeout=30):
    """Run a shell command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        if logger:
            logger.error(f"Command timed out: {' '.join(cmd)}")
        return -1, "", "timeout"
    except FileNotFoundError:
        if logger:
            logger.error("tailscale binary not found. Is Tailscale installed?")
        return -1, "", "tailscale not found"


def is_tailscale_installed():
    """Check if the tailscale CLI binary is available."""
    return shutil.which("tailscale") is not None


def get_tailscale_status(logger=None):
    """
    Get the current Tailscale connection status.

    Returns a dict with:
        - 'running' (bool): Whether Tailscale daemon is running
        - 'connected' (bool): Whether Tailscale is connected to the network
        - 'ip' (str or None): Tailscale IP address if connected
        - 'hostname' (str or None): Tailscale hostname if connected
        - 'backend_state' (str or None): Backend state string
    """
    status = {
        "running": False,
        "connected": False,
        "ip": None,
        "hostname": None,
        "backend_state": None,
    }

    rc, stdout, _stderr = _run_cmd(["tailscale", "status", "--json"], logger=logger)
    if rc != 0:
        return status

    try:
        data = json.loads(stdout)
        status["running"] = True
        status["backend_state"] = data.get("BackendState", "")
        status["connected"] = status["backend_state"] == "Running"

        self_node = data.get("Self", {})
        ts_ips = self_node.get("TailscaleIPs", [])
        if ts_ips:
            status["ip"] = ts_ips[0]
        status["hostname"] = self_node.get("HostName")
    except (json.JSONDecodeError, KeyError):
        pass

    return status


def tailscale_up(auth_key, logger=None, hostname=None, advertise_tags=None, accept_routes=False, timeout=60):
    """
    Bring Tailscale up using a pre-auth key.

    Parameters:
        auth_key (str): Tailscale pre-authentication key.
        logger: Logger instance.
        hostname (str, optional): Override the machine hostname on the tailnet.
        advertise_tags (str, optional): Comma-separated ACL tags (e.g. "tag:backup").
        accept_routes (bool): Accept advertised routes from other nodes.
        timeout (int): Timeout in seconds for the 'tailscale up' command.

    Returns:
        bool: True if Tailscale connected successfully, False otherwise.
    """
    if not is_tailscale_installed():
        if logger:
            logger.error("Tailscale is not installed. Cannot establish VPN connection.")
        return False

    # Check if already connected
    status = get_tailscale_status(logger)
    if status["connected"]:
        if logger:
            logger.info(f"Tailscale already connected (IP: {status['ip']}). Skipping 'tailscale up'.")
        return True

    # Pass the key via a private file ("file:" prefix) rather than argv, where
    # any local user could read it from `ps` / /proc/<pid>/cmdline.
    key_dir = tempfile.mkdtemp(prefix="backup-handler-ts-")
    key_path = Path(key_dir) / "authkey"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(auth_key)

    cmd = ["sudo", "tailscale", "up", f"--auth-key=file:{key_path}", "--reset"]

    if hostname:
        cmd.extend(["--hostname", hostname])
    if advertise_tags:
        cmd.extend(["--advertise-tags", advertise_tags])
    if accept_routes:
        cmd.append("--accept-routes")

    if logger:
        logger.info("Bringing Tailscale up with pre-auth key...")

    try:
        rc, _stdout, stderr = _run_cmd(cmd, logger=logger, timeout=timeout)
    finally:
        shutil.rmtree(key_dir, ignore_errors=True)

    if rc != 0:
        if logger:
            logger.error(f"Failed to bring Tailscale up: {stderr}")
        return False

    # Verify connection
    status = get_tailscale_status(logger)
    if status["connected"]:
        if logger:
            logger.info(f"Tailscale connected successfully (IP: {status['ip']})")
        return True
    else:
        if logger:
            logger.error(f"Tailscale 'up' completed but not connected. State: {status['backend_state']}")
        return False


def tailscale_down(logger=None):
    """
    Bring Tailscale down (disconnect from the tailnet).

    Returns:
        bool: True if successfully disconnected, False otherwise.
    """
    if not is_tailscale_installed():
        return True

    status = get_tailscale_status(logger)
    if not status["connected"]:
        if logger:
            logger.info("Tailscale already disconnected.")
        return True

    if logger:
        logger.info("Bringing Tailscale down...")

    rc, _stdout, stderr = _run_cmd(["sudo", "tailscale", "down"], logger=logger)

    if rc != 0:
        if logger:
            logger.error(f"Failed to bring Tailscale down: {stderr}")
        return False

    if logger:
        logger.info("Tailscale disconnected.")
    return True


def resolve_tailscale_ip(hostname, logger=None):
    """
    Resolve a Tailscale hostname to its IP address.

    Parameters:
        hostname (str): Tailscale machine name or FQDN.
        logger: Logger instance.

    Returns:
        str or None: The Tailscale IP address, or None if resolution failed.
    """
    rc, stdout, stderr = _run_cmd(["tailscale", "ip", "-4", hostname], logger=logger)
    if rc == 0 and stdout:
        return stdout.split("\n")[0]

    if logger:
        logger.warning(f"Could not resolve Tailscale IP for '{hostname}': {stderr}")
    return None
