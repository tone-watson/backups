# Farm recovery toolkit

This is the portable external-drive backup owner. It uses system Python 3.10+
and restic, without importing Farm applications. It includes metadata discovery,
consistent database capture, destination/source filesystem guards, encrypted
incremental snapshots, a standalone restore entry point and a retention preview.

The first policy preserves the whole observed root/Farm/boot filesystems except
virtual/runtime paths, swap, the backup destination and source files replaced by
consistent captures. It intentionally retains unverified models/environments.
Metadata scan boundaries are separate from payload exclusions. The
[classification](/srv/farm/docs/guides/disaster-recovery/2026-10-12-classification.md)
describes how later verified recipes can reduce this selection.

## Before a drive exists

Run the read-only checks:

```sh
/srv/farm/sys/backups/recovery/farm-recovery preflight
/srv/farm/sys/backups/recovery/farm-recovery prepare --output /tmp/farm-recovery-preparation
```

The output directory must be new or empty, owned by the invoking user and mode
0700. `prepare` writes `manifest.json`, a policy copy and a portable rescue kit.
It reads metadata, selected public runtime/model references and safe Git/host
fields. It does not dump a database, copy private payloads, contact Git remotes,
initialize a backup repository or change services. Bounds and permission failures
remain explicit. For complete protected-path visibility, the later operator run is:

```sh
sudo /srv/farm/sys/backups/recovery/farm-recovery prepare --output /var/lib/farm-recovery-preparation
```

An `incomplete` preparation status can also describe declared depth bounds, even
with zero errors. Review `errors` and `scan_boundaries`: the backup runner accepts
depth-limited mapping only where the actual payload is retained. Permission
errors, exhausted entry budgets and other incomplete discovery block backup.

No timer is installed: each real backup refreshes its own manifest. Existing
dated inventories remain evidence, not a substitute for that refresh. Git history,
dirty files, ignored local data and runtime recovery bundles remain actual backup
payloads, so the manifest does not depend on a remote clone succeeding.

## Engine and portable tools

The prepared local binary is `recovery/.tools/restic` (gitignored). If absent,
fetch the pinned Linux x86_64 binary without a system installation:

```sh
mkdir -m 700 /srv/farm/sys/backups/recovery/.tools
/usr/bin/python3 -I /srv/farm/sys/backups/recovery/fetch-restic.py --output /srv/farm/sys/backups/recovery/.tools/restic
```

The downloader checks fixed archive and extracted-binary SHA-256 values for
restic 0.19.1. They were checked against the upstream release over HTTPS; no PGP
signature verification is claimed. The rescue kit copied to the drive contains
the binary, scripts and instructions, but never the repository password. A
separate architecture needs a compatible verified restic binary.

## When the drive is available

No command here formats or partitions a drive. Use a dedicated mounted ext4,
XFS or Btrfs filesystem with enough free space; the 4 TB recommendation and
three-month history assumptions are in the classification. Inspect devices and
mounts before choosing the drive. Keep its passphrase/recovery copy independent
of both this server and the encrypted drive.

These are **future operator commands**, not preparation commands. Substitute the
verified mountpoint and filesystem UUID. The password file must be nonempty,
root-owned mode 0600 for a root invocation, and outside the backup drive. Prepare
it using your password manager or a hidden prompt; never put the password in a
shell command, Git file or chat.

```sh
lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINTS
findmnt --target /mnt/farm-backup -o TARGET,SOURCE,FSTYPE,UUID

sudo /srv/farm/sys/backups/recovery/farm-recovery init --drive /mnt/farm-backup --uuid REPLACE_WITH_DRIVE_UUID --password-file /root/farm-recovery.pass
```

Initialization refuses an existing recovery repository/marker and leaves unrelated
drive files alone. The drive marker pins both filesystem UUID and restic repository
identity. Backup checks the mount again, checks the four source filesystem UUIDs,
rejects unclassified mounts and uses operation locks. It will not write a backup
into a bare mountpoint directory on the server's own disk.

Review `policy.server.json` before the first run. Its native captures are:

- SQLite online snapshots for central DBs, MCP, Flamenco and `jobs.db`, with exact
  integrity-check results, hashes and source metadata.
- An offline PostgreSQL 14 cluster archive, including local data-directory
  configuration. Optional native `pg_dumpall` support retains raw data as well;
  logical dumps alone are not a full replacement for the cluster directory.
- A fresh validated Redis RDB; persistence path must match and AOF mode must be
  disabled for this recovery recipe.
- Offline archives for Farm DP, Neo4j, Meilisearch, Docker and LXD, including
  ACLs/xattrs.

Redis verification runs before any offline writer is stopped and again around
the RDB export. The server policy explicitly selects `disabled-config` because
this host disables Redis's `CONFIG` command. This Redis 6.0.16 recipe reads the protected
configuration and verifies its unchanged startup settings against the active
systemd unit, the local Redis process and `INFO` responses. It requires the
reviewed direct service launch, no includes or drop-ins, a matching persistence
directory/filename, and AOF disabled. Unsupported or changed startup settings
fail closed; a Redis upgrade requires recipe revalidation. The recipe never
enables `CONFIG` or changes Redis settings.
The capture records a stable verification identity and requires it to match
after `redis-cli --rdb` and `redis-check-rdb` succeed. Other deployments retain
the default `config-command` verification unless explicitly configured otherwise.

