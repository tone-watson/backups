#!/bin/bash
#
# config-snapshot — capture the box's NON-REGENERABLE identity (config-as-code).
#
# WHAT this is for (DR-1): off-box backup already covers the irreplaceable BYTES
# (production/ renders+blends and the DB snapshots). It does NOT cover the config
# that makes THIS box this box. If the disk dies, those bits live only in /etc and
# in runtime recipes. This script snapshots them into a single dir so the off-box
# push can carry them too, making a rebuild a checklist instead of archaeology.
#
# WHAT it captures (into $SNAP_DIR):
#   nginx/        — /etc/nginx (sites-available + sites-enabled + nginx.conf + conf.d)
#   cloudflared/  — /etc/cloudflared/config.yml AND the tunnel credential *.json
#                   *** SENSITIVE *** losing the credential forces a NEW tunnel and
#                   re-binding every DNS hostname in the Cloudflare dashboard.
#   systemd/      — the farm-relevant, locally-defined /etc/systemd/system units
#                   (*.service / *.timer that are real files, not vendor symlinks)
#   crontab/      — `crontab -l` for the owner (and root), the recipe not the runtime
#   uv/api/       — committed API runtime/build recipes + verified recovery wheels
#                   (independent copies, retained and hash-checked between runs)
#   private/stable-diffusion/ — exact service.env used by the uv service override
#                   *** SENSITIVE *** independent root-only 0600 copy
#   apt-list.txt  — `apt list --installed` (the package recipe)
#   farm-symlink.txt, manifest.json — small provenance/metadata
#
# *** SENSITIVITY / PERMS ***  The snapshot contains a private tunnel credential.
# $SNAP_DIR is created 700 root-only and the credential copy is forced to 600. Do
# NOT loosen these, and treat any off-box copy as secret (see README — prefer an
# encrypted remote for this dir; do not push the credential to a plaintext store).
#
# *** MUST RUN AS ROOT ***  /etc/cloudflared is root-only (0700). Run via the
# systemd unit (which runs as root) or `sudo`. See ../systemd/farm-config-snapshot.*
#
# This script is READ-ONLY against the system: it reads /etc and reviewed uv
# artifacts, lists apt, and writes solely under $SNAP_DIR. It changes nothing it captures.

set -uo pipefail


RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'


SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SNAP_DIR="/var/backups/config-snapshot"
LOG_FILE="/var/log/farm-config-snapshot.log"
STAMP_FILE="/var/log/farm-config-snapshot.stamp"
OWNER_USER="gradywoodruff"

NGINX_SRC="/etc/nginx"
CLOUDFLARED_SRC="/etc/cloudflared"
SYSTEMD_SRC="/etc/systemd/system"
FARM_LINK_SRC="/usr/local/bin/farm"

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"


WARNINGS=()
CAPTURED=()
FAILURES=()

log() {
    local level="$1"; shift
    local msg="$*"
    echo -e "$msg"
    if [ -w "$LOG_FILE" ] || [ -w "$(dirname "$LOG_FILE")" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $(echo -e "$msg" | sed 's/\x1b\[[0-9;]*m//g')" >> "$LOG_FILE" 2>/dev/null || true
    fi
}

warn() { WARNINGS+=("$1"); log "WARN" "  ${YELLOW}⚠ $1${NC}"; }
ok()   { CAPTURED+=("$1"); log "INFO" "  ${GREEN}✓ $1${NC}"; }


crit() { FAILURES+=("$1"); warn "DR-critical: $1"; }



write_manifest() {
    /usr/bin/python3 -I -S -B - "$SNAP_DIR" "$TIMESTAMP" \
        "$(hostname 2>/dev/null || echo unknown)" "$1" \
        "${#CAPTURED[@]}" "${CAPTURED[@]}" \
        "${#WARNINGS[@]}" "${WARNINGS[@]}" \
        "${#FAILURES[@]}" "${FAILURES[@]}" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

directory, timestamp, host, status = sys.argv[1:5]
arguments = iter(sys.argv[5:])
captured, warnings, failures = [[next(arguments) for _ in range(int(next(arguments)))]
                                for _ in range(3)]
receipt = {"schema": "farm.config-snapshot/1", "status": status,
           "timestamp": timestamp, "host": host, "snapshot_dir": directory,
           "captured": captured, "warnings": warnings, "warning_count": len(warnings),
           "failures": failures, "failure_count": len(failures)}
descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=directory)
try:
    with os.fdopen(descriptor, "w") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, Path(directory) / "manifest.json")
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

write_success_stamp() {
    /usr/bin/python3 -I -S -B - "$STAMP_FILE" <<'PY'
import os
from pathlib import Path
import sys
import tempfile
import time

destination = Path(sys.argv[1])
descriptor, temporary = tempfile.mkstemp(prefix=".config-snapshot-stamp-", dir=destination.parent)
try:
    with os.fdopen(descriptor, "w") as stream:
        stream.write(str(int(time.time())) + "\n")
        stream.flush()
        os.fchmod(stream.fileno(), 0o644)
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}


