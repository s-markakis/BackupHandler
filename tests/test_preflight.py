"""Tests for the preflight self-check + self-heal module."""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

from src import preflight
from src.preflight import (
    PreflightConfig,
    check_staleness,
    ensure_writable,
    read_status_sentinel,
    run_preflight,
    send_local_mail,
    verify_destination,
    write_status_sentinel,
)

# ─── Sentinel I/O ───────────────────────────────────────────────────────────


class TestStatusSentinel:
    def test_write_then_read_roundtrip(self, tmp_dir: Path):
        path = tmp_dir / "status.json"
        write_status_sentinel(
            path,
            status="success",
            run_id="abc123",
            message="all good",
            extra={"modes": ["local", "ssh"]},
        )
        data = read_status_sentinel(path)
        assert data is not None
        assert data["status"] == "success"
        assert data["run_id"] == "abc123"
        assert data["message"] == "all good"
        assert data["modes"] == ["local", "ssh"]
        assert "ts" in data and isinstance(data["ts"], int)
        assert "host" in data

    def test_creates_parent_directory(self, tmp_dir: Path):
        path = tmp_dir / "deep" / "nested" / "status.json"
        write_status_sentinel(path, status="started")
        assert path.exists()

    def test_atomic_write_no_partial_file(self, tmp_dir: Path, monkeypatch):
        path = tmp_dir / "status.json"
        write_status_sentinel(path, status="success")
        # No leftover .tmp file from the rename
        assert not (tmp_dir / "status.json.tmp").exists()

    def test_read_missing_returns_none(self, tmp_dir: Path):
        assert read_status_sentinel(tmp_dir / "nope.json") is None

    def test_read_invalid_json_returns_none(self, tmp_dir: Path):
        path = tmp_dir / "garbage.json"
        path.write_text("{not json")
        assert read_status_sentinel(path) is None

    def test_write_swallows_oserror(self, tmp_dir: Path):
        # Pointing at a path under a non-writable parent should not raise
        path = tmp_dir / "ro" / "status.json"
        (tmp_dir / "ro").mkdir()
        with mock.patch.object(Path, "write_text", side_effect=OSError("read-only")):
            # Must not raise — sentinel write must never abort the backup
            write_status_sentinel(path, status="success")


# ─── ensure_writable ────────────────────────────────────────────────────────


class TestEnsureWritable:
    def test_writable_dir_passes(self, logger, tmp_dir: Path):
        r = ensure_writable(logger, tmp_dir)
        assert r.ok and r.fatal is False

    def test_creates_missing_directory(self, logger, tmp_dir: Path):
        target = tmp_dir / "newdir"
        r = ensure_writable(logger, target)
        assert r.ok and r.healed and target.exists()

    def test_unwritable_returns_fatal_when_no_autofix(self, logger, tmp_dir: Path):
        target = tmp_dir / "ro"
        target.mkdir()
        # Simulate an unwritable directory. chmod 0555 is not enough: root
        # (CI containers, sudo runs) bypasses permission bits.
        with mock.patch.object(preflight, "_is_writable", return_value=False):
            r = ensure_writable(logger, target)
        assert r.ok is False and r.fatal is True


# ─── verify_destination ─────────────────────────────────────────────────────


