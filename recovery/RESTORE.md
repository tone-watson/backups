# Fresh-machine recovery

Use this copy from the backup drive's `rescue-kit/`; Farm and GitHub are not
required to begin extraction. Keep the original server off or isolated before
reusing network identities, SSH keys, tunnel credentials or writable shares.

## 1. Prepare a compatible host and inspect the recovery set

Install Ubuntu 22.04 LTS x86_64 (the captured host's platform) on the replacement
machine. Partition/format only devices you have identified yourself. Use the
captured mount/LVM/boot metadata as a layout reference; new disks need new UUIDs
and possibly different sizes. No toolkit command repartitions a disk, installs a
bootloader or overwrites a running operating system.

Mount the backup filesystem and recover its password from the independent copy.
Use a current-user-owned mode 0600 password file outside the drive. To preserve
numeric ownership, capabilities and ACLs on extraction, run as root on a Linux
filesystem that supports them. Ensure space for extracted payloads and the staged
native archives; archive application can temporarily need both copies.

The following paths are operator choices; this example uses `/mnt/farm-backup`
and a new empty `/mnt/farm-restored` directory:

```sh
sudo /mnt/farm-backup/rescue-kit/farm-recovery --restic /mnt/farm-backup/rescue-kit/restic restore --drive /mnt/farm-backup --password-file /root/farm-recovery.pass --target /mnt/farm-restored
```

Use `--snapshot FULL_SNAPSHOT_ID` to choose an older complete recovery set.
The command checks the repository, extracts and verifies restored file content.
It creates `FARM-RESTORE-RECEIPT.json`. This means extraction succeeded; it does
not mean services or database imports are ready. Absolute source paths appear
under the target, e.g. `/mnt/farm-restored/srv/farm`.

## 2. Apply the consistent captures to the extracted tree

The normal capture set is at
`/mnt/farm-restored/var/lib/farm-recovery/current/captures`. Keep the matching
manifest, policy and receipt together. Run the capture application helper against
the extracted tree, never the live `/`:

```sh
sudo /mnt/farm-backup/rescue-kit/farm-recovery apply-captures --target /mnt/farm-restored --capture-dir /mnt/farm-restored/var/lib/farm-recovery/current/captures
```

It validates every artifact before writes, places SQLite/RDB payloads at their
original relative locations with recorded metadata, and extracts validated offline
store archives. Existing target files, symlink escapes and stale SQLite sidecars
are refused. Optional PostgreSQL SQL remains an explicit native import task. Keep any
reported pending tasks with the restore receipt; do not treat extraction alone
as successful database recovery.

Archives containing absolute links, links outside their declared store, or
special device files are refused. Container stores can contain such entries and
then require a reviewed native/manual restore into an isolated environment.
Do not disable these checks or extract an unreviewed archive over the live host.
If application fails after writing some captures, retry from a new extraction;
the helper deliberately refuses to overwrite an already populated destination.

## 3. Rebuild the operating-system layer

Use the protected `manifest.json` for package versions, source lists, numeric
account/group mappings, service states, enabled units/drop-ins, mount topology,
runtime references and repository structure. The extracted `/etc`, `/usr/local`,
`/opt`, package state and recovery bundles are additional inputs.

Recreate required service accounts/groups with their recorded numeric identities
before applying ownership. Install compatible OS packages, database versions,
GPU driver, CUDA/native libraries and custom binaries. Reconcile `/etc/fstab`,
networking and boot settings with the new host. Never copy the old machine's
entire `/etc`, `/usr` or bootloader over the fresh OS blindly. Keep services and
timers stopped while restoring data and configuration; package installers may
start services automatically, so inspect and stop them before importing state.

Restore the preserved `/srv/farm`, `/data`, selected home/root state, custom
software and identified persistent stores from the extracted tree. Use a tool
that preserves numeric ownership, hardlinks, ACLs and xattrs, for example
`rsync -aHAX --numeric-ids` after reviewing each exact source/destination. Do not
use `--delete` against a live machine. The original `/srv/farm/models` and home
Conda symlinks and all Git worktree relationships must be retained.

The first profile already preserves local source and runtime payloads. Internet
clone/install recipes are optional alternatives until validated. For repaired
runtimes, follow the preserved per-component locks, runtime scripts, local wheels,
interpreter archives and native-build inputs; see the extracted
`srv/farm/sys/backups/config/README.md` and the API's deployment/recovery guides.
Do not promote a model to download-only recovery from its filename. Preserve the
custom Voice of the Noise/RVC/training material even if base weights are fetched.

## 4. Restore native stores while application writers remain stopped

SQLite files were validated during capture; after application, re-run integrity
checks on the restored copies. Preserve the captured cross-file dependencies and
never mix them with a different snapshot's media or config intentionally. WAL/SHM
files from live source capture were excluded; do not reintroduce stale sidecars.

The default PostgreSQL capture is the complete **offline PostgreSQL 14 cluster**.
Install compatible PostgreSQL 14 binaries and reconcile the preserved
`/etc/postgresql/14/main` configuration. Keep `postgresql@14-main.service` stopped
while copying the restored cluster to `/var/lib/postgresql/14/main`; preserve the
postgres account's numeric ownership. Review `postgresql.auto.conf`, tablespace
links and WAL paths against the captured storage map before starting the cluster.
Do not initialize or upgrade over this restored directory. Prove database health
with the matching server before any major-version migration.

If a later policy explicitly enables logical PostgreSQL capture instead, initialize
a new empty compatible cluster and restore globals/roles and databases from SQL.
Review the dump's role and tablespace statements against the new host. One possible verified-binary
invocation for PostgreSQL 14 is below; the input remains root-readable rather
than exposing it through a shared directory:

```sh
sudo sh -c 'exec /usr/sbin/runuser -u postgres -- /usr/lib/postgresql/14/bin/psql --no-password --set ON_ERROR_STOP=on --dbname postgres < /mnt/farm-restored/var/lib/farm-recovery/current/captures/postgres-cluster.sql'
```

Existing roles in a freshly initialized cluster can conflict with `pg_dumpall`
role creation. Resolve them deliberately or import reviewed globals/database
sections; do not ignore errors or report partial imports as successful. No native
PostgreSQL restore has been exercised by the scratch fixture tests.

For Redis, restore the validated RDB to the captured configured path while
`redis-server.service` is stopped. Reconcile persistence settings and review queued
jobs before enabling Farm workers. The implemented recipe rejects AOF-enabled
capture; do not add an old append-only file on restoration.

For Neo4j and Meilisearch, restore their offline archives with compatible binaries
and the captured configuration while `neo4j.service` and `meilisearch.service`
remain stopped. Test database health before reconnecting callers. Docker/LXD
archives require matching storage-driver/runtime support and their recorded
services/sockets to remain stopped during restoration. Database major-version
migrations and changed container storage backends require separate vendor recipes.

For captures with the reviewed `docker-snap-v1` or `lxd-snap-v1` runtime recipes,
`apply-captures` validates the receipt and requires those omitted entries to be
absent from the archive. It reconstructs the exact recorded LXD `shmounts`
symlink only after every payload has been applied, without following that link.
Keep the alternate root offline and do not follow its absolute symlinks into the
running host. Docker's device node and LXD's listening socket remain absent;
the application receipt lists their pending runtime activation explicitly.

- Docker recreates `volumes/backingFsBlockDev` using the recovered filesystem's
  device identity when its local-volume quota controller initializes. Never
  recreate the old host's recorded major/minor numbers manually. This was checked
  against the installed Docker 29.8.0 build and matching
  [Moby quota implementation](https://github.com/moby/moby/blob/docker-v29.8.0/daemon/internal/quota/projectquota.go#L406)
  and [local-volume startup](https://github.com/moby/moby/blob/docker-v29.8.0/daemon/volume/local/local.go#L74).
- LXD's installed `snap.lxd.daemon.unix.socket` unit creates `unix.socket` when
  started. Start its recorded socket units before its daemon service. The snap
  wrapper recreates `shmounts` only during shared-mount setup, so do not rely on
  an arbitrary restart to replace a missing link; `apply-captures` handles the
  reviewed link directly.

These recipes do not activate Docker or LXD, import databases, validate container
health, or permit arbitrary absolute links/device nodes in captured archives.
Farm DP's captured state must likewise be installed while `farm-dp.service` is
stopped and checked before that application is started.

## 5. Activate in dependency order and validate

Install reviewed service units and drop-ins together. The uv runtime overrides
are essential where base units still name retired Conda environments. Before
starting services after unit installation:

```sh
sudo systemctl daemon-reload
```

Start storage/database services first, then API/auth and Manager, then queue
workers/render services, then external ingress/shares, and finally approved timers.
Only activate services confirmed as intended in the captured service map; dormant
or retired unit files are not a request to reactivate them. Exact component
commands and dependencies belong to the preserved deployment guides.

Verify numeric ownership, ACLs/capabilities, network access, login/auth, databases,
queue state, file shares, custom models, one representative render/generation,
and existing offsite backups. Confirm credentials and the backup key can be
recovered without the original server. Take another external snapshot only after
validation. A full fresh-machine rehearsal is still required before calling this
an accepted whole-server recovery procedure.
