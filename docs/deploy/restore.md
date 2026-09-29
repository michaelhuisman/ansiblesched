# Backup and restore

## Backups

The role installs a systemd timer, `lamplighter-backup.timer`, that runs a daily
`pg_dump` (custom format) at `lamplighter_backup_time` (UTC). The dumps are stored in
`/opt/lamplighter/backups/lamplighter-<timestamp>.dump` (0600, root). Dumps older than
`lamplighter_backup_keep_days` are deleted.

```bash
systemctl list-timers lamplighter-backup.timer      # next and last run
sudo systemctl start lamplighter-backup.service     # backup now
journalctl -u lamplighter-backup.service            # output of the last runs
```

Every attempt is recorded in the database and exposed on `/metrics`:

| Metric | Meaning |
|---|---|
| `lamplighter_maintenance_last_success_timestamp_seconds{task="backup"}` | last successful backup |
| `lamplighter_maintenance_last_attempt_failed{task="backup"}` | 1 if the last attempt failed |

The same metrics exist for `task="retention"`. A sensible alert:

```yaml
- alert: LamplighterBackupMissing
  expr: time() - lamplighter_maintenance_last_success_timestamp_seconds{task="backup"} > 26 * 3600
```

The backups stay on the host. Copy them elsewhere with `lamplighter_backup_post_command`,
which runs on the host as root after every successful dump, with `BACKUP_FILE` set:

```yaml
lamplighter_backup_post_command: rclone copy "$BACKUP_FILE" offsite:lamplighter/
```

## Restore with the playbook (recommended)

```bash
cd deploy
ansible-playbook restore.yml -i <inventory> -e lamplighter_restore_file=lamplighter-20260928T023000Z.dump
```

The playbook:

1. makes a safety backup of the current database (skip with
   `-e lamplighter_restore_safety_backup=false`);
2. stops api, scheduler and workers. Workers finish their current run first (at most
   `lamplighter_worker_stop_grace`);
3. replaces the `public` schema with the contents of the dump;
4. runs the migrations, so an older dump is upgraded to the current schema;
5. starts everything again and waits for `/readyz`.

Runs that were `running` in the dump are marked `error` ("worker lost") by the reaper
after the restore.

## Restore by hand

On the host, as root:

```bash
cd /opt/lamplighter
docker compose stop api scheduler worker-a worker-b
docker compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U lamplighter -d lamplighter \
    -c "DROP SCHEMA public CASCADE" -c "CREATE SCHEMA public"
docker compose --profile backup run --rm -T --entrypoint pg_restore backup \
    --no-owner --single-transaction --exit-on-error -d lamplighter /backups/<file>.dump
docker compose run --rm -T --no-deps migrate
docker compose up -d --no-deps api scheduler worker-a worker-b
```

## Moving to a new host

Install the new host with the role, copy the dump to `/opt/lamplighter/backups/` on the
new host (0600, root) and run `restore.yml` there. Credentials are references to
OpenBao, so the new host needs its own AppRole secret ids but no other secrets.
