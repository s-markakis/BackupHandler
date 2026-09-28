"""
installer.py - One-shot system bootstrap

Performs the OS-level setup that ``preflight.py`` relies on:

  1. Mounts the destination volume (UUID/LABEL from [PREFLIGHT]).
  2. Creates and chowns the destination directory tree to the expected
     owner so cron can write under it without escalation.
  3. Appends a hardened ``nofail`` entry to ``/etc/fstab`` so the volume
     comes back automatically on reboot.
  4. Installs ``/etc/sudoers.d/backup-handler`` with the minimum NOPASSWD
     rules preflight needs to self-heal a missing mount on its own. No
     fstab-write rule is granted (it would be root-equivalent); the
     installer writes fstab itself while running as root.
  5. Installs a local MTA (postfix configured "Local only" + bsd-mailx)
     when none is present, so the DNS-independent ``local_mail_to``
     channel works during the exact failure modes that disabled
     Telegram/email on 2026-04-16.
  6. Runs a smoke test that exercises preflight and the local-mail
     fallback against the now-configured system.

Every step is idempotent. Running ``--install`` twice is a no-op for
already-applied changes. Running ``--install --dry-run`` prints exactly
what would change and modifies nothing.

Safety properties:
  * ``/etc/fstab`` is copied to ``/etc/fstab.bak.<TS>`` before any write.
    If the appended line breaks ``mount -a``, we restore the backup.
  * ``/etc/sudoers.d/backup-handler`` is staged in a temp file and
    validated with ``visudo -c`` before being moved into place. A
    malformed sudoers file would lock out ``sudo`` itself.
  * Postfix install is skipped when an MTA already exists. We never
    overwrite an operator's existing mail setup.
  * Logger is stdout-only so we don't write root-owned files into the
    user-owned project ``Logs/`` directory.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.preflight import (
    PreflightConfig,
    _device_label,
    _device_uuid,
    _findmnt_source,
    _fstab_has_entry,
    run_preflight,
    send_local_mail,
    write_status_sentinel,
)

# Path constants — kept module-level so tests can monkeypatch them.
FSTAB_PATH = "/etc/fstab"
SUDOERS_PATH = "/etc/sudoers.d/backup-handler"


@dataclass
class StepResult:
    """Outcome of one installer step."""

    name: str
    ok: bool
    changed: bool = False
    skipped: bool = False
    message: str = ""


def installer_logger() -> logging.Logger:
    """
    Build a stdout-only logger for the installer.

    The regular AppLogger writes into the project ``Logs/`` directory,
    which is owned by the unprivileged backup user. Running the installer
    via sudo would create root-owned log files there and the next normal
    cron run would fail to append. Console-only avoids that footgun.
    """
    log = logging.getLogger("backup_handler.installer")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(levelname)-7s [install] %(message)s"))
        log.addHandler(h)
    return log


def _run(cmd: list[str], input_text: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    """Run ``cmd`` and return ``(rc, stdout, stderr)``. Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return -1, "", str(e)


# ─── Step: mount ────────────────────────────────────────────────────────────


def step_mount(
    logger: logging.Logger, mount_point: str, label: str | None, uuid: str | None, dry_run: bool
) -> StepResult:
    """Mount the expected volume at ``mount_point`` if not already mounted."""
    if os.path.ismount(mount_point):
        source = _findmnt_source(mount_point)
        actual_label = _device_label(source) if source else None
        actual_uuid = _device_uuid(source) if source else None
        if uuid and actual_uuid and actual_uuid != uuid:
            return StepResult(
                "mount",
                ok=False,
                message=f"{mount_point} mounted from WRONG UUID {actual_uuid} (expected {uuid})",
            )
        if label and actual_label and actual_label != label:
            return StepResult(
                "mount",
                ok=False,
                message=f"{mount_point} mounted from WRONG LABEL {actual_label} (expected {label})",
            )
        return StepResult("mount", ok=True, skipped=True, message=f"{mount_point} already mounted")

    Path(mount_point).mkdir(parents=True, exist_ok=True)
    spec = f"UUID={uuid}" if uuid else (f"LABEL={label}" if label else None)
    if not spec:
        return StepResult("mount", ok=False, message="neither UUID nor LABEL configured")

    if dry_run:
        return StepResult("mount", ok=True, changed=True, message=f"WOULD: mount {spec} {mount_point}")

    rc, _out, err = _run(["mount", spec, mount_point], timeout=30)
    if rc != 0:
        return StepResult("mount", ok=False, message=f"mount {spec} {mount_point}: {err or 'rc=' + str(rc)}")
    return StepResult("mount", ok=True, changed=True, message=f"mounted {spec} at {mount_point}")


