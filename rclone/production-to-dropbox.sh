#!/bin/bash
#
# One-way, versioned backup of the production tree to Dropbox via rclone.
#
# SAFETY: this script ONLY READS /srv/farm/production and writes to the Dropbox
# remote. It NEVER writes to or deletes anything under /srv/farm/production, so
# it cannot conflict with Syncthing (which owns the local folder). Syncthing is
# the source of truth for the directory; this is a read-only push to the cloud.
#
# VERSIONING: instead of deleting/overwriting files on Dropbox, old versions are
# moved into a dated _archive/ folder (--backup-dir). So if a file is deleted on
# the Mac and Syncthing propagates that deletion to the server, the Dropbox copy
# is preserved under _archive/<date>/ rather than lost. (Dropbox also keeps its
# own native version history as a second safety net.)
#
# Activation (one time): see ../README.md  (install rclone, `rclone config` a
# remote named "dropbox", then enable the timer).

set -euo pipefail

SRC="/srv/farm/production"
REMOTE_NAME="dropbox"                       # must match the rclone remote you created
DEST_ROOT="${REMOTE_NAME}:farm"             # the Dropbox folder to back up into
DEST="${DEST_ROOT}/production"
ARCHIVE="${DEST_ROOT}/_archive/$(date +%Y-%m-%d)"
LOG="/var/log/farm-dropbox-sync.log"
STAMP="/srv/farm/logs/farm-dropbox-sync.stamp"   # success heartbeat, read by dbops health-check.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXCLUDE="${SCRIPT_DIR}/exclude.txt"
source "${SCRIPT_DIR}/dropbox-common.sh"

# Log to file and stdout (systemd journal)
exec > >(tee -a "$LOG") 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Dropbox sync starting ==="
acquire_dropbox_lock

if ! command -v rclone >/dev/null 2>&1; then
    echo "ERROR: rclone is not installed. See sys/backups/README.md"
    exit 1
fi

if ! rclone listremotes 2>/dev/null | grep -q "^${REMOTE_NAME}:"; then
    echo "ERROR: rclone remote '${REMOTE_NAME}:' not configured. Run: rclone config"
    exit 1
fi

# --backup-dir: preserve replaced/deleted files. --track-renames: cheap moves.
run_dropbox_upload 6h "$SRC" "$DEST" \
    --exclude-from "$EXCLUDE" \
    --backup-dir "$ARCHIVE" \
    --track-renames

date +%s > "$STAMP" 2>/dev/null || true   # record off-box success time for freshness monitoring
echo "=== $(date '+%Y-%m-%d %H:%M:%S') Dropbox sync complete ==="