class TestVerifyDestination:
    def test_not_a_mountpoint_with_no_automount_is_fatal(self, logger, tmp_dir: Path):
        # tmp_dir is on the host's root fs and is NOT a mountpoint.
        r = verify_destination(logger, str(tmp_dir), expected_label="DATA", auto_mount=False)
        assert r.ok is False and r.fatal is True
        assert "not a mountpoint" in r.message

    def test_unmounted_attempts_remount_and_fails_loudly(self, logger, tmp_dir: Path):
        with mock.patch.object(preflight, "_try_mount", return_value=False) as m:
            r = verify_destination(
                logger, str(tmp_dir), expected_label="DATA", auto_mount=True, ensure_fstab=False
            )
        assert r.ok is False and r.fatal is True
        assert m.called
        assert "auto-mount failed" in r.message

    def test_mounted_with_correct_label_passes(self, logger, tmp_dir: Path):
        with (
            mock.patch("os.path.ismount", return_value=True),
            mock.patch.object(preflight, "_findmnt_source", return_value="/dev/sdb1"),
            mock.patch.object(preflight, "_device_label", return_value="DATA"),
            mock.patch.object(preflight, "_device_uuid", return_value="abc-123"),
            mock.patch.object(preflight, "_fstab_has_entry", return_value=True),
        ):
            r = verify_destination(
                logger, str(tmp_dir), expected_label="DATA", auto_mount=True, ensure_fstab=False
            )
        assert r.ok is True
        assert r.details["actual_label"] == "DATA"

    def test_mounted_with_wrong_label_is_fatal(self, logger, tmp_dir: Path):
        # The /mnt/data-fell-back-to-rootfs scenario: ismount is True
        # (because /tmp itself can be a mount on some hosts) but the label
        # is wrong. Must abort to avoid corrupting the wrong disk.
        with (
            mock.patch("os.path.ismount", return_value=True),
            mock.patch.object(preflight, "_findmnt_source", return_value="/dev/sda1"),
            mock.patch.object(preflight, "_device_label", return_value="ROOT"),
            mock.patch.object(preflight, "_device_uuid", return_value=None),
        ):
            r = verify_destination(
                logger, str(tmp_dir), expected_label="DATA", auto_mount=True, ensure_fstab=False
            )
        assert r.ok is False and r.fatal is True
        assert "WRONG volume" in r.message

    def test_uuid_mismatch_is_fatal(self, logger, tmp_dir: Path):
        with (
            mock.patch("os.path.ismount", return_value=True),
            mock.patch.object(preflight, "_findmnt_source", return_value="/dev/sda1"),
            mock.patch.object(preflight, "_device_label", return_value="DATA"),
            mock.patch.object(preflight, "_device_uuid", return_value="aaaa-1111"),
        ):
            r = verify_destination(
                logger,
                str(tmp_dir),
                expected_label="DATA",
                expected_uuid="bbbb-2222",
                auto_mount=True,
                ensure_fstab=False,
            )
        assert r.ok is False and r.fatal is True
        assert "WRONG volume" in r.message

    def test_self_heal_remount_then_passes(self, logger, tmp_dir: Path):
        ismount_calls = {"n": 0}

        def fake_ismount(_):
            ismount_calls["n"] += 1
            return ismount_calls["n"] > 1  # False first call, True after remount

        with (
            mock.patch("os.path.ismount", side_effect=fake_ismount),
            mock.patch.object(preflight, "_try_mount", return_value=True) as mount,
            mock.patch.object(preflight, "_findmnt_source", return_value="/dev/sdb1"),
            mock.patch.object(preflight, "_device_label", return_value="DATA"),
            mock.patch.object(preflight, "_device_uuid", return_value=None),
            mock.patch.object(preflight, "_fstab_has_entry", return_value=True),
        ):
            r = verify_destination(
                logger,
                str(tmp_dir),
                expected_label="DATA",
                auto_mount=True,
                ensure_fstab=False,
            )
        assert r.ok is True and r.healed is True
        mount.assert_called_once()


# ─── Staleness ──────────────────────────────────────────────────────────────


class TestStaleness:
    def test_no_prior_run_is_not_stale(self, logger):
        with mock.patch.object(preflight, "get_last_backup_time", return_value=0):
            r = check_staleness(logger, interval_minutes=60)
        assert r.ok is True
        assert r.details.get("stale") is None

    def test_recent_backup_is_not_stale(self, logger):
        recent = int(time.time()) - 30 * 60  # 30 min ago
        with mock.patch.object(preflight, "get_last_backup_time", return_value=recent):
            r = check_staleness(logger, interval_minutes=60, staleness_factor=2.0)
        assert r.ok is True
        assert r.details.get("stale") is None

    def test_old_backup_is_flagged_stale_but_not_fatal(self, logger):
        # interval=60m, factor=2 -> threshold=120m. Set last run 6h ago.
        old = int(time.time()) - 6 * 3600
        with mock.patch.object(preflight, "get_last_backup_time", return_value=old):
            r = check_staleness(logger, interval_minutes=60, staleness_factor=2.0)
        # Staleness is informational: ok=True so the run still proceeds,
        # but details["stale"] surfaces the alert to the orchestrator.
        assert r.ok is True
        assert r.fatal is False
        assert r.details.get("stale") is True
        assert "STALE" in r.message


# ─── Local mail ─────────────────────────────────────────────────────────────