if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    log "ERROR" "${RED}This script must run as root (it reads root-only /etc/cloudflared). Use sudo or the systemd unit.${NC}"
    exit 1
fi

log "INFO" "${GREEN}=== Config snapshot starting ($TIMESTAMP) ===${NC}"



if [ -L "$SNAP_DIR" ] || ! mkdir -p "$SNAP_DIR" \
    || ! chown root:root "$SNAP_DIR" || ! chmod 700 "$SNAP_DIR"; then
    log "ERROR" "${RED}Cannot prepare root-only snapshot directory.${NC}"
    exit 1
fi

if ! exec {SNAPSHOT_LOCK}<"$SNAP_DIR" || ! flock -n "$SNAPSHOT_LOCK"; then
    log "ERROR" "${RED}Cannot lock snapshot directory; another capture may be running.${NC}"
    exit 1
fi
if ! write_manifest incomplete; then
    log "ERROR" "${RED}Cannot record an incomplete capture; snapshot contents were not replaced.${NC}"
    exit 1
fi


for sub in nginx cloudflared systemd crontab conda private; do
    rm -rf "${SNAP_DIR:?}/$sub" || crit "cannot clear snapshot subtree: $sub"
    mkdir -p "$SNAP_DIR/$sub" || crit "cannot create snapshot subtree: $sub"
done


log "INFO" "${CYAN}Capturing nginx config...${NC}"
if [ -d "$NGINX_SRC" ]; then



    for item in sites-available sites-enabled nginx.conf conf.d cors-headers.conf mime.types; do
        if [ -e "$NGINX_SRC/$item" ] || [ -L "$NGINX_SRC/$item" ]; then
            cp -aL "$NGINX_SRC/$item" "$SNAP_DIR/nginx/" 2>/dev/null \
                && ok "nginx/$item" || crit "nginx/$item copy failed"
        elif [[ "$item" == sites-available || "$item" == sites-enabled || "$item" == nginx.conf ]]; then
            crit "nginx/$item not found"
        fi
    done
else
    crit "nginx: $NGINX_SRC not found"
fi