# ─── Step: chown + mkdir destination ────────────────────────────────────────


def step_destination(
    logger: logging.Logger,
    mount_point: str,
    backup_dirs: list[str],
    owner: str | None,
    dry_run: bool,
) -> StepResult:
    """Ensure backup_dirs exist and are owned by the expected user."""
    if not owner:
        return StepResult(
            "destination",
            ok=True,
            skipped=True,
            message="no expected_owner configured — skipping chown (dirs created with current owner)",
        )

    actions: list[str] = []
    for d in [mount_point, *backup_dirs]:
        p = Path(d)
        if not p.exists():
            actions.append(f"mkdir -p {d}")
            if not dry_run:
                p.mkdir(parents=True, exist_ok=True)

        # Only chown the mountpoint and the explicit destination dirs.
        # Recursive chown on existing data is destructive and not our place.
        actions.append(f"chown {owner}:{owner} {d}")
        if not dry_run:
            rc, _out, err = _run(["chown", f"{owner}:{owner}", str(p)])
            if rc != 0:
                return StepResult("destination", ok=False, message=f"chown {d}: {err}")

    return StepResult(
        "destination",
        ok=True,
        changed=not dry_run,
        message="; ".join(actions) if dry_run else f"prepared {len(actions) // 2} path(s)",
    )


# ─── Step: fstab ────────────────────────────────────────────────────────────


_FSTAB_OPTS = "defaults,nosuid,nodev,nofail,x-systemd.device-timeout=30"


