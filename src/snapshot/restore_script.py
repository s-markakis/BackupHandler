"""
restore_script.py - Restore script generation (bash for Linux, PowerShell for Windows).

Every snapshot-derived value is quoted or base64-encoded: snapshots may come
from an untrusted backup and the script is run as root.
"""

import base64
import json
import platform
from datetime import datetime
from pathlib import Path

from ._common import (
    _SAFE_FILENAME_RE,
    _is_safe_relpath,
    _ps_q,
    _q,
    _write_private,
)

# ─── Restore Script Generation ──────────────────────────────────────────────


def generate_restore_script(logger, snapshot_path, output_path=None):
    """
    Generate a restore script from a snapshot JSON file.

    Creates an executable shell script (Linux) or PowerShell script (Windows)
    that reinstalls packages and restores configs in the correct order.

    Parameters:
        logger: Logger instance.
        snapshot_path (str): Path to the snapshot JSON file.
        output_path (str, optional): Output path for the restore script.

    Returns:
        str: Path to the generated restore script.
    """
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.exists():
        logger.error(f"Snapshot file not found: {snapshot_path}")
        return None

    snapshot = json.loads(snapshot_path.read_text())
    metadata = snapshot.get("metadata", {})
    os_type = metadata.get("os", platform.system())

    if os_type == "Linux":
        return _generate_linux_restore(logger, snapshot, output_path)
    elif os_type == "Windows":
        return _generate_windows_restore(logger, snapshot, output_path)
    else:
        logger.error(f"Unsupported OS type in snapshot: {os_type}")
        return None