log "INFO" "${CYAN}Capturing cloudflared config + tunnel credential (SENSITIVE)...${NC}"
if [ -d "$CLOUDFLARED_SRC" ]; then
    if [ -f "$CLOUDFLARED_SRC/config.yml" ]; then
        cp -a "$CLOUDFLARED_SRC/config.yml" "$SNAP_DIR/cloudflared/" 2>/dev/null \
            && ok "cloudflared/config.yml" || crit "cloudflared/config.yml copy failed"
    else
        crit "cloudflared/config.yml not found"
    fi

    cred_count=0
    shopt -s nullglob
    for cred in "$CLOUDFLARED_SRC"/*.json; do
        [ -f "$cred" ] && [ ! -L "$cred" ] || continue
        cp -a "$cred" "$SNAP_DIR/cloudflared/" 2>/dev/null || { crit "credential $(basename "$cred") copy failed"; continue; }
        chmod 600 "$SNAP_DIR/cloudflared/$(basename "$cred")" \
            || { crit "credential $(basename "$cred") permissions failed"; continue; }
        cred_count=$((cred_count + 1))
    done
    shopt -u nullglob
    if [ "$cred_count" -gt 0 ]; then
        ok "cloudflared tunnel credential(s): $cred_count (chmod 600)"
    else
        crit "no cloudflared *.json credential found"
    fi
else
    crit "cloudflared: $CLOUDFLARED_SRC not found"
fi


log "INFO" "${CYAN}Capturing systemd units...${NC}"
unit_count=0
shopt -s nullglob
for unit in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.timer; do


    [ -f "$unit" ] && [ ! -L "$unit" ] || continue
    cp -a "$unit" "$SNAP_DIR/systemd/" 2>/dev/null && unit_count=$((unit_count + 1)) || crit "systemd/$(basename "$unit") copy failed"
done

for dropin in "$SYSTEMD_SRC"/*.service.d "$SYSTEMD_SRC"/*.timer.d; do
    [ -d "$dropin" ] && [ ! -L "$dropin" ] || continue
    cp -aL "$dropin" "$SNAP_DIR/systemd/" 2>/dev/null \
        && ok "systemd/$(basename "$dropin")" || crit "systemd drop-in copy failed: $(basename "$dropin")"
done
shopt -u nullglob
if [ "$unit_count" -gt 0 ]; then
    ok "systemd units: $unit_count locally-defined .service/.timer"
else
    crit "no locally-defined systemd units captured"
fi


log "INFO" "${CYAN}Capturing crontabs...${NC}"
if command -v crontab >/dev/null 2>&1; then
    for cron_user in "$OWNER_USER" root; do
        cron_error="$SNAP_DIR/crontab/.${cron_user}.error"
        if LC_ALL=C crontab -l -u "$cron_user" > "$SNAP_DIR/crontab/${cron_user}.cron" 2> "$cron_error"; then
            ok "crontab/${cron_user}.cron"
        elif grep -Fxq "no crontab for $cron_user" "$cron_error"; then
            echo "# no crontab for $cron_user at $TIMESTAMP" > "$SNAP_DIR/crontab/${cron_user}.cron" \
                || crit "crontab/$cron_user placeholder write failed"
            [ "$cron_user" = root ] || warn "no crontab for $cron_user (empty placeholder written)"
        else
            crit "crontab/$cron_user capture failed"
        fi
        rm -f "$cron_error" || crit "cannot clear crontab error file for $cron_user"
    done
else
    crit "crontab command not available"
fi


log "INFO" "${CYAN}Capturing API uv recovery recipes and verified wheels...${NC}"


if (umask 077; /usr/bin/python3 -I -S -B "$SCRIPT_DIR/capture-api-uv.py" "$SNAP_DIR/uv/api"); then
    ok "uv/api: committed recipes, receipt, lock and verified wheelhouse"
else
    crit "API uv recovery capture failed; inspect uv/api/capture.json and this log"
fi


log "INFO" "${CYAN}Capturing Stable Diffusion private service environment...${NC}"
if (umask 077; /usr/bin/python3 -I -S -B "$SCRIPT_DIR/capture-api-uv.py" \
        --stable-diffusion-env "$SNAP_DIR/private/stable-diffusion"); then
    ok "private/stable-diffusion/service.env (root-only 0600)"
else
    crit "Stable Diffusion private environment missing or capture failed"
fi


log "INFO" "${CYAN}Capturing apt installed list...${NC}"
if command -v apt >/dev/null 2>&1; then
    if apt list --installed > "$SNAP_DIR/apt-list.txt" 2>/dev/null && [ -s "$SNAP_DIR/apt-list.txt" ]; then
        ok "apt-list.txt ($(grep -c '/' "$SNAP_DIR/apt-list.txt" 2>/dev/null || echo '?') packages)"
    else
        crit "apt list --installed failed"
    fi
elif command -v dpkg >/dev/null 2>&1; then
    if dpkg -l > "$SNAP_DIR/apt-list.txt" 2>/dev/null && [ -s "$SNAP_DIR/apt-list.txt" ]; then
        ok "apt-list.txt (dpkg -l fallback)"
    else
        crit "dpkg -l failed"
    fi
else
    crit "neither apt nor dpkg available"
fi


ls -la "$FARM_LINK_SRC" > "$SNAP_DIR/farm-symlink.txt" 2>/dev/null \
    || warn "Farm launcher provenance unavailable"





find "$SNAP_DIR" -path "$SNAP_DIR/uv" -prune -o -exec chown -h root:root {} + \
    || crit "snapshot ownership lockdown failed"
find "$SNAP_DIR" -path "$SNAP_DIR/uv" -prune -o -type d -exec chmod 700 {} + \
    || crit "snapshot directory permissions failed"
find "$SNAP_DIR" -path "$SNAP_DIR/uv" -prune -o -type f -exec chmod 600 {} + \
    || crit "snapshot file permissions failed"

capture_status=passed
[ "${#FAILURES[@]}" -eq 0 ] || capture_status=incomplete
if ! write_manifest "$capture_status"; then
    log "ERROR" "${RED}Cannot publish capture receipt; success heartbeat was not updated.${NC}"
    exit 1
fi
if [ "${#FAILURES[@]}" -gt 0 ]; then
    log "ERROR" "${RED}=== Config snapshot incomplete — ${#FAILURES[@]} required capture(s) failed ===${NC}"
    log "ERROR" "Inspect $SNAP_DIR/manifest.json; success heartbeat was not updated."
    exit 1
fi




if ! write_success_stamp; then
    crit "success heartbeat could not be published"
    write_manifest incomplete || log "ERROR" "Cannot mark capture receipt incomplete."
    exit 1
fi


log "INFO" ""
if [ "${#WARNINGS[@]}" -eq 0 ]; then
    log "INFO" "${GREEN}=== Config snapshot complete — ${#CAPTURED[@]} item(s), 0 warnings ===${NC}"
    log "INFO" "Snapshot: $SNAP_DIR (root-only 700; credential 600)"
    exit 0
else
    log "INFO" "${YELLOW}=== Config snapshot complete with ${#WARNINGS[@]} warning(s) ===${NC}"
    for w in "${WARNINGS[@]}"; do log "WARN" "  • $w"; done
    log "INFO" "Snapshot: $SNAP_DIR (root-only 700; credential 600)"

    exit 0
fi
