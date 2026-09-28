"""Tests for Tailscale auth-key handling (no tailscale binary needed)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from src import tailscale


def test_auth_key_is_passed_via_private_file_not_argv(logger):
    seen = {}

    def fake_run(cmd, logger=None, timeout=30):
        if "up" in cmd:
            seen["cmd"] = cmd
            key_arg = next(a for a in cmd if a.startswith("--auth-key=file:"))
            key_path = Path(key_arg.split("file:", 1)[1])
            seen["key"] = key_path.read_text()
            seen["mode"] = key_path.stat().st_mode & 0o777
            seen["path"] = key_path
        return 0, "", ""

    statuses = iter([{"connected": False}, {"connected": True, "ip": "100.64.0.1"}])
    with (
        mock.patch.object(tailscale, "is_tailscale_installed", return_value=True),
        mock.patch.object(tailscale, "get_tailscale_status", side_effect=lambda _l=None: next(statuses)),
        mock.patch.object(tailscale, "_run_cmd", side_effect=fake_run),
    ):
        assert tailscale.tailscale_up("tskey-secret", logger=logger)
    assert all("tskey-secret" not in arg for arg in seen["cmd"])
    assert seen["key"] == "tskey-secret" and seen["mode"] == 0o600
    assert not seen["path"].exists()
