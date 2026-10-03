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

# Colors for output (matches sys/dbops conventions)
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SNAP_DIR="/var/backups/config-snapshot"
LOG_FILE="/var/log/farm-config-snapshot.log"
STAMP_FILE="/var/log/farm-config-snapshot.stamp"     # world-readable success heartbeat for health-check
OWNER_USER="gradywoodruff"

NGINX_SRC="/etc/nginx"
CLOUDFLARED_SRC="/etc/cloudflared"
SYSTEMD_SRC="/etc/systemd/system"

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

# Track per-capture outcome so one missing piece doesn't abort the whole snapshot
WARNINGS=()
CAPTURED=()

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
# crit: a warning that is ALSO DR-critical — the irreplaceable identity (cloudflared
# tunnel credential, nginx/cloudflared config) failed to capture; keep that distinction in the local log.
crit() { warn "DR-critical: $1"; }

# Must be root (needed to read /etc/cloudflared)
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    log "ERROR" "${RED}This script must run as root (it reads root-only /etc/cloudflared). Use sudo or the systemd unit.${NC}"
    exit 1
fi

log "INFO" "${GREEN}=== Config snapshot starting ($TIMESTAMP) ===${NC}"

# Create the snapshot dir, root-only (700). Recreate the per-run subdirs cleanly so
# a removed source file doesn't linger in the snapshot.
mkdir -p "$SNAP_DIR"
chown root:root "$SNAP_DIR"
chmod 700 "$SNAP_DIR"
# uv is retained separately; do not recopy unchanged wheels on every run.
# Clear the obsolete conda snapshot subtree too; retired runtimes are not exported.
for sub in nginx cloudflared systemd crontab conda private; do
    rm -rf "${SNAP_DIR:?}/$sub"
    mkdir -p "$SNAP_DIR/$sub"
done

# --- 1. nginx ---------------------------------------------------------------
log "INFO" "${CYAN}Capturing nginx config...${NC}"
if [ -d "$NGINX_SRC" ]; then
    # sites-available + sites-enabled are the task-critical bits; nginx.conf, conf.d
    # and the shared snippet(s) round out a working restore. -L deref'd so the
    # sites-enabled symlinks land as real files in the snapshot.
    for item in sites-available sites-enabled nginx.conf conf.d cors-headers.conf mime.types; do
        if [ -e "$NGINX_SRC/$item" ]; then
            cp -aL "$NGINX_SRC/$item" "$SNAP_DIR/nginx/" 2>/dev/null \
                && ok "nginx/$item" || warn "nginx/$item copy failed"
        fi
    done
else
    crit "nginx: $NGINX_SRC not found"
fi

# --- 2. cloudflared (SENSITIVE) --------------------------------------------
log "INFO" "${CYAN}Capturing cloudflared config + tunnel credential (SENSITIVE)...${NC}"
if [ -d "$CLOUDFLARED_SRC" ]; then
    if [ -f "$CLOUDFLARED_SRC/config.yml" ]; then
        cp -a "$CLOUDFLARED_SRC/config.yml" "$SNAP_DIR/cloudflared/" 2>/dev/null \
            && ok "cloudflared/config.yml" || crit "cloudflared/config.yml copy failed"
    else
        crit "cloudflared/config.yml not found"
    fi
    # The tunnel credential(s): <UUID>.json — the irreplaceable secret.
    cred_count=0
    while IFS= read -r cred; do
        [ -n "$cred" ] || continue
        cp -a "$cred" "$SNAP_DIR/cloudflared/" 2>/dev/null || { crit "credential $(basename "$cred") copy failed"; continue; }
        chmod 600 "$SNAP_DIR/cloudflared/$(basename "$cred")"
        cred_count=$((cred_count + 1))
    done < <(find "$CLOUDFLARED_SRC" -maxdepth 1 -name '*.json' -type f 2>/dev/null)
    if [ "$cred_count" -gt 0 ]; then
        ok "cloudflared tunnel credential(s): $cred_count (chmod 600)"
    else
        crit "no cloudflared *.json credential found"
    fi
else
    crit "cloudflared: $CLOUDFLARED_SRC not found"
fi

