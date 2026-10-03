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
| ComfyUI / Stable Diffusion / Graphiti / ACE-Step / LivePortrait / Flood Map / TikTok / Hardware / Noise Agent / InstantMesh / Riffusion / RAVE uv recovery | hash-verified local wheels referenced by the committed upstream locks; both Graphiti profiles share one wheel bundle | `uv/api/upstream/` |
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
`rave` recipe before releasing this backup update or running its capture;
the published `noise-agent`, `instantmesh` and `riffusion` recipes must remain committed, and
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
Stable Diffusion, Graphiti, ACE-Step, LivePortrait, Flood Map, TikTok Scraper,
Hardware, Noise Agent, InstantMesh, Riffusion and RAVE locks. These
include recovered native extensions, preserved editable-installation wheels
and ACE-Step's retained
setuptools patch, which must not be replaced by arbitrary fresh builds. The only
accepted source roots are
`/srv/farm/.uv/migrations/2026-10-02-comfyui/wheels`,
`/srv/farm/.uv/migrations/2026-10-02-stable-diffusion/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-graphiti/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-ace-step/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-live-portrait/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-flood-map/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-tiktok-scraper/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-hardware/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-noise-agent/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-instantmesh/wheels`,
`/srv/farm/.uv/migrations/2026-10-03-riffusion/wheels` and
`/srv/farm/.uv/migrations/2026-10-03-rave/wheels`; files must have a
single SHA-256 in their lock, remain inside the corresponding canonical root and
be regular files. Symlink sources or directories are refused. Each wheel is
copied using the same independent-file verification as the API wheelhouse.
Nothing is downloaded, installed or imported.

Copies live under `uv/api/upstream/<profile>/wheelhouse/`, preserving nested paths
such as ComfyUI's `mode-preserved/`, ACE-Step/LivePortrait/Flood Map/Hardware/Noise Agent
`retained/`, and InstantMesh's `recovered/`, `recovered-large/` and `reused/`.
`capture.json` records each original path, backup-relative
filename, hash, byte count and
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

LivePortrait adds **122 unique wheels**, approximately 3.19 GiB, from the
committed `deploy/upstream/live-portrait/requirements.lock`, under
`uv/api/upstream/live-portrait/wheelhouse/`. Capture retains the selected
`retained/pip-24.0-py3-none-any.whl` and
`retained/setuptools-69.5.1-py3-none-any.whl` paths, without substituting the
original registry wheels. Selection follows the committed lock's exact paths
and hashes; the prepared artifact inventory is
`/srv/farm/.uv/migrations/2026-10-03-live-portrait/wheel-manifest-preserved.json`.
The LivePortrait recipe and lock must be committed in the API before deploying
this capture extension; an uncommitted prepared lock is deliberately rejected.
Restore `/srv/farm/code/live-portrait`, its matching local customizations,
configuration, model weights and inputs separately. This package capture does
not establish source, model, Python-interpreter or live GPU recovery. The
runtime belongs at `/srv/farm/code/live-portrait/.venv`; follow the committed
deployment guide's validation and acceptance requirements before launching it.

Flood Map adds **29 unique wheels**, approximately 114.9 MiB, from the
committed `deploy/upstream/flood-map/requirements.lock`, under
`uv/api/upstream/flood-map/wheelhouse/`. Capture preserves the exact
`retained/pip-23.3.1-py3-none-any.whl` and
`retained/setuptools-68.2.2-py3-none-any.whl` paths selected by that lock;
the artifact inventory is
`/srv/farm/.uv/migrations/2026-10-03-flood-map/wheel-manifest-preserved.json`.
The API recipe and lock must be committed before this capture extension
runs. Its package installation is offline after restoring the pinned
Python 3.9.18 and uv prerequisites. The unversioned
`/srv/farm/code/flood-map` source, input media and output files require
separate recovery; this wheel capture does not preserve them. System
FFmpeg and its linked OS libraries are also separate prerequisites.
Synthetic isolated CPU/audio parity has passed at the final project runtime;
real-media acceptance remains separate. The script currently names a missing
MP3 directory, `/srv/farm/audio/assets/scrapes/tiktok/2024-01-18`; restore or
review those inputs before considering a normal manual run. Follow the
committed deployment guide for current runtime and acceptance status.

For ComfyUI and Stable Diffusion, published package-index and HTTPS artifacts remain
represented by committed hash locks; their wheels are not duplicated by this
upstream capture. The overall recovery set is therefore not a fully offline
installer.