class TestSendLocalMail:
    def test_rejects_malformed_recipient(self, logger):
        with mock.patch("subprocess.run") as run:
            ok = send_local_mail(logger, "not a valid; recipient", "subj", "body")
        assert ok is False
        run.assert_not_called()

    def test_accepts_local_user(self, logger):
        with (
            mock.patch("shutil.which", return_value="/usr/sbin/sendmail"),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch("subprocess.run") as run,
        ):
            run.return_value = mock.MagicMock(returncode=0, stderr="")
            ok = send_local_mail(logger, "root", "subject", "body")
        assert ok is True

    def test_falls_back_to_mail_when_sendmail_fails(self, logger):
        sendmail_call = mock.MagicMock(returncode=1, stderr="reject")
        mail_call = mock.MagicMock(returncode=0, stderr="")
        with (
            mock.patch("shutil.which", side_effect=["/usr/sbin/sendmail", "/usr/bin/mail"]),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch("subprocess.run", side_effect=[sendmail_call, mail_call]) as run,
        ):
            ok = send_local_mail(logger, "root@localhost", "subj", "body")
        assert ok is True
        assert run.call_count == 2

    def test_returns_false_when_no_mta_present(self, logger):
        with (
            mock.patch("shutil.which", return_value=None),
            mock.patch.object(Path, "exists", return_value=False),
        ):
            ok = send_local_mail(logger, "root", "subj", "body")
        assert ok is False


# ─── Top-level orchestrator ─────────────────────────────────────────────────


class TestRunPreflight:
    def test_disabled_short_circuits(self, logger, tmp_dir: Path):
        cfg = PreflightConfig(enabled=False)
        r = run_preflight(logger, cfg, backup_dirs=[str(tmp_dir)], interval_minutes=60)
        assert r.ok is True
        assert r.message == "Preflight disabled"

    def test_no_expected_mount_skips_mount_check(self, logger, tmp_dir: Path):
        cfg = PreflightConfig(enabled=True)
        with mock.patch.object(preflight, "get_last_backup_time", return_value=0):
            r = run_preflight(logger, cfg, backup_dirs=[str(tmp_dir)], interval_minutes=60)
        assert r.ok is True

    def test_unmounted_destination_aborts_run(self, logger, tmp_dir: Path):
        # The April 2026 scenario: /mnt/data is not a mountpoint, no mount
        # entry, sudo cannot remount. Run must abort with a fatal result —
        # NEVER fall through to writing a backup onto the system disk.
        cfg = PreflightConfig(
            enabled=True,
            expected_mount=str(tmp_dir / "fake_mount"),
            expected_label="DATA",
            auto_mount=True,
            ensure_fstab=False,
        )
        with mock.patch.object(preflight, "_try_mount", return_value=False):
            r = run_preflight(logger, cfg, backup_dirs=[], interval_minutes=60)
        assert r.ok is False and r.fatal is True

    def test_self_heal_path_logs_healed_flag(self, logger, tmp_dir: Path):
        cfg = PreflightConfig(
            enabled=True,
            expected_mount=str(tmp_dir),
            expected_label="DATA",
            auto_mount=True,
            ensure_fstab=False,
        )
        ismount_calls = {"n": 0}

        def fake_ismount(_):
            ismount_calls["n"] += 1
            return ismount_calls["n"] > 1

        with (
            mock.patch("os.path.ismount", side_effect=fake_ismount),
            mock.patch.object(preflight, "_try_mount", return_value=True),
            mock.patch.object(preflight, "_findmnt_source", return_value="/dev/sdb1"),
            mock.patch.object(preflight, "_device_label", return_value="DATA"),
            mock.patch.object(preflight, "_device_uuid", return_value=None),
            mock.patch.object(preflight, "_fstab_has_entry", return_value=True),
            mock.patch.object(preflight, "get_last_backup_time", return_value=0),
        ):
            r = run_preflight(logger, cfg, backup_dirs=[str(tmp_dir)], interval_minutes=60)
        assert r.ok is True
        assert r.healed is True
        assert "self-healed" in r.message

    def test_stale_alert_surfaces_in_details(self, logger, tmp_dir: Path):
        cfg = PreflightConfig(enabled=True)
        old = int(time.time()) - 7 * 24 * 3600  # 7 days ago
        with mock.patch.object(preflight, "get_last_backup_time", return_value=old):
            r = run_preflight(logger, cfg, backup_dirs=[str(tmp_dir)], interval_minutes=60)
        assert r.ok is True
        assert r.details.get("stale_alert")
        assert "STALE" in r.details["stale_alert"]