# --- 3. systemd units (farm-relevant, locally-defined) ----------------------
log "INFO" "${CYAN}Capturing systemd units...${NC}"
unit_count=0
shopt -s nullglob
for unit in "$SYSTEMD_SRC"/*.service "$SYSTEMD_SRC"/*.timer; do
    # Only real files: vendor/enabled symlinks point into /lib and are regenerated
    # by `systemctl enable`. The regular files here are THIS box's own unit defs.
    [ -f "$unit" ] && [ ! -L "$unit" ] || continue
    cp -a "$unit" "$SNAP_DIR/systemd/" 2>/dev/null && unit_count=$((unit_count + 1)) || warn "systemd/$(basename "$unit") copy failed"
done
# Preserve runtime overrides and delegated-resource settings as well as bases.
for dropin in "$SYSTEMD_SRC"/*.service.d "$SYSTEMD_SRC"/*.timer.d; do
    [ -d "$dropin" ] && [ ! -L "$dropin" ] || continue
    cp -aL "$dropin" "$SNAP_DIR/systemd/" 2>/dev/null \
        && ok "systemd/$(basename "$dropin")" || crit "systemd drop-in copy failed: $(basename "$dropin")"
done
shopt -u nullglob
if [ "$unit_count" -gt 0 ]; then
    ok "systemd units: $unit_count locally-defined .service/.timer"
else
    warn "no locally-defined systemd units captured"
fi

# --- 4. crontab (owner + root) ---------------------------------------------
log "INFO" "${CYAN}Capturing crontabs...${NC}"
if command -v crontab >/dev/null 2>&1; then
    if crontab -l -u "$OWNER_USER" > "$SNAP_DIR/crontab/${OWNER_USER}.cron" 2>/dev/null; then
        ok "crontab/${OWNER_USER}.cron"
    else
        echo "# no crontab for $OWNER_USER at $TIMESTAMP" > "$SNAP_DIR/crontab/${OWNER_USER}.cron"
        warn "no crontab for $OWNER_USER (empty placeholder written)"
    fi
    if crontab -l -u root > "$SNAP_DIR/crontab/root.cron" 2>/dev/null; then
        ok "crontab/root.cron"
    else
        echo "# no crontab for root at $TIMESTAMP" > "$SNAP_DIR/crontab/root.cron"
    fi
else
    warn "crontab command not available"
fi

# --- 5. reviewed API uv recovery artifacts ---------------------------------
log "INFO" "${CYAN}Capturing API uv recovery recipes and verified wheels...${NC}"
# Use the OS stdlib, not a runtime that may itself need restoration. The helper
# never imports API/packages, changes source files, or creates source hardlinks.
if (umask 077; /usr/bin/python3 -I -S -B "$SCRIPT_DIR/capture-api-uv.py" "$SNAP_DIR/uv/api"); then
    ok "uv/api: committed recipes, receipt, lock and verified wheelhouse"
else
    crit "API uv recovery capture failed; inspect uv/api/capture.json and this log"
fi

# --- 5b. exact Stable Diffusion private environment --------------------------
log "INFO" "${CYAN}Capturing Stable Diffusion private service environment...${NC}"
if (umask 077; /usr/bin/python3 -I -S -B "$SCRIPT_DIR/capture-api-uv.py" \
        --stable-diffusion-env "$SNAP_DIR/private/stable-diffusion"); then
    ok "private/stable-diffusion/service.env (root-only 0600)"
else
    crit "Stable Diffusion private environment missing or capture failed"
fi

# --- 6. apt installed list --------------------------------------------------
log "INFO" "${CYAN}Capturing apt installed list...${NC}"
if command -v apt >/dev/null 2>&1; then
    if apt list --installed > "$SNAP_DIR/apt-list.txt" 2>/dev/null && [ -s "$SNAP_DIR/apt-list.txt" ]; then
        ok "apt-list.txt ($(grep -c '/' "$SNAP_DIR/apt-list.txt" 2>/dev/null || echo '?') packages)"
    else
        warn "apt list --installed failed"
    fi
elif command -v dpkg >/dev/null 2>&1; then
    dpkg -l > "$SNAP_DIR/apt-list.txt" 2>/dev/null && ok "apt-list.txt (dpkg -l fallback)" || warn "dpkg -l failed"
else
    warn "neither apt nor dpkg available"
fi

# --- 7. small provenance bits ----------------------------------------------
ls -la /usr/local/bin/farm > "$SNAP_DIR/farm-symlink.txt" 2>/dev/null || true

# --- manifest --------------------------------------------------------------
captured_json=$(printf '"%s",' "${CAPTURED[@]}" | sed 's/,$//')
warnings_json=$(printf '"%s",' "${WARNINGS[@]}" | sed 's/,$//')
cat > "$SNAP_DIR/manifest.json" <<EOF
{
  "timestamp": "$TIMESTAMP",
  "host": "$(hostname 2>/dev/null || echo unknown)",
  "snapshot_dir": "$SNAP_DIR",
  "captured": [${captured_json}],
  "warnings": [${warnings_json}],
  "warning_count": ${#WARNINGS[@]}
}
EOF

# Lock down everything we just wrote: dir tree 700/600, no group/other access.
# uv files are independently created/repaired as 600 by capture-api-uv.py.
# Exclude that retained subtree even after capture failure: blindly chmod/chown
# on a pre-existing hardlink could change a source wheel's metadata.
find "$SNAP_DIR" -path "$SNAP_DIR/uv" -prune -o -exec chown -h root:root {} +
find "$SNAP_DIR" -path "$SNAP_DIR/uv" -prune -o -type d -exec chmod 700 {} +
find "$SNAP_DIR" -path "$SNAP_DIR/uv" -prune -o -type f -exec chmod 600 {} +

# Freshness heartbeat for monitoring: a world-readable success stamp OUTSIDE the
# 700 root-only snapshot dir (it leaks only a timestamp), so the unprivileged
# health-check can tell whether this DR snapshot is still running.
date +%s > "$STAMP_FILE" 2>/dev/null || true
chmod 644 "$STAMP_FILE" 2>/dev/null || true

# Summary
log "INFO" ""
if [ "${#WARNINGS[@]}" -eq 0 ]; then
    log "INFO" "${GREEN}=== Config snapshot complete — ${#CAPTURED[@]} item(s), 0 warnings ===${NC}"
    log "INFO" "Snapshot: $SNAP_DIR (root-only 700; credential 600)"
    exit 0
else
    log "INFO" "${YELLOW}=== Config snapshot complete with ${#WARNINGS[@]} warning(s) ===${NC}"
    for w in "${WARNINGS[@]}"; do log "WARN" "  • $w"; done
    log "INFO" "Snapshot: $SNAP_DIR (root-only 700; credential 600)"
    # Warnings are non-fatal (e.g. an optional capture missing). Exit 0 so the
    # timer doesn't mark itself failed for an expected-missing optional capture.
    exit 0
fi
