"""
preflight.py - Destination Self-Check and Self-Heal

Runs before any backup operation touches the filesystem. Detects the
class of silent failure that bit us on 2026-04-16: the destination disk
got unmounted after a reboot, /mnt/data fell back to an empty root-owned
directory on the system disk, the script crashed before logging, and
nobody noticed for 16 days.

What this module guarantees by the time it returns ok=True:

  1. The destination mount point is an actual mountpoint
     (``os.path.ismount``), not a fallback directory on the root fs.
  2. The mounted device matches the expected label or UUID — protects
     against the case where the mountpoint is mounted but pointing at
     the wrong volume.
  3. The destination tree is writable by the current user.
  4. (Optional) /etc/fstab contains an entry for the expected device so
     the volume comes back automatically on reboot.

If any check fails and self-heal is enabled, the module attempts to
remount the volume (``sudo -n mount LABEL=...``), append the missing
fstab entry (``sudo -n tee -a /etc/fstab``), or chown the destination.
Self-heal requires sudoers rules — see RUNBOOK.md §4. The installer does
not grant ``tee -a /etc/fstab`` (root-equivalent), so fstab self-heal
only succeeds if an operator adds that rule deliberately; ``--install``
writes the fstab entry up front instead.

Independent alerting:
  * ``write_status_sentinel`` writes ``Logs/last_run_status.json`` so a
    monitoring agent can detect missed/failed runs without depending on
    Telegram/DNS/heartbeat URL.
  * ``send_local_mail`` shells out to ``mail``/``sendmail`` (no DNS
    needed for local delivery to ``root@localhost``).

Staleness check (``check_staleness``) looks at the last-successful
backup timestamp and surfaces an alert if it exceeds ``staleness_factor
x interval`` — so even if the prior run silently never wrote a sentinel,
the next run that does start will scream loudly.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.utils import get_last_backup_time

# ─── Result Types ───────────────────────────────────────────────────────────


@dataclass
class PreflightResult:
    """
    Outcome of a single preflight check or the aggregate run.

    ``ok`` — check passed (possibly after self-heal).
    ``healed`` — check initially failed but was repaired.
    ``fatal`` — check failed and could not be healed; backup must abort.
    ``message`` — human-readable summary, suitable for log + sentinel.
    ``details`` — structured data for downstream consumers.
    """

    ok: bool
    healed: bool = False
    fatal: bool = False
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# ─── Mount / device introspection ───────────────────────────────────────────


def _run_cmd(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run ``cmd`` and return (returncode, stdout, stderr). Never raises."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return -1, "", str(e)


def _findmnt_source(mount_point: str) -> str | None:
    """
    Return the source device backing ``mount_point``, or None if not mounted.

    Uses ``findmnt -n -o SOURCE --target`` which resolves the longest matching
    mount, so ``/mnt/data/backups`` returns the device for ``/mnt/data`` if
    that's the relevant mount.
    """
    rc, out, _err = _run_cmd(["findmnt", "-n", "-o", "SOURCE", "--target", mount_point])
    if rc != 0 or not out:
        return None
    return out.splitlines()[0].strip() or None


def _blkid_lookup(label: str | None, uuid: str | None) -> str | None:
    """Resolve a LABEL/UUID to a device node via blkid. Returns None on miss."""
    if uuid:
        rc, out, _err = _run_cmd(["blkid", "-U", uuid])
        if rc == 0 and out:
            return out.strip()
    if label:
        rc, out, _err = _run_cmd(["blkid", "-L", label])
        if rc == 0 and out:
            return out.strip()
    return None


def _device_label(device: str) -> str | None:
    """Read the LABEL from a device via blkid."""
    rc, out, _err = _run_cmd(["blkid", "-s", "LABEL", "-o", "value", device])
    return out.strip() if rc == 0 and out else None


def _device_uuid(device: str) -> str | None:
    """Read the UUID from a device via blkid."""
    rc, out, _err = _run_cmd(["blkid", "-s", "UUID", "-o", "value", device])
    return out.strip() if rc == 0 and out else None


# ─── Mount + heal ───────────────────────────────────────────────────────────


def _try_mount(logger, mount_point: str, label: str | None, uuid: str | None) -> bool:
    """
    Attempt to mount the expected volume at ``mount_point`` via sudo.

    Tries three strategies in order:
      1. ``sudo -n mount <mount_point>`` (works if fstab has an entry)
      2. ``sudo -n mount UUID=<uuid> <mount_point>``
      3. ``sudo -n mount LABEL=<label> <mount_point>``

    The ``-n`` flag refuses any sudo invocation that would prompt for a
    password — this code path runs unattended from cron, so a prompt
    would hang forever. Operators must configure NOPASSWD rules in
    /etc/sudoers.d/backup-handler (see RUNBOOK).
    """
    attempts: list[list[str]] = [["sudo", "-n", "mount", mount_point]]
    if uuid:
        attempts.append(["sudo", "-n", "mount", f"UUID={uuid}", mount_point])
    if label:
        attempts.append(["sudo", "-n", "mount", f"LABEL={label}", mount_point])

    for cmd in attempts:
        rc, _out, err = _run_cmd(cmd, timeout=30)
        if rc == 0:
            logger.info(f"Preflight: mounted {mount_point} via: {' '.join(cmd)}")
            return True
        logger.warning(f"Preflight: mount attempt failed ({' '.join(cmd)}): {err or 'rc=' + str(rc)}")
    return False


def _fstab_has_entry(mount_point: str) -> bool:
    """Return True if /etc/fstab already references ``mount_point``."""
    try:
        with open("/etc/fstab", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split()
                if len(fields) >= 2 and fields[1] == mount_point:
                    return True
    except OSError:
        return False
    return False


def _ensure_fstab_entry(
    logger,
    mount_point: str,
    label: str | None,
    uuid: str | None,
    fs_type: str,
    options: str = "defaults,nosuid,nodev,nofail,x-systemd.device-timeout=30",
) -> bool:
    """
    Ensure /etc/fstab has an entry for the expected device.

    ``nofail`` is used so a missing/unhealthy disk does not block boot —
    we'd rather have the system come up degraded and let preflight log
    + alert than have ``emergency.target`` lock everyone out.

    Writes via ``sudo -n tee -a`` so the operation requires an explicit
    sudoers rule. Refuses to write if neither label nor uuid is set
    (would leave fstab in an unrecoverable state on the next boot).
    """
    if _fstab_has_entry(mount_point):
        return True
    if not (label or uuid):
        logger.warning(
            "Preflight: cannot ensure fstab entry — no expected_label or expected_uuid configured."
        )
        return False

    spec = f"UUID={uuid}" if uuid else f"LABEL={label}"
    line = f"{spec}\t{mount_point}\t{fs_type}\t{options}\t0\t2\n"

    try:
        proc = subprocess.run(
            ["sudo", "-n", "tee", "-a", "/etc/fstab"],
            input=line,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        rc = proc.returncode
        err = proc.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        rc = -1
        err = str(e)

    if rc == 0:
        logger.info(f"Preflight: appended fstab entry for {mount_point}: {line.strip()}")
        return True
    logger.warning(f"Preflight: failed to append fstab entry: {err or 'rc=' + str(rc)}")
    return False


# ─── Ownership / writability ────────────────────────────────────────────────


def _is_writable(path: Path) -> bool:
    """
    Probe-write to confirm the current user can actually write under ``path``.

    ``os.access`` lies on filesystems with ACLs or capabilities, so we
    create-and-delete a sentinel file. The file name includes the PID so
    parallel preflight checks don't collide.
    """
    if not path.exists():
        return False
    probe = path / f".preflight_probe_{os.getpid()}_{int(time.time())}"
    try:
        probe.touch()
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _try_chown(logger, path: Path, owner: str) -> bool:
    """Attempt ``sudo -n chown -R owner:owner path``."""
    rc, _out, err = _run_cmd(
        ["sudo", "-n", "chown", "-R", f"{owner}:{owner}", str(path)],
        timeout=60,
    )
    if rc == 0:
        logger.info(f"Preflight: chowned {path} to {owner}:{owner}")
        return True
    logger.warning(f"Preflight: chown failed for {path}: {err or 'rc=' + str(rc)}")
    return False


# ─── Public checks ──────────────────────────────────────────────────────────


def verify_destination(
    logger,
    mount_point: str,
    expected_label: str | None = None,
    expected_uuid: str | None = None,
    expected_fs_type: str = "xfs",
    auto_mount: bool = True,
    ensure_fstab: bool = True,
) -> PreflightResult:
    """
    Verify ``mount_point`` is the expected mounted volume; self-heal if not.

    Sequence:
      1. If not ``ismount`` → try to mount it.
      2. After mount, read the backing device and compare LABEL/UUID
         against the expected values. A mismatch is fatal — we will not
         silently write the wrong volume.
      3. If ``ensure_fstab`` and no fstab entry exists, append one so the
         volume comes back on reboot.
    """
    healed = False

    if not os.path.ismount(mount_point):
        msg = f"{mount_point} is not a mountpoint"
        logger.error(f"Preflight: {msg}")
        if not auto_mount:
            return PreflightResult(ok=False, fatal=True, message=msg)
        if not _try_mount(logger, mount_point, expected_label, expected_uuid):
            return PreflightResult(
                ok=False,
                fatal=True,
                message=f"{msg} and auto-mount failed (check sudoers + device presence)",
            )
        healed = True

    # We are now mounted (either originally or just remounted). Verify identity.
    source = _findmnt_source(mount_point)
    if not source:
        return PreflightResult(
            ok=False,
            fatal=True,
            message=f"{mount_point} appears mounted but findmnt returned no source",
        )

    actual_label = _device_label(source)
    actual_uuid = _device_uuid(source)

    if expected_uuid and actual_uuid and expected_uuid != actual_uuid:
        return PreflightResult(
            ok=False,
            fatal=True,
            message=(
                f"{mount_point} is mounted from the WRONG volume: "
                f"expected UUID={expected_uuid}, actual UUID={actual_uuid} (device={source}). "
                f"Refusing to write to avoid corrupting the wrong disk."
            ),
            details={"source": source, "actual_label": actual_label, "actual_uuid": actual_uuid},
        )
    if expected_label and actual_label and expected_label != actual_label:
        return PreflightResult(
            ok=False,
            fatal=True,
            message=(
                f"{mount_point} is mounted from the WRONG volume: "
                f"expected LABEL={expected_label}, actual LABEL={actual_label} (device={source}). "
                f"Refusing to write to avoid corrupting the wrong disk."
            ),
            details={"source": source, "actual_label": actual_label, "actual_uuid": actual_uuid},
        )

    # fstab repair is best-effort — failure is non-fatal because the
    # current run's mount is already verified above. The fstab entry only
    # matters for the NEXT reboot.
    if (
        ensure_fstab
        and not _fstab_has_entry(mount_point)
        and _ensure_fstab_entry(logger, mount_point, expected_label, expected_uuid, expected_fs_type)
    ):
        healed = True

    return PreflightResult(
        ok=True,
        healed=healed,
        message=f"{mount_point} verified (source={source}, label={actual_label}, uuid={actual_uuid})",
        details={"source": source, "actual_label": actual_label, "actual_uuid": actual_uuid},
    )


def ensure_writable(
    logger,
    path: str | os.PathLike[str],
    expected_owner: str | None = None,
    auto_fix_ownership: bool = False,
) -> PreflightResult:
    """
    Ensure ``path`` exists and the current user can write under it.

    If ``auto_fix_ownership`` is True and a write probe fails, attempts
    a recursive chown to ``expected_owner`` (requires sudo NOPASSWD).
    Off by default — chown -R on a backup tree is a heavy operation.
    """
    p = Path(path)
    healed = False

    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
            healed = True
            logger.info(f"Preflight: created destination {p}")
        except OSError as e:
            return PreflightResult(
                ok=False,
                fatal=True,
                message=f"Cannot create destination {p}: {e}",
            )

    if _is_writable(p):
        return PreflightResult(ok=True, healed=healed, message=f"{p} is writable")

    msg = f"{p} is not writable by user {os.environ.get('USER', os.getuid())}"
    if not auto_fix_ownership or not expected_owner:
        return PreflightResult(ok=False, fatal=True, message=msg)

    if not _try_chown(logger, p, expected_owner):
        return PreflightResult(
            ok=False,
            fatal=True,
            message=f"{msg} and chown to {expected_owner} failed",
        )

    if _is_writable(p):
        return PreflightResult(
            ok=True,
            healed=True,
            message=f"{p} writable after chown to {expected_owner}",
        )
    return PreflightResult(
        ok=False,
        fatal=True,
        message=f"{p} still not writable after chown",
    )


def check_staleness(
    logger,
    interval_minutes: int,
    staleness_factor: float = 2.0,
) -> PreflightResult:
    """
    Surface an alert if the last successful backup is older than the threshold.

    Threshold = ``staleness_factor x interval_minutes``. Default factor is
    2.0 — gives one missed run of grace before alarming, so a single
    transient failure does not page anyone, but a sustained outage does.

    Reads from BackupTimestamp/backup_timestamp.json (the same file
    update_last_backup_time writes). If the file does not exist, this
    is treated as a first run — not an alert.
    """
    last = get_last_backup_time()
    if last == 0:
        return PreflightResult(ok=True, message="No prior backup recorded — first run")

    age_seconds = int(time.time()) - last
    threshold = int(interval_minutes * 60 * staleness_factor)

    if age_seconds <= threshold:
        return PreflightResult(
            ok=True,
            message=f"Last backup {age_seconds // 60}m ago (threshold {threshold // 60}m)",
        )

    # Stale — surface as a NON-fatal alert. Backup still proceeds; the
    # caller dispatches the alert via local mail + sentinel so an
    # operator finds out even if Telegram/DNS are down.
    age_h = age_seconds / 3600
    msg = (
        f"STALE: last successful backup {age_h:.1f}h ago "
        f"(>{staleness_factor}x interval of {interval_minutes}m). "
        f"Prior runs may have been failing silently."
    )
    logger.error(f"Preflight: {msg}")
    return PreflightResult(ok=True, healed=False, fatal=False, message=msg, details={"stale": True})


# ─── Status sentinel + local mail ───────────────────────────────────────────


def write_status_sentinel(
    path: str | os.PathLike[str],
    status: str,
    run_id: str = "-",
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Write a JSON status sentinel for external monitoring.

    A monitoring agent (Nagios check, cron job, dashboard) can read this
    file and alert on:
      - file missing or older than the schedule interval -> run skipped
      - status != "success" -> last run failed
      - sentinel ``host`` mismatch -> wrong host wrote it (config drift)

    The write is atomic via tmp+rename so a partial write never confuses
    a concurrent reader. Errors are swallowed: failing to write the
    sentinel must never crash a backup that otherwise succeeded.
    """
    target = Path(path)
    payload = {
        "status": status,
        "run_id": run_id,
        "ts": int(time.time()),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "message": message,
    }
    if extra:
        payload.update(extra)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        os.replace(tmp, target)
    except OSError:
        # Sentinel write must never abort a backup. Caller's logger has
        # already been initialized so a real failure already lands in
        # application.log via other paths.
        return


