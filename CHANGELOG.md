# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Telegram bot no longer auto-subscribes any chat that messages it; only
  chat IDs in `[USERS] interacted_users` receive notifications and
  `zip_pw` archive passwords.
- `--install` no longer grants `NOPASSWD: tee -a /etc/fstab` (root-equivalent)
  and rejects owner/label/UUID/mount-point values that could inject extra
  sudoers rules. New fstab entries use `nosuid,nodev`.
- SMTP connections (STARTTLS and implicit TLS) now verify the server
  certificate; previously credentials could be captured by a MITM.
- Restore rejects path traversal from SFTP file names and S3 object keys,
  and manifest-based restore no longer copies files from outside the backup.
- SSH clients load `known_hosts`, so a changed host key is rejected.
- HTML email bodies are escaped; webhook/heartbeat URLs are logged as
  `scheme://host` only.
- Snapshot JSON and generated restore scripts are written owner-only and
  quote every snapshot-derived value (no shell/PowerShell injection,
  no heredoc break-out, no writes outside the target directories).
- SSH and S3 uploads are now encrypted client-side when `[ENCRYPTION]` is
  enabled (uploaded as `<name>.enc`); previously the raw source directory
  was uploaded in plaintext. Opt out with `[ENCRYPTION] remote = False`.
- `[SSH] strict_host_key_checking` / `known_hosts_file` options; SSH key
  authentication via `[SSH] key_file`.
- Optional S3 server-side encryption (`[S3] server_side_encryption`,
  `sse_kms_key_id`).
- The Tailscale auth key is passed via a private file (`--auth-key=file:`)
  instead of the command line, where `ps` exposed it.
- `zip_pw` archives use WinZip AES-256 (`pyzipper`) instead of ZipCrypto;
  password delivery can be disabled with `[NOTIFICATIONS] send_zip_password`.
- Dependencies upgraded past 34 published advisories in the old pins
  (cryptography, urllib3, requests, paramiko, PyNaCl, idna, jaraco.context).
  `requirements.txt` is now a fully pinned, `pip-audit`-clean set.
- `--show-setup` no longer reveals the SSH password length.

### Changed

- Encryption format v2: streaming 1 MiB authenticated chunks (constant
  memory), HKDF per-file keys, truncation/reordering detection, atomic
  writes, one PBKDF2 run per backup run instead of per file. v1 files
  still decrypt.
- Pipeline order is now dedup → encrypt, and encryption preserves
  hardlinks, so dedup savings survive encryption.
- Hash-while-copy: each file is read twice instead of three times.
- Manifest-based restore picks the file whose path best matches the
  manifest entry instead of the first file with the same name.
- `main.py` split into the `src/cli/` package and `src/snapshot.py` into
  the `src/snapshot/` package. `bot/`, `banner/` and `email_nots/` moved
  into `src/` (`telegram_bot.py`, `banner.py`, `email_notify.py`) and are
  now linted and type-checked. `python main.py` still works.
- Runtime files resolve from `$BACKUP_HANDLER_HOME`, the source checkout,
  or the current directory.

### Fixed

- The installed `backup-handler` command (and therefore the Docker image)
  crashed with `ModuleNotFoundError: main`; the entry point is now
  `src.cli:main` and the wheel contains every module it needs.
- Encryption failures, S3 upload failures, and per-server/per-file SSH
  failures were logged but the run still reported success (exit 0).
- `clear_keyring` removed only one stored archive password.
- The `zip_pw` archive flattened directory paths (same-named files collided).
- A preflight test failed when the suite ran as root.

### Removed

- `db_backup/mysql_backup.py` and `config/db_config.ini.example` — a
  standalone duplicate of `--operation-modes db` (combine with `ssh` to
  ship the dump off-host).
- `pyminizip` dependency.

### Added

- `--install` (`src/installer.py`): one-shot system bootstrap that
  performs every OS-level prerequisite the preflight self-heal needs.
  Single privileged invocation (`sudo -E backup-handler --install`)
  mounts the destination volume, creates and chowns the destination
  tree, appends a hardened `nofail` entry to `/etc/fstab` (with a
  timestamped backup + auto-revert if `mount -a` fails), drops a
  visudo-validated `/etc/sudoers.d/backup-handler` with the minimum
  NOPASSWD rules, installs `postfix` (Local only) + `bsd-mailx` if no
  MTA is present, and runs a live preflight smoke test. Every step is
  idempotent. `--install --dry-run` prints the exact plan and changes
  nothing. Ownership of the project `Logs/` and `BackupTimestamp/` is
  handed back to the unprivileged owner at the end so the next normal
  cron run can write through.
- Preflight self-check + self-heal (`src/preflight.py`, new
  `[PREFLIGHT]` config section). Runs before every backup and detects
  the silent-failure mode that produced 16 days of zero-backups in
  April 2026: destination unmounted, falls back to a regular directory
  on the system disk, script crashes before logging, no alert. Now:
  verifies the destination is a real mountpoint AND the backing device
  matches `expected_label`/`expected_uuid`; auto-mounts via
  `sudo -n mount` when missing; appends a missing fstab entry with
  `nofail`; ensures the destination tree is writable and (optionally)
  chowns it. On fatal failure, writes a JSON status sentinel
  (`Logs/last_run_status.json`) and dispatches a local-MTA mail
  (`local_mail_to`) — both work even when DNS/Telegram are down.
  Surfaces a non-fatal STALE alert when the last successful backup
  exceeds `staleness_factor x interval`. Logger is now initialized
  before any filesystem operation so pre-logger crashes are no longer
  possible. RUNBOOK §2.4 + §4 cover triage and the required sudoers /
  fstab / MTA setup.
