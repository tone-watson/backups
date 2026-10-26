# sys/backups — offsite & content backups

Complements **`sys/dbops`** (which backs up the SQLite databases under `/data/databases`).
This component handles the **big, irreplaceable creative work** and **offsite** copies.

| What | Where | Tool |
|---|---|---|
| Databases (`/data/databases`) | local `/var/backups`, daily | **`sys/dbops`** |
| Creative work (`/srv/farm/production`, 138 GB) → Dropbox | offsite | **this repo** (`rclone/`) |
| Config-as-code snapshot (box identity) | local `/var/backups/config-snapshot`, daily | **`config/`** (`config/snapshot.sh`) |
| External-drive disaster recovery | prepared; real-drive acceptance pending | [`recovery/README.md`](recovery/README.md), [`recovery/RESTORE.md`](recovery/RESTORE.md) |

The recovery toolkit refreshes the server map, stages consistent state, creates
encrypted incremental snapshots, and restores to a separate tree on a fresh host.
Its portable scratch tests do not establish whole-server recovery acceptance.
See the [preparation record](/srv/farm/docs/guides/disaster-recovery/README.md)
for current status, capacity planning and the remaining operator checks.

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

### Wi-Fi upload budget

Production and database Dropbox jobs share one upload slot. Each run waits for
`/srv/farm/logs/farm-dropbox-upload.lock`, then uploads at **1 MiB/s** (about
8.4 Mbps), with **one transfer** and two checkers. This protects interactive
SSH/NFS traffic on the Wi-Fi-only server. The limit is deliberately conservative;
validate latency and backup completion before raising it in
`rclone/dropbox-common.sh`. Do not delete the lock file while jobs are running.

The lock covers manual script runs as well as both systemd services. Waiting jobs
are not silently skipped. Systemd does not create another instance of the same
service for timer ticks that occur while that service is already running.

The database sync ships only the compressed `backup_<stamp>.tar.gz` archives that
dbops writes at capture time; the uncompressed `backup_<stamp>/` directories are
excluded (they are kept locally for fast restores).

The scripts allow six hours of production upload time and four hours for database
uploads, **after acquiring the lock**. The Redis snapshot has a separate five-minute
limit. Failed or timed-out uploads do not update the success heartbeat. A 4.3 GiB
database upload needs about 73 minutes at this cap before checking/retry overhead;
the old one-hour service timeout is insufficient. Very large production backlogs
can take several scheduled runs. Monitor successful completion and the off-box
heartbeat; the cap does not establish that Wi-Fi driver crashes are fixed.

Existing services execute the repository scripts directly, so cap and lock changes
apply to the next run without a release or service restart. Install the updated
timeout policy once, before the next run (an active service must finish before
replacing its timeout assumptions):

```bash
sudo install -m 0644 /srv/farm/sys/backups/systemd/farm-dropbox-sync.service /etc/systemd/system/farm-dropbox-sync.service
sudo install -m 0644 /srv/farm/sys/backups/systemd/farm-dropbox-db-sync.service /etc/systemd/system/farm-dropbox-db-sync.service
sudo systemctl daemon-reload
systemctl show farm-dropbox-sync.service farm-dropbox-db-sync.service -p Id -p TimeoutStartUSec
```

Both should report `TimeoutStartUSec=infinity`. The per-upload limits still apply
inside the scripts. Leave the existing timers enabled; no service or network
restart is required, and these operator steps do not depend on `farm release`.

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
   farm `/etc/systemd/system` units and drop-ins, `crontab -l`, API uv recovery
   recipes and the reviewed wheelhouse, ComfyUI/Stable Diffusion/Graphiti/
   ACE-Step/LivePortrait/Flood Map/TikTok Scraper/Hardware/Noise Agent/
   InstantMesh/Riffusion/RAVE
   runtime recipes and their hash-locked local wheels (Graphiti's two profiles
   share one verified 55-wheel bundle; ACE-Step adds 164 wheels, LivePortrait 122
   Flood Map 29, TikTok Scraper 13, Hardware 18, Noise Agent 68, InstantMesh 123
   Riffusion 147 and RAVE 90),
   the exact private Stable Diffusion service environment,
   `apt list --installed`, and the `/usr/local/bin/farm` symlink into
   `/var/backups/config-snapshot` (root-only `700`; credential `600`). See **`config/README.md`**
   for install + the one-page bootstrap-restore order. Separate verified archives
   retain the existing managed Python 3.9.18 bundle plus six additional versions:
   3.9.19, 3.10.14, 3.10.16, 3.10.18, 3.10.19 and 3.12.11.
   Python 3.10.14's scratch restore has an explicit 168-entry group-ownership
   qualification. The uv executable, separate CUDA toolkit/header bundles, model
   weights and application source still require separate recovery; this does not
   establish full generation recovery. See the
   [managed interpreter guide](/srv/farm/sys/api/docs/deployment/uv-managed-python-recovery.md).
   (The credential is SENSITIVE —
   push that dir off-box only via an **encrypted** remote.)
3. **Bootstrap** — a documented script that, on a fresh Ubuntu box, restores the above and re-clones the repos.

Tracked as a task; see `/srv/farm/docs/roadmap.md`.