The Docker/LXD archives use two explicit recipes tied to their exact source
paths. They record and omit only Docker's `volumes/backingFsBlockDev`, LXD's
`unix.socket`, and LXD's `shmounts` link to
`/var/snap/lxd/common/shmounts/instances`. Their types and link/device identities
are checked before any service stop and again around archive creation; an
unexpected regular file, directory, link target or changed entry fails capture.
Other data and the general archive-link/special-file restrictions are unchanged.
The capture receipt records each runtime entry, including absence. During
restore the exact recorded LXD link is recreated after payload extraction;
Docker and systemd create the device node and listening socket when explicitly
activated on the recovered machine. See [runtime restoration](RESTORE.md).

Offline stores must have their listed writer units inactive. An explicit
`--quiesce` allows the tool to stop those listed services/sockets for capture and
restore their original active state afterward. SSH, networking, Cloudflare and
Tailscale units are protected from this mechanism. This is a maintenance window:
finish jobs, stop submissions and avoid concurrent manual writers. The initial
states and service actions are journaled in the protected capture stage; after a
power loss or forced kill, inspect that receipt before manually restarting units.
An incomplete prior capture blocks the next run. Review its journal, recover the
recorded service states, then move the entire `current/` directory to a private
incident directory outside the managed `current/` staging tree before retrying. The tool
never discards that journal automatically.

```sh
sudo /srv/farm/sys/backups/recovery/farm-recovery backup --drive /mnt/farm-backup --password-file /root/farm-recovery.pass --quiesce
sudo /srv/farm/sys/backups/recovery/farm-recovery snapshots --drive /mnt/farm-backup --password-file /root/farm-recovery.pass
sudo /srv/farm/sys/backups/recovery/farm-recovery check --drive /mnt/farm-backup --password-file /root/farm-recovery.pass --read-data
```

Omit `--quiesce` when writers were stopped separately; the tool still checks their
state. Stopped sockets are restored before stopped services, with reverse stop
order within each group. Service stop/start waits use systemd's own deadlines
and dependency ordering, not the 900-second native capture timeout. Receipts
record those waits and the original unit timeout settings. Infinite systemd
deadlines may require operator intervention; a timed-out or interrupted client
does not cancel a manager-owned job. No automatic system-wide freeze occurs.
Each SQLite/database capture is
consistent within its own scope; live content and separate databases do not form
one atomic application transaction. Session/identity stores copied as ordinary
files need service-specific acceptance during the first maintenance run. New
stores, filesystem mounts or persistence modes require policy review.

The tool stages private captures under `/var/lib/farm-recovery/current`. Its
receipt records capture paths and hashes; restic excludes the replaced live DB
paths and includes the capture stage. Failures or restic exit 3 leave no newly
complete recovery set. Only successful backup plus repository check receives the
complete tag. The check validates repository structure; use `check --read-data`
for full stored-data verification and rehearse restores separately.

Restic stores new content chunks once and reuses unchanged content between
snapshots. File and metadata changes remain recoverable by snapshot; deletion
from the source does not delete the older snapshot's copy. See upstream
[backup behavior](https://restic.readthedocs.io/en/stable/040_backup.html).

## Retention

Preview keeping all complete snapshots within 90 days of the latest snapshot,
plus at least the last three complete snapshots:

```sh
sudo /srv/farm/sys/backups/recovery/farm-recovery retention-plan --drive /mnt/farm-backup --password-file /root/farm-recovery.pass
```

This command never deletes anything. Automatic forget/prune is intentionally not
enabled before the first real backup and restore rehearsal. Pending/failed sets
are excluded from the complete-set retention selection. Review obsolete pending
sets explicitly; do not automatically unlock or delete them while a writer might
still be active. After acceptance, a separately reviewed retention operation can
use the previewed policy and reserve space for pruning. See upstream
[retention rules](https://restic.readthedocs.io/en/stable/060_forget.html).

## Restore and checks

Follow [RESTORE.md](RESTORE.md). The drive's rescue kit can extract a verified
snapshot on a fresh machine before Farm exists. Restore refuses `/`, nonempty
targets, destination/repository overlap and symlinked target paths.

Portable tests use scratch directories and fake native commands; restic integration
tests use actual encrypted repositories containing disposable fixtures only:

```sh
cd /srv/farm/sys/backups
FARM_TEST_RESTIC=/srv/farm/sys/backups/recovery/.tools/restic /usr/bin/python3 -m unittest discover -s recovery/tests -p 'test_*.py'
/usr/bin/python3 -m unittest discover -s config -p 'test_*.py'
/usr/bin/python3 -m unittest discover -s rclone -p 'test_*.py'
```

Run restic metadata round trips in the normal host user namespace. The Codex
sandbox remaps owners and can make restoring a parent `/tmp` entry fail with
`lchown: invalid argument`. Scratch success does not establish root ownership,
POSIX ACL/capability, real-device, native database import or fresh-hardware
acceptance; those checks remain in the operator rehearsal.
