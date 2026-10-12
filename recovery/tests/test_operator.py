"""Operator fixtures never mount, eject, or launch a real recovery job."""
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("farm_operator_fixture", HERE / "farm_recovery_operator.py")
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)
recovery = operator.recovery


class OperatorTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        temporary = tempfile.TemporaryDirectory(prefix="farm-operator-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = json.loads((HERE / "operator.server.json").read_text())
        self.mountpoint = self.root / "mount"
        self.mountpoint.mkdir()
        (self.mountpoint / "repository").mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.public = self.root / "public"
        self.public.mkdir()
        self.disk = self.root / "disk"
        self.disk.write_text("fixture block identity only")
        self.partition = self.root / "partition"
        self.partition.write_text("fixture partition identity only")
        self.by_id = self.root / "by-id"
        self.by_id.symlink_to(self.disk)
        self.partition_by_id = self.root / "partition-by-id"
        self.partition_by_id.symlink_to(self.partition)
        self.profile.update(mountpoint=str(self.mountpoint), state_dir=str(self.state),
                            device_by_id=str(self.by_id), partition_by_id=str(self.partition_by_id),
                            password_file=str(self.root / "private.pass"),
                            policy=str(self.root / "policy.json"))
        self.partition_row = {"name": str(self.partition), "type": "part", "pkname": str(self.disk),
                              "uuid": self.profile["filesystem_uuid"], "mountpoints": [], "ro": False}
        self.disk_row = {"name": str(self.disk), "type": "disk", "serial": self.profile["serial"],
                         "tran": "usb", "mountpoints": [], "ro": False, "children": [self.partition_row]}
        self.mounted = None
        self.calls = []
        self.marker = {"schema": "farm.recovery-drive/1", "filesystem_uuid": self.profile["filesystem_uuid"],
                       "repository_id": self.profile["repository_id"]}
        for target, value in (("RUNTIME", self.runtime), ("PUBLIC", self.public)):
            patcher = mock.patch.object(operator, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def mount_row(self, **changes):
        row = {"target": str(self.mountpoint), "source": str(self.partition), "fstype": "ext4",
               "uuid": self.profile["filesystem_uuid"], "avail": 10000, "size": 20000,
               "options": "rw,nodev,nosuid,relatime"}
        row.update(changes)
        return row

    def command(self, argv, **kwargs):
        self.calls.append(list(argv))
        name = Path(argv[0]).name
        if name == "findmnt":
            return json.dumps({"filesystems": [self.mounted] if self.mounted else []})
        if name == "lsblk":
            return json.dumps({"blockdevices": [self.disk_row]})
        if name == "mount":
            self.mounted = self.mount_row()
            self.partition_row["mountpoints"] = [str(self.mountpoint)]
            return ""
        if name == "umount":
            self.mounted = None
            self.partition_row["mountpoints"] = []
            return ""
        if name in {"systemd-run", "udisksctl"}:
            return ""
        raise AssertionError("Unexpected host command: " + repr(argv))

    @contextlib.contextmanager
    def operations(self):
        with mock.patch.object(operator, "_command", side_effect=self.command), \
                mock.patch.object(operator, "_require_root"), \
                mock.patch.object(operator, "_lifecycle", side_effect=lambda **_: contextlib.nullcontext()), \
                mock.patch.object(operator, "_busy"), \
                mock.patch.object(operator, "_mountpoint", return_value=self.mountpoint), \
                mock.patch.object(operator, "_read_json", return_value=self.marker):
            yield

    def test_profile_pins_enrolled_identity_and_has_no_password_value(self):
        profile = operator.load_profile()
        self.assertEqual(profile["filesystem_uuid"], "069c71d8-0e56-444c-afd0-782d0802ca6b")
        self.assertEqual(profile["serial"], "00000000NL6F7VD2")
        self.assertEqual(profile["password_file"], "/root/farm-recovery.pass")
        self.assertNotIn("password", profile)

    def test_drive_status_distinguishes_absent_attached_mounted_and_foreign(self):
        with mock.patch.object(operator, "_command", side_effect=self.command):
            self.assertEqual(operator.drive_state(self.profile)["drive_state"], "attached")
            self.mounted = self.mount_row()
            self.assertEqual(operator.drive_state(self.profile)["drive_state"], "mounted")
            self.mounted = self.mount_row(uuid="foreign")
            self.assertEqual(operator.drive_state(self.profile)["drive_state"], "wrong-drive")
            self.mounted = None
            self.by_id.unlink()
            self.assertEqual(operator.drive_state(self.profile)["drive_state"], "disconnected")
        self.assertTrue(all(Path(argv[0]).name in {"lsblk", "findmnt"} for argv in self.calls))

    def test_disk_identity_rejects_internal_readonly_wrong_serial_and_layout(self):
        for key, value in (("tran", "nvme"), ("type", "part"), ("serial", "wrong"), ("ro", True)):
            old = self.disk_row[key]
            with self.subTest(key=key), mock.patch.object(operator, "_command", side_effect=self.command):
                self.disk_row[key] = value
                with self.assertRaises(operator.OperatorError):
                    operator._device(self.profile)
            self.disk_row[key] = old
        self.disk_row["children"].append(dict(self.partition_row, name=str(self.root / "extra"), uuid="other"))
        with mock.patch.object(operator, "_command", side_effect=self.command), self.assertRaises(operator.OperatorError):
            operator._device(self.profile)

    def test_partition_must_have_expected_uuid_and_whole_disk_parent(self):
        for key, value in (("uuid", "different"), ("pkname", "/dev/internal"), ("type", "lvm")):
            old = self.partition_row[key]
            with self.subTest(key=key), mock.patch.object(operator, "_command", side_effect=self.command):
                self.partition_row[key] = value
                with self.assertRaises(operator.OperatorError):
                    operator._device(self.profile)
            self.partition_row[key] = old

    def test_mount_is_idempotent_and_uses_only_verified_partition_and_options(self):
        with self.operations():
            result = operator.mount(self.profile)
            again = operator.mount(self.profile)
        mounts = [argv for argv in self.calls if Path(argv[0]).name == "mount"]
        self.assertEqual(mounts, [["/usr/bin/mount", "-o", "nodev,nosuid", str(self.partition_by_id), str(self.mountpoint)]])
        self.assertEqual(result["status"], "success")
        self.assertEqual(again["drive_state"], "mounted")

    def test_missing_drive_and_foreign_mount_never_mount_or_initialize(self):
        for condition in ("absent", "foreign", "mounted-elsewhere"):
            self.calls.clear()
            with self.subTest(condition=condition), self.operations():
                self.mounted = self.mount_row(uuid="wrong") if condition == "foreign" else None
                self.partition_row["mountpoints"] = ["/elsewhere"] if condition == "mounted-elsewhere" else []
                device = None if condition == "absent" else {"disk": str(self.disk), "device": str(self.partition),
                                                            "nodes": [self.disk_row, self.partition_row]}
                with mock.patch.object(operator, "_device", return_value=device), self.assertRaises(operator.OperatorError):
                    operator.mount(self.profile)
            self.assertFalse(any(Path(argv[0]).name in {"mount", "mkfs", "systemd-run"} for argv in self.calls))

    def test_already_mounted_clone_cannot_borrow_enrolled_hardware_identity(self):
        self.mounted = self.mount_row(source=str(self.root / "different-partition"))
        with self.operations(), self.assertRaises(operator.OperatorError):
            operator.mount(self.profile)

    def test_incomplete_mount_security_or_repository_identity_fails_closed(self):
        for changes in ({"fstype": "exfat"}, {"options": "rw,nodev"}, {"options": "ro,nodev,nosuid"}):
            self.mounted = self.mount_row(**changes)
            with self.subTest(changes=changes), self.operations(), self.assertRaises(operator.OperatorError):
                operator.mount(self.profile)
        self.mounted = self.mount_row()
        self.marker["repository_id"] = "0" * 64
        with self.operations(), self.assertRaises(operator.OperatorError):
            operator.mount(self.profile)

    def test_active_and_pending_launches_block_before_storage_mutations(self):
        running = {"farm-recovery-legacy.service": {"ActiveState": "active", "SubState": "running", "MainPID": "77"}}
        for units, reservation in ((running, None), ({}, {"token": "a" * 32}),
                                   ({"farm-recovery-new.service": {"ActiveState": "activating", "MainPID": "0"}}, None)):
            with self.subTest(units=units, reservation=reservation), \
                    mock.patch.object(operator, "_reservation", return_value=reservation), \
                    mock.patch.object(operator, "_units", return_value=units), \
                    mock.patch.object(operator, "_probe_lock") as probe:
                with self.assertRaises(operator.OperatorError):
                    operator._busy(self.profile)
                probe.assert_not_called()

    def test_busy_probes_both_legacy_locks_and_allows_retained_completion(self):
        units = {"farm-recovery-finished.service": {"ActiveState": "active", "SubState": "exited", "MainPID": "0", "ControlPID": "0"}}
        with mock.patch.object(operator, "_reservation", return_value=None), \
                mock.patch.object(operator, "_units", return_value=units), \
                mock.patch.object(operator, "_mounted", return_value=self.mount_row()), \
                mock.patch.object(operator, "_probe_lock") as probe:
            operator._busy(self.profile)
        self.assertEqual(probe.call_args_list, [mock.call(self.state / "operation.lock"),
                                               mock.call(self.mountpoint / ".farm-recovery.lock")])

    def test_actual_legacy_flock_is_not_bypassed(self):
        lock = self.state / "operation.lock"
        lock.touch(mode=0o600)
        native_fstat = os.fstat
        def root_owned(fd):
            value = native_fstat(fd)
            return SimpleNamespace(st_mode=value.st_mode, st_uid=0)
        with recovery.locked(lock), mock.patch.object(operator.os, "fstat", side_effect=root_owned):
            with self.assertRaisesRegex(operator.OperatorError, "already running"):
                operator._probe_lock(lock)
        with mock.patch.object(operator.os, "fstat", side_effect=root_owned):
            operator._probe_lock(lock)

    def test_plain_unmount_never_powers_off_and_busy_unmount_is_not_forced(self):
        self.mounted = self.mount_row()
        with self.operations(), mock.patch.object(operator, "_poweroff_device", return_value=str(self.disk)):
            result = operator.detach(self.profile)
        self.assertTrue(result["safe_to_unplug"])
        self.assertFalse(result["powered_off"])
        self.assertFalse(any(Path(argv[0]).name == "udisksctl" for argv in self.calls))
        self.assertIn(["/usr/bin/umount", "--", str(self.mountpoint)], self.calls)
        self.mounted = self.mount_row()
        original = self.command
        def busy(argv, **kwargs):
            if Path(argv[0]).name == "umount":
                raise operator.OperatorError("Busy fixture")
            return original(argv, **kwargs)
        with self.operations(), mock.patch.object(operator, "_command", side_effect=busy), self.assertRaises(operator.OperatorError):
            operator.detach(self.profile, power_off=True)
        self.assertIsNotNone(self.mounted)
        self.assertFalse(any("--force" in argv or "-f" in argv or "-l" in argv for argv in self.calls))

    def test_eject_unmounts_before_powering_off_verified_whole_disk(self):
        self.mounted = self.mount_row()
        def verify(profile):
            self.assertIsNone(self.mounted)
            self.assertEqual(profile, self.profile)
            return str(self.disk)
        with self.operations(), mock.patch.object(operator, "_poweroff_device", side_effect=verify) as verified:
            result = operator.detach(self.profile, power_off=True)
        verified.assert_called_once_with(self.profile)
        self.assertTrue(result["powered_off"])
        self.assertTrue(result["safe_to_unplug"])
        self.assertEqual(result["drive_state"], "disconnected")
        self.assertIn(["/usr/bin/udisksctl", "power-off", "--block-device", str(self.by_id), "--no-user-interaction"], self.calls)

    def test_new_busy_state_after_unmount_prevents_poweroff_and_safe_claim(self):
        self.mounted = self.mount_row()
        with self.operations(), mock.patch.object(operator, "_poweroff_device", side_effect=operator.OperatorError("Identity changed")):
            with self.assertRaises(operator.OperatorError):
                operator.detach(self.profile, power_off=True)
        self.assertFalse(any(Path(argv[0]).name == "udisksctl" for argv in self.calls))

    def test_start_auto_mounts_authenticates_and_launches_detached_quiesced_runner(self):
        with self.operations(), mock.patch.object(recovery, "load_policy", return_value={"mode": "server", "state_dir": str(self.state)}), \
                mock.patch.object(recovery, "validate_source_mounts"), mock.patch.object(recovery, "managed_state"), \
                mock.patch.object(operator, "_private", side_effect=lambda path: path), \
                mock.patch.object(recovery, "open_repository") as authenticate:
            result = operator.start(self.profile)
        launches = [argv for argv in self.calls if Path(argv[0]).name == "systemd-run"]
        self.assertEqual(len(launches), 1)
        for argument in ("--service-type=exec", "--property=KillMode=mixed",
                         "--property=TimeoutStopSec=infinity", "--property=UMask=0077", "--property=Restart=no"):
            self.assertIn(argument, launches[0])
        self.assertNotIn("--remain-after-exit", launches[0])
        self.assertNotIn("--collect", launches[0])
        self.assertIn("run", launches[0])
        self.assertTrue(authenticate.call_args.args[0].quiesce)
        reservation = json.loads((self.runtime / "launch.json").read_text())
        self.assertIn(reservation["token"], launches[0])
        self.assertEqual(reservation["unit"], result["unit"])
        self.assertEqual(result["status"], "running")

    def test_invalid_capture_state_prevents_job_launch(self):
        self.mounted = self.mount_row()
        with self.operations(), mock.patch.object(recovery, "load_policy", return_value={"mode": "server", "state_dir": str(self.state)}), \
                mock.patch.object(recovery, "validate_source_mounts"), \
                mock.patch.object(operator, "_private", side_effect=lambda path: path), \
                mock.patch.object(recovery, "managed_state", side_effect=recovery.RecoveryError("Incomplete capture")), \
                self.assertRaises(recovery.RecoveryError):
            operator.start(self.profile)
        self.assertFalse(any(Path(argv[0]).name == "systemd-run" for argv in self.calls))
        self.assertFalse((self.runtime / "launch.json").exists())

    def test_launch_failure_clears_only_proven_unstarted_reservation(self):
        for unstarted in (True, False):
            with self.subTest(unstarted=unstarted):
                self.mounted = self.mount_row()
                def command(argv, **kwargs):
                    if Path(argv[0]).name == "systemd-run":
                        raise operator.OperatorError("Launch query interrupted")
                    if Path(argv[0]).name == "systemctl":
                        if unstarted:
                            return "LoadState=not-found\nActiveState=inactive\nMainPID=0\nControlPID=0\n"
                        return "LoadState=loaded\nActiveState=activating\nMainPID=777\nControlPID=0\n"
                    return self.command(argv, **kwargs)
                with self.operations(), mock.patch.object(operator, "_command", side_effect=command), \
                        mock.patch.object(recovery, "load_policy", return_value={"mode": "server", "state_dir": str(self.state)}), \
                        mock.patch.object(recovery, "validate_source_mounts"), mock.patch.object(recovery, "managed_state"), \
                        mock.patch.object(operator, "_private", side_effect=lambda path: path), \
                        mock.patch.object(recovery, "open_repository"), \
                        mock.patch.object(operator, "_reservation", side_effect=lambda: json.loads((self.runtime / "launch.json").read_text())), \
                        self.assertRaises(operator.OperatorError):
                    operator.start(self.profile)
                self.assertEqual((self.runtime / "launch.json").exists(), not unstarted)

    def test_accepted_runner_preflight_failure_is_published_before_unit_collection(self):
        token = "a" * 32
        reserved = {"unit": "farm-recovery-new.service", "token": token,
                    "created_at": "2026-10-12T11:00:00+00:00"}
        for failing_check in ("_busy", "_validate_repository"):
            (self.runtime / "launch.json").write_text(json.dumps(reserved))
            published = []
            def record(value):
                published.append(dict(value))
            with self.subTest(failing_check=failing_check), \
                    mock.patch.object(operator, "_require_root"), \
                    mock.patch.object(operator, "_lifecycle", side_effect=lambda **_: contextlib.nullcontext()), \
                    mock.patch.object(operator, "_reservation", return_value=reserved), \
                    mock.patch.object(operator, "_busy"), \
                    mock.patch.object(operator, "_validate_repository"), \
                    mock.patch.object(operator, failing_check, side_effect=operator.OperatorError("Fixture preflight failure")), \
                    mock.patch.object(operator, "_publish", side_effect=record), \
                    mock.patch.object(recovery, "backup") as backup:
                with self.assertRaises(operator.OperatorError):
                    operator.run(self.profile, token)
            backup.assert_not_called()
            self.assertTrue(published, "Accepted launch must publish its failure before its service is collected")
            self.assertEqual(published[-1]["status"], "failed")
            self.assertEqual(published[-1]["unit"], reserved["unit"])
            self.assertIsNotNone(operator._timestamp(published[-1]["finished_at"]))

    def test_poweroff_prechecks_reject_mounts_holders_and_swap(self):
        device = {"disk": str(self.disk), "device": str(self.partition), "nodes": [self.disk_row, self.partition_row]}
        for condition in ("root-mounted", "holder", "unknown-holders", "swap", "idle"):
            self.partition_row["mountpoints"] = ["/"] if condition == "root-mounted" else []
            swaps = "Filename Type Size Used Priority\n"
            if condition == "swap":
                swaps += f"{self.partition} partition 100 0 -2\n"
            with self.subTest(condition=condition), \
                    mock.patch.object(operator, "_device", return_value=device), \
                    mock.patch.object(Path, "is_dir", return_value=condition != "unknown-holders"), \
                    mock.patch.object(Path, "iterdir", return_value=iter([Path("holder")] if condition == "holder" else [])), \
                    mock.patch.object(Path, "read_text", return_value=swaps):
                if condition == "idle":
                    self.assertEqual(operator._poweroff_device(self.profile), str(self.disk))
                else:
                    with self.assertRaises(operator.OperatorError):
                        operator._poweroff_device(self.profile)

    def test_poweroff_command_failure_rechecks_idle_before_partial_success(self):
        self.mounted = self.mount_row()
        def command(argv, **kwargs):
            if Path(argv[0]).name == "udisksctl":
                raise operator.OperatorError("Power-off unavailable")
            return self.command(argv, **kwargs)
        with self.operations(), mock.patch.object(operator, "_command", side_effect=command), \
                mock.patch.object(operator, "_poweroff_device", return_value=str(self.disk)) as verify:
            result = operator.detach(self.profile, power_off=True)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["powered_off"])
        self.assertTrue(result["safe_to_unplug"])
        self.mounted = self.mount_row()
        with self.operations(), mock.patch.object(operator, "_command", side_effect=command), \
                mock.patch.object(operator, "_poweroff_device", side_effect=[str(self.disk), operator.OperatorError("New holder")]), \
                self.assertRaises(operator.OperatorError):
            operator.detach(self.profile, power_off=True)

    def test_mountpoint_symlinks_and_writable_parents_are_rejected(self):
        real = self.root / "other"
        real.mkdir()
        alias = self.root / "mount-alias"
        alias.symlink_to(real)
        with self.assertRaises(recovery.RecoveryError):
            operator._mountpoint(dict(self.profile, mountpoint=str(alias)))
        native = Path.stat
        def writable(path, *args, **kwargs):
            value = native(path, *args, **kwargs)
            return SimpleNamespace(st_uid=0, st_mode=value.st_mode | (0o020 if path == self.mountpoint else 0))
        with mock.patch.object(Path, "stat", side_effect=None, autospec=True) as patched:
            patched.side_effect = writable
            with self.assertRaises(operator.OperatorError):
                operator._mountpoint(self.profile)

    def test_new_lifecycle_gate_rejects_second_operation_and_releases_after_exit(self):
        lock = self.root / "lifecycle.lock"
        native_stat, native_fstat = Path.stat, os.fstat
        def root_stat(path, *args, **kwargs):
            value = native_stat(path, *args, **kwargs)
            return SimpleNamespace(st_uid=0, st_mode=value.st_mode, st_nlink=value.st_nlink)
        def root_fstat(fd):
            value = native_fstat(fd)
            return SimpleNamespace(st_uid=0, st_mode=value.st_mode, st_nlink=value.st_nlink)
        with mock.patch.object(recovery, "LIFECYCLE_LOCK", lock), \
                mock.patch.object(Path, "stat", root_stat), mock.patch.object(os, "fstat", side_effect=root_fstat):
            with recovery.lifecycle_lock():
                with self.assertRaisesRegex(recovery.RecoveryError, "operation is running"):
                    with recovery.lifecycle_lock():
                        self.fail("Second operation must not enter")
            with recovery.lifecycle_lock():
                self.assertTrue(lock.exists())

    def test_direct_server_runner_shares_gate_and_respects_pending_operator_launch(self):
        import io
        for mode, pending in (("server", False), ("server", True), ("fixture", True)):
            entered = []
            @contextlib.contextmanager
            def gate():
                entered.append(True)
                try:
                    yield
                finally:
                    entered.pop()
            def perform_backup(args, policy):
                self.assertEqual(bool(entered), mode == "server")
                return {"status": "fixture-complete"}
            with self.subTest(mode=mode, pending=pending), \
                    mock.patch.object(recovery, "load_policy", return_value={"mode": mode}), \
                    mock.patch.object(recovery, "lifecycle_lock", side_effect=gate) as lock, \
                    mock.patch.object(recovery.os, "geteuid", return_value=0), \
                    mock.patch.object(recovery.os, "umask"), mock.patch.object(recovery.signal, "signal"), \
                    mock.patch.object(recovery.os.path, "lexists", return_value=pending), \
                    mock.patch.object(recovery, "backup", side_effect=perform_backup) as backup, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = recovery.main(["--policy", self.profile["policy"], "--restic", "/fixture/restic", "backup",
                                        "--drive", str(self.mountpoint), "--password-file", self.profile["password_file"]])
            self.assertEqual(lock.call_count, int(mode == "server"))
            self.assertEqual(result, 1 if mode == "server" and pending else 0)
            self.assertEqual(backup.call_count, 0 if mode == "server" and pending else 1)
            self.assertFalse(entered)

    def test_saved_completion_survives_older_journal_and_observation_failure(self):
        old = dt.datetime(2026, 10, 12, 10, tzinfo=dt.timezone.utc)
        new = dt.datetime(2026, 10, 12, 11, tzinfo=dt.timezone.utc)
        saved = {"schema": "farm.recovery-operator-state/1", "last_backup": new.isoformat()}
        (self.public / "latest.json").write_text("{}")
        receipt = {"schema": "farm.recovery-run/1", "status": "complete", "id": "a" * 32,
                   "snapshot_id": "b" * 64, "finished_at": old.isoformat()}
        events = [(("farm-recovery-collected.service", "c" * 32, "123"), old.timestamp(), receipt)]
        with mock.patch.object(operator, "drive_state", return_value={"drive_state": "disconnected"}), \
                mock.patch.object(operator, "_units", return_value={}), \
                mock.patch.object(operator, "_read_json", return_value=saved), \
                mock.patch.object(operator, "_events", return_value=events):
            result = operator.status(self.profile)
            self.assertEqual(result["last_backup"], new.isoformat())
            with mock.patch.object(operator, "_events", side_effect=operator.OperatorError("Unavailable")):
                failed = operator.status(self.profile)
            self.assertEqual(failed["last_backup"], new.isoformat())
            self.assertEqual(failed["status"], "unknown")

    def test_current_completion_requires_successful_exit_and_matching_identity(self):
        unit = "farm-recovery-current.service"
        invocation = "c" * 32
        now = dt.datetime.now(dt.timezone.utc)
        receipt = {"schema": "farm.recovery-run/1", "status": "complete", "id": "a" * 32,
                   "snapshot_id": "b" * 64, "finished_at": now.isoformat()}
        active = {"ActiveState": "active", "SubState": "running", "MainPID": "123", "ControlPID": "0",
                  "ExecMainPID": "123", "InvocationID": invocation, "Result": "success",
                  "ExecMainCode": "1", "ExecMainStatus": "0"}
        events = [((unit, invocation, "123"), now.timestamp(), receipt)]
        with mock.patch.object(operator, "drive_state", return_value={"drive_state": "mounted"}), \
                mock.patch.object(operator, "_units", return_value={unit: active}), \
                mock.patch.object(operator, "_events", return_value=events):
            running = operator.status(self.profile)
            self.assertEqual(running["status"], "running")
            self.assertIsNone(running["last_backup"])
            active.update(SubState="exited", MainPID="0")
            completed = operator.status(self.profile)
            self.assertEqual(completed["status"], "complete")
            events[0] = ((unit, invocation, "different"), now.timestamp(), receipt)
            wrong_process = operator.status(self.profile)
            self.assertNotEqual(wrong_process["status"], "complete")
            self.assertIsNone(wrong_process["last_backup"])

    def test_progress_stream_preserves_stdout_and_publishes_only_progress_events(self):
        import io
        output = io.StringIO()
        summary = {"status": "running"}
        stream = operator._Progress(output, summary)
        first = '{"phase":"backup","bytes_done":42,'
        second = '"files_done":3}\nnot JSON\n'
        with mock.patch.object(operator, "_publish") as publish:
            stream.write(first)
            publish.assert_not_called()
            stream.write(second)
            stream.flush()
        self.assertEqual(output.getvalue(), first + second)
        self.assertEqual(summary["bytes_done"], 42)
        self.assertEqual(summary["files_done"], 3)
        publish.assert_called_once_with(summary)

    def test_collected_unit_keeps_terminal_result_and_missing_running_unit_is_unknown(self):
        previous = "2026-10-12T10:00:00+00:00"
        latest = "2026-10-12T11:00:00+00:00"
        saved = {"schema": "farm.recovery-operator-state/1", "unit": "farm-recovery-collected.service",
                 "started_at": latest, "finished_at": latest, "last_backup": previous,
                 "bytes_done": 12345, "files_done": 56}
        (self.public / "latest.json").write_text("{}")
        for state, expected in (("complete", "complete"), ("failed", "failed"), ("running", "unknown")):
            saved.update(status=state, last_backup=latest if state == "complete" else previous)
            with self.subTest(state=state), \
                    mock.patch.object(operator, "drive_state", return_value={"drive_state": "disconnected"}), \
                    mock.patch.object(operator, "_units", return_value={}), \
                    mock.patch.object(operator, "_events", return_value=[]), \
                    mock.patch.object(operator, "_read_json", return_value=saved):
                result = operator.status(self.profile)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["last_backup"], saved["last_backup"])
            if state != "running":
                self.assertEqual(result["bytes_done"], 12345)
                self.assertEqual(result["files_done"], 56)
            else:
                self.assertTrue(result["progress_stale"])

    def test_new_failure_is_not_masked_by_retained_old_unit_or_older_success(self):
        unit = {"ActiveState": "failed", "SubState": "failed", "MainPID": "0", "ControlPID": "0",
                "ExecMainStartTimestamp": "Mon 2026-10-12 09:00:00 UTC",
                "ExecMainExitTimestamp": "Mon 2026-10-12 09:01:00 UTC", "Result": "exit-code"}
        saved = {"schema": "farm.recovery-operator-state/1", "status": "failed",
                 "finished_at": "2026-10-12T11:00:00+00:00", "last_backup": "2026-10-12T10:00:00+00:00"}
        (self.public / "latest.json").write_text("{}")
        with mock.patch.object(operator, "drive_state", return_value={"drive_state": "mounted"}), \
                mock.patch.object(operator, "_units", return_value={"farm-recovery-old.service": unit}), \
                mock.patch.object(operator, "_events", return_value=[]), \
                mock.patch.object(operator, "_read_json", return_value=saved):
            result = operator.status(self.profile)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["last_backup"], saved["last_backup"])

    def test_newer_missing_run_is_not_masked_by_retained_successful_unit(self):
        unit = {"ActiveState": "active", "SubState": "exited", "MainPID": "0", "ControlPID": "0",
                "ExecMainStartTimestamp": "Mon 2026-10-12 09:00:00 UTC",
                "ExecMainExitTimestamp": "Mon 2026-10-12 09:01:00 UTC", "Result": "success",
                "ExecMainCode": "1", "ExecMainStatus": "0"}
        saved = {"schema": "farm.recovery-operator-state/1", "status": "running",
                 "unit": "farm-recovery-newer.service", "started_at": "2026-10-12T11:00:00+00:00",
                 "last_backup": "2026-10-12T10:00:00+00:00"}
        (self.public / "latest.json").write_text("{}")
        with mock.patch.object(operator, "drive_state", return_value={"drive_state": "mounted"}), \
                mock.patch.object(operator, "_units", return_value={"farm-recovery-old.service": unit}), \
                mock.patch.object(operator, "_events", return_value=[]), \
                mock.patch.object(operator, "_read_json", return_value=saved):
            result = operator.status(self.profile)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["last_backup"], saved["last_backup"])
        self.assertTrue(result["progress_stale"])

    def test_public_summary_omits_private_payload_and_preserves_previous_completion(self):
        previous = "2026-10-12T10:00:00+00:00"
        (self.public / "latest.json").write_text("{}")
        native_stat = Path.stat
        def root_stat(path, *args, **kwargs):
            fields = list(native_stat(path, *args, **kwargs))
            fields[4] = 0
            return os.stat_result(fields)
        with mock.patch.object(Path, "stat", root_stat), \
                mock.patch.object(operator, "_read_json", return_value={"last_backup": previous}):
            operator._publish({"status": "running", "phase": "backup", "bytes_done": 42,
                               "password": "private fixture", "stderr": "private diagnostic", "capture_paths": ["private"]})
        path = self.public / "latest.json"
        value = json.loads(path.read_text())
        self.assertEqual(value["last_backup"], previous)
        self.assertEqual(value["bytes_done"], 42)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertNotIn("private", path.read_text())
        self.assertFalse(list(self.public.glob("*.tmp-*")))

    def test_public_status_directory_is_readable_under_private_runner_umask(self):
        self.public.rmdir()
        private = self.state / "private-capture"
        private.mkdir(mode=0o700)
        native_stat = Path.stat
        def root_stat(path, *args, **kwargs):
            fields = list(native_stat(path, *args, **kwargs))
            fields[4] = 0
            return os.stat_result(fields)
        previous_umask = os.umask(0o077)
        try:
            with mock.patch.object(Path, "stat", root_stat):
                operator._publish({"status": "running", "phase": "manifest"})
        finally:
            os.umask(previous_umask)
        self.assertEqual(self.public.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.public / "latest.json").stat().st_mode & 0o777, 0o644)
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)

    def test_native_command_timeout_and_output_limit_reap_child_and_close_pipes(self):
        native_popen = operator.subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = native_popen(*args, **kwargs)
            children.append(child)
            return child
        cases = (("import time; time.sleep(30)", "timed out", 0.02),
                 ("import os,time; os.write(1,b'x'*4096); time.sleep(30)", "exceeded", 2),
                 ("import os,time; os.write(2,b'x'*4096); time.sleep(30)", "exceeded", 2),
                 ("import sys; sys.stderr.write('private fixture'); sys.exit(2)", "failed", 2))
        for code, message, timeout in cases:
            with self.subTest(message=message, code=code), \
                    mock.patch.object(operator.subprocess, "Popen", side_effect=spawn), \
                    mock.patch.object(operator, "MAX_OUTPUT", 1024):
                with self.assertRaisesRegex(operator.OperatorError, message) as error:
                    operator._command([sys.executable, "-I", "-B", "-c", code], timeout=timeout)
            self.assertNotIn("private fixture", str(error.exception))
            self.assertIsNotNone(children[-1].poll())
            self.assertTrue(children[-1].stdout.closed)
            self.assertTrue(children[-1].stderr.closed)


if __name__ == "__main__":
    unittest.main()
