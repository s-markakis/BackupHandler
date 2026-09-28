"""
collectors_linux.py - Linux / Ubuntu state collectors.
"""

import contextlib
import json
import os
import platform
import sys
from pathlib import Path

from ._common import _home, _run, _run_single, _which

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