def read_status_sentinel(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Read and parse the status sentinel; returns None if missing or invalid."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+$")


def _valid_local_recipient(addr: str) -> bool:
    """Allow either ``user`` or ``user@host`` shapes; reject everything else."""
    if not addr:
        return False
    if "@" in addr:
        return bool(_EMAIL_RE.match(addr))
    # bare local user — sendmail/mail will route it via /etc/aliases
    return bool(re.match(r"^[a-zA-Z0-9._\-]+$", addr))


def send_local_mail(logger, to: str, subject: str, body: str) -> bool:
    """
    Deliver a one-shot notification via the local MTA — no DNS required.

    Tries ``/usr/sbin/sendmail -t`` first (universal MTA interface, works
    with postfix, exim, msmtp, ssmtp, opensmtpd), falls back to ``mail``
    from bsd-mailx/mailutils. Both deliver to ``user@localhost`` without
    touching the network, so this works during the exact failure mode
    that broke us in April: Telegram unreachable because DNS was broken.

    Returns False without attempting delivery if ``to`` looks malformed —
    we will not pass arbitrary strings to a setuid binary.
    """
    if not _valid_local_recipient(to):
        logger.warning(f"send_local_mail: refusing malformed recipient {to!r}")
        return False

    # Try sendmail first.
    sendmail = shutil.which("sendmail") or "/usr/sbin/sendmail"
    if Path(sendmail).exists():
        try:
            payload = f"To: {to}\nSubject: {subject}\n\n{body}\n"
            proc = subprocess.run(
                [sendmail, "-t", "-oi"],
                input=payload,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0:
                return True
            logger.warning(f"send_local_mail: sendmail rc={proc.returncode} err={proc.stderr.strip()}")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"send_local_mail: sendmail failed: {e}")

    # Fallback to mail(1).
    mail_bin = shutil.which("mail")
    if mail_bin:
        try:
            proc = subprocess.run(
                [mail_bin, "-s", subject, to],
                input=body,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0:
                return True
            logger.warning(f"send_local_mail: mail rc={proc.returncode} err={proc.stderr.strip()}")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"send_local_mail: mail failed: {e}")

    logger.warning("send_local_mail: no working local MTA (sendmail/mail) found")
    return False


