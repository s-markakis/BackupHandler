"""
snapshot.py - System State Snapshot & Restore

Captures a complete snapshot of installed packages, configurations, and system
state across Ubuntu/Linux and Windows. Generates restore scripts that can
rebuild the system after a fresh OS install.

Collectors are organized by category and gracefully skip unavailable tools.
"""

import base64
import contextlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ─── Utility ────────────────────────────────────────────────────────────────


def _run(cmd, timeout=30, shell=False):
    """Run a command and return stdout lines, or empty list on failure.

    shell is opt-in and only used by callers passing a trusted, fixed command
    string (e.g., 'lsblk | awk ...'); never user-controlled input.
    """
    try:
        result = subprocess.run(  # nosec B602
            cmd, capture_output=True, text=True, timeout=timeout, shell=shell
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().splitlines()
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _run_single(cmd, timeout=30, shell=False):
    """Run a command and return stdout as a single string, or None."""
    lines = _run(cmd, timeout=timeout, shell=shell)  # nosec B604
    return "\n".join(lines) if lines else None


def _write_private(path, text, mode=0o600):
    """Write ``text`` to ``path`` readable only by the owner.

    Snapshots and generated restore scripts carry dotfiles, crontabs and
    environment variables, which routinely contain tokens.
    """
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    path.chmod(mode)  # O_CREAT mode is ignored when the file already exists


def _q(value):
    """Shell-quote a value taken from a snapshot for use in the bash restore script."""
    return shlex.quote(str(value))


def _ps_q(value):
    """Quote a value as a PowerShell single-quoted string literal."""
    return "'" + str(value).replace("'", "''") + "'"


_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_relpath(rel_path):
    """True if ``rel_path`` is relative and stays inside its base directory."""
    p = Path(rel_path)
    return bool(rel_path) and not p.is_absolute() and ".." not in p.parts


def _which(name):
    """Check if a command is available."""
    return shutil.which(name) is not None


def _is_linux():
    return platform.system() == "Linux"


def _is_windows():
    return platform.system() == "Windows"


def _home():
    return Path.home()


# ─── Linux Collectors ───────────────────────────────────────────────────────


def _collect_apt_packages(logger):
    """Collect manually installed APT packages (not auto-installed deps)."""
    logger.info("Collecting APT packages...")
    lines = _run(["apt-mark", "showmanual"])
    logger.info(f"  Found {len(lines)} manually installed APT packages")
    return lines


def _collect_apt_repositories(logger):
    """Collect custom APT repositories and PPAs."""
    logger.info("Collecting APT repositories...")
    repos = []

    # Sources list files
    sources_dir = Path("/etc/apt/sources.list.d")
    if sources_dir.exists():
        for f in sorted(sources_dir.iterdir()):
            if f.suffix in (".list", ".sources"):
                try:
                    content = f.read_text().strip()
                    if content:
                        repos.append({"file": f.name, "content": content})
                except PermissionError:
                    pass

    # GPG keys for repos
    keyrings_dir = Path("/etc/apt/keyrings")
    keyring_files = []
    if keyrings_dir.exists():
        for f in sorted(keyrings_dir.iterdir()):
            if f.suffix in (".gpg", ".asc"):
                keyring_files.append(f.name)

    # Also check /usr/share/keyrings for custom ones
    usr_keyrings = Path("/usr/share/keyrings")
    if usr_keyrings.exists():
        for f in sorted(usr_keyrings.iterdir()):
            if f.suffix in (".gpg", ".asc") and not f.name.startswith("ubuntu"):
                keyring_files.append(f"usr-share-keyrings/{f.name}")

    logger.info(f"  Found {len(repos)} repository sources, {len(keyring_files)} keyring files")
    return {"sources": repos, "keyrings": keyring_files}


def _collect_snap_packages(logger):
    """Collect installed Snap packages."""
    if not _which("snap"):
        return []
    logger.info("Collecting Snap packages...")
    lines = _run(["snap", "list"])
    packages = []
    for line in lines[1:]:  # Skip header
        parts = line.split()
        if parts and parts[0] not in (
            "bare",
            "core",
            "core18",
            "core20",
            "core22",
            "core24",
            "snapd",
            "gnome-3-28-1804",
            "gnome-3-38-2004",
            "gnome-42-2204",
            "gnome-46-2404",
            "gtk-common-themes",
            "snap-store",
            "snapd-desktop-integration",
            "mesa-2404",
            "firmware-updater",
        ):
            pkg = {"name": parts[0], "version": parts[1] if len(parts) > 1 else ""}
            if len(parts) > 5 and parts[5]:
                pkg["channel"] = parts[3] if len(parts) > 3 else ""
            if "--classic" in line.lower() or (len(parts) > 6 and "classic" in parts[6]):
                pkg["classic"] = True
            packages.append(pkg)
    logger.info(f"  Found {len(packages)} Snap packages")
    return packages


def _collect_flatpak_packages(logger):
    """Collect installed Flatpak packages."""
    if not _which("flatpak"):
        return []
    logger.info("Collecting Flatpak packages...")
    lines = _run(["flatpak", "list", "--app", "--columns=application,origin"])
    packages = []
    for line in lines:
        parts = line.split("\t")
        if parts:
            pkg = {"app_id": parts[0].strip()}
            if len(parts) > 1:
                pkg["origin"] = parts[1].strip()
            packages.append(pkg)
    logger.info(f"  Found {len(packages)} Flatpak packages")
    return packages


def _collect_pip_packages(logger):
    """Collect pip/pipx user-installed packages."""
    result = {}

    # pipx packages
    if _which("pipx"):
        logger.info("Collecting pipx packages...")
        lines = _run(["pipx", "list", "--short"])
        pipx_pkgs = []
        for line in lines:
            parts = line.split()
            if parts:
                pipx_pkgs.append(parts[0])
        result["pipx"] = pipx_pkgs
        logger.info(f"  Found {len(pipx_pkgs)} pipx packages")

    # pip user packages (--user)
    logger.info("Collecting pip user packages...")
    lines = _run([sys.executable, "-m", "pip", "list", "--user", "--format=freeze"])
    pip_user = []
    for line in lines:
        if "==" in line:
            pip_user.append(line.split("==")[0])
    result["pip_user"] = pip_user
    logger.info(f"  Found {len(pip_user)} pip user packages")

    return result


def _collect_npm_packages(logger):
    """Collect globally installed npm packages."""
    if not _which("npm"):
        return []
    logger.info("Collecting npm global packages...")
    lines = _run(["npm", "list", "-g", "--depth=0", "--json"], timeout=15)
    if not lines:
        return []
    try:
        data = json.loads("\n".join(lines))
        deps = data.get("dependencies", {})
        packages = list(deps.keys())
        logger.info(f"  Found {len(packages)} npm global packages")
        return packages
    except json.JSONDecodeError:
        return []


def _collect_cargo_packages(logger):
    """Collect cargo-installed binaries."""
    if not _which("cargo"):
        return []
    logger.info("Collecting Cargo packages...")
    cargo_bin = _home() / ".cargo" / "bin"
    if not cargo_bin.exists():
        return []
    # Get installed crates from cargo install --list
    lines = _run(["cargo", "install", "--list"], timeout=30)
    packages = []
    for line in lines:
        if not line.startswith(" ") and " v" in line:
            name = line.split(" v")[0].strip()
            version = line.split(" v")[1].strip().rstrip(":")
            packages.append({"name": name, "version": version})
    logger.info(f"  Found {len(packages)} Cargo packages")
    return packages


def _collect_go_packages(logger):
    """Collect go-installed binaries."""
    if not _which("go"):
        return []
    logger.info("Collecting Go binaries...")
    gobin = Path(os.environ.get("GOBIN", _home() / "go" / "bin"))
    if not gobin.exists():
        return []
    binaries = [f.name for f in gobin.iterdir() if f.is_file() and os.access(f, os.X_OK)]
    logger.info(f"  Found {len(binaries)} Go binaries")
    return binaries


def _collect_dotfiles(logger):
    """Collect key dotfiles and config files."""
    logger.info("Collecting dotfiles...")
    home = _home()
    dotfile_paths = [
        ".bashrc",
        ".bash_aliases",
        ".bash_profile",
        ".profile",
        ".zshrc",
        ".zsh_aliases",
        ".zprofile",
        ".gitconfig",
        ".gitignore_global",
        ".ssh/config",
        ".tmux.conf",
        ".vimrc",
        ".nanorc",
        ".config/starship.toml",
    ]

    collected = {}
    for rel_path in dotfile_paths:
        full_path = home / rel_path
        if full_path.exists() and full_path.is_file():
            try:
                content = full_path.read_text(errors="replace")
                collected[rel_path] = content
            except (PermissionError, OSError):
                pass

    logger.info(f"  Found {len(collected)} dotfiles")
    return collected


def _collect_ssh_keys_info(logger):
    """Collect SSH key metadata (NOT private keys — just filenames and types for reference)."""
    logger.info("Collecting SSH key info...")
    ssh_dir = _home() / ".ssh"
    if not ssh_dir.exists():
        return []
    keys = []
    for f in sorted(ssh_dir.iterdir()):
        if f.suffix == ".pub":
            try:
                content = f.read_text().strip()
                parts = content.split()
                keys.append(
                    {
                        "name": f.stem,
                        "type": parts[0] if parts else "unknown",
                        "comment": parts[2] if len(parts) > 2 else "",
                    }
                )
            except (PermissionError, OSError):
                pass
    logger.info(f"  Found {len(keys)} SSH keys (public metadata only)")
    return keys


def _collect_gpg_keys(logger):
    """Collect GPG key IDs and metadata."""
    if not _which("gpg"):
        return []
    logger.info("Collecting GPG keys...")
    lines = _run(["gpg", "--list-keys", "--keyid-format", "long", "--with-colons"])
    keys = []
    for line in lines:
        parts = line.split(":")
        if parts[0] == "pub":
            current_key = {
                "keyid": parts[4],
                "creation": parts[5],
                "algo": parts[3],
            }
            keys.append(current_key)
        elif parts[0] == "uid" and keys:
            keys[-1]["uid"] = parts[9]
    logger.info(f"  Found {len(keys)} GPG keys")
    return keys


def _collect_cron_jobs(logger):
    """Collect user crontab entries."""
    logger.info("Collecting cron jobs...")
    lines = _run(["crontab", "-l"])
    if lines and "no crontab" not in lines[0].lower():
        logger.info(f"  Found {len(lines)} crontab lines")
        return "\n".join(lines)
    return None


def _collect_systemd_user_services(logger):
    """Collect enabled user systemd services."""
    logger.info("Collecting systemd user services...")
    lines = _run(
        [
            "systemctl",
            "--user",
            "list-unit-files",
            "--state=enabled",
            "--type=service",
            "--no-legend",
            "--no-pager",
        ]
    )
    services = []
    for line in lines:
        parts = line.split()
        if parts:
            services.append(parts[0])
    logger.info(f"  Found {len(services)} enabled user services")
    return services


def _collect_dconf_settings(logger):
    """Dump GNOME/desktop settings via dconf."""
    if not _which("dconf"):
        return None
    logger.info("Collecting dconf/GNOME settings...")
    dump = _run_single(["dconf", "dump", "/"], timeout=10)
    if dump:
        logger.info(f"  Captured dconf dump ({len(dump)} bytes)")
    return dump


def _collect_vscode_extensions(logger):
    """Collect VS Code extensions list."""
    code_cmd = None
    for cmd in ["code", "code-insiders"]:
        if _which(cmd):
            code_cmd = cmd
            break
    if not code_cmd:
        return []
    logger.info("Collecting VS Code extensions...")
    lines = _run([code_cmd, "--list-extensions"], timeout=15)
    logger.info(f"  Found {len(lines)} VS Code extensions")
    return lines


def _collect_vscode_settings(logger):
    """Collect VS Code user settings.json."""
    logger.info("Collecting VS Code settings...")
    settings_paths = [
        _home() / ".config" / "Code" / "User" / "settings.json",
        _home() / ".config" / "Code - Insiders" / "User" / "settings.json",
    ]
    for p in settings_paths:
        if p.exists():
            try:
                content = p.read_text()
                logger.info(f"  Found VS Code settings at {p}")
                return content
            except (PermissionError, OSError):
                pass
    return None


def _collect_sublime_settings(logger):
    """Collect Sublime Text user settings and installed packages."""
    logger.info("Collecting Sublime Text settings...")
    result = {}

    subl_config = _home() / ".config" / "sublime-text" / "Packages" / "User"
    if not subl_config.exists():
        subl_config = _home() / ".config" / "sublime-text-3" / "Packages" / "User"

    if subl_config.exists():
        for fname in (
            "Preferences.sublime-settings",
            "Package Control.sublime-settings",
            "Default (Linux).sublime-keymap",
        ):
            fpath = subl_config / fname
            if fpath.exists():
                with contextlib.suppress(PermissionError, OSError):
                    result[fname] = fpath.read_text()

    if result:
        logger.info(f"  Found {len(result)} Sublime Text config files")
    return result


def _collect_network_connections(logger):
    """Collect NetworkManager saved connections (WiFi, VPN configs)."""
    logger.info("Collecting network connections...")
    connections = []
    nm_dir = Path("/etc/NetworkManager/system-connections")
    if nm_dir.exists():
        for f in sorted(nm_dir.iterdir()):
            if f.suffix in (".nmconnection", ""):
                try:
                    content = f.read_text()
                    # Redact WiFi passwords — flag for manual re-entry
                    connections.append(
                        {
                            "name": f.stem,
                            "file": f.name,
                            "has_wifi_psk": "psk=" in content,
                            "type": _extract_nm_type(content),
                        }
                    )
                except PermissionError:
                    connections.append(
                        {
                            "name": f.stem,
                            "file": f.name,
                            "readable": False,
                        }
                    )
    logger.info(f"  Found {len(connections)} network connections")
    return connections


def _extract_nm_type(content):
    """Extract connection type from NM config."""
    for line in content.splitlines():
        if line.strip().startswith("type="):
            return line.strip().split("=", 1)[1]
    return "unknown"


def _collect_wireguard_configs(logger):
    """Collect WireGuard config file names (not contents — they contain private keys)."""
    logger.info("Collecting WireGuard configs...")
    wg_dir = Path("/etc/wireguard")
    configs = []
    if wg_dir.exists():
        try:
            for f in sorted(wg_dir.iterdir()):
                if f.suffix == ".conf":
                    configs.append(f.stem)
        except PermissionError:
            pass
    logger.info(f"  Found {len(configs)} WireGuard configs")
    return configs


def _collect_hosts_file(logger):
    """Collect custom /etc/hosts entries."""
    logger.info("Collecting /etc/hosts customizations...")
    hosts_path = Path("/etc/hosts")
    if not hosts_path.exists():
        return None
    try:
        content = hosts_path.read_text()
        # Filter out default entries
        custom_lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if (
                stripped
                and not stripped.startswith("#")
                and stripped
                not in (
                    "127.0.0.1 localhost",
                    "127.0.1.1 " + platform.node(),
                    "::1 localhost ip6-localhost ip6-loopback",
                    "ff02::1 ip6-allnodes",
                    "ff02::2 ip6-allrouters",
                    "::1     ip6-localhost ip6-loopback",
                    "fe00::0 ip6-localnet",
                )
            ):
                custom_lines.append(line)
        if custom_lines:
            logger.info(f"  Found {len(custom_lines)} custom host entries")
            return "\n".join(custom_lines)
    except PermissionError:
        pass
    return None


def _collect_fstab(logger):
    """Collect /etc/fstab for reference."""
    logger.info("Collecting /etc/fstab...")
    fstab = Path("/etc/fstab")
    if fstab.exists():
        try:
            content = fstab.read_text()
            logger.info("  Captured /etc/fstab")
            return content
        except PermissionError:
            pass
    return None


def _collect_fonts(logger):
    """Collect user-installed font names."""
    logger.info("Collecting user fonts...")
    font_dirs = [
        _home() / ".local" / "share" / "fonts",
        _home() / ".fonts",
    ]
    fonts = []
    for d in font_dirs:
        if d.exists():
            for f in d.rglob("*"):
                if f.suffix.lower() in (".ttf", ".otf", ".woff", ".woff2"):
                    fonts.append(str(f.relative_to(d)))
    logger.info(f"  Found {len(fonts)} user fonts")
    return fonts


def _collect_shell_history(logger):
    """Collect shell history files (last 5000 entries)."""
    logger.info("Collecting shell history...")
    history_files = {}
    for name in (".bash_history", ".zsh_history"):
        path = _home() / name
        if path.exists():
            try:
                lines = path.read_text(errors="replace").splitlines()
                # Keep last 5000 entries
                history_files[name] = "\n".join(lines[-5000:])
                logger.info(f"  {name}: {len(lines)} entries (keeping last 5000)")
            except (PermissionError, OSError):
                pass
    return history_files


def _collect_custom_bin(logger):
    """Collect list of custom scripts in common bin directories."""
    logger.info("Collecting custom scripts/binaries...")
    bin_dirs = [
        _home() / "bin",
        _home() / ".local" / "bin",
    ]
    scripts = []
    for d in bin_dirs:
        if d.exists():
            for f in sorted(d.iterdir()):
                if f.is_file() and os.access(f, os.X_OK):
                    scripts.append(
                        {
                            "path": str(f.relative_to(_home())),
                            "size": f.stat().st_size,
                        }
                    )
    logger.info(f"  Found {len(scripts)} custom scripts")
    return scripts


def _collect_docker_info(logger):
    """Collect Docker images and compose file locations."""
    if not _which("docker"):
        return None
    logger.info("Collecting Docker info...")
    result = {}

    # Images
    lines = _run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=10)
    result["images"] = [line for line in lines if line and "<none>" not in line]

    # Find compose files
    compose_files = []
    projects_dir = _home() / "projects"
    if projects_dir.exists():
        for f in projects_dir.rglob("docker-compose*.yml"):
            compose_files.append(str(f.relative_to(_home())))
        for f in projects_dir.rglob("docker-compose*.yaml"):
            compose_files.append(str(f.relative_to(_home())))
        for f in projects_dir.rglob("compose*.yml"):
            compose_files.append(str(f.relative_to(_home())))
    result["compose_files"] = compose_files

    logger.info(f"  Found {len(result['images'])} images, {len(compose_files)} compose files")
    return result