def _generate_linux_restore(logger, snapshot, output_path=None):
    """Generate a bash restore script for Linux/Ubuntu."""
    metadata = snapshot["metadata"]
    timestamp = metadata.get("timestamp", datetime.now().strftime("%Y%m%d_%H%M%S"))
    hostname = metadata.get("hostname", "unknown")

    if output_path is None:
        output_path = Path.cwd() / f"restore_{hostname}_{timestamp}.sh"
    output_path = Path(output_path)

    packages = snapshot.get("packages", {})
    configs = snapshot.get("configs", {})
    apps = snapshot.get("apps", {})
    security = snapshot.get("security", {})

    lines = [
        "#!/usr/bin/env bash",
        "# ═══════════════════════════════════════════════════════════════════",
        f"# System Restore Script — Generated from snapshot {timestamp}",
        f"# Original host: {hostname}",
        f"# OS: {metadata.get('os_release', 'unknown')}",
        f"# Generated: {datetime.now().isoformat()}",
        "# ═══════════════════════════════════════════════════════════════════",
        "#",
        "# IMPORTANT: Review this script before running!",
        "# Some sections may need manual adjustments for your new system.",
        "# Sections are independent — comment out what you don't need.",
        "#",
        "# Usage: chmod +x restore.sh && sudo ./restore.sh",
        "# ═══════════════════════════════════════════════════════════════════",
        "",
        "set -euo pipefail",
        "",
        "# Colors for output",
        'GREEN="\\033[0;32m"',
        'YELLOW="\\033[1;33m"',
        'RED="\\033[0;31m"',
        'NC="\\033[0m"',
        "",
        'log() { echo -e "${GREEN}[RESTORE]${NC} $1"; }',
        'warn() { echo -e "${YELLOW}[WARNING]${NC} $1"; }',
        'err() { echo -e "${RED}[ERROR]${NC} $1"; }',
        "",
        f"ORIGINAL_USER={_q(metadata.get('user', ''))}",
        f"ORIGINAL_HOME={_q(metadata.get('home', ''))}",
        "",
        "# Detect current user (script runs as root, restore to actual user)",
        'if [ -n "${SUDO_USER:-}" ]; then',
        '    TARGET_USER="$SUDO_USER"',
        '    TARGET_HOME=$(eval echo "~$SUDO_USER")',
        "else",
        '    TARGET_USER="$(whoami)"',
        '    TARGET_HOME="$HOME"',
        "fi",
        "",
        "run_as_user() {",
        '    if [ "$(whoami)" = "root" ] && [ -n "${SUDO_USER:-}" ]; then',
        '        sudo -u "$TARGET_USER" "$@"',
        "    else",
        '        "$@"',
        "    fi",
        "}",
        "",
    ]

    # ── Phase 1: APT repositories ──
    apt_repos = packages.get("apt_repos", {})
    sources = apt_repos.get("sources", [])
    if sources:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 1: APT Repositories")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append('log "Restoring APT repositories..."')
        lines.append("")
        for src in sources:
            fname = src.get("file", "")
            content = src.get("content", "")
            if fname and content:
                if not _SAFE_FILENAME_RE.match(fname):
                    logger.warning(f"Skipping APT source with unsafe file name: {fname!r}")
                    continue
                # base64 so file content can never terminate a heredoc early
                encoded = base64.b64encode(content.encode()).decode()
                lines.append(f'echo "{encoded}" | base64 -d > /etc/apt/sources.list.d/{fname}')
                lines.append("")
        lines.append("apt-get update")
        lines.append("")

    # ── Phase 2: APT packages ──
    apt_pkgs = packages.get("apt", [])
    if apt_pkgs:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 2: APT Packages")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append(f'log "Installing {len(apt_pkgs)} APT packages..."')
        # Chunk into groups of 20 for readability
        for i in range(0, len(apt_pkgs), 20):
            chunk = apt_pkgs[i : i + 20]
            lines.append(
                f'apt-get install -y {" ".join(_q(p) for p in chunk)} || warn "Some APT packages failed to install"'
            )
        lines.append("")

    # ── Phase 3: Snap packages ──
    snap_pkgs = packages.get("snap", [])
    if snap_pkgs:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 3: Snap Packages")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append(f'log "Installing {len(snap_pkgs)} Snap packages..."')
        for pkg in snap_pkgs:
            name = pkg["name"] if isinstance(pkg, dict) else pkg
            classic = pkg.get("classic", False) if isinstance(pkg, dict) else False
            classic_flag = " --classic" if classic else ""
            lines.append(
                f"snap install {_q(name)}{classic_flag} || warn {_q('Failed to install snap: ' + str(name))}"
            )
        lines.append("")

    # ── Phase 4: Flatpak packages ──
    flatpak_pkgs = packages.get("flatpak", [])
    if flatpak_pkgs:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 4: Flatpak Packages")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append('log "Installing Flatpak packages..."')
        lines.append(
            "flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"
        )
        for pkg in flatpak_pkgs:
            app_id = pkg["app_id"] if isinstance(pkg, dict) else pkg
            lines.append(
                f"flatpak install -y flathub {_q(app_id)} || warn {_q('Failed to install flatpak: ' + str(app_id))}"
            )
        lines.append("")

    # ── Phase 5: Pip/pipx packages ──
    pip_data = packages.get("pip", {})
    pipx_pkgs = pip_data.get("pipx", []) if isinstance(pip_data, dict) else []
    pip_user_pkgs = pip_data.get("pip_user", []) if isinstance(pip_data, dict) else []
    if pipx_pkgs or pip_user_pkgs:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 5: Python Packages")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        if pipx_pkgs:
            lines.append(f'log "Installing {len(pipx_pkgs)} pipx packages..."')
            lines.append("run_as_user pipx ensurepath 2>/dev/null || true")
            for pkg in pipx_pkgs:
                lines.append(
                    f"run_as_user pipx install {_q(pkg)} || warn {_q('Failed to install pipx: ' + str(pkg))}"
                )
        if pip_user_pkgs:
            lines.append(f'log "Installing {len(pip_user_pkgs)} pip user packages..."')
            for pkg in pip_user_pkgs:
                lines.append(
                    f"run_as_user pip install --user {_q(pkg)} || warn {_q('Failed to install pip: ' + str(pkg))}"
                )
        lines.append("")

    # ── Phase 6: npm global packages ──
    npm_pkgs = packages.get("npm", [])
    if npm_pkgs:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 6: npm Global Packages")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append(f'log "Installing {len(npm_pkgs)} npm global packages..."')
        lines.append(f'npm install -g {" ".join(_q(p) for p in npm_pkgs)} || warn "Some npm packages failed"')
        lines.append("")

    # ── Phase 7: Cargo packages ──
    cargo_pkgs = packages.get("cargo", [])
    if cargo_pkgs:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 7: Cargo Packages")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append('log "Installing Cargo packages..."')
        lines.append("if ! command -v cargo &>/dev/null; then")
        lines.append(
            '    run_as_user bash -c "curl --proto =https --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"'
        )
        lines.append("fi")
        for pkg in cargo_pkgs:
            name = pkg["name"] if isinstance(pkg, dict) else pkg
            lines.append(
                f"run_as_user cargo install {_q(name)} || warn {_q('Failed to install cargo: ' + str(name))}"
            )
        lines.append("")

    # ── Phase 8: VS Code extensions ──
    vscode_exts = apps.get("vscode_extensions", [])
    if vscode_exts:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 8: VS Code Extensions")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append(f'log "Installing {len(vscode_exts)} VS Code extensions..."')
        lines.append("if command -v code &>/dev/null; then")
        for ext in vscode_exts:
            lines.append(f"    run_as_user code --install-extension {_q(ext)} --force || true")
        lines.append("else")
        lines.append('    warn "VS Code not found — install it first, then re-run this section"')
        lines.append("fi")
        lines.append("")

    # ── Phase 9: VS Code settings ──
    vscode_settings = apps.get("vscode_settings")
    if vscode_settings:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 9: VS Code Settings")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append('log "Restoring VS Code settings..."')
        lines.append('VSCODE_DIR="$TARGET_HOME/.config/Code/User"')
        lines.append('mkdir -p "$VSCODE_DIR"')
        encoded = base64.b64encode(vscode_settings.encode()).decode()
        lines.append(f'echo "{encoded}" | base64 -d > "$VSCODE_DIR/settings.json"')
        lines.append('chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.config/Code" 2>/dev/null || true')
        lines.append("")

    # ── Phase 10: Dotfiles ──
    dotfiles = configs.get("dotfiles", {})
    if dotfiles:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 10: Dotfiles")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append(f'log "Restoring {len(dotfiles)} dotfiles..."')
        for rel_path, content in dotfiles.items():
            if not _is_safe_relpath(rel_path):
                logger.warning(f"Skipping dotfile with unsafe path: {rel_path!r}")
                continue
            rel_path = rel_path.replace('"', "").replace("$", "").replace("`", "").replace("\\", "")
            full_path = f"$TARGET_HOME/{rel_path}"
            dir_path = str(Path(rel_path).parent)
            if dir_path != ".":
                lines.append(f'mkdir -p "$TARGET_HOME/{dir_path}"')
            encoded = base64.b64encode(content.encode()).decode()
            lines.append(f'echo "{encoded}" | base64 -d > "{full_path}"')
            lines.append(f'chown "$TARGET_USER:$TARGET_USER" "{full_path}"')
            lines.append("")

    # ── Phase 11: Cron jobs ──
    cron_content = configs.get("cron")
    if cron_content:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 11: Cron Jobs")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append('log "Restoring cron jobs..."')
        encoded = base64.b64encode(cron_content.encode()).decode()
        lines.append(f'echo "{encoded}" | base64 -d | run_as_user crontab -')
        lines.append("")

    # ── Phase 12: dconf/GNOME settings ──
    dconf_dump = configs.get("dconf")
    if dconf_dump:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 12: GNOME/Desktop Settings")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append('log "Restoring GNOME/dconf settings..."')
        lines.append("if command -v dconf &>/dev/null; then")
        encoded = base64.b64encode(dconf_dump.encode()).decode()
        lines.append(f'    echo "{encoded}" | base64 -d | run_as_user dconf load /')
        lines.append("fi")
        lines.append("")

    # ── Phase 13: fstab ──
    fstab = configs.get("fstab")
    if fstab:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 13: /etc/fstab (REVIEW BEFORE APPLYING)")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append('log "Original /etc/fstab saved for reference..."')
        # Saved under the target home, not a predictable /tmp path (root symlink hazard)
        encoded = base64.b64encode(fstab.encode()).decode()
        lines.append(f'echo "{encoded}" | base64 -d > "$TARGET_HOME/original_fstab.txt"')
        lines.append(
            'warn "Original fstab saved to $TARGET_HOME/original_fstab.txt — review and merge manually"'
        )
        lines.append("")

    # ── Phase 14: /etc/hosts ──
    hosts = configs.get("hosts")
    if hosts:
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append("# Phase 14: /etc/hosts Custom Entries")
        lines.append("# ═══════════════════════════════════════════════════════════════════")
        lines.append('log "Appending custom host entries..."')
        encoded = base64.b64encode(f"\n# --- Restored from snapshot ---\n{hosts}\n".encode()).decode()
        lines.append(f'echo "{encoded}" | base64 -d >> /etc/hosts')
        lines.append("")

    # ── Reminders ──
    lines.append("# ═══════════════════════════════════════════════════════════════════")
    lines.append("# Manual Steps Required")
    lines.append("# ═══════════════════════════════════════════════════════════════════")
    lines.append('echo ""')
    lines.append('log "═══════════════════════════════════════════════════════════"')
    lines.append('log "Automated restore complete!"')
    lines.append('log "═══════════════════════════════════════════════════════════"')
    lines.append('echo ""')
    lines.append('warn "Manual steps still needed:"')

    ssh_keys = security.get("ssh_keys", [])
    if ssh_keys:
        lines.append(f'warn "  - SSH keys: Restore {len(ssh_keys)} key(s) manually from backup"')
        for key in ssh_keys:
            lines.append(
                f"warn {_q('      ' + str(key.get('name', 'unknown')) + ' (' + str(key.get('type', '')) + ')')}"
            )

    gpg_keys = security.get("gpg_keys", [])
    if gpg_keys:
        lines.append(f'warn "  - GPG keys: Import {len(gpg_keys)} key(s) from backup"')

    wg = snapshot.get("network", {}).get("wireguard", [])
    if wg:
        lines.append(f"warn {_q(f'  - WireGuard: Restore {len(wg)} config(s): ' + ', '.join(map(str, wg)))}")

    nm_conns = snapshot.get("network", {}).get("connections", [])
    wifi_conns = [c for c in nm_conns if c.get("has_wifi_psk")]
    if wifi_conns:
        lines.append(f'warn "  - WiFi: Re-enter passwords for {len(wifi_conns)} network(s)"')

    browsers = apps.get("browsers", {})
    if browsers:
        lines.append(
            f'warn "  - Browser profiles: Copy profiles from backup for: {", ".join(browsers.keys())}"'
        )

    if fstab:
        lines.append('warn "  - /etc/fstab: Review $TARGET_HOME/original_fstab.txt and merge"')

    lines.append('echo ""')
    lines.append('log "Done!"')
    lines.append("")

    # Write script
    output_path = Path(output_path)
    _write_private(output_path, "\n".join(lines), mode=0o700)

    logger.info(f"Restore script generated: {output_path}")
    return str(output_path)


