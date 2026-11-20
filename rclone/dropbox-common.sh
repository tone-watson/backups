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

# Set to 1 by run_dropbox_upload when an upload finished but skipped items after
# temporary errors only. The caller then exits successfully but must NOT advance
# its success heartbeat, so the freshness alert still fires if skips persist.
DROPBOX_UPLOAD_SKIPPED=0

# Read an rclone log on stdin and print "<temporary> <other>" ERROR-line counts.
# Temporary means a retry on the next run should succeed: a source file changed
# while it was uploading (Syncthing or Blender writing it), Dropbox returned a 5xx
# page or garbage instead of JSON, or the network dropped. Summary lines that only
# repeat earlier errors are ignored. Anything unrecognised counts as other.
classify_rclone_errors() {
    awk '
        function settle() {
            if (html) { if (server) temporary++; else other++ }
            html = server = 0
        }
        /^[0-9][0-9][0-9][0-9]\/[0-9][0-9]\/[0-9][0-9] / { settle() }
        html && /Error \(5[0-9][0-9]\)/ { server = 1 }
        / ERROR : / {
            line = tolower($0)
            if (line ~ /attempt [0-9]+\/[0-9]+ (failed|succeeded)|not deleting (files|directories) as there were io errors/) next
            # Dropbox error pages are HTML; the status code comes a few lines later.
            if (line ~ /<!doctype html>$/) { html = 1; next }
            if (line ~ /source file is being updated|corrupted on transfer|looking for beginning of value|too_many_requests|too_many_write_operations|internal_error|service unavailable|bad gateway|gateway timeout|status code 5[0-9][0-9]|connection reset by peer|i\/o timeout|tls handshake timeout|unexpected eof|context deadline exceeded|temporary failure in name resolution|network is unreachable/) temporary++
            else other++
        }
        END { settle(); print temporary + 0, other + 0 }'
}

run_dropbox_upload() {
    local duration="$1"
    shift
    local status capture temporary other

    echo "Dropbox upload budget: 1 MiB/s, one transfer, time limit $duration"

    capture="$(mktemp)"
    if timeout --kill-after=1m "$duration" rclone sync "$@" \
        --bwlimit 1M \
        --transfers 1 \
        --checkers 2 \
        --fast-list \
        --log-level INFO 2>&1 | tee "$capture"; then
        rm -f "$capture"
        return 0
    else
        status=${PIPESTATUS[0]}
    fi
    if [ "$status" -eq 0 ]; then
        rm -f "$capture"
        return 0
    fi

    read -r temporary other < <(classify_rclone_errors < "$capture")
    rm -f "$capture"
    # rclone exits 1 (uncategorised) or 5 (temporary) for these; a timeout (124),
    # usage error or fatal error always fails the run.
    if { [ "$status" -eq 1 ] || [ "$status" -eq 5 ]; } && [ "$temporary" -gt 0 ] && [ "$other" -eq 0 ]; then
        DROPBOX_UPLOAD_SKIPPED=1
        echo "WARNING: Dropbox upload skipped $temporary item(s) after temporary errors" \
            "(files changing during upload or Dropbox server errors); the next run retries them"
        return 0
    fi
    echo "ERROR: Dropbox upload failed (exit $status; 124 means time limit reached)"
    return "$status"
}