def _collect_browser_profiles(logger):
    """Collect browser profile paths for manual backup reference."""
    logger.info("Collecting browser profile info...")
    browsers = {}

    # Firefox (snap)
    ff_profiles = _home() / "snap" / "firefox" / "common" / ".mozilla" / "firefox"
    if not ff_profiles.exists():
        ff_profiles = _home() / ".mozilla" / "firefox"
    if ff_profiles.exists():
        profiles_ini = ff_profiles / "profiles.ini"
        if profiles_ini.exists():
            try:
                browsers["firefox"] = {
                    "path": str(ff_profiles),
                    "profiles_ini": profiles_ini.read_text(),
                }
            except (PermissionError, OSError):
                browsers["firefox"] = {"path": str(ff_profiles)}

    # Brave
    brave_dir = _home() / ".config" / "BraveSoftware" / "Brave-Browser"
    if not brave_dir.exists():
        brave_dir = _home() / "snap" / "brave" / "current" / ".config" / "BraveSoftware" / "Brave-Browser"
    if brave_dir.exists():
        browsers["brave"] = {"path": str(brave_dir)}

    # Chrome
    chrome_dir = _home() / ".config" / "google-chrome"
    if chrome_dir.exists():
        browsers["chrome"] = {"path": str(chrome_dir)}

    logger.info(f"  Found {len(browsers)} browser profiles")
    return browsers


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
        f'ORIGINAL_USER={_q(metadata.get("user", ""))}',
        f'ORIGINAL_HOME={_q(metadata.get("home", ""))}',
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