A failed capture is logged as DR-critical. When a capture has started but fails,
`uv/api/capture.json` remains `status: incomplete`; require `status: passed` and no
uv warning in the top-level manifest before considering this recovery set usable.
The existing snapshot command still exits zero for warnings; inspect its report.
The snapshot includes the accepted API wheel set and locked local upstream
wheels, not installed environments,
model weights, or the uv executable. Separate exact installed-interpreter archives
cover Python 3.9.18, 3.9.19, 3.10.14, 3.10.16, 3.10.18, 3.10.19 and 3.12.11 as described
below. Other managed versions and uv 0.8.19 still require separate recovery at
the recorded paths, along with OS libraries/drivers, API source and application
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

## Exact managed Python 3.9.18 recovery archive

The API uv capture also retains the accepted installed-byte archive for
`/srv/farm/.uv/python/cpython-3.9.18-linux-x86_64-gnu`, build `20240224`, under
`uv/api/managed-python/cpython-3.9.18-build-20240224/`. This is independent of
any application's wheel set. Snapshot capture copies only the archive,
`source-manifest.json` and `receipt.json` from
`/srv/farm/.uv/migrations/2026-10-03-managed-python-recovery`; it does not
rearchive or modify a live interpreter, download files or extract the archive.

The receipt is pinned by SHA-256 in `capture-api-uv.py`:
`a5b616f544ca695793321b4f72e3e6859ac7aee0aa109f476a3659832e1e1a16`.
Its manifest hash is
`f5ad06cb019669db55abed5046d17654d471b2574c1bcda79bbe9549998a7ac2`;
its 27,868,993-byte `python-3.9.18-build-20240224.tar.gz` hash is
`fb338b67a7f5339da97c88e04237d6fae8c418e30172c39770b6c575511d93b1`.
Every source and retained copy is hash-checked. Valid independent copies are
reused; copies are mode 0600 beneath mode 0700 directories. Missing, changed,
unapproved or symlinked artifacts leave capture status incomplete.

The accepted manifest records all 5,636 entries: 4,355 regular files totaling
85,333,567 bytes, 234 directories and 1,047 internal symlinks. The private
archive was checked against those exact bytes and metadata, with unchanged
before/after source manifests. This original capture preserves the installed
interpreter without claiming independent upstream binary provenance.

A subsequent
[qualified restore validation](/srv/farm/.uv/migrations/2026-10-03-managed-python-recovery/restore-validation-bbffvhs4/restore-validation.json),
SHA-256 `42e6e73a653db98f0ad8ada2a9086f2f76d6fffd5442f263b587714d16843879`,
extracted all 5,636 entries into private scratch and verified their contents,
links, modes, owner UID and nanosecond timestamps. Twelve standard-library
imports plus SSL, SQLite, compression and hashing fixtures passed in a
network/GPU-free namespace using the restored interpreter and recorded system
library providers. The original pinned receipt, archive and manifest were
unchanged; this separate local proof is not an added snapshot input.

The unprivileged scratch restore used group 1000 for 21 bytecode files and two
cache directories whose original archive group is 33. Those 23 differences are
explicitly recorded. Exact numeric group restoration requires root and remains
untested, as do root/multi-user API/uWSGI permissions and full application
recovery. The archive does not include uv, OS libraries/drivers, application
code, configuration or models. Keep both the original capture limitations and
this qualified restore proof with the recovery plan.
Restore first into a private review directory, verify its manifest and perform
contained interpreter validation before considering any final-path recovery.
Do not unpack over an active runtime. No automatic extraction or runtime
replacement is part of snapshot capture.

### TikTok Scraper recovery

Capture retains the 13 artifacts in the committed `deploy/upstream/tiktok-scraper/requirements.lock`, including the recovered Playwright driver payload, under `uv/api/upstream/tiktok-scraper/wheelhouse/`. Restore the source separately and follow the API-owned deployment guide. This wheelhouse does not contain Chromium or establish browser/TikTok acceptance; retain the original Conda runtime until those checks pass.

### Hardware controller recovery

Capture retains the 18 artifacts in the committed
`deploy/upstream/hardware/requirements.lock`, including native PyAudio/evdev and
the preserved packaging payloads. They are copied independently to
`uv/api/upstream/hardware/wheelhouse/`, keeping the `retained/` subdirectory.
Restore the source and its existing local changes, Vosk model, OS audio libraries
and udev configuration separately. The API-owned recipe installs
`/srv/farm/sys/hardware/.venv` without changing its launcher. See the
[hardware runtime guide](/srv/farm/sys/api/docs/deployment/uv-hardware.md) for
validation and device-permission limits. This addition has fixture coverage;
root snapshot capture and full controller recovery still require separate checks.