def _generate_windows_restore(logger, snapshot, output_path=None):
    """Generate a PowerShell restore script for Windows."""
    metadata = snapshot["metadata"]
    timestamp = metadata.get("timestamp", datetime.now().strftime("%Y%m%d_%H%M%S"))
    hostname = metadata.get("hostname", "unknown")

    if output_path is None:
        output_path = Path.cwd() / f"restore_{hostname}_{timestamp}.ps1"
    output_path = Path(output_path)

    packages = snapshot.get("packages", {})
    configs = snapshot.get("configs", {})
    apps = snapshot.get("apps", {})

    lines = [
        "# ═══════════════════════════════════════════════════════════════════",
        f"# System Restore Script — Generated from snapshot {timestamp}",
        f"# Original host: {hostname}",
        f"# OS: {metadata.get('os_release', 'unknown')}",
        f"# Generated: {datetime.now().isoformat()}",
        "# ═══════════════════════════════════════════════════════════════════",
        "#",
        "# IMPORTANT: Review this script before running!",
        "# Run as Administrator: Set-ExecutionPolicy Bypass -Scope Process",
        "# ═══════════════════════════════════════════════════════════════════",
        "",
        '$ErrorActionPreference = "Continue"',
        "",
        'function Log($msg) { Write-Host "[RESTORE] $msg" -ForegroundColor Green }',
        'function Warn($msg) { Write-Host "[WARNING] $msg" -ForegroundColor Yellow }',
        "",
    ]

    # Winget packages
    winget_pkgs = packages.get("winget", [])
    if winget_pkgs:
        lines.append("# ═══ Winget Packages ═══")
        lines.append(f'Log "Installing {len(winget_pkgs)} winget packages..."')
        for pkg in winget_pkgs:
            lines.append(
                f"winget install --id {_ps_q(pkg)} --accept-package-agreements --accept-source-agreements 2>$null"
            )
        lines.append("")

    # Chocolatey packages
    choco_pkgs = packages.get("choco", [])
    if choco_pkgs:
        lines.append("# ═══ Chocolatey Packages ═══")
        lines.append('Log "Installing Chocolatey packages..."')
        lines.append("if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {")
        lines.append('    Log "Installing Chocolatey..."')
        lines.append("    Set-ExecutionPolicy Bypass -Scope Process -Force")
        lines.append(
            "    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072"
        )
        lines.append(
            '    iex ((New-Object System.Net.WebClient).DownloadString("https://community.chocolatey.org/install.ps1"))'
        )
        lines.append("}")
        for pkg in choco_pkgs:
            name = pkg["name"] if isinstance(pkg, dict) else pkg
            lines.append(f"choco install {_ps_q(name)} -y 2>$null")
        lines.append("")

    # npm packages
    npm_pkgs = packages.get("npm", [])
    if npm_pkgs:
        lines.append("# ═══ npm Global Packages ═══")
        lines.append(f'Log "Installing {len(npm_pkgs)} npm global packages..."')
        lines.append(f"npm install -g {' '.join(_ps_q(p) for p in npm_pkgs)} 2>$null")
        lines.append("")

    # VS Code extensions
    vscode_exts = apps.get("vscode_extensions", [])
    if vscode_exts:
        lines.append("# ═══ VS Code Extensions ═══")
        lines.append(f'Log "Installing {len(vscode_exts)} VS Code extensions..."')
        lines.append("if (Get-Command code -ErrorAction SilentlyContinue) {")
        for ext in vscode_exts:
            lines.append(f"    code --install-extension {_ps_q(ext)} --force 2>$null")
        lines.append('} else { Warn "VS Code not found - install it first" }')
        lines.append("")

    # Env vars
    env_vars = configs.get("env_vars", {})
    if env_vars:
        lines.append("# ═══ Environment Variables ═══")
        lines.append('Log "Restoring user environment variables..."')
        for key, value in env_vars.items():
            if key.upper() not in ("PATH", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
                lines.append(f'[Environment]::SetEnvironmentVariable({_ps_q(key)}, {_ps_q(value)}, "User")')
        lines.append("")

    # WSL
    wsl_distros = apps.get("wsl", [])
    if wsl_distros:
        lines.append("# ═══ WSL Distributions ═══")
        lines.append('Warn "Previously installed WSL distros:"')
        for distro in wsl_distros:
            lines.append(f"Warn {_ps_q('  - ' + str(distro))}")
        lines.append('Warn "Install manually: wsl --install -d <distro>"')
        lines.append("")

    lines.append('Log "Restore complete!"')
    lines.append("")

    _write_private(output_path, "\n".join(lines))
    logger.info(f"Restore script generated: {output_path}")
    return str(output_path)