# ─── Snapshot Diff ──────────────────────────────────────────────────────────


def diff_snapshots(logger, snapshot_a_path, snapshot_b_path):
    """
    Compare two snapshots and return what was added/removed.

    Parameters:
        logger: Logger instance.
        snapshot_a_path (str): Path to the older snapshot.
        snapshot_b_path (str): Path to the newer snapshot.

    Returns:
        dict: Differences organized by category with 'added' and 'removed' lists.
    """
    a = json.loads(Path(snapshot_a_path).read_text())
    b = json.loads(Path(snapshot_b_path).read_text())

    diff = {}

    # Compare package lists
    for category in ("apt", "snap", "npm", "go", "winget"):
        pkgs_a = set(_extract_names(a.get("packages", {}).get(category, [])))
        pkgs_b = set(_extract_names(b.get("packages", {}).get(category, [])))
        added = sorted(pkgs_b - pkgs_a)
        removed = sorted(pkgs_a - pkgs_b)
        if added or removed:
            diff[category] = {"added": added, "removed": removed}

    # Cargo
    cargo_a = {p["name"] for p in a.get("packages", {}).get("cargo", []) if isinstance(p, dict)}
    cargo_b = {p["name"] for p in b.get("packages", {}).get("cargo", []) if isinstance(p, dict)}
    if cargo_a != cargo_b:
        diff["cargo"] = {"added": sorted(cargo_b - cargo_a), "removed": sorted(cargo_a - cargo_b)}

    # VS Code extensions
    ext_a = set(a.get("apps", {}).get("vscode_extensions", []))
    ext_b = set(b.get("apps", {}).get("vscode_extensions", []))
    if ext_a != ext_b:
        diff["vscode_extensions"] = {"added": sorted(ext_b - ext_a), "removed": sorted(ext_a - ext_b)}

    # Dotfiles
    dot_a = set(a.get("configs", {}).get("dotfiles", {}).keys())
    dot_b = set(b.get("configs", {}).get("dotfiles", {}).keys())
    if dot_a != dot_b:
        diff["dotfiles"] = {"added": sorted(dot_b - dot_a), "removed": sorted(dot_a - dot_b)}

    logger.info(
        f"Snapshot diff: {sum(len(v.get('added', [])) + len(v.get('removed', [])) for v in diff.values())} changes across {len(diff)} categories"
    )
    return diff


def _extract_names(items):
    """Extract package names from a list that may contain strings or dicts."""
    names = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            names.append(item.get("name", item.get("app_id", "")))
    return names
