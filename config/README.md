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
| API uv recovery | committed API and upstream runtime recipes + accepted API receipt, lock and 147 wheel files | `uv/api/` |
| ComfyUI / Stable Diffusion / Graphiti / ACE-Step uv recovery | hash-verified local wheels referenced by the committed upstream locks; both Graphiti profiles share one wheel bundle | `uv/api/upstream/` |
| Stable Diffusion service credential | exact `/srv/farm/private/stable-diffusion/service.env` file | `private/stable-diffusion/service.env` |
| OS packages (recipe) | `apt list --installed` | `apt-list.txt` |
| provenance | `/usr/local/bin/farm` symlink, capture manifest | `farm-symlink.txt`, `manifest.json` |

## ⚠ Sensitive — handle as a secret

The snapshot contains the **Cloudflare tunnel private credential** (`<UUID>.json`)
and the **Stable Diffusion API credential** in its service environment file.
Losing the tunnel credential forces creating a **new** tunnel and re-binding **every** DNS hostname in
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
2. **Repos + runtimes.** Re-clone the component repos under `/srv/farm` (see
   `/srv/farm/CLAUDE.md` for the layout), retaining the recorded release revisions.
   Restore uv and the specified managed Python installations. Restore the API's
   reviewed artifact bundle from `uv/api/` as described below, then, as the Farm
   owner in `/srv/farm/sys/api`, run `sh scripts/runtime.sh sync` and
   `sh scripts/runtime.sh check`. For Distribution, use its committed
   `pyproject.toml`, `uv.lock`, `.python-version` and `scripts/runtime.sh`: as the
   owner in `/srv/farm/sys/distribution`, run `sh scripts/runtime.sh sync` and
   `sh scripts/runtime.sh check`. This selects managed Python 3.11.13 and creates
   the project `.venv`. Keep services stopped while restoring runtimes.
   API and Distribution restore from their reviewed uv recipes; their retired
   Conda environments are neither exported nor required for recovery.
3. **Data.** Restore the irreplaceable bytes from the off-box copies *before*
   starting services: `/srv/farm/production` (Dropbox) and `/data/databases` via
   `sys/dbops` restore. Fix ownership (e.g. `pipeline.db` is `gradywoodruff:www-data`).
4. **nginx.** Copy `nginx/` back to `/etc/nginx` (sites-available + sites-enabled +
   nginx.conf + conf.d). `sudo nginx -t` then enable nginx.
5. **Cloudflare tunnel.** Copy `cloudflared/config.yml` and `cloudflared/<UUID>.json`
   to `/etc/cloudflared/` (credential `chmod 600`, dir root-only). This **reuses the
   existing tunnel** — no DNS re-binding needed. Enable `cloudflared`.
6. **systemd units.** Copy the captured base units and `*.service.d` / `*.timer.d`
   directories to `/etc/systemd/system/`. The API, Distribution, ComfyUI and
   Stable Diffusion base units still reference Conda; their captured
   `90-uv-runtime.conf` drop-ins are required to
   select the restored `.venv` runtimes. Preserve their other drop-ins and private
   configuration too. Before starting Stable Diffusion with its uv override,
   restore `private/stable-diffusion/service.env` to its exact original path
   using the private-environment instructions below. Run
   `sudo systemctl daemon-reload`, inspect the effective
   unit commands, then enable/start only the services and timers confirmed to be
   active in the recovery plan. Captured retired or dormant units are historical
   configuration, not instructions to reactivate them.
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
Uncommitted API edits are deliberately excluded. Commit and release the API's
`ace-step` recipe before releasing this backup update or running its capture;
the existing `graphiti-root` and `graphiti-mcp` recipes must also remain committed.
A missing committed profile lock fails capture; a dirty working tree is not a
substitute for the recorded API revision.

The accepted bundle currently lives at
`/srv/farm/.uv/migrations/2026-10-01-api-audit/artifact-set.2obcd4ez`.
Its `receipt.json`, `requirements.lock` and all 147 reviewed wheels are copied into
`uv/api/`, alongside the committed `artifacts.json`. The wheel bytes need about
2.51 GiB once; subsequent runs hash-check source and backup, retaining unchanged
independent destination files. Corrupt copies are replaced. No hardlinks to the
source are created, and the retained uv subtree is excluded from the shell's
recursive permission changes. The helper creates directories/files with 700/600
permissions and replaces a linked destination before changing file permissions.
Conda exports are no longer collected after retirement of the API rollback
environment. Each run clears the obsolete `conda/` snapshot subtree so an old
export cannot appear current. The reviewed uv artifacts and installed service
overrides define runtime recovery.

The same capture preserves the local wheels required by the committed ComfyUI,
Stable Diffusion, Graphiti and ACE-Step locks. These include recovered native
extensions, preserved editable-installation wheels and ACE-Step's retained
setuptools patch, which must not be replaced by arbitrary fresh builds. The only
accepted source roots are
`/srv/farm/.uv/migrations/2026-10-02-comfyui/wheels`,
`/srv/farm/.uv/migrations/2026-10-02-stable-diffusion/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-graphiti/wheels` and
`/srv/farm/.uv/migrations/2026-10-03-ace-step/wheels`; files must have a
single SHA-256 in their lock, remain inside the corresponding canonical root and
be regular files. Symlink sources or directories are refused. Each wheel is
copied using the same independent-file verification as the API wheelhouse.
Nothing is downloaded, installed or imported.