- Hardened systemd unit + timer pair in `contrib/systemd/`
  (`backup-handler.service` / `.timer`). Runs under an unprivileged
  `backup` user with `ProtectSystem=strict`, `MemoryDenyWriteExecute`,
  and a `SystemCallFilter` allowlist.
- Weekly restore-drill systemd pair
  (`backup-handler-drill.service` / `.timer`) that exercises the full
  restore path and verifies every file's checksum. Failed drills are a
  higher-severity incident than failed backups.
- `scripts/restore_drill.sh` — picks the latest manifest, dry-runs,
  restores to a scratch dir, verifies, and optionally pings a webhook.
  Exit codes `0`/`1`/`2`/`3`/`4` distinguish pass, config, restore,
  verify, and notification failures.
- `RUNBOOK.md` — step-by-step procedures for full-host, single-file,
  MySQL PITR, encrypted archive, and system-snapshot restores, plus
  failure triage by exit code.
- `[HEARTBEAT]` config section and `src/heartbeat.py`: optional
  dead-man's-switch ping on successful runs
  (healthchecks.io / Dead Man's Snitch / Uptime Kuma compatible).
  Scheme is restricted to `http`/`https`.
- `pyproject.toml` with full tooling configuration (ruff, black, mypy,
  pytest, coverage, bandit).
- `SECURITY.md` with disclosure policy and hardening guidance.
- `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md`.
- GitHub Actions CI workflow: lint, type-check, test matrix
  (Python 3.10/3.11/3.12), security scan (bandit, pip-audit), secrets scan
  (gitleaks), build smoke test.
- GitHub Actions release workflow triggered on `v*.*.*` tags.
- `.pre-commit-config.yaml` with ruff, black, gitleaks, bandit.
- Structured / JSON-capable logger with correlation IDs, separate
  `Logs/audit.log` audit stream, and syslog handler option.
- `src/__version__.py` as the single source of truth for the version string.
- Dockerfile (multi-stage, non-root user) and `.dockerignore`.
- Issue and PR templates, CODEOWNERS, dependabot config.
- Type hints on pure-logic modules (`utils`, `manifest`, `webhook_notify`,
  `logger`).

### Changed

- `main.backup_operation` now returns a process exit code
  (`0` / `2` / `3`) and per-mode failures (local / SSH / S3 / DB) are
  tracked individually. A partial-success run exits non-zero so systemd
  and Prometheus alerting can page an operator. Silent pre-flight and
  pre-hook failure paths now propagate non-zero.
- `update_last_backup_time()` only advances on full success — a partial
  failure no longer skews future incremental-window calculations.
- OTP / zip-password generation (`utils.generate_otp`) now uses `secrets`
  (CSPRNG) instead of `random`, and defaults to 16 characters.
- Webhook URLs are validated against an `http`/`https` allowlist before
  sending.
- Bare `except Exception` handlers were narrowed to specific exception
  classes across `utils`, `db_sync`, `sync`, and `webhook_notify`.
- Lock-file PID verification now also checks the process command name to
  avoid false-positives from PID recycling.
- Test suite split from a single file into per-module suites with a shared
  `conftest.py`.

### Removed

- Dead code: `src/test.py`, `src/backup.py` (neither was imported anywhere).

### Security

- Replaced `random.choice` with `secrets.choice` in OTP/password generation —
  the OTP is used as a zip-archive password in `zip_pw` mode, so the prior
  use of `random` was a real weakness.
- Webhook URL scheme allowlist prevents accidental SSRF via `file://`,
  `gopher://`, or similar schemes.

---

## [2.5.0] — 2026-04-04

### Added

- System snapshot and restore feature for OS rebuild after format
  (`--snapshot`, `--restore-snapshot`, `--snapshot-diff`).

## [2.4.0] — 2026-03-15

### Added

- Tailscale VPN integration for SSH backups (`--tailscale`,
  `--tailscale-authkey`, `[TAILSCALE]` config section).
- Pre-flight mount check to prevent silent backup failures when a backup
  target is an unmounted mountpoint.

## [2.3.0] — 2026-03-10

### Added

- Config schema versioning via `[META] schema_version`.
- SHA-256 checksum verification in manifests.
- Parallel encryption / decryption with `ThreadPoolExecutor`.
- Incremental database backup support (`--single-transaction`,
  `--master-data=2`).
- HTML email notifications with status-colour header.
- Bandwidth-aware S3 uploads via `boto3.s3.transfer.TransferConfig`.
- Restore dry-run preview mode.
- Webhook notifications (Slack, Discord, Teams compatible).
- Progress bars (tqdm) for encryption, decryption, and deduplication.
- Compression + encryption compatibility warning.

## [2.2.0] — 2026-02-28

### Added

- Seven new features including verification, deduplication, manifests,
  email notifications (SMTP), and remote restore.

### Fixed

- SMTP notification connection leaks.
- File-pattern matching bugs.

## [2.0.0] — 2026-02-27

### Added

- Ten new features: encryption at rest, S3 sync, dry-run, retention
  policies, environment-variable config resolution, and more.

## [1.2.0] — 2026-02-22

### Added

- `--version` flag.
- SMTP retry with exponential backoff.
- POSIX signal handling for graceful shutdown.

### Removed

- Dead code paths identified during audit.

## [1.1.0] — 2026-02-21

### Added

- Dry-run mode, PID locking, hardening fixes.
- Config validation and graceful error handling.
- systemd service support for startup.

## [1.0.0] — 2026-02-21

### Added

- Initial release: backup_handler with full code-review fixes,
  local/SSH modes, Telegram bot notifications.
