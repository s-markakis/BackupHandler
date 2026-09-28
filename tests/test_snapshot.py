"""Tests for snapshot restore-script generation and diffing."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from src.snapshot import diff_snapshots, generate_restore_script

MALICIOUS = {
    "metadata": {"os": "Linux", "timestamp": "t", "hostname": "h", "user": 'a"; touch /tmp/p1; "'},
    "packages": {
        "apt": ["vim", "x;touch /tmp/p2"],
        "snap": [{"name": "$(touch /tmp/p3)"}],
        "npm": ["a`id`"],
        "apt_repos": {
            "sources": [
                {"file": "../../cron.d/evil", "content": "x"},
                {"file": "ok.list", "content": "deb x\nREPOEOF\ntouch /tmp/p4"},
            ]
        },
    },
    "configs": {
        "dotfiles": {"../../etc/passwd": "x", ".bashrc": "export T=1"},
        "hosts": "1.1.1.1 a\nHOSTSEOF\ntouch /tmp/p5",
        "fstab": "x",
    },
    "security": {"ssh_keys": [{"name": "$(touch /tmp/p6)", "type": "rsa"}]},
    "network": {"wireguard": ["w`id`"]},
    "apps": {},
}


def _generate(tmp_dir, logger, snapshot):
    path = tmp_dir / "snap.json"
    path.write_text(json.dumps(snapshot))
    return generate_restore_script(logger, str(path), output_path=str(tmp_dir / "restore.sh"))


class TestLinuxRestoreScript:
    def test_script_is_owner_only_and_valid_bash(self, tmp_dir, logger):
        script = _generate(tmp_dir, logger, MALICIOUS)
        assert os.stat(script).st_mode & 0o777 == 0o700
        subprocess.run(["bash", "-n", script], check=True)

    def test_values_cannot_inject_commands(self, tmp_dir, logger):
        text = Path(_generate(tmp_dir, logger, MALICIOUS)).read_text()
        assert "ORIGINAL_USER='a\"; touch /tmp/p1; \"'" in text
        assert "'x;touch /tmp/p2'" in text
        assert "snap install '$(touch /tmp/p3)'" in text
        assert "'a`id`'" in text
        # heredoc terminators inside content can no longer break out
        assert "REPOEOF" not in text and "HOSTSEOF" not in text
        assert "/tmp/original_fstab.txt" not in text  # noqa: S108

    def test_unsafe_paths_are_skipped(self, tmp_dir, logger):
        text = Path(_generate(tmp_dir, logger, MALICIOUS)).read_text()
        assert "cron.d" not in text
        assert "etc/passwd" not in text
        assert '"$TARGET_HOME/.bashrc"' in text


class TestWindowsRestoreScript:
    def test_powershell_values_are_single_quoted(self, tmp_dir, logger):
        snap = {
            "metadata": {"os": "Windows", "timestamp": "t", "hostname": "h"},
            "packages": {"winget": ["x'; Remove-Item C:\\ -Recurse; '"]},
            "configs": {"env_vars": {"TOKEN": "$(calc)"}},
            "apps": {},
        }
        path = tmp_dir / "snap.json"
        path.write_text(json.dumps(snap))
        script = generate_restore_script(logger, str(path), output_path=str(tmp_dir / "r.ps1"))
        text = Path(script).read_text()
        assert "--id 'x''; Remove-Item C:\\ -Recurse; '''" in text
        assert "'$(calc)'" in text
        assert os.stat(script).st_mode & 0o777 == 0o600


def test_diff_reports_added_and_removed_packages(tmp_dir, logger):
    a = {"packages": {"apt": ["vim", "git"]}, "configs": {}}
    b = {"packages": {"apt": ["vim", "curl"]}, "configs": {}}
    (tmp_dir / "a.json").write_text(json.dumps(a))
    (tmp_dir / "b.json").write_text(json.dumps(b))
    diff = diff_snapshots(logger, str(tmp_dir / "a.json"), str(tmp_dir / "b.json"))
    assert diff["apt"] == {"added": ["curl"], "removed": ["git"]}