# ─── Top-level orchestrator ─────────────────────────────────────────────────


@dataclass
class PreflightConfig:
    """Resolved [PREFLIGHT] settings from config.ini."""

    enabled: bool = True
    expected_mount: str | None = None
    expected_label: str | None = None
    expected_uuid: str | None = None
    expected_fs_type: str = "xfs"
    expected_owner: str | None = None
    auto_mount: bool = True
    auto_fix_ownership: bool = False
    ensure_fstab: bool = True
    staleness_factor: float = 2.0
    local_mail_to: str | None = None
    status_sentinel: str = "Logs/last_run_status.json"


def run_preflight(
    logger,
    pf: PreflightConfig,
    backup_dirs: list[str],
    interval_minutes: int,
) -> PreflightResult:
    """
    Run all preflight checks for a single backup invocation.

    Returns a single aggregated result. Stops at the first fatal failure.
    On success, returns a result whose ``healed`` flag indicates whether
    self-heal made any change (worth surfacing in the success notification).
    """
    if not pf.enabled:
        return PreflightResult(ok=True, message="Preflight disabled")

    healed = False

    # 1. Destination volume identity + mount.
    if pf.expected_mount:
        r = verify_destination(
            logger,
            pf.expected_mount,
            expected_label=pf.expected_label,
            expected_uuid=pf.expected_uuid,
            expected_fs_type=pf.expected_fs_type,
            auto_mount=pf.auto_mount,
            ensure_fstab=pf.ensure_fstab,
        )
        if not r.ok:
            return r
        healed = healed or r.healed
        logger.info(f"Preflight: {r.message}")

    # 2. Each backup_dir is writable.
    for bdir in backup_dirs:
        r = ensure_writable(
            logger,
            bdir,
            expected_owner=pf.expected_owner,
            auto_fix_ownership=pf.auto_fix_ownership,
        )
        if not r.ok:
            return r
        healed = healed or r.healed

    # 3. Staleness check (non-fatal — surfaces as alert).
    stale_msg = ""
    if interval_minutes > 0:
        s = check_staleness(logger, interval_minutes, pf.staleness_factor)
        if s.details.get("stale"):
            stale_msg = s.message

    summary = "Preflight OK" + (" (self-healed)" if healed else "")
    if stale_msg:
        summary += f" | {stale_msg}"
    return PreflightResult(
        ok=True,
        healed=healed,
        message=summary,
        details={"stale_alert": stale_msg or None},
    )
