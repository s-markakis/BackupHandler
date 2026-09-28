# Backup-Handler Runbook

This is the operational playbook for backup-handler. It is written so that a
junior on-call at 3am, with no prior context, can recover data. Follow the
steps in order. Do not improvise unless the steps explicitly tell you to.

**If in doubt, STOP and page the backup owner.** Better to wake someone up
than to destroy a restore target.

- **Primary owner:** fill in before deployment (`team-infra@example.com`)
- **Escalation:** fill in before deployment (`oncall-lead@example.com`)
- **Source code:** https://github.com/SP1R4/BackupHandler
- **Install root:** `/opt/backup-handler`
- **State dir:** `/var/lib/backup-handler`
- **Logs:** `/var/lib/backup-handler/Logs/application.log`, `audit.log`
- **Journal:** `journalctl -u backup-handler.service`, `-u backup-handler-drill.service`

---

## 0. Before you touch anything

1. Read the alert. Note the unit that fired (`backup-handler.service` or
   `backup-handler-drill.service`) and the exit code.
2. Pull the last 200 lines of journal:
   ```
   journalctl -u backup-handler.service -n 200 --no-pager
   ```
3. Check `/var/lib/backup-handler/Logs/application.log` for the matching
   correlation ID — every run emits a `run_id` in JSON log mode.
4. Decide: is this a **failed backup** (nothing to restore from, but
   yesterday's backup is still good) or a **failed restore** (we are in an
   actual outage)? The two paths diverge below.

---

## 1. Restore scenarios

### 1.1 Full-host restore (disaster recovery)

Use when a host is lost entirely and you are rebuilding onto fresh hardware
or a fresh VM.

**Prerequisites**
- A reachable backup destination (local path, mounted NAS, or S3 bucket).
- The encryption passphrase or key file, if encryption is enabled. **The key
  is NOT stored on the destination.** Retrieve it from your secrets manager
  (Vault path: fill in before deployment).
- Root/sudo on the restore target.

**Procedure**

1. Install backup-handler on the new host from the same release tag that was
   running in production. Do not use `main`.
2. Copy the production `config/config.ini` to the new host. Do not re-type
   it; a typo in `backup_dirs` will point at nothing.
3. Identify the manifest timestamp you want to restore:
   ```
   ls -lt /path/to/backup/dir/backup_manifest_*.json | head
   ```
   Manifests are named `backup_manifest_YYYYMMDD_HHMMSS.json`. Pick the
   newest one that pre-dates the incident.
4. Dry-run first — this shows what would be written without touching disk:
   ```
   backup-handler --restore \
       --from-dir /path/to/backup/dir \
       --to-dir /mnt/restore-target \
       --restore-timestamp YYYYMMDD_HHMMSS \
       --dry-run
   ```
5. If the dry-run output looks correct, re-run without `--dry-run`.
6. Verify the restored tree against the manifest:
   ```
   backup-handler --verify --backup-dirs /mnt/restore-target
   ```
   Exit 0 = clean. Non-zero = STOP and escalate.
7. Cut traffic over only after verify passes.

**Common failure modes**
- *"Decryption failed"* → wrong passphrase or key file. Do not guess. Check
  the secrets manager.
- *"Manifest not found"* → the `--restore-timestamp` does not match any
  manifest in `--from-dir`. Re-check step 3.

---

### 1.2 Single-file restore

Use when a user deletes or corrupts a small number of files and you need to
recover them without a full restore.

**Procedure**

1. Restore to a scratch directory, not to the production path:
   ```
   mkdir -p /tmp/restore-scratch
   backup-handler --restore \
       --from-dir /path/to/backup/dir \
       --to-dir /tmp/restore-scratch \
       --restore-timestamp YYYYMMDD_HHMMSS
   ```
2. Locate the file under `/tmp/restore-scratch/`.
3. Copy the file back to its production path with `cp -av` (preserves mode
   and timestamps). **Do not `mv`** from the scratch dir if you may need to
   retry.
4. Confirm the restore with the affected user before deleting
   `/tmp/restore-scratch`.

---

### 1.3 Database (MySQL) point-in-time restore

Use when the `[DATABASE]` section is configured and you need to roll a
database back to a specific backup.

**Procedure**

1. Identify the dump file. Database dumps land alongside file backups and
   are named `db_<database>_YYYYMMDD_HHMMSS.sql` (or `.sql.gz` / `.sql.enc`
   depending on compression/encryption).
2. Decrypt (if encrypted):
   ```
   backup-handler --restore \
       --from-dir /path/to/backup/dir \
       --to-dir /tmp/db-restore \
       --restore-timestamp YYYYMMDD_HHMMSS
   ```
   The dump file is now at `/tmp/db-restore/db_<database>_*.sql`.
3. **Never** restore directly over the production database. Create a
   sibling database first:
   ```
   mysql -u root -p -e "CREATE DATABASE <db>_restore_$(date +%s);"
   mysql -u root -p <db>_restore_<ts> < /tmp/db-restore/db_<database>_*.sql
   ```
