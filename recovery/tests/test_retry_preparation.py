"""Incident retry preparation fixtures never query or alter the real host."""
import contextlib
import errno
import fcntl
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("farm_retry_preparation_fixture", HERE / "prepare-backup-retry.py")
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)
runner_spec = importlib.util.spec_from_file_location("farm_retry_runner_fixture", HERE / "farm_recovery.py")
runner = importlib.util.module_from_spec(runner_spec)
runner_spec.loader.exec_module(runner)


class RetryPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm-retry-preparation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.drive = self.root / "drive"
        self.current = self.state / "current"
        self.captures = self.current / "captures"
        self.receipts = self.state / "receipts"
        self.destination = self.state / ("failed-" + retry.RUN_ID)
        for directory in (self.state, self.drive, self.current, self.captures, self.receipts):
            directory.mkdir(mode=0o700)
        for path in (self.state / "operation.lock", self.drive / ".farm-recovery.lock"):
            path.touch(mode=0o600)
        self.bad_source = self.root / "removed-coverage.html"
        patcher = mock.patch.object(retry, "BAD_SOURCE", self.bad_source)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile = json.loads((HERE / "operator.server.json").read_text())
        self.profile.update(state_dir=str(self.state), mountpoint=str(self.drive))
        self.policy = {
            "schema": "farm.recovery-policy/1", "mode": "server", "state_dir": str(self.state),
            "host_id": "fixture-host", "source_roots": [str(self.root / "source")],
            "offline_stores": [{"units": ["fixture.service", "fixture.socket", "inactive.service"]}],
        }
        initial = {
            "fixture.service": {"ActiveState": "active", "SubState": "running", "MainPID": "20"},
            "fixture.socket": {"ActiveState": "active", "SubState": "listening", "MainPID": "0"},
            "inactive.service": {"ActiveState": "inactive", "SubState": "dead", "MainPID": "0"},
        }
        active = ["fixture.service", "fixture.socket"]
        self.capture = {
            "schema": "farm.recovery-captures/1", "status": "complete", "errors": [],
            "initial_unit_states": initial,
            "service_actions": [{"unit": unit, "action": action, "status": "confirmed"}
                                for unit in active for action in ("stop", "start")],
            "service_restoration": [{"unit": unit, "status": "restored"} for unit in active],
        }
        self.run = {
            "schema": "farm.recovery-run/1", "id": retry.RUN_ID, "status": "incomplete",
            "error": "RecoveryError", "started_at": "2026-10-12T21:41:34+00:00",
            "finished_at": "2026-10-13T13:02:31+00:00",
        }
        self.prior = {
            "schema": "farm.recovery-run/1", "id": retry.PRIOR_RUN_ID, "status": "incomplete",
            "error": "RecoveryError", "started_at": "2026-10-12T21:12:00+00:00",
            "finished_at": "2026-10-12T21:12:47+00:00",
        }
        self.selection = {"roots": self.policy["source_roots"], "excluded_exact_paths": []}
        for path, value in (
            (self.state / ".farm-recovery-state.json", {"schema": "farm.recovery-state/1"}),
            (self.receipts / (retry.RUN_ID + ".json"), self.run),
            (self.receipts / (retry.PRIOR_RUN_ID + ".json"), self.prior),
            (self.current / "policy.json", self.policy),
            (self.current / "capture.json", self.capture),
            (self.captures / "manifest.json", self.capture),
            (self.current / "selection.json", self.selection),
        ):
            self.write_json(path, value)
        self.artifact = self.captures / "fixture-database.tar"
        self.artifact.write_bytes(b"preserve exact original capture bytes\x00\xff")
        self.artifact.chmod(0o600)
        self.snapshot = {
            "id": "a" * 64, "hostname": self.policy["host_id"],
            "time": "2026-10-12T21:42:22+00:00",
            "paths": [*self.selection["roots"], str(self.current)],
            "tags": ["farm-recovery", "pending", "run-" + retry.RUN_ID],
        }
        self.engine = SimpleNamespace(json=mock.Mock(return_value=[self.snapshot]))
        self.recovery = SimpleNamespace(
            open_repository=mock.Mock(return_value=(self.drive, self.engine)),
            write_json=runner.write_json, absolute=runner.absolute,
            now=lambda: "2026-10-13T17:00:00+00:00",
        )
        self.operator = SimpleNamespace(
            _lifecycle=mock.Mock(side_effect=lambda **kwargs: contextlib.nullcontext()),
            _busy=mock.Mock(), _validate_repository=mock.Mock(), _arguments=mock.Mock(),
        )
        self.unit = {
            "Id": retry.UNIT, "LoadState": "loaded", "ActiveState": "failed", "SubState": "failed",
            "MainPID": "0", "ControlPID": "0", "Job": "", "Result": "exit-code",
            "ExecMainCode": "1", "ExecMainStatus": "1",
        }
        self.operator._command = mock.Mock(side_effect=lambda *args, **kwargs:
                                           "".join(f"{key}={value}\n" for key, value in self.unit.items()))
        self.query = retry.unit_state

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def prepare(self, *, execute=False):
        return retry.prepare(recovery=self.recovery, operator=self.operator, policy=self.policy,
                             profile=self.profile, execute=execute, query=self.query)

    def assert_preserved_in_place(self):
        self.assertTrue(self.current.is_dir())
        self.assertEqual(self.artifact.read_bytes(), b"preserve exact original capture bytes\x00\xff")
        self.assertFalse(self.destination.exists())

    def test_default_inspects_without_renaming_or_writing(self):
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        result = self.prepare()
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["status"], "checks-passed-no-changes")
        self.assertFalse(result["backup_started"])
        self.assert_preserved_in_place()
        self.operator._busy.assert_called_once_with(self.profile)
        self.operator._validate_repository.assert_called_once_with(self.profile)
        self.assertEqual(self.engine.json.call_count, 1)
        self.assertEqual(self.engine.json.call_args.args[0][0], "snapshots")

    def test_execute_moves_existing_inodes_and_preserves_receipts(self):
        inode = self.current.stat().st_ino
        artifact_inode = self.artifact.stat().st_ino
        receipts = {path.name: path.read_bytes() for path in self.receipts.iterdir()}
        result = self.prepare(execute=True)
        self.assertEqual(result["status"], "preserved-for-retry")
        self.assertFalse(result["backup_started"])
        self.assertFalse(self.current.exists())
        self.assertEqual(self.destination.stat().st_ino, inode)
        artifact = self.destination / "captures" / self.artifact.name
        self.assertEqual(artifact.stat().st_ino, artifact_inode)
        self.assertEqual(artifact.read_bytes(), b"preserve exact original capture bytes\x00\xff")
        self.assertEqual(receipts, {path.name: path.read_bytes() for path in self.receipts.iterdir()})
        report = self.destination / "retry-preparation.json"
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        saved = json.loads(report.read_text())
        baseline = saved["smart_baseline"]
        self.assertEqual(baseline["source"], "user-reported")
        self.assertEqual(baseline["reported_date"], "2026-10-13")
        self.assertIsNone(baseline["collected_at"])
        self.assertEqual([baseline[key] for key in ("media_errors", "error_log_entries", "unsafe_shutdowns")],
                         [26, 123, 16])
        self.assertEqual(saved["prepared_at"], "2026-10-13T17:00:00+00:00")
        self.assertEqual(saved["pending_snapshot_id"], self.snapshot["id"])

    def test_existing_preservation_destination_never_overwritten(self):
        self.destination.mkdir(mode=0o700)
        sentinel = self.destination / "evidence"
        sentinel.write_text("keep")
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertTrue(self.current.is_dir())

    def test_active_operation_or_pending_launch_prevents_all_evidence_changes(self):
        self.operator._busy.side_effect = RuntimeError("Another operation is active")
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assert_preserved_in_place()
        self.recovery.open_repository.assert_not_called()

    def test_each_real_operation_lock_refuses_contention_and_is_released(self):
        for lock in (self.state / "operation.lock", self.drive / ".farm-recovery.lock"):
            with self.subTest(lock=lock), lock.open("rb") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises((BlockingIOError, RuntimeError)):
                    self.prepare(execute=True)
                self.assert_preserved_in_place()
            self.prepare()

    def test_reappeared_bad_source_and_broken_symlink_both_block(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if symlink:
                    self.bad_source.symlink_to(self.root / "nonexistent-target")
                else:
                    self.bad_source.write_text("extension recreated the coverage report")
                with self.assertRaises(RuntimeError):
                    self.prepare(execute=True)
                self.assert_preserved_in_place()
                self.bad_source.unlink()

    def test_new_run_receipt_requires_new_review(self):
        self.write_json(self.receipts / ("f" * 32 + ".json"), {"status": "incomplete"})
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assert_preserved_in_place()

    def test_wrong_or_completed_failed_run_is_not_preserved(self):
        for change in ({"id": "f" * 32}, {"status": "complete"}, {"snapshot_id": "f" * 64},
                       {"error": "SomethingElse"}, {"finished_at": None},
                       {"finished_at": "2026-10-12T20:00:00+00:00"}):
            with self.subTest(change=change):
                self.write_json(self.receipts / (retry.RUN_ID + ".json"), dict(self.run, **change))
                with self.assertRaises(RuntimeError):
                    self.prepare(execute=True)
                self.assert_preserved_in_place()
        self.write_json(self.receipts / (retry.RUN_ID + ".json"), self.run)

    def test_snapshot_must_match_run_host_paths_time_and_pending_status(self):
        variations = [
            {"id": "short"}, {"hostname": "other-host"}, {"paths": [str(self.current)]},
            {"paths": [*self.snapshot["paths"], "/unexpected"]},
            {"tags": ["farm-recovery", "pending", "run-" + "f" * 32]},
            {"tags": [*self.snapshot["tags"], "farm-recovery-complete"]},
            {"tags": ["farm-recovery", "run-" + retry.RUN_ID]},
            {"time": "2026-10-13T15:00:00+00:00"},
        ]
        for change in variations:
            with self.subTest(change=change):
                self.engine.json.return_value = [dict(self.snapshot, **change)]
                with self.assertRaises(RuntimeError):
                    self.prepare(execute=True)
                self.assert_preserved_in_place()

    def test_no_unique_snapshot_is_never_guessed(self):
        for snapshots in ([], [self.snapshot, self.snapshot]):
            with self.subTest(snapshots=snapshots):
                self.engine.json.return_value = snapshots
                with self.assertRaises(RuntimeError):
                    self.prepare(execute=True)
                self.assert_preserved_in_place()

    def test_unrestored_capture_and_changed_capture_receipt_block(self):
        for change in ({"status": "incomplete"}, {"errors": ["read error"]},
                       {"service_restoration": []}, {"service_actions": []},
                       {"initial_unit_states": {}}):
            with self.subTest(change=change):
                modified = dict(self.capture, **change)
                self.write_json(self.current / "capture.json", modified)
                self.write_json(self.captures / "manifest.json", modified)
                with self.assertRaises(RuntimeError):
                    self.prepare(execute=True)
                self.assert_preserved_in_place()
        self.write_json(self.current / "capture.json", self.capture)
        self.write_json(self.captures / "manifest.json", dict(self.capture, status="incomplete"))
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assert_preserved_in_place()

    def test_current_tree_symlink_cannot_redirect_preservation(self):
        elsewhere = self.state / "unrelated"
        self.current.rename(elsewhere)
        self.current.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assertTrue(elsewhere.is_dir())
        self.assertFalse(self.destination.exists())

    def test_world_readable_metadata_is_rejected(self):
        (self.current / "policy.json").chmod(0o644)
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assert_preserved_in_place()

    def test_saved_policy_drift_blocks(self):
        self.write_json(self.current / "policy.json", dict(self.policy, host_id="changed"))
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assert_preserved_in_place()

    def test_failed_unit_must_be_fully_stopped_without_a_pending_job(self):
        original = dict(self.unit)
        for change in ({"Id": "another.service"}, {"LoadState": "not-found"},
                       {"ActiveState": "active"}, {"SubState": "running"}, {"MainPID": "10"},
                       {"ControlPID": "10"}, {"Job": "123"}, {"Result": "success"},
                       {"ExecMainStatus": "0"}, {"ExecMainCode": "killed"}):
            with self.subTest(change=change):
                self.unit = dict(original, **change)
                with self.assertRaises(RuntimeError):
                    self.prepare(execute=True)
                self.assert_preserved_in_place()

    def test_selection_does_not_allow_missing_duplicate_or_nonabsolute_sources(self):
        for roots in ([], [self.selection["roots"][0]] * 2, ["relative"], ["/tmp/../source"]):
            with self.subTest(roots=roots):
                self.write_json(self.current / "selection.json", dict(self.selection, roots=roots))
                self.engine.json.return_value = [dict(self.snapshot, paths=[*roots, str(self.current)])]
                with self.assertRaises(RuntimeError):
                    self.prepare(execute=True)
                self.assert_preserved_in_place()

    def test_unknown_state_marker_never_authorizes_preservation(self):
        self.write_json(self.state / ".farm-recovery-state.json", {"schema": "something-else"})
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assert_preserved_in_place()

    def test_lock_symlink_is_rejected_without_touching_its_target(self):
        lock = self.state / "operation.lock"
        lock.unlink()
        target = self.root / "unrelated-lock"
        target.write_text("unrelated")
        target.chmod(0o600)
        lock.symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.prepare(execute=True)
        self.assertEqual(target.read_text(), "unrelated")
        self.assert_preserved_in_place()

    def test_source_lstat_io_or_permission_error_does_not_prove_absence(self):
        original_lstat = Path.lstat
        for error_number in (errno.EIO, errno.EACCES):
            def probe(path, *args, **kwargs):
                if path == self.bad_source:
                    raise OSError(error_number, "fixture metadata lookup failed")
                return original_lstat(path, *args, **kwargs)
            with self.subTest(error_number=error_number), \
                    mock.patch.object(Path, "lstat", autospec=True, side_effect=probe):
                with self.assertRaises(OSError):
                    self.prepare(execute=True)
            self.assert_preserved_in_place()
            self.recovery.open_repository.assert_not_called()

    def test_preparation_evidence_write_failure_keeps_original_capture_bytes(self):
        original_inode = self.artifact.stat().st_ino
        with mock.patch.object(self.recovery, "write_json", side_effect=OSError(errno.EIO, "fixture write failure")):
            with self.assertRaisesRegex(RuntimeError, "Stage was preserved.*could not be saved"):
                self.prepare(execute=True)
        self.assertFalse(self.current.exists())
        preserved = self.destination / "captures" / self.artifact.name
        self.assertEqual(preserved.stat().st_ino, original_inode)
        self.assertEqual(preserved.read_bytes(), b"preserve exact original capture bytes\x00\xff")
        self.assertFalse((self.destination / "retry-preparation.json").exists())


if __name__ == "__main__":
    unittest.main()
