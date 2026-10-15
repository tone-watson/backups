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
REMOTE_NAME="dropbox"
DEST_ROOT="${REMOTE_NAME}:farm"
DEST="${DEST_ROOT}/databases"
ARCHIVE="${DEST_ROOT}/_archive/databases/$(date +%Y-%m-%d)"
LOG="/srv/farm/logs/farm-dropbox-db-sync.log"
STAMP="/srv/farm/logs/farm-dropbox-db-sync.stamp"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/dropbox-common.sh"


exec > >(tee -a "$LOG") 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Dropbox DB sync starting ==="
acquire_dropbox_lock

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





if [ -z "$(find "$SRC" -type f -print -quit 2>/dev/null)" ]; then
    echo "ERROR: source $SRC contains no files — refusing to sync (would empty the Dropbox backup)"
    exit 1
fi





REDIS_DUMP="${SRC}/redis-dump.rdb"
if ! command -v redis-cli >/dev/null 2>&1; then
    echo "WARN: redis-cli not found — skipping Redis snapshot (DB backup continues)"
elif timeout --kill-after=10s 5m redis-cli --rdb "$REDIS_DUMP" >/dev/null 2>&1 && [ -s "$REDIS_DUMP" ]; then
    echo "Redis snapshot written: $REDIS_DUMP ($(du -h "$REDIS_DUMP" | cut -f1))"
else
    echo "WARN: Redis snapshot failed (redis-cli --rdb) — shipping DBs without it"

fi



run_dropbox_upload 4h "$SRC" "$DEST" \
    --exclude '/.incomplete-*/**' \
    --exclude '/.backup-archive-*' \
    --exclude '/.backup.lock' \
    --backup-dir "$ARCHIVE"

date +%s > "$STAMP" 2>/dev/null || true
echo "=== $(date '+%Y-%m-%d %H:%M:%S') Dropbox DB sync complete ==="
