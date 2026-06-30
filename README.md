# sys/backups — offsite & content backups

Complements **`sys/dbops`** (which backs up the SQLite databases under `/data/databases`).
This component handles the **big, irreplaceable creative work** and **offsite** copies.

| What | Where | Tool |
|---|---|---|
| Databases (`/data/databases`) | local `/var/backups`, daily | **`sys/dbops`** |
| Creative work (`/srv/farm/production`, 138 GB) → Dropbox | offsite | **this repo** (`rclone/`) |
| Config-as-code snapshot (box identity) | local `/var/backups/config-snapshot`, daily | **`config/`** (`config/snapshot.sh`) |
| Full-system disaster recovery (bootstrap order) | documented | `config/README.md` + "Disaster recovery" below |

---

## 1. Dropbox one-way backup of `/srv/farm/production`

**The guarantee:** the sync script **only reads** `/srv/farm/production` and pushes to Dropbox. It **never writes to or deletes** anything in that folder, so it **cannot conflict with Syncthing**, which remains the sole owner of the local directory. (Syncthing keeps the server and your Mac in sync; this just copies the result up to the cloud.)

**Versioned, not just mirrored:** replaced/deleted files are moved into `dropbox:farm-backup/_archive/<date>/` (rclone `--backup-dir`) rather than deleted — so a deletion that propagates from the Mac through Syncthing doesn't wipe the Dropbox copy. Dropbox's own file-version history is a second safety net.

### One-time activation

> ⚠️ Do **not** use the `apt` rclone — Ubuntu ships 1.53 (2020), which gets a Dropbox token that expires in hours. Use the official installer (current version) on **both** the server and your Mac.

```bash
# --- ON THE SERVER ---
# 1) Install current rclone (replaces any apt version)
curl https://rclone.org/install.sh | sudo bash
rclone version            # should be v1.6x or newer, NOT 1.53

# --- ON YOUR MAC (it has a browser; the server doesn't) ---
# 2) Install current rclone
curl https://rclone.org/install.sh | sudo bash
# 3) Configure the Dropbox remote (browser auth happens automatically here)
rclone config
#    n (new remote) → name EXACTLY: dropbox → storage: dropbox
#    client_id: <Enter>   client_secret: <Enter>   advanced config: n
#    "Use auto config?": y  → browser opens → log in → Allow → y → q
# 4) Copy the resulting config to the server
ssh gradywoodruff@noiseflood.farm 'mkdir -p ~/.config/rclone'
scp ~/.config/rclone/rclone.conf gradywoodruff@noiseflood.farm:~/.config/rclone/

# --- BACK ON THE SERVER ---
# 5) Verify the remote works
rclone listremotes        # should list  dropbox:
rclone lsd dropbox:       # should list your Dropbox top-level folders

# 6) Enable the 6-hourly timer
sudo cp /srv/farm/sys/backups/systemd/farm-dropbox-sync.service /etc/systemd/system/
sudo cp /srv/farm/sys/backups/systemd/farm-dropbox-sync.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now farm-dropbox-sync.timer

# 7) Kick off the first (~138 GB) sync now and watch it
/srv/farm/sys/backups/rclone/production-to-dropbox.sh
```

The first run uploads ~138 GB (hours, depending on upstream bandwidth); subsequent runs only send changes. Log: `/var/log/farm-dropbox-sync.log`. Tune excludes in `rclone/exclude.txt`.

**Keep the backup off your Mac (important if your Dropbox is synced there).** rclone uploads to the cloud; the Dropbox *desktop app* would then try to download `farm/` to the Mac. Prevent that with Selective Sync — and do it *before* the first big sync:
1. Server: `rclone mkdir dropbox:farm`
2. Mac: when the empty `farm` folder appears, Dropbox app → Preferences → Sync → Selective Sync → uncheck `farm`.
3. Server: then run the first sync.

(Note: Dropbox's dedicated "Backups" section is device-managed and can't be targeted by rclone/the API — Selective Sync on a normal folder is the right approach.)

### Operate
```bash
farm run sync-now      # run a sync immediately
farm run sync-status   # timer status
farm run sync-logs     # tail the log
```

---

## 2. Disaster recovery (rebuild on new hardware) — _planned_

A single disk image isn't the right model on Linux (hardware/driver drift, and ~870 GB of `code/` + `miniconda3/` is re-creatable, not worth imaging). The tractable target = **data backup + config-as-code + a bootstrap script**:

1. **Data** — the irreplaceable bytes:
   - `/srv/farm/production/data` (renders/blends) → Dropbox (above).
   - `/data/databases` → dbops, plus push the dbops backups offsite too.
2. **Config-as-code** — a snapshot script that captures what makes this box *this box*.
   **Implemented:** `config/snapshot.sh` + `systemd/farm-config-snapshot.{service,timer}`
   write `/etc/nginx`, `/etc/cloudflared/config.yml` **+ the tunnel credential**, the
   farm `/etc/systemd/system` units, `crontab -l`, `conda env export` (api + distribution),
   `apt list --installed`, and the `/usr/local/bin/farm` symlink into
   `/var/backups/config-snapshot` (root-only `700`; credential `600`). See **`config/README.md`**
   for install + the one-page bootstrap-restore order. (The credential is SENSITIVE —
   push that dir off-box only via an **encrypted** remote.)
3. **Bootstrap** — a documented script that, on a fresh Ubuntu box, restores the above and re-clones the repos.

Tracked as a task; see `/srv/farm/docs/roadmap.md`.
