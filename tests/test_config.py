"""Tests for config loading, env-var resolution, and normalisation."""

from __future__ import annotations

import os

import pytest

from src.config import _resolve_all_env_vars, normalize_none, resolve_env_vars


class TestEnvVarResolution:
    def test_resolve_single_var(self):
        os.environ["TEST_BH_VAR"] = "secret123"
        try:
            assert resolve_env_vars("${TEST_BH_VAR}") == "secret123"
        finally:
            del os.environ["TEST_BH_VAR"]

    def test_resolve_multiple_vars(self):
        os.environ["TEST_BH_A"] = "hello"
        os.environ["TEST_BH_B"] = "world"
        try:
            result = resolve_env_vars("${TEST_BH_A}-${TEST_BH_B}")
            assert result == "hello-world"
        finally:
            del os.environ["TEST_BH_A"]
            del os.environ["TEST_BH_B"]

    def test_unset_var_raises(self):
        with pytest.raises(ValueError, match="UNSET_VAR_12345"):
            resolve_env_vars("${UNSET_VAR_12345}")

    def test_no_vars_passthrough(self):
        assert resolve_env_vars("plain_value") == "plain_value"

    def test_empty_string(self):
        assert resolve_env_vars("") == ""

    def test_resolve_all_env_vars(self, logger):
        import configparser

        config = configparser.ConfigParser()
        config.read_string("""
[DEFAULT]
source_dir = /tmp/test

[SSH]
password = ${TEST_BH_PASS}
""")
        os.environ["TEST_BH_PASS"] = "my_secret"
        try:
            _resolve_all_env_vars(config, logger)
            assert config.get("SSH", "password") == "my_secret"
        finally:
            del os.environ["TEST_BH_PASS"]


class TestNormalizeNone:
    def test_none_value(self):
        assert normalize_none(None) is None

    def test_none_string(self):
        assert normalize_none("None") is None
        assert normalize_none("none") is None

    def test_empty_string(self):
        assert normalize_none("") is None
        assert normalize_none("   ") is None

    def test_valid_value(self):
        assert normalize_none("hello") == "hello"
        assert normalize_none("  hello  ") == "hello"


class TestRemoteSecurityValidation:
    BASE = (
        "[DEFAULT]\nsource_dir = /src\nmode = full\n[BACKUPS]\nbackup_dirs = /b\n"
        "[MODES]\nlocal = True\nssh = True\ns3 = True\n"
        "[SSH]\nssh_servers = host\nusername = u\n{ssh}\n"
        "[S3]\nbucket = b\nregion = r\n{s3}\n"
    )

    def _validate(self, logger, ssh="", s3=""):
        import configparser

        from src.config import validate_config

        cfg = configparser.ConfigParser()
        cfg.read_string(self.BASE.format(ssh=ssh, s3=s3))
        validate_config(logger, cfg)

    def test_key_file_is_enough_for_ssh(self, logger):
        self._validate(logger, ssh="key_file = ~/.ssh/id_ed25519")

    def test_ssh_needs_key_or_password(self, logger):
        with pytest.raises(SystemExit):
            self._validate(logger)

    def test_rejects_unknown_sse_mode(self, logger):
        with pytest.raises(SystemExit):
            self._validate(logger, ssh="password = p", s3="server_side_encryption = rot13")

    def test_accepts_kms(self, logger):
        self._validate(logger, ssh="password = p", s3="server_side_encryption = aws:kms")
