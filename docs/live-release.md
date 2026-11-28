# Live release under /opt/farm/backups

The Dropbox push timers and `farm backup` run a released copy,
`/opt/farm/backups/current`, never this checkout. The farm-wide design is in
[live releases](/srv/farm/docs/architecture/live-releases.md) and the how-to for
every repository in [release and build](/srv/farm/docs/guides/release-and-build.md).

## Shipping a change

```bash
farm release            # in /srv/farm/sys/backups, as for every repository
farm build backups      # export the new tag into /opt/farm/backups/releases/<version> and switch to it
```

Nothing is installed or restarted: the units are oneshots started by timers, and
`farm backup` starts the helper fresh each time. `farm build backups --version X.Y.Z`
switches back to an older release.

## What runs from the release

- `farm-dropbox-sync` runs `rclone/production-to-dropbox.sh`, and
  `farm-dropbox-db-sync` runs `rclone/databases-to-dropbox.sh`. Both find
  `dropbox-common.sh` and `exclude.txt` through their physical folder (`pwd -P`).
  rclone's own configuration stays in the owner's `~/.config/rclone`.
- `farm backup` (sys/cli) runs `recovery/farm_recovery_operator.py`, as root for
  mutations. The downloaded `restic` and its `download.json` live in
  `/opt/farm/backups/shared/recovery/.tools` (folder `0700`), linked into each
  release as `recovery/.tools`. Recovery state lives in the policy's `state_dir`.
- `farm-config-snapshot` is different: it runs a root-owned copy in
  `/usr/local/lib/farm-config-snapshot`. After a release that changes
  `config/snapshot.sh` or `config/capture-api-uv.py`, reinstall them from
  `/opt/farm/backups/current/config/` with the commands in
  [config/README.md](../config/README.md).

## Installing the units

The unit files in `systemd/` name `/opt/farm/backups/current`. After changing one:

```bash
sudo install -m 0644 /srv/farm/sys/backups/systemd/<unit>.service /etc/systemd/system/
sudo systemctl daemon-reload
```

## What stays in the checkout

Development and tests (see recovery/README.md), and the copy the rescue kit is
built from when you work on it. A disaster-recovery restore follows
[RESTORE.md](../recovery/RESTORE.md) from the rescue kit, not from either copy.