4. Validate the sibling database (row counts, recent rows, schema).
5. Coordinate the cutover with the application owner. Options:
   - Rename tables / swap databases (atomic if done carefully).
   - Replay binlogs from the dump point forward if you need PITR beyond the
     backup itself (requires binlog retention — check with DBA).
6. Update the application connection string only after validation.

---

### 1.4 Encrypted archive restore

Backups may be AES-256-GCM encrypted with either a passphrase (PBKDF2-HMAC-
SHA256, 600k iterations) or a key file. The restore subcommand handles
decryption inline when the config provides the credential.

**If you have the passphrase**

1. Put the passphrase in `config/config.ini` `[ENCRYPTION] passphrase=` on
   the restore host (never check it into git).
2. Run the restore as in section 1.1 or 1.2.

**If you have a key file**

1. Copy the key file to the restore host. Protect it: `chmod 0400`, owned
   by `backup:backup`.
2. Point `[ENCRYPTION] key_file=/absolute/path/to/key` at it.
3. Run the restore.

**If you have neither**

STOP. The data is unrecoverable from this destination. Escalate.

---

### 1.5 System-snapshot restore (OS-level rebuild)

Snapshots capture installed packages, enabled services, users, network
config, and other host metadata. They are not a filesystem backup — they
generate a shell script that reproduces the host's OS state after a fresh
install.

**Procedure**

1. Transfer the snapshot JSON from `/opt/backup-handler/snapshots/` to the
   freshly installed host.
2. Generate the restore script:
   ```
   backup-handler --restore-snapshot snapshot_YYYYMMDD_HHMMSS.json
   ```
3. **Read the generated script before running it.** It will install
   packages, create users, and modify system config.
4. Execute:
   ```
   chmod +x snapshot_*_restore.sh
   sudo ./snapshot_*_restore.sh
   ```
5. Then run the file restore (section 1.1) to populate `/home`, `/srv`,
   etc.

---

## 2. Failure triage

### 2.1 `backup-handler.service` failed (daily backup)

1. `journalctl -u backup-handler.service -n 200 --no-pager`
2. Match on the exit code.
   - **Config error** → fix `config/config.ini`, run
     `backup-handler --show-setup` to confirm, trigger a retry with
     `systemctl start backup-handler.service`.
   - **Mount point not mounted** → `mount -a`, verify
     `mountpoint /mnt/<dest>`, retry.
   - **Network / SSH / S3 transient** → retry once. If it fails twice in a
     row, escalate.
   - **Encryption failure** → check `[ENCRYPTION]` credentials are readable
     by the `backup` user. **Do not disable encryption to "make it work".**
3. Once a run succeeds, confirm with:
   ```
   backup-handler --status
   ```

### 2.2 `backup-handler-drill.service` failed (restore drill)

This is **more important than a failed backup**. A failed drill means the
backup is untrusted, even if nightly backups keep writing.

1. Check the drill exit code in the journal:
   - `1` — config missing / no manifests. Likely the backup never ran or
     the disk isn't mounted. Go to 2.1.
   - `2` — restore itself failed. The backup is corrupt or unreadable.
     **Escalate to primary owner.** Do not clear the alert.
   - `3` — verify failed, restore produced zero files, or checksums
     mismatch. Same severity as `2`. **Escalate.**
   - `4` — drill passed but the webhook notification failed. The backup is
     fine; fix the webhook at your leisure.
2. Preserve `/tmp/backup-drill/.restore.log` and `.verify.log` before the
   next drill run wipes them.

### 2.3 No alert, but you suspect a silent failure

Run the drill manually:
```
sudo systemctl start backup-handler-drill.service
journalctl -u backup-handler-drill.service -f
```

### 2.4 Preflight aborted with "destination is not a mountpoint" / "WRONG volume"

Preflight is the self-check that runs before every backup. It is what
turns the 2026-04-16 failure mode (16 days of silent zero-backups) into
a one-line abort with an alert.

**"is not a mountpoint and auto-mount failed"** — the destination disk
is unmounted and the script could not remount it. Usual causes:
1. The disk is genuinely unplugged or failed. Check `lsblk` and `dmesg`.
2. The sudoers rule for `mount` is missing. Verify with:
   ```
   sudo -n -l | grep mount
   ```
   If empty, install the rule from §5 below.
3. There is no fstab entry for the mountpoint. Either run with
   `[PREFLIGHT] ensure_fstab = True` or add one manually:
   ```
   echo "LABEL=DATA /mnt/data xfs defaults,nosuid,nodev,nofail,x-systemd.device-timeout=30 0 2" | sudo tee -a /etc/fstab
   ```