Copies live under `uv/api/upstream/<profile>/wheelhouse/`, preserving nested paths
such as ComfyUI's `mode-preserved/` and ACE-Step's `retained/`. `capture.json`
records each original path, backup-relative filename, hash, byte count and
referring lock, plus the lock
hashes and copied/reused counts. Only lock-referenced wheels are added; older
upstream copies may remain but are not part of the current recorded recovery set.
The image-service recovery sets remain 28 ComfyUI wheels and 5 Stable Diffusion
wheels. Graphiti adds **55 unique wheels**, approximately 46.4 MiB, from the two
committed locks at `deploy/upstream/graphiti-root/requirements.lock` and
`deploy/upstream/graphiti-mcp/requirements.lock`. Its 31-package root and
49-package MCP profiles share 25 wheels; these are copied once under
`uv/api/upstream/graphiti/wheelhouse/`, with both referring locks recorded.
All Graphiti package artifacts are local hash-locked wheels, permitting offline
package installation once its managed Python and uv prerequisites are restored.
Graphiti's root wheel retains the exact editable path to `/srv/farm/code/graphiti`;
its MCP profile independently retains installed Graphiti 0.14.0. Restore the
matching upstream source and local modifications separately. This addition does
not read or capture Graphiti `.env`, live process environments or Neo4j data.

ACE-Step adds **164 unique wheels**, approximately 3.02 GiB, from the committed
`deploy/upstream/ace-step/requirements.lock`, under
`uv/api/upstream/ace-step/wheelhouse/`. The selected artifacts match the reviewed
`/srv/farm/.uv/migrations/2026-10-03-ace-step/wheel-manifest-preserved.json`;
capture uses the committed lock's exact paths and hashes, not a directory-wide
copy of that evidence bundle. In particular, it retains the reviewed
`retained/setuptools-80.9.0-py3-none-any.whl`, excluding the unpatched wheel at the
bundle root unless a future committed lock explicitly selects it. Recovered
editable/source artifacts and the pinned Typer overlay remain part of the
recipe; restore them unchanged. Package installation is offline once the
recipe's managed Python and uv prerequisites are available. The editable
ACE-Step artifact links to `/srv/farm/code/ACE-Step`; its matching source checkout,
reviewed local customizations, private configuration and model weights require
separate recovery. This wheel capture does not preserve or prove recovery of
those files, and does not read application environments or run model code.

For the image services, published package-index and HTTPS artifacts remain
represented by committed hash locks; their wheels are not duplicated by this
upstream capture. The overall recovery set is therefore not a fully offline
installer.

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
extensions and models separately. For Graphiti, separately recover its upstream
checkout and reviewed local changes, private configuration and Neo4j data; this
package capture does not establish graph-data or credential recovery. For
ACE-Step, recover its matching source checkout, customizations, configuration
and model weights separately before syncing the `ace-step` profile; its final
runtime belongs at `/srv/farm/code/ACE-Step/.venv`. Restore each recorded
upstream wheel beneath its exact original `wheel_root`, preserving the recorded
relative filename and
verifying its SHA-256. The archive includes the runtime manifest, baseline,
payload checks, lock, validation helpers and deployment guide. Install that
profile's specified managed Python and uv prerequisites, then run
`sh scripts/upstream_runtime.sh <profile> sync` and `check` from the restored API
checkout with the application stopped. Review the captured profile deployment
guide before starting services. For the shared Graphiti wheel bundle, restore
both profiles with `graphiti-root` and `graphiti-mcp` separately; their final
runtimes remain `/srv/farm/code/graphiti/.venv` and
`/srv/farm/code/graphiti/mcp_server/.venv`. Backup capture alone does not establish
live GPU or Graphiti MCP/database acceptance; retain the deployment guide's
separate acceptance records.

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

## Stable Diffusion private environment

The installed uv service override requires
`/srv/farm/private/stable-diffusion/service.env`. The snapshot captures exactly
this file using the helper's `--stable-diffusion-env` mode, which requires root.
It refuses symlink files or ancestor directories, nonregular files, a source
mode other than 0600, and empty or oversized input. The independent copy is
created atomically with mode 0600 under a root-only 0700 directory; the source
contents, ownership and permissions are unchanged. No contents or credential
hashes are printed or added to the capture manifest.

The `private/` snapshot subtree is recreated each run so a missing file cannot
leave an old credential appearing current. A missing or invalid source produces
a DR-critical warning in the top-level manifest and an incomplete
`private/stable-diffusion/capture.json` when the directory could be created.
Require that receipt to say `passed` before using this part of a restore.
No other `/srv/farm/private` files are captured by this addition.

After recovering the snapshot to `/var/backups/config-snapshot`, restore the
environment before starting `stable-diffusion.service` with the uv override:

```sh
sudo install -d -m 0700 -o gradywoodruff -g gradywoodruff /srv/farm/private/stable-diffusion
sudo install -m 0600 -o gradywoodruff -g gradywoodruff /var/backups/config-snapshot/private/stable-diffusion/service.env /srv/farm/private/stable-diffusion/service.env
```

This restores the required environment file directly; the credential does not
need to be extracted from the historical base unit. Do not print the file during
verification. The existing encrypted-off-box requirements apply to this secret
as well; this change neither starts a snapshot nor configures a transfer.

Kitsu is being retired at the owner's request, rather than migrated. Its
prepared wheel-capture extension was withdrawn. Follow the API's
`docs/deployment/kitsu-retirement.md`; after removing its two units and routing,
refresh the root snapshot so recovery does not recreate the retired host.
PostgreSQL database `zoudb` deletion remains an owner-run operation.
