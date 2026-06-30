#!/bin/bash
#
# One-way, versioned, OFF-BOX backup of the database snapshots to Dropbox via rclone.
#
# WHAT it syncs: /var/backups/data/databases — the CONSISTENT snapshots dbops
# produces daily (sqlite3 .backup of every *.db). We sync these, NOT the live
# /data/databases, so we never copy a half-written WAL/locked state. dbops runs at
# 03:00; this timer runs at 04:00 to push the freshest snapshot off the box.
#
# WHY: /var/backups and /data/databases live on the SAME root LVM volume, so a
# disk/LVM/rm/ransomware event would destroy the DBs AND their only backups at
# once. This pushes them to Dropbox (farm/databases) as the survivable off-box copy.
# (See docs/evaluation/2026-06-29-deep-dive.md — C4.)
#
# SAFETY: read-only push from /var/backups. Replaced/deleted files are preserved
# under _archive/databases/<date>/ (--backup-dir), so a bad snapshot overwriting a
# good one on Dropbox stays recoverable. Dropbox also keeps native version history.
#
# Activation (one time): the "dropbox" rclone remote must already exist (it's the
# same one the production sync uses). Then install + enable the .timer — see
# ../systemd/farm-dropbox-db-sync.timer and ../README.md.

set -euo pipefail

SRC="/var/backups/data/databases"
REMOTE_NAME="dropbox"                       # must match the rclone remote you created
DEST_ROOT="${REMOTE_NAME}:farm"             # the Dropbox folder to back up into
DEST="${DEST_ROOT}/databases"
ARCHIVE="${DEST_ROOT}/_archive/databases/$(date +%Y-%m-%d)"
LOG="/srv/farm/logs/farm-dropbox-db-sync.log"
STAMP="/srv/farm/logs/farm-dropbox-db-sync.stamp"   # success heartbeat, read by dbops health-check.sh
NOTIFY="/srv/farm/sys/dbops/scripts/farm-notify.sh"  # shared best-effort Farmhand pager

# Log to file and stdout (systemd journal)
exec > >(tee -a "$LOG") 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Dropbox DB sync starting ==="

if ! command -v rclone >/dev/null 2>&1; then
    echo "ERROR: rclone is not installed. See sys/backups/README.md"
    exit 1
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE_NAME}:"; then
    echo "ERROR: rclone remote '${REMOTE_NAME}:' not configured. Run: rclone config"
    exit 1
fi

if [ ! -d "$SRC" ]; then
    echo "ERROR: source $SRC does not exist (has dbops produced a backup yet?)"
    exit 1
fi

# Fail closed on an EMPTY source. `rclone sync` makes DEST identical to SRC, so a
# transiently-empty source (a botched dbops run, over-aggressive retention, a bug)
# would otherwise wipe the live Dropbox backup folder. (--backup-dir would make it
# recoverable, but we refuse rather than rely on that.)
if [ -z "$(find "$SRC" -type f -print -quit 2>/dev/null)" ]; then
    echo "ERROR: source $SRC contains no files — refusing to sync (would empty the Dropbox backup)"
    exit 1
fi

# Redis snapshot (DR-3): the Redis task queue + API-key store are RDB-only and
# are in NO other backup. Dump a fresh RDB into the snapshot set so it ships
# off-box alongside the DBs. Best-effort: if redis-cli is missing or the SYNC
# fails, log and continue — a Redis hiccup must NOT abort the database backup.
REDIS_DUMP="${SRC}/redis-dump.rdb"
if ! command -v redis-cli >/dev/null 2>&1; then
    echo "WARN: redis-cli not found — skipping Redis snapshot (DB backup continues)"
elif redis-cli --rdb "$REDIS_DUMP" >/dev/null 2>&1 && [ -s "$REDIS_DUMP" ]; then
    echo "Redis snapshot written: $REDIS_DUMP ($(du -h "$REDIS_DUMP" | cut -f1))"
else
    echo "WARN: Redis snapshot failed (redis-cli --rdb) — shipping DBs without it"
    # DR-3: the Redis RDB (task queue + API-key store) is in NO other backup, so a
    # persistent failure means it silently stops being protected. Page (best-effort,
    # non-fatal — a Redis hiccup must still not abort the DB backup).
    [ -x "$NOTIFY" ] && "$NOTIFY" warning "Redis snapshot failed in off-box DB sync" \
        "redis-cli --rdb could not write ${REDIS_DUMP}. The Redis task queue + API-key store (RDB-only, no other backup) is shipping STALE or ABSENT off-box. Check redis-cli availability/auth." || true
fi

# --backup-dir: preserve replaced/deleted files. The snapshots are small (~35M),
# so no exclude list or rename tracking is needed.
rclone sync "$SRC" "$DEST" \
    --backup-dir "$ARCHIVE" \
    --transfers 4 \
    --checkers 8 \
    --fast-list \
    --log-level INFO

date +%s > "$STAMP" 2>/dev/null || true   # record off-box success time for freshness monitoring
echo "=== $(date '+%Y-%m-%d %H:%M:%S') Dropbox DB sync complete ==="