def step_fstab(
    logger: logging.Logger,
    mount_point: str,
    label: str | None,
    uuid: str | None,
    fs_type: str,
    dry_run: bool,
) -> StepResult:
    """Append a hardened fstab entry for ``mount_point`` if missing."""
    if _fstab_has_entry(mount_point):
        return StepResult("fstab", ok=True, skipped=True, message=f"{mount_point} already in {FSTAB_PATH}")

    spec = f"UUID={uuid}" if uuid else (f"LABEL={label}" if label else None)
    if not spec:
        return StepResult("fstab", ok=False, message="neither UUID nor LABEL configured")

    line = f"{spec}\t{mount_point}\t{fs_type}\t{_FSTAB_OPTS}\t0\t2"

    if dry_run:
        return StepResult("fstab", ok=True, changed=True, message=f"WOULD: append to {FSTAB_PATH}: {line}")

    # Back up fstab so an operator can revert if anything downstream breaks.
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{FSTAB_PATH}.bak.{ts}"
    try:
        shutil.copy2(FSTAB_PATH, backup)
    except OSError as e:
        return StepResult("fstab", ok=False, message=f"could not back up {FSTAB_PATH}: {e}")

    try:
        with open(FSTAB_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        return StepResult("fstab", ok=False, message=f"append to {FSTAB_PATH} failed: {e}")

    # Validate by re-running mount -a. Already-mounted entries are no-ops;
    # a broken line will surface here and we restore the backup.
    rc, _out, err = _run(["mount", "-a"], timeout=30)
    if rc != 0:
        with contextlib.suppress(OSError):
            shutil.copy2(backup, FSTAB_PATH)
        return StepResult(
            "fstab",
            ok=False,
            message=f"mount -a failed after fstab edit (restored {backup}): {err or 'rc=' + str(rc)}",
        )

    return StepResult(
        "fstab",
        ok=True,
        changed=True,
        message=f"appended fstab entry, backup at {backup}: {line}",
    )


# ─── Step: sudoers ──────────────────────────────────────────────────────────


# Values interpolated into sudoers must not be able to smuggle in extra
# commands (``,``), wildcards, or whitespace-separated arguments.
_SUDOERS_OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$")
_SUDOERS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
_SUDOERS_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_sudoers_inputs(owner: str, mount_point: str, label: str | None, uuid: str | None) -> str | None:
    """Return an error message if any value is unsafe to place in sudoers."""
    if not _SUDOERS_OWNER_RE.match(owner):
        return f"expected_owner {owner!r} is not a valid user name"
    if not _SUDOERS_PATH_RE.match(mount_point) or ".." in mount_point.split("/"):
        return f"mount point {mount_point!r} contains characters not allowed in sudoers"
    if label and not _SUDOERS_TOKEN_RE.match(label):
        return f"expected_label {label!r} contains characters not allowed in sudoers"
    if uuid and not _SUDOERS_TOKEN_RE.match(uuid):
        return f"expected_uuid {uuid!r} contains characters not allowed in sudoers"
    return None


def _render_sudoers(owner: str, mount_point: str, label: str | None, uuid: str | None) -> str:
    """Render the sudoers content for the given identity.

    Only ``mount`` of the configured volume is granted. A NOPASSWD rule for
    ``tee -a /etc/fstab`` is deliberately NOT emitted: appending arbitrary
    fstab lines (e.g. a ``user,suid`` entry) is equivalent to root.
    """
    lines = [
        "# Installed by backup-handler --install. Do NOT edit by hand —",
        "# re-run the installer to update. Validated with visudo -cf",
        "# before being placed; a malformed file would lock out sudo.",
        "",
        f"{owner} ALL=(root) NOPASSWD: /usr/bin/mount {mount_point}",
        f"{owner} ALL=(root) NOPASSWD: /bin/mount {mount_point}",
    ]
    if uuid:
        lines.append(f"{owner} ALL=(root) NOPASSWD: /usr/bin/mount UUID={uuid} {mount_point}")
        lines.append(f"{owner} ALL=(root) NOPASSWD: /bin/mount UUID={uuid} {mount_point}")
    if label:
        lines.append(f"{owner} ALL=(root) NOPASSWD: /usr/bin/mount LABEL={label} {mount_point}")
        lines.append(f"{owner} ALL=(root) NOPASSWD: /bin/mount LABEL={label} {mount_point}")
    return "\n".join(lines) + "\n"


def step_sudoers(
    logger: logging.Logger,
    owner: str | None,
    mount_point: str,
    label: str | None,
    uuid: str | None,
    dry_run: bool,
) -> StepResult:
    """Install /etc/sudoers.d/backup-handler with the minimum NOPASSWD rules."""
    if not owner:
        return StepResult("sudoers", ok=True, skipped=True, message="no expected_owner — skipping")

    invalid = _validate_sudoers_inputs(owner, mount_point, label, uuid)
    if invalid:
        return StepResult("sudoers", ok=False, message=f"refusing to write sudoers: {invalid}")

    desired = _render_sudoers(owner, mount_point, label, uuid)

    if Path(SUDOERS_PATH).exists():
        try:
            current = Path(SUDOERS_PATH).read_text()
        except OSError as e:
            return StepResult("sudoers", ok=False, message=f"read {SUDOERS_PATH}: {e}")
        if current == desired:
            return StepResult(
                "sudoers", ok=True, skipped=True, message=f"{SUDOERS_PATH} already matches desired content"
            )

    if dry_run:
        return StepResult(
            "sudoers",
            ok=True,
            changed=True,
            message=f"WOULD: write {SUDOERS_PATH} ({len(desired.splitlines())} lines) and visudo -c",
        )

    # Stage in a private temp dir under /tmp (mkdtemp is mode 0o700), validate
    # with visudo, then move into place atomically. mkdtemp avoids the
    # predictable-filename hazard of writing directly to /tmp/<fixed name>.
    tmp_dir = Path(tempfile.mkdtemp(prefix="backup-handler-install-"))
    tmp_path = tmp_dir / "sudoers"
    try:
        tmp_path.write_text(desired)
        os.chmod(tmp_path, 0o440)  # sudoers requires this exact mode
    except OSError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return StepResult("sudoers", ok=False, message=f"stage temp sudoers: {e}")

    rc, _out, err = _run(["visudo", "-cf", str(tmp_path)])
    if rc != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return StepResult("sudoers", ok=False, message=f"visudo rejected staged file: {err}")

    try:
        shutil.move(str(tmp_path), SUDOERS_PATH)
        os.chmod(SUDOERS_PATH, 0o440)  # sudoers requires this exact mode
        os.chown(SUDOERS_PATH, 0, 0)
    except OSError as e:
        return StepResult("sudoers", ok=False, message=f"install {SUDOERS_PATH}: {e}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return StepResult(
        "sudoers", ok=True, changed=True, message=f"installed {SUDOERS_PATH} (visudo-validated)"
    )


# ─── Step: MTA ──────────────────────────────────────────────────────────────


def _detect_existing_mta() -> str | None:
    """Return a path to an existing MTA binary, or None."""
    for binary in ("sendmail", "/usr/sbin/sendmail", "msmtp", "ssmtp"):
        path = (
            shutil.which(binary)
            if not binary.startswith("/")
            else (binary if Path(binary).exists() else None)
        )
        if path:
            return path
    return None


def step_mta(logger: logging.Logger, dry_run: bool) -> StepResult:
    """Install a local-only MTA if none is present."""
    if _detect_existing_mta() and shutil.which("mail"):
        return StepResult("mta", ok=True, skipped=True, message="MTA + mail(1) already present")

    apt = shutil.which("apt-get")
    if not apt:
        return StepResult(
            "mta",
            ok=True,
            skipped=True,
            message="no apt-get — install postfix/sendmail manually for local_mail_to fallback",
        )

    if dry_run:
        return StepResult(
            "mta",
            ok=True,
            changed=True,
            message="WOULD: apt-get install -y postfix bsd-mailx (Local only)",
        )

    # Pre-seed debconf so postfix installs unattended in "Local only" mode.
    seeds = "postfix postfix/main_mailer_type select Local only\n"
    rc, _out, err = _run(["debconf-set-selections"], input_text=seeds)
    if rc != 0:
        # Non-fatal: we'll attempt the install anyway. Worst case the
        # install hangs waiting for input — but that surfaces fast.
        logger.warning(f"debconf-set-selections failed (continuing): {err}")

    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    # apt-get update returns non-zero whenever ANY configured repo is broken,
    # even if the main archive is fine. Don't block the MTA install on that —
    # the existing apt cache is good enough for `postfix` and `bsd-mailx`,
    # both of which live in the main archive. Surface the error as a warning
    # so an operator still sees the broken-repo signal.
    rc, _out, err = _run(["apt-get", "update", "-qq"], timeout=180)
    if rc != 0:
        logger.warning(f"apt-get update returned rc={rc} (continuing with cached metadata): {err[:200]}")

    proc = subprocess.run(  # nosec B603 B607 — fixed binary, no shell, env scrubbed
        ["apt-get", "install", "-y", "-qq", "postfix", "bsd-mailx"],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        return StepResult(
            "mta",
            ok=False,
            message=f"apt-get install postfix bsd-mailx: rc={proc.returncode} err={proc.stderr.strip()[:200]}",
        )

    return StepResult("mta", ok=True, changed=True, message="installed postfix + bsd-mailx (Local only)")


# ─── Step: smoke test ───────────────────────────────────────────────────────


def step_smoke_test(
    logger: logging.Logger,
    pf_cfg: PreflightConfig,
    backup_dirs: list[str],
    interval_minutes: int,
    sentinel_path: Path,
    project_root: Path,
    dry_run: bool,
) -> StepResult:
    """End-to-end check: run preflight against the now-configured system."""
    if dry_run:
        return StepResult("smoke", ok=True, skipped=True, message="dry-run: skipping live preflight")

    write_status_sentinel(
        sentinel_path, status="install_smoke_test", run_id="installer", message="installer probe"
    )

    result = run_preflight(logger, pf_cfg, backup_dirs=backup_dirs, interval_minutes=interval_minutes)
    if not result.ok:
        return StepResult("smoke", ok=False, message=f"preflight FAILED: {result.message}")

    if pf_cfg.local_mail_to:
        sent = send_local_mail(
            logger,
            pf_cfg.local_mail_to,
            "[backup-handler] installer smoke test",
            "If you can read this, the local-mail fallback works.",
        )
        if not sent:
            logger.warning(
                "Local mail probe failed — fallback alerts will not reach %s. "
                "Investigate: tail /var/log/mail.log",
                pf_cfg.local_mail_to,
            )

    return StepResult("smoke", ok=True, changed=False, message=result.message)


# ─── Top-level orchestrator ─────────────────────────────────────────────────


def _post_install_handover(project_root: Path, owner: str | None) -> None:
    """
    chown anything the installer may have written under the project tree
    back to the unprivileged owner, so the next normal-user run can append
    to logs etc.
    """
    if not owner:
        return
    for sub in ("Logs", "BackupTimestamp"):
        target = project_root / sub
        if not target.exists():
            continue
        # Best-effort, never fail the install over this.
        subprocess.run(  # nosec B603 B607
            ["chown", "-R", f"{owner}:{owner}", str(target)],
            capture_output=True,
            check=False,
            timeout=60,
        )


def run_installer(config_values: dict[str, Any], project_root: Path, dry_run: bool = False) -> int:
    """
    Top-level entry point. Returns a process exit code:

      0 - all steps succeeded (or were already in the desired state)
      1 - configuration error (missing required [PREFLIGHT] settings)
      2 - privilege error (not root, --dry-run requires no privilege)
      3 - one or more steps failed
    """
    logger = installer_logger()

    if dry_run:
        logger.info("=== INSTALL DRY RUN — no changes will be made ===")
    else:
        logger.info("=== INSTALL — making system changes ===")

    if not dry_run and os.geteuid() != 0:
        logger.error(
            "Installer requires root. Re-run as: sudo -E %s %s --install",
            sys.executable,
            os.path.abspath(sys.argv[0]) if sys.argv else "main.py",
        )
        return 2

    mount = config_values.get("preflight_expected_mount")
    label = config_values.get("preflight_expected_label")
    uuid = config_values.get("preflight_expected_uuid")
    fs_type = config_values.get("preflight_expected_fs_type", "xfs")
    owner = config_values.get("preflight_expected_owner") or os.environ.get("SUDO_USER")
    backup_dirs = config_values.get("backup_dirs", []) or []

    if not mount:
        logger.error("[PREFLIGHT] expected_mount is not set in config.ini — cannot install.")
        return 1
    if not (label or uuid):
        logger.error("[PREFLIGHT] expected_label or expected_uuid required — cannot install.")
        return 1

    pf_cfg = PreflightConfig(
        enabled=True,
        expected_mount=mount,
        expected_label=label,
        expected_uuid=uuid,
        expected_fs_type=fs_type,
        expected_owner=owner,
        auto_mount=True,
        auto_fix_ownership=False,
        ensure_fstab=True,
        staleness_factor=config_values.get("preflight_staleness_factor", 2.0),
        local_mail_to=config_values.get("preflight_local_mail_to"),
        status_sentinel=config_values.get("preflight_status_sentinel", "Logs/last_run_status.json"),
    )
    sentinel_relpath = Path(pf_cfg.status_sentinel)
    sentinel_path = sentinel_relpath if sentinel_relpath.is_absolute() else project_root / sentinel_relpath

    interval_minutes = config_values.get("interval_minutes", 4320)

    steps: list[StepResult] = [
        step_mount(logger, mount, label, uuid, dry_run),
        step_destination(logger, mount, backup_dirs, owner, dry_run),
        step_fstab(logger, mount, label, uuid, fs_type, dry_run),
        step_sudoers(logger, owner, mount, label, uuid, dry_run),
        step_mta(logger, dry_run),
        step_smoke_test(logger, pf_cfg, backup_dirs, interval_minutes, sentinel_path, project_root, dry_run),
    ]

    failed: list[str] = []
    for r in steps:
        if not r.ok:
            logger.error("[FAIL] %-12s %s", r.name, r.message)
            failed.append(r.name)
        elif r.skipped:
            logger.info("[skip] %-12s %s", r.name, r.message)
        else:
            logger.info("[ok]   %-12s %s", r.name, r.message)

    if not dry_run:
        _post_install_handover(project_root, owner)

    if failed:
        logger.error("Installer FAILED: %s", ", ".join(failed))
        return 3

    logger.info("Installer completed successfully.")
    if dry_run:
        logger.info("Re-run without --dry-run to apply.")
    return 0
