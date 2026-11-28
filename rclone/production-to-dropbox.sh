#!/bin/bash
#
# One-way versioned backup of the production tree to Dropbox via rclone.
#
# SAFETY: this script ONLY READS /srv/farm/production and writes to the Dropbox
# remote. It NEVER writes to or deletes anything under /srv/farm/production, so
# it cannot conflict with Syncthing (which owns the local folder). Syncthing is
# the source of truth for the directory; this is a read-only push to the cloud.
#
# VERSIONING: instead of deleting/overwriting files on Dropbox, old versions are
# moved into a dated _archive/ folder (--backup-dir). So if a file is deleted on
# the Mac and Syncthing propagates the deletion to the server, the Dropbox copy
# is preserved under _archive/<date>/ rather than lost. (Dropbox also keeps its
# own native version history as a second safety net.)
#
# Activation (one time): see ../README.md (install rclone, `rclone config` a
# remote named "dropbox", then enable the timer).

set -euo pipefail

SRC="/srv/farm/production"
THUMBNAILS_SRC="/srv/farm/assets/images/library"
REMOTE_NAME="dropbox"
DEST_ROOT="${REMOTE_NAME}:farm"
DEST="${DEST_ROOT}/production"
THUMBNAILS_DEST="${DEST_ROOT}/library-thumbnails"
ARCHIVE="${DEST_ROOT}/_archive/$(date +%Y-%m-%d)"
LOG="/var/log/farm-dropbox-sync.log"
STAMP="/srv/farm/logs/farm-dropbox-sync.stamp"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
EXCLUDE="${SCRIPT_DIR}/exclude.txt"
source "${SCRIPT_DIR}/dropbox-common.sh"


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


run_dropbox_upload 6h "$SRC" "$DEST" \
    --exclude-from "$EXCLUDE" \
    --backup-dir "$ARCHIVE" \
    --track-renames

# Library thumbnails are published by Manager on the Assets CDN, outside the production tree,
# so nothing else backs them up. The folder is created by the operator; skip until it exists.
if [ -d "$THUMBNAILS_SRC" ]; then
    run_dropbox_upload 30m "$THUMBNAILS_SRC" "$THUMBNAILS_DEST" \
        --backup-dir "${ARCHIVE}/_library-thumbnails"
fi

if [ "$DROPBOX_UPLOAD_SKIPPED" -eq 1 ]; then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') Dropbox sync finished with skipped items (heartbeat not advanced) ==="
    exit 0
fi

date +%s > "$STAMP" 2>/dev/null || true
echo "=== $(date '+%Y-%m-%d %H:%M:%S') Dropbox sync complete ==="