**"is mounted from the WRONG volume"** — `/mnt/data` is mounted but the
device label/UUID does not match the expected value. This is fatal by
design — refusing to write avoids corrupting the wrong disk. Check the
expected value in `[PREFLIGHT]` and reconcile with `lsblk -o NAME,LABEL,UUID,MOUNTPOINTS`.

**"STALE: last successful backup Nh ago"** — non-fatal warning surfaced
by preflight at the start of every run. If the previous N runs failed
silently (preflight off, sentinel writes blocked, alerts disabled),
this is the next run that screams loudly and reaches `local_mail_to`
even when DNS/Telegram are down.

### 2.5 Heartbeat ("dead-man's-switch") alert fired

If the external watchdog (healthchecks.io, Dead Man's Snitch, Uptime Kuma)
pages about a missed ping, the host either never ran a backup or every run
failed. Unlike webhook/telegram/email alerts, the heartbeat is the one
thing that fires when the host is off, the unit is disabled, or the
network is partitioned before the run even starts.

1. SSH to the host. If you can't, that is already the answer — it's off.
2. `systemctl status backup-handler.timer` — confirm the timer is enabled
   and active. If `inactive`, `systemctl enable --now backup-handler.timer`.
3. `journalctl -u backup-handler.service --since "48 hours ago"` — find
   the last attempted run and the exit code, then follow section 2.1.

---

## 3. Operational checks

### 3.1 Daily

- Drill has run in the last 7 days. Check:
  ```
  systemctl list-timers backup-handler-drill.timer
  ```
- Last backup is within SLA. Check:
  ```
  backup-handler --status
  ```

### 3.2 Weekly

- Read the latest 3 drill journals end-to-end. The drill passing is not
  enough; verify that "restored N files" is non-trivial.
- Rotate any credentials whose rotation window has elapsed.

### 3.3 Quarterly

- Execute a full-host restore (section 1.1) to a staging VM and validate
  that the host is functional. Do this even if weekly drills pass —
  a checksum-verified file tree is not the same as a working host.
- Review this runbook. Update the escalation contacts.

---

## 4. Preflight self-heal: required system configuration

Preflight self-heal needs explicit OS-level permissions. None of these
are installed by default — the package ships them as templates for
operators to review and apply.

### 4.1 Sudoers rule (required for auto_mount)

```
# /etc/sudoers.d/backup-handler   (mode 0440, owner root:root)
# Allow the backup user to mount the data volume.
# NOPASSWD is required because cron has no terminal.
backup-handler ALL=(root) NOPASSWD: /usr/bin/mount /mnt/data
backup-handler ALL=(root) NOPASSWD: /usr/bin/mount LABEL=DATA /mnt/data
backup-handler ALL=(root) NOPASSWD: /usr/bin/mount UUID=<your-uuid> /mnt/data
```

Do **not** add a `NOPASSWD: /usr/bin/tee -a /etc/fstab` rule or a
`UUID=*` wildcard. Being able to append arbitrary fstab lines (for
example a `user,suid` entry) or mount any device at the destination is
equivalent to root. Run `--install` once as root instead; it writes the
fstab entry itself.

Validate before saving with `visudo -cf /etc/sudoers.d/backup-handler`.
If the file is malformed `sudo` itself stops working — never edit it
without `visudo`.

### 4.2 fstab entry (required so the disk comes back on reboot)

```
LABEL=DATA  /mnt/data  xfs  defaults,nosuid,nodev,nofail,x-systemd.device-timeout=30  0  2
```

The `nofail` option is non-negotiable — without it, an unhealthy data
disk blocks boot and you lose remote access. With it, the system comes
up degraded, preflight detects the missing volume, and the alert fires.

### 4.3 Local MTA (required for `local_mail_to` fallback)

```
sudo apt install -y bsd-mailx postfix     # postfix in "Local only" mode
echo "test" | mail -s "preflight test" root
```

`local_mail_to = root` works because postfix delivers `root@localhost`
without a single DNS query. This is the channel that survives the
exact failure mode that broke us in April: api.telegram.org unreachable.

### 4.4 Verifying the chain end-to-end

```
sudo umount /mnt/data
backup-handler --dry-run
# expect: "Preflight: mounted /mnt/data via: sudo -n mount LABEL=DATA /mnt/data"
mountpoint /mnt/data && echo "OK, healed"
```

If the dry run aborts with exit code 2, the sudoers / fstab / MTA chain
is misconfigured — fix it before relying on the next scheduled run.

---

## 5. Safety rules

1. **Never restore on top of a live production path.** Always restore to a
   scratch directory and copy in.
2. **Never disable encryption or verification "to make it work".** If the
   backup can't be read, that is the incident.
3. **Never delete the oldest backup manifest until the newest one has been
   drill-verified.**
4. **Never edit `config/config.ini` on a failing host without committing
   the change to the config repo first.** Untracked config drift is how
   silent failures survive for months.
5. If a command in this runbook doesn't match the system's current
   behavior, the runbook is wrong — update it in the same PR as the fix.
