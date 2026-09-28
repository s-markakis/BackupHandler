"""
create.py - Snapshot capture: runs the collectors and writes the snapshot JSON.
"""

import json
import os
import platform
from datetime import datetime
from pathlib import Path

from ._common import _home, _is_linux, _is_windows, _run_single, _write_private
from .collectors_linux import (
    _collect_apt_packages,
    _collect_apt_repositories,
    _collect_browser_profiles,
    _collect_cargo_packages,
    _collect_cron_jobs,
    _collect_custom_bin,
    _collect_dconf_settings,
    _collect_docker_info,
    _collect_dotfiles,
    _collect_flatpak_packages,
    _collect_fonts,
    _collect_fstab,
    _collect_go_packages,
    _collect_gpg_keys,
    _collect_hosts_file,
    _collect_network_connections,
    _collect_npm_packages,
    _collect_pip_packages,
    _collect_shell_history,
    _collect_snap_packages,
    _collect_ssh_keys_info,
    _collect_sublime_settings,
    _collect_systemd_user_services,
    _collect_vscode_extensions,
    _collect_vscode_settings,
    _collect_wireguard_configs,
)
from .collectors_windows import (
    _collect_choco_packages,
    _collect_windows_env_vars,
    _collect_windows_scheduled_tasks,
    _collect_winget_packages,
    _collect_wsl_distros,
)

# ─── Main Snapshot Function ─────────────────────────────────────────────────


def create_snapshot(logger, output_dir=None, output_file=None):
    """
    Create a full system snapshot.

    Scans the system for installed packages, configs, and state. Saves
    the result as a JSON file.

    Parameters:
        logger: Logger instance.
        output_dir (str, optional): Directory to save snapshot file.
        output_file (str, optional): Exact file path for the snapshot.

    Returns:
        str: Path to the saved snapshot file.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os_type = platform.system()
    hostname = platform.node()

    logger.info(f"Creating system snapshot on {hostname} ({os_type})...")

    snapshot = {
        "metadata": {
            "timestamp": timestamp,
            "datetime": datetime.now().isoformat(),
            "hostname": hostname,
            "os": os_type,
            "os_release": _run_single(["lsb_release", "-d", "-s"]) if _is_linux() else platform.version(),
            "os_version": platform.release(),
            "arch": platform.machine(),
            "python_version": platform.python_version(),
            "user": os.environ.get("USER", os.environ.get("USERNAME", "unknown")),
            "home": str(_home()),
            "snapshot_version": "1.0",
        },
        "packages": {},
        "configs": {},
        "security": {},
        "network": {},
        "apps": {},
        "system": {},
    }

    if _is_linux():
        # Packages
        snapshot["packages"]["apt"] = _collect_apt_packages(logger)
        snapshot["packages"]["apt_repos"] = _collect_apt_repositories(logger)
        snapshot["packages"]["snap"] = _collect_snap_packages(logger)
        snapshot["packages"]["flatpak"] = _collect_flatpak_packages(logger)
        snapshot["packages"]["pip"] = _collect_pip_packages(logger)
        snapshot["packages"]["npm"] = _collect_npm_packages(logger)
        snapshot["packages"]["cargo"] = _collect_cargo_packages(logger)
        snapshot["packages"]["go"] = _collect_go_packages(logger)

        # Configs
        snapshot["configs"]["dotfiles"] = _collect_dotfiles(logger)
        snapshot["configs"]["cron"] = _collect_cron_jobs(logger)
        snapshot["configs"]["systemd_user"] = _collect_systemd_user_services(logger)
        snapshot["configs"]["dconf"] = _collect_dconf_settings(logger)
        snapshot["configs"]["fstab"] = _collect_fstab(logger)
        snapshot["configs"]["hosts"] = _collect_hosts_file(logger)
        snapshot["configs"]["shell_history"] = _collect_shell_history(logger)
        snapshot["configs"]["fonts"] = _collect_fonts(logger)
        snapshot["configs"]["custom_bin"] = _collect_custom_bin(logger)

        # Security
        snapshot["security"]["ssh_keys"] = _collect_ssh_keys_info(logger)
        snapshot["security"]["gpg_keys"] = _collect_gpg_keys(logger)

        # Network
        snapshot["network"]["connections"] = _collect_network_connections(logger)
        snapshot["network"]["wireguard"] = _collect_wireguard_configs(logger)

        # Apps
        snapshot["apps"]["vscode_extensions"] = _collect_vscode_extensions(logger)
        snapshot["apps"]["vscode_settings"] = _collect_vscode_settings(logger)
        snapshot["apps"]["sublime"] = _collect_sublime_settings(logger)
        snapshot["apps"]["browsers"] = _collect_browser_profiles(logger)
        snapshot["apps"]["docker"] = _collect_docker_info(logger)

    elif _is_windows():
        # Packages
        snapshot["packages"]["winget"] = _collect_winget_packages(logger)
        snapshot["packages"]["choco"] = _collect_choco_packages(logger)
        snapshot["packages"]["pip"] = _collect_pip_packages(logger)
        snapshot["packages"]["npm"] = _collect_npm_packages(logger)
        snapshot["packages"]["cargo"] = _collect_cargo_packages(logger)

        # Configs
        snapshot["configs"]["env_vars"] = _collect_windows_env_vars(logger)
        snapshot["configs"]["dotfiles"] = _collect_dotfiles(logger)
        snapshot["configs"]["scheduled_tasks"] = _collect_windows_scheduled_tasks(logger)

        # Security
        snapshot["security"]["ssh_keys"] = _collect_ssh_keys_info(logger)

        # Apps
        snapshot["apps"]["vscode_extensions"] = _collect_vscode_extensions(logger)
        snapshot["apps"]["vscode_settings"] = _collect_vscode_settings(logger)
        snapshot["apps"]["wsl"] = _collect_wsl_distros(logger)
        snapshot["apps"]["docker"] = _collect_docker_info(logger)

    # Determine output path
    if output_file:
        out_path = Path(output_file)
    elif output_dir:
        out_path = Path(output_dir) / f"snapshot_{hostname}_{timestamp}.json"
    else:
        out_path = Path.cwd() / f"snapshot_{hostname}_{timestamp}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(out_path, json.dumps(snapshot, indent=2, ensure_ascii=False))

    logger.info(f"Snapshot saved to {out_path}")
    return str(out_path)
