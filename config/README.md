# sys/backups/config — config-as-code snapshot (DR-1)

Captures the box's **non-regenerable identity** — the config that makes *this box this
box* — into `/var/backups/config-snapshot/` so the disaster-recovery picture is
complete: the off-box sync already carries the irreplaceable **bytes**
(`production/` renders+blends, the DB snapshots); this carries the **config**.

| Piece | Source | In snapshot |
|---|---|---|
| nginx vhosts + core conf | `/etc/nginx` (sites-available + sites-enabled + nginx.conf + conf.d) | `nginx/` |
| Cloudflare tunnel | `/etc/cloudflared/config.yml` **+ the `<UUID>.json` tunnel credential** | `cloudflared/` |
| farm systemd units | locally-defined `/etc/systemd/system/*.{service,timer}` (real files, not vendor symlinks) | `systemd/` |
| crontabs | `crontab -l` for the owner + root | `crontab/` |
| python envs (recipe) | `conda env export` for the `api` + `distribution` envs | `conda/` |
| OS packages (recipe) | `apt list --installed` | `apt-list.txt` |
| provenance | `/usr/local/bin/farm` symlink, capture manifest | `farm-symlink.txt`, `manifest.json` |

## ⚠ Sensitive — handle as a secret

The snapshot contains the **Cloudflare tunnel private credential** (`<UUID>.json`).
Losing it forces creating a **new** tunnel and re-binding **every** DNS hostname in
the Cloudflare dashboard — so we keep it, but it must stay secret:

- `/var/backups/config-snapshot` is created **`700` root-only**; every file is `600`,
  the credential explicitly so. **Do not loosen these.**
- The snapshot **must run as root** (it reads root-only `/etc/cloudflared`).
- Any **off-box** copy of this dir is secret. Prefer an **encrypted** remote (e.g. an
  rclone `crypt` remote) — do **not** push the credential to a plaintext store.

## Run / install (owner, needs root)

The script writes files and changes nothing it captures, but it needs root to read the
tunnel credential. Install the unit + timer (mirrors the other `sys/backups` timers):

```bash
sudo cp /srv/farm/sys/backups/systemd/farm-config-snapshot.service /etc/systemd/system/
sudo cp /srv/farm/sys/backups/systemd/farm-config-snapshot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now farm-config-snapshot.timer
sudo systemctl start farm-config-snapshot.service   # take the first snapshot now
sudo cat /var/backups/config-snapshot/manifest.json # verify what was captured
```

Timer runs daily at **02:45**, ahead of the 03:00 dbops DB backup. Log:
`/var/log/farm-config-snapshot.log`.

### Getting it off-box

The existing `rclone/databases-to-dropbox.sh` only syncs `/var/backups/data/databases`.
To carry the config snapshot off-box too, push `/var/backups/config-snapshot` to an
**encrypted** remote (because of the credential), e.g.:

```bash
sudo rclone sync /var/backups/config-snapshot dropbox-crypt:farm/config-snapshot --transfers 2
```

(Set up a `crypt` remote wrapping `dropbox:` with `rclone config` first.)

---

## One-page bootstrap-restore order (fresh Ubuntu box)

Restore in dependency order. The snapshot is at `<SNAP>` (the recovered
`config-snapshot/` dir); run as root.

1. **OS + base packages.** Fresh Ubuntu, same major version. Re-create accounts:
   the `gradywoodruff` user (uid/gid as before — DB/file ownership depends on it).
   Reinstall packages from the recipe: review `apt-list.txt` and
   `sudo apt install <the farm-relevant ones>` (it's a list, not a lockfile).
2. **Repos + conda.** Re-clone the component repos under `/srv/farm` (see
   `/srv/farm/CLAUDE.md` for the layout). Install miniconda to `/srv/farm/miniconda3`
   (and the `~/.../miniconda3 -> /srv/farm/miniconda3` symlink). Re-create the envs
   from the recipes: `conda env create -f conda/api.yml` and `conda/distribution.yml`.
3. **Data.** Restore the irreplaceable bytes from the off-box copies *before*
   starting services: `/srv/farm/production` (Dropbox) and `/data/databases` via
   `sys/dbops` restore. Fix ownership (e.g. `pipeline.db` is `gradywoodruff:www-data`).
4. **nginx.** Copy `nginx/` back to `/etc/nginx` (sites-available + sites-enabled +
   nginx.conf + conf.d). `sudo nginx -t` then enable nginx.
5. **Cloudflare tunnel.** Copy `cloudflared/config.yml` and `cloudflared/<UUID>.json`
   to `/etc/cloudflared/` (credential `chmod 600`, dir root-only). This **reuses the
   existing tunnel** — no DNS re-binding needed. Enable `cloudflared`.
6. **systemd units.** Copy `systemd/*.{service,timer}` to `/etc/systemd/system/`,
   `sudo systemctl daemon-reload`, then `enable --now` the timers/services you need
   (farm-api, farmhand, farm-manager, websocket, comfyui-worker, flamenco-manager,
   the dbops + backup timers, and this `farm-config-snapshot.timer`).
7. **crontab.** Reinstall the owner's jobs: `crontab -u gradywoodruff crontab/gradywoodruff.cron`.
8. **Verify.** `systemctl --failed`, `nginx -t`, hit the public hostnames, run
   `sys/dbops/scripts/health-check.sh`, and take a fresh config snapshot.

> A full disk image is deliberately **not** the model: ~870 GB of `code/` + conda is
> re-creatable from the recipes above. We back up the irreplaceable bytes + this
> config recipe, not the whole filesystem. See `../README.md` §2.
