# sys/backups/config — config-as-code snapshot (DR-1)

Captures the box's **non-regenerable identity** — the config that makes *this box this
box* — into `/var/backups/config-snapshot/` so the disaster-recovery picture is
complete: the off-box sync already carries the irreplaceable **bytes**
(`production/` renders+blends, the DB snapshots); this carries the **config**.

| Piece | Source | In snapshot |
|---|---|---|
| nginx vhosts + core conf | `/etc/nginx` (sites-available + sites-enabled + nginx.conf + conf.d) | `nginx/` |
| Cloudflare tunnel | `/etc/cloudflared/config.yml` **+ the `<UUID>.json` tunnel credential** | `cloudflared/` |
| farm systemd units | locally-defined `/etc/systemd/system/*.{service,timer}` and their drop-in directories (not vendor symlinks) | `systemd/` |
| crontabs | `crontab -l` for the owner + root | `crontab/` |
| python envs (recipe) | `conda env export` for the `api` + `distribution` envs | `conda/` |
| API uv recovery | committed API and upstream runtime recipes + accepted API receipt, lock and 147 wheel files | `uv/api/` |
| ComfyUI / Stable Diffusion uv recovery | hash-verified local wheels referenced by the committed upstream locks | `uv/api/upstream/` |
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
6. **systemd units.** Copy the captured base units and `*.service.d` / `*.timer.d` directories to `/etc/systemd/system/`,
   `sudo systemctl daemon-reload`, then `enable --now` the timers/services you need
   (farm-api, farm-manager, websocket, comfyui-worker, flamenco-manager,
   the dbops + backup timers, and this `farm-config-snapshot.timer`).
7. **crontab.** Reinstall the owner's jobs: `crontab -u gradywoodruff crontab/gradywoodruff.cron`.
8. **Verify.** `systemctl --failed`, `nginx -t`, hit the public hostnames, run
   `sys/dbops/scripts/health-check.sh`, and take a fresh config snapshot.

> A full disk image is deliberately **not** the model: ~870 GB of `code/` + conda is
> re-creatable from the recipes above. We back up the irreplaceable bytes + this
> config recipe, not the whole filesystem. See `../README.md` §2.


## API uv recovery capture

`capture-api-uv.py` uses system Python with `-I -S` and imports no API or installed
packages. It captures the committed API revision's `.python-version`, runtime
helpers (including `scripts/upstream_runtime.*`), `deploy/runtime`,
`deploy/upstream`, native-build input recipes, deployment configuration,
`lib/testing` validation/build helpers and deployment documentation into
`uv/api/api-recipes.tar`. `capture.json` records the exact commit and archive hash.
Uncommitted API edits are deliberately excluded.

The accepted bundle currently lives at
`/srv/farm/.uv/migrations/2026-10-01-api-audit/artifact-set.2obcd4ez`.
Its `receipt.json`, `requirements.lock` and all 147 reviewed wheels are copied into
`uv/api/`, alongside the committed `artifacts.json`. The wheel bytes need about
2.51 GiB once; subsequent runs hash-check source and backup, retaining unchanged
independent destination files. Corrupt copies are replaced. No hardlinks to the
source are created, and the retained uv subtree is excluded from the shell's
recursive permission changes. The helper creates directories/files with 700/600
permissions and replaces a linked destination before changing file permissions.
Conda exports remain as rollback recipes.

The same capture preserves the local wheels required by the committed ComfyUI
and Stable Diffusion locks. These include recovered native extensions that cannot
be replaced by an arbitrary fresh source build. The only accepted source roots
are `/srv/farm/.uv/migrations/2026-10-02-comfyui/wheels` and
`/srv/farm/.uv/migrations/2026-10-02-stable-diffusion/wheels`; files must have a
single SHA-256 in their lock, remain inside the corresponding canonical root and
be regular files. Symlink sources or directories are refused. Each wheel is
copied using the same independent-file verification as the API wheelhouse.
Nothing is downloaded, installed or imported.

Copies live under `uv/api/upstream/<profile>/wheelhouse/`, preserving nested paths
such as ComfyUI's `mode-preserved/`. `capture.json` records each original path,
backup-relative filename, hash, byte count and referring lock, plus the lock
hashes and copied/reused counts. Only lock-referenced wheels are added; older
upstream copies may remain but are not part of the current recorded recovery set.
At introduction this is 28 ComfyUI wheels and 5 Stable Diffusion wheels. Published
package-index and HTTPS artifacts are represented by the committed hash locks;
their wheels are not duplicated by this upstream capture. The recovery set is
therefore not a fully offline installer.

A failed capture is logged as DR-critical. When a capture has started but fails,
`uv/api/capture.json` remains `status: incomplete`; require `status: passed` and no
uv warning in the top-level manifest before considering this recovery set usable.
The existing snapshot command still exits zero for warnings; inspect its report.
The snapshot includes the accepted API wheel set and locked local upstream
wheels, not installed environments,
model weights, managed Python itself, or the uv executable. Restore the reviewed
Python 3.9.18 and uv 0.8.19 prerequisites separately at the paths recorded in the
runtime scripts, along with the OS libraries/drivers, API source and application
data. Extract the archived recipes into a separate review directory, compare them
against the recorded API commit, then restore the bundle to its recorded absolute
path. Run `sh scripts/runtime.sh sync` and `check` as the Farm owner before
starting the restored services. Never restore by copying a relocatable `.venv`.

For ComfyUI or Stable Diffusion, recover the upstream checkout, custom nodes or
extensions and models separately. Restore each recorded upstream wheel beneath
its exact original `wheel_root`, preserving the recorded relative filename and
verifying its SHA-256. The archive includes the runtime manifest, baseline,
payload checks, lock, validation helpers and deployment guide. Install that
profile's specified managed Python and uv prerequisites, then run
`sh scripts/upstream_runtime.sh <profile> sync` and `check` from the restored API
checkout with the application stopped. Review the captured profile deployment
guide before starting services. Backup capture does not imply those staged
runtime migrations have passed live GPU acceptance or been deployed.

Validation of this change uses temporary fixture files, not the root snapshot:

```sh
/usr/bin/python3 -I -S -B config/test_capture_api_uv.py
bash -n config/snapshot.sh
```

After release, the existing timer automatically reads the updated script; no
daemon reload is needed. To request and inspect a new local capture now:

```sh
sudo systemctl start farm-config-snapshot.service
sudo cat /var/backups/config-snapshot/uv/api/capture.json
sudo cat /var/backups/config-snapshot/manifest.json
```

These files remain a local backup until an encrypted off-box transfer is configured
and verified. This change does not activate or verify such a transfer.