Release the API recipe before this Backups update. Then refresh recovery data:

```sh
sudo systemctl start farm-config-snapshot.service
```

No service restart or systemd reload is required for the capture-helper change.

### Noise Agent Python recovery

Capture retains the 68 wheels in the committed
`deploy/upstream/noise-agent/requirements.lock`, including the selected
`retained/pycparser` artifact with its 129 preserved headers. Copies live under
`uv/api/upstream/noise-agent/wheelhouse/`. This recovers the Python package set
for the API-owned offline project recipe; it does not start the backend or adopt
its media tools.

Preserve the upstream checkout and local changes separately. The independent
85-file FFmpeg bundle is retained under the migration evidence directory and is
not included in this wheel capture. Managed Python 3.10.16 is retained in the
separate interpreter archive described below. OS libraries, credentials,
database/media, yt-dlp and provider configuration still require separate recovery
coverage. See the
[Noise Agent guide](/srv/farm/sys/api/docs/deployment/uv-noise-agent.md).
Do not retire its Conda environment based on this Python-only snapshot.

Release the API recipe before this Backups update, then refresh the configuration
snapshot with the command above. No service restart or daemon reload is needed.


### InstantMesh Python recovery

Capture retains the 123 wheels selected by the committed
`deploy/upstream/instantmesh/requirements.lock`, totaling 2,949,898,762 bytes.
Copies live under `uv/api/upstream/instantmesh/wheelhouse/` and preserve nested
paths including `recovered/`, `recovered-large/` and `reused/`. Only lock-selected
files are copied from
`/srv/farm/.uv/migrations/2026-10-03-instantmesh/wheels`; unused originals and
other migration artifacts are excluded. Source and destination hashes are
verified, and retained files are independent of source hardlinks.

The API recipe and deployment guide are included from the recorded API commit.
The original selected wheel manifest has SHA-256
`d5d0739b5e59b28f31f48e65c6f8add45a0f2d7d54e3940e0cdc6d9d53eb276b`;
see the [InstantMesh guide](/srv/farm/sys/api/docs/deployment/uv-instantmesh.md)
for the Python 3.10.14/123-package baseline and acceptance receipts.

This addition covers the Python recipe and wheel payloads, including retained
package-native binaries. Managed Python 3.10.14 is retained in the separate
interpreter archive described below, with an explicit scratch-restore ownership
qualification. The wheel capture does not include the independent CUDA toolkit,
separate header overlay, host compiler and OS libraries, NVIDIA driver, upstream
source/local assets or model weights. Those still require separate recovery
coverage. CPU/CLI-help parity and an isolated
explicit-target extension build do not establish normal application JIT, GPU
inference or full generation recovery. Preserve Conda pending the remaining
acceptance and consumer checks.

Commit and release the API recipe before this Backups update. Then refresh the
root configuration snapshot with `sudo systemctl start farm-config-snapshot.service`
and verify its receipt. Fixture/static artifact checks do not run that snapshot
or establish off-host recovery. No service restart or systemd reload is required.


## Riffusion recovery

Riffusion's capture profile selects only the local wheels named by the committed
`deploy/upstream/riffusion/requirements.lock` for its exact 147-package baseline.
The reviewed set contains 147 artifacts totaling **3,092,422,808 bytes**; capture
verifies every selected wheel against the committed lock. Sources must stay inside
`/srv/farm/.uv/migrations/2026-10-03-riffusion/wheels`;
verified independent copies go beneath `uv/api/upstream/riffusion/wheelhouse/`,
with nested paths preserved and unselected artifacts excluded. No wheels are
downloaded, rebuilt, installed or imported during capture.

The separate managed Python 3.9.19 build 20240814 archive below provides the
interpreter bytes. Package wheels and interpreter recovery do not capture the
modified upstream checkout, local models/assets, OS libraries or media tools.
They do not establish GPU generation, application adoption or full workflow
recovery. Preserve the original Conda environment pending its own acceptance and
consumer audit. Commit and release the API recipe before this Backups update;
a fresh owner-run root snapshot and its receipt must then be verified.


## RAVE recovery

RAVE's capture profile selects only local wheels named by the committed
`deploy/upstream/rave/requirements.lock` for its exact 90-package baseline.
The reviewed set contains 90 artifacts totaling **2,973,298,383 bytes**;
each selected wheel must match its committed hash and remain inside
`/srv/farm/.uv/migrations/2026-10-03-rave/wheels`. Independent verified copies go
beneath `uv/api/upstream/rave/wheelhouse/`, preserving nested paths and excluding
unselected artifacts. Capture neither installs packages nor imports RAVE.

