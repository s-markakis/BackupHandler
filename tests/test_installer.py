"""Tests for the system bootstrap installer.

The real installer mutates /etc/fstab, /etc/sudoers.d, and apt. None of
those are touched here — every step that would write to a privileged
path is monkeypatched to point at a temp dir, and every external command
is mocked. The dry-run path is also exercised end-to-end so the
``--install --dry-run`` UX stays guaranteed-safe.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from src import installer
from src.installer import (
    StepResult,
    run_installer,
    step_destination,
    step_fstab,
    step_mount,
    step_mta,
    step_sudoers,
)

# ─── step_mount ─────────────────────────────────────────────────────────────


class TestStepMount:
    def test_already_mounted_correctly_is_skipped(self, logger):
        with (
            mock.patch("os.path.ismount", return_value=True),
            mock.patch.object(installer, "_findmnt_source", return_value="/dev/sdb1"),
            mock.patch.object(installer, "_device_label", return_value="DATA"),
            mock.patch.object(installer, "_device_uuid", return_value="abc"),
        ):
            r = step_mount(logger, "/mnt/data", "DATA", "abc", dry_run=False)
        assert r.ok and r.skipped

    def test_already_mounted_wrong_uuid_is_fatal(self, logger):
        with (
            mock.patch("os.path.ismount", return_value=True),
            mock.patch.object(installer, "_findmnt_source", return_value="/dev/sda1"),
            mock.patch.object(installer, "_device_label", return_value="ROOT"),
            mock.patch.object(installer, "_device_uuid", return_value="zzz"),
        ):
            r = step_mount(logger, "/mnt/data", "DATA", "abc", dry_run=False)
        assert r.ok is False
        assert "WRONG" in r.message

    def test_dry_run_does_not_call_mount(self, logger, tmp_dir: Path):
        target = tmp_dir / "mnt"
        with (
            mock.patch("os.path.ismount", return_value=False),
            mock.patch.object(installer, "_run") as run,
        ):
            r = step_mount(logger, str(target), "DATA", None, dry_run=True)
        run.assert_not_called()
        assert r.ok and r.changed
        assert "WOULD" in r.message

    def test_real_mount_calls_mount_command(self, logger, tmp_dir: Path):
        target = tmp_dir / "mnt"
        with (
            mock.patch("os.path.ismount", return_value=False),
            mock.patch.object(installer, "_run", return_value=(0, "", "")) as run,
        ):
            r = step_mount(logger, str(target), "DATA", "uuid-1", dry_run=False)
        assert r.ok and r.changed
        run.assert_called_once()
        assert run.call_args[0][0][:2] == ["mount", "UUID=uuid-1"]


# ─── step_destination ───────────────────────────────────────────────────────


class TestStepDestination:
    def test_no_owner_skips(self, logger, tmp_dir: Path):
        r = step_destination(logger, str(tmp_dir), [], owner=None, dry_run=False)
        assert r.ok and r.skipped

    def test_dry_run_creates_no_dirs(self, logger, tmp_dir: Path):
        target = tmp_dir / "newdir"
        with mock.patch.object(installer, "_run") as run:
            r = step_destination(logger, str(target), [str(target / "x")], owner="alice", dry_run=True)
        run.assert_not_called()
        assert r.ok and not target.exists()

    def test_real_run_creates_and_chowns(self, logger, tmp_dir: Path):
        target = tmp_dir / "data"
        sub = target / "backups"
        with mock.patch.object(installer, "_run", return_value=(0, "", "")) as run:
            r = step_destination(logger, str(target), [str(sub)], owner="alice", dry_run=False)
        assert r.ok
        assert target.exists() and sub.exists()
        # mountpoint chown + sub chown -> two calls
        assert run.call_count == 2


# ─── step_fstab ─────────────────────────────────────────────────────────────


class TestStepFstab:
    def test_existing_entry_skipped(self, logger, tmp_dir: Path):
        fake_fstab = tmp_dir / "fstab"
        fake_fstab.write_text("UUID=xxx /mnt/data xfs defaults 0 2\n")
        with (
            mock.patch.object(installer, "FSTAB_PATH", str(fake_fstab)),
            mock.patch.object(installer, "_fstab_has_entry", return_value=True),
        ):
            r = step_fstab(logger, "/mnt/data", "DATA", "xxx", "xfs", dry_run=False)
        assert r.ok and r.skipped

    def test_dry_run_does_not_write(self, logger, tmp_dir: Path):
        fake_fstab = tmp_dir / "fstab"
        fake_fstab.write_text("# orig\n")
        before = fake_fstab.read_text()
        with (
            mock.patch.object(installer, "FSTAB_PATH", str(fake_fstab)),
            mock.patch.object(installer, "_fstab_has_entry", return_value=False),
        ):
            r = step_fstab(logger, "/mnt/data", "DATA", "xxx", "xfs", dry_run=True)
        assert r.ok and r.changed
        assert fake_fstab.read_text() == before, "dry-run must not modify fstab"

    def test_real_run_appends_and_validates(self, logger, tmp_dir: Path):
        fake_fstab = tmp_dir / "fstab"
        fake_fstab.write_text("# orig\n")
        with (
            mock.patch.object(installer, "FSTAB_PATH", str(fake_fstab)),
            mock.patch.object(installer, "_fstab_has_entry", return_value=False),
            mock.patch.object(installer, "_run", return_value=(0, "", "")),
        ):
            r = step_fstab(logger, "/mnt/data", "DATA", "xxx", "xfs", dry_run=False)
        assert r.ok
        content = fake_fstab.read_text()
        assert "UUID=xxx" in content and "nofail" in content
        # backup file present alongside
        backups = list(tmp_dir.glob("fstab.bak.*"))
        assert backups, "fstab backup must be written before append"

    def test_mount_a_failure_restores_backup(self, logger, tmp_dir: Path):
        fake_fstab = tmp_dir / "fstab"
        fake_fstab.write_text("# orig\n")
        original = fake_fstab.read_text()
        with (
            mock.patch.object(installer, "FSTAB_PATH", str(fake_fstab)),
            mock.patch.object(installer, "_fstab_has_entry", return_value=False),
            mock.patch.object(installer, "_run", return_value=(1, "", "boom")),
        ):
            r = step_fstab(logger, "/mnt/data", "DATA", "xxx", "xfs", dry_run=False)
        assert r.ok is False
        assert "mount -a failed" in r.message
        assert fake_fstab.read_text() == original, "broken fstab must be reverted"


# ─── step_sudoers ───────────────────────────────────────────────────────────


class TestStepSudoers:
    def test_does_not_grant_fstab_write(self):
        from src.installer import _render_sudoers

        assert "tee" not in _render_sudoers("alice", "/mnt/data", "DATA", "abc")

    def test_rejects_values_that_inject_sudoers_rules(self, logger, tmp_dir: Path):
        sudoers = tmp_dir / "backup-handler"
        with mock.patch.object(installer, "SUDOERS_PATH", str(sudoers)):
            r = step_sudoers(logger, "alice", "/mnt/data", "DATA /mnt/data, /bin/sh", None, dry_run=False)
        assert not r.ok
        assert not sudoers.exists()

    def test_no_owner_skips(self, logger):
        r = step_sudoers(logger, None, "/mnt/data", "DATA", "abc", dry_run=False)
        assert r.ok and r.skipped

    def test_idempotent_when_already_correct(self, logger, tmp_dir: Path):
        sudoers = tmp_dir / "backup-handler"
        from src.installer import _render_sudoers

        sudoers.write_text(_render_sudoers("alice", "/mnt/data", "DATA", "abc"))
        with mock.patch.object(installer, "SUDOERS_PATH", str(sudoers)):
            r = step_sudoers(logger, "alice", "/mnt/data", "DATA", "abc", dry_run=False)
        assert r.ok and r.skipped

    def test_dry_run_does_not_write(self, logger, tmp_dir: Path):
        sudoers = tmp_dir / "backup-handler"
        with mock.patch.object(installer, "SUDOERS_PATH", str(sudoers)):
            r = step_sudoers(logger, "alice", "/mnt/data", "DATA", "abc", dry_run=True)
        assert r.ok and r.changed
        assert not sudoers.exists()

    def test_real_run_validates_with_visudo(self, logger, tmp_dir: Path):
        sudoers = tmp_dir / "backup-handler"

        # visudo accepts; chown to root is patched out (we are not root in tests)
        def fake_run(cmd, **_):
            if cmd[:2] == ["visudo", "-cf"]:
                return (0, "parsed OK", "")
            return (0, "", "")

        with (
            mock.patch.object(installer, "SUDOERS_PATH", str(sudoers)),
            mock.patch.object(installer, "_run", side_effect=fake_run),
            mock.patch("os.chown"),  # would need root
        ):
            r = step_sudoers(logger, "alice", "/mnt/data", "DATA", "abc", dry_run=False)
        assert r.ok
        assert sudoers.exists()
        body = sudoers.read_text()
        assert "alice ALL=(root) NOPASSWD:" in body
        assert "/usr/bin/mount UUID=abc /mnt/data" in body

    def test_visudo_rejection_does_not_install(self, logger, tmp_dir: Path):
        sudoers = tmp_dir / "backup-handler"
        with (
            mock.patch.object(installer, "SUDOERS_PATH", str(sudoers)),
            mock.patch.object(installer, "_run", return_value=(1, "", "syntax error")),
        ):
            r = step_sudoers(logger, "alice", "/mnt/data", "DATA", "abc", dry_run=False)
        assert r.ok is False
        assert not sudoers.exists(), "must NOT install a malformed sudoers file"


# ─── step_mta ───────────────────────────────────────────────────────────────


class TestStepMta:
    def test_skipped_when_mta_present(self, logger):
        with (
            mock.patch.object(installer, "_detect_existing_mta", return_value="/usr/sbin/sendmail"),
            mock.patch("shutil.which", return_value="/usr/bin/mail"),
        ):
            r = step_mta(logger, dry_run=False)
        assert r.ok and r.skipped

    def test_skipped_when_no_apt(self, logger):
        with (
            mock.patch.object(installer, "_detect_existing_mta", return_value=None),
            mock.patch("shutil.which", side_effect=lambda b: None),
        ):
            r = step_mta(logger, dry_run=False)
        assert r.ok and r.skipped
        assert "manually" in r.message

    def test_dry_run_does_not_invoke_apt(self, logger):
        with (
            mock.patch.object(installer, "_detect_existing_mta", return_value=None),
            mock.patch("shutil.which", return_value="/usr/bin/apt-get"),
            mock.patch("subprocess.run") as run,
        ):
            r = step_mta(logger, dry_run=True)
        run.assert_not_called()
        assert r.ok and r.changed


# ─── run_installer ──────────────────────────────────────────────────────────


class TestRunInstaller:
    def test_missing_expected_mount_returns_1(self, tmp_dir: Path):
        rc = run_installer(
            {"preflight_expected_mount": None, "preflight_expected_label": "DATA"},
            tmp_dir,
            dry_run=True,
        )
        assert rc == 1

    def test_missing_label_and_uuid_returns_1(self, tmp_dir: Path):
        rc = run_installer(
            {"preflight_expected_mount": "/mnt/data"},
            tmp_dir,
            dry_run=True,
        )
        assert rc == 1

    def test_non_root_real_run_returns_2(self, tmp_dir: Path, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        rc = run_installer(
            {
                "preflight_expected_mount": "/mnt/data",
                "preflight_expected_label": "DATA",
                "preflight_expected_uuid": "abc",
                "backup_dirs": [],
            },
            tmp_dir,
            dry_run=False,
        )
        assert rc == 2

    def test_dry_run_full_pipeline_succeeds(self, tmp_dir: Path):
        # Each step's dry-run path is no-op-safe, so the orchestrator
        # should report success without root. Point /etc paths at the
        # tmp dir so the test does not depend on whether the installer
        # has previously been run on the host.
        config = {
            "preflight_expected_mount": str(tmp_dir / "mnt" / "data"),
            "preflight_expected_label": "DATA",
            "preflight_expected_uuid": "abc",
            "preflight_expected_fs_type": "xfs",
            "preflight_expected_owner": "alice",
            "backup_dirs": [str(tmp_dir / "mnt" / "data" / "backups")],
            "preflight_local_mail_to": "root",
            "preflight_status_sentinel": "Logs/last_run_status.json",
            "interval_minutes": 4320,
        }
        with (
            mock.patch.object(installer, "SUDOERS_PATH", str(tmp_dir / "sudoers.d" / "backup-handler")),
            mock.patch.object(installer, "FSTAB_PATH", str(tmp_dir / "fstab")),
        ):
            rc = run_installer(config, tmp_dir, dry_run=True)
        assert rc == 0

    def test_step_failure_reports_rc_3(self, tmp_dir: Path, monkeypatch):
        # Simulate every step ok-skipped except one that fails.
        ok_skip = StepResult(name="x", ok=True, skipped=True, message="-")
        boom = StepResult(name="boom", ok=False, message="kaboom")

        monkeypatch.setattr(installer, "step_mount", lambda *a, **k: boom)
        monkeypatch.setattr(installer, "step_destination", lambda *a, **k: ok_skip)
        monkeypatch.setattr(installer, "step_fstab", lambda *a, **k: ok_skip)
        monkeypatch.setattr(installer, "step_sudoers", lambda *a, **k: ok_skip)
        monkeypatch.setattr(installer, "step_mta", lambda *a, **k: ok_skip)
        monkeypatch.setattr(installer, "step_smoke_test", lambda *a, **k: ok_skip)

        config = {
            "preflight_expected_mount": "/mnt/data",
            "preflight_expected_label": "DATA",
            "preflight_expected_uuid": "abc",
            "backup_dirs": [],
        }
        rc = run_installer(config, tmp_dir, dry_run=True)
        assert rc == 3
