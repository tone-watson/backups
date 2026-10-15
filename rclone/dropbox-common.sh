#!/bin/bash
# Shared upload budget for the Wi-Fi-only farm. Source from the backup scripts.

acquire_dropbox_lock() {
    local lock_file="${1:-/srv/farm/logs/farm-dropbox-upload.lock}"
    local status




    exec 9>>"$lock_file"
    if flock --exclusive --nonblock --conflict-exit-code 75 9; then
        return 0
    else
        status=$?
        if [ "$status" -ne 75 ]; then
            echo "ERROR: cannot acquire Dropbox upload lock (exit $status)"
            return "$status"
        fi
    fi
    echo "Waiting for the other Dropbox backup to finish..."
    flock --exclusive 9
}

run_dropbox_upload() {
    local duration="$1"
    shift
    local status

    echo "Dropbox upload budget: 1 MiB/s, one transfer, time limit $duration"


    if timeout --kill-after=1m "$duration" rclone sync "$@" \
        --bwlimit 1M \
        --transfers 1 \
        --checkers 2 \
        --fast-list \
        --log-level INFO; then
        return 0
    else
        status=$?
        echo "ERROR: Dropbox upload failed (exit $status; 124 means time limit reached)"
        return "$status"
    fi
}