The existing managed Python 3.9.19 build 20240814 archive supplies the interpreter
bytes; this profile adds no interpreter archive. Preserve the upstream source,
training data, checkpoints, native system dependencies and Conda separately.
Package capture does not establish training, model/export or consumer acceptance.
Commit and release the API recipe before this Backups update, then refresh
`sudo systemctl start farm-config-snapshot.service` and verify that invocation's
receipt. No service restart, systemd reload, offsite-copy or full-restore claim
is part of this update.


## Additional exact managed Python recovery archives

Six additional accepted interpreter bundles are retained separately from the
application wheelhouses. Their source root is
`/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/<version>/`;
the snapshot destination is
`uv/api/managed-python/cpython-<version>-build-<build>/`.

| Python | Build | Compressed bytes | Accepted receipt |
| --- | --- | ---: | --- |
| 3.9.19 | 20240814 | 19,951,674 | [Receipt](/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/3.9.19/receipt.json) |
| 3.10.14 | 20240814 | 21,386,053 | [Receipt](/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/3.10.14/receipt.json) |
| 3.10.16 | 20250317 | 20,733,453 | [Receipt](/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/3.10.16/receipt.json) |
| 3.10.18 | 20250918 | 28,738,871 | [Receipt](/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/3.10.18/receipt.json) |
| 3.10.19 | 20251031 | 27,887,538 | [Receipt](/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/3.10.19/receipt.json) |
| 3.12.11 | 20250918 | 34,483,022 | [Receipt](/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/3.12.11/receipt.json) |

The capture helper pins each receipt's SHA-256 and requires its exact version,
build, source prefix, archive filename and accepted verification flags. It copies
only `python-<version>-build-<build>.tar.gz`, `source-manifest.json` and
`receipt.json`, plus the one explicitly pinned ownership policy for Python
3.10.14. Source/copy hashes and bounded file sizes are verified; symlinks and
changed receipts are refused. Copies are independent mode-0600 files beneath
mode-0700 directories and can be reused after verification. Capture does not
rearchive, extract, execute or modify a live interpreter. The original 3.9.18
capture function, destination and `managed_python` result remain unchanged;
these six entries appear in a separate `additional_managed_python` mapping.

All six bundles passed local scratch extraction, full manifest comparison and
12 standard-library import checks with SSL, SQLite, compression and hashing
fixtures in a network/GPU-free namespace. Original artifacts and restored
prefixes remained unchanged. Five versions retained exact numeric ownership.
Python **3.9.19** passed all 5,363 manifest entries and 12 standard-library
imports with exact numeric ownership. Its
[restore report](/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/restore-validation-3.9.19-g9tb_75a/restore-validation.json)
has SHA-256 `f04f008c053c1195c0633beb48abd94b8bf2f88e0377a825c258a2194471639c`.

Python **3.10.14** has a qualified result: its archive preserves host UID 1000
and GID 33 for **157 bytecode files (0644) and 11 cache directories (0755)**,
but the unprivileged scratch restore used GID 1000 for precisely those 168
entries. Their exact paths are retained in
`python_31014_host_ownership_policy.json`, SHA-256
`d4b0636012ff5af87edb7adcbfff6e273b9dec4b14ce1ec4b99a7d6cf852e5ef`.
The [qualified restore report](/srv/farm/.uv/migrations/2026-10-03-managed-python-extra/restore-validation-3.10.14-kq5rb7wu/restore-validation.json)
has SHA-256 `510d9fe0ab5c100d522b72ec0e5d829b3e490904bbc83fddb1399d8b9eb9a486`.
Exact privileged GID-33 restoration and cross-user application access remain
untested. The other versions have no added group-restoration qualification.

These archives preserve installed bytes, not independent upstream provenance.
Restore each interpreter at its recorded absolute source prefix after reviewing
its manifest and ownership requirements. The
[managed interpreter guide](/srv/farm/sys/api/docs/deployment/uv-managed-python-recovery.md)
records the individual validation receipts and limits. The uv executable, OS
libraries/drivers, application code/configuration/models and separate native
toolkits remain outside these interpreter bundles. Local restore checks do not
establish off-host or complete application/generation recovery.

After the coordinated releases, refresh the root snapshot with
`sudo systemctl start farm-config-snapshot.service` and verify its capture
receipt. The implementation tests do not run that service. No service restart
or systemd reload is required for this addition.
