"""
collectors_windows.py - Windows state collectors.
"""

import json

from ._common import _is_windows, _run, _which

# ─── Windows Collectors ─────────────────────────────────────────────────────


def _collect_winget_packages(logger):
    """Collect winget-installed packages."""
    if not _which("winget"):
        return []
    logger.info("Collecting winget packages...")
    lines = _run(["winget", "list", "--source", "winget"], timeout=30)
    packages = []
    # Skip header lines
    started = False
    for line in lines:
        if "---" in line:
            started = True
            continue
        if started and line.strip():
            parts = line.split()
            if len(parts) >= 2:
                packages.append(parts[-2] if "." in parts[-2] else parts[0])
    logger.info(f"  Found {len(packages)} winget packages")
    return packages


def _collect_choco_packages(logger):
    """Collect Chocolatey-installed packages."""
    if not _which("choco"):
        return []
    logger.info("Collecting Chocolatey packages...")
    lines = _run(["choco", "list", "--local-only"], timeout=30)
    packages = []
    for line in lines:
        if " " in line and not line.startswith("Chocolatey"):
            parts = line.split()
            if len(parts) >= 2 and not parts[0].endswith(":"):
                packages.append({"name": parts[0], "version": parts[1]})
    logger.info(f"  Found {len(packages)} Chocolatey packages")
    return packages


def _collect_windows_env_vars(logger):
    """Collect user environment variables (Windows)."""
    if not _is_windows():
        return {}
    logger.info("Collecting Windows user environment variables...")
    lines = _run(
        ["powershell", "-Command", '[Environment]::GetEnvironmentVariables("User") | ConvertTo-Json'],
        timeout=10,
    )
    if lines:
        try:
            return json.loads("\n".join(lines))
        except json.JSONDecodeError:
            pass
    return {}


def _collect_wsl_distros(logger):
    """Collect WSL distributions."""
    if not _which("wsl"):
        return []
    logger.info("Collecting WSL distributions...")
    lines = _run(["wsl", "--list", "--verbose"], timeout=10)
    distros = []
    for line in lines[1:]:  # Skip header
        parts = line.strip().split()
        if parts:
            name = parts[0].replace("*", "").strip()
            if name:
                distros.append(name)
    logger.info(f"  Found {len(distros)} WSL distributions")
    return distros


def _collect_windows_scheduled_tasks(logger):
    """Collect user-created scheduled tasks."""
    if not _is_windows():
        return []
    logger.info("Collecting Windows scheduled tasks...")
    lines = _run(
        [
            "powershell",
            "-Command",
            'Get-ScheduledTask | Where-Object {$_.Author -notlike "Microsoft*"} '
            "| Select-Object TaskName, TaskPath, State | ConvertTo-Json",
        ],
        timeout=15,
    )
    if lines:
        try:
            return json.loads("\n".join(lines))
        except json.JSONDecodeError:
            pass
    return []
