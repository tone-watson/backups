"""Coordinator safety and real restic round trips on disposable fixtures only."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import farm_recovery as recovery


class GuardTests(unittest.TestCase):
    def test_rescue_kit_contains_redis_verifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "rescue-kit"
            recovery.kit(destination)
            verifier = destination / "redis_state.py"
            self.assertEqual(verifier.read_bytes(), (HERE / "redis_state.py").read_bytes())
            hashes = json.loads((destination / "SHA256SUMS.json").read_text())
            self.assertEqual(hashes["redis_state.py"], recovery.digest(verifier))

    def test_path_traversal_and_relative_rejected(self):
        for path in ("relative", "/tmp/../etc"):
            with self.assertRaises(recovery.RecoveryError):
                recovery.absolute(path)

    def test_real_directory_rejects_symlink_parent(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real")
            with self.assertRaises(recovery.RecoveryError):
                recovery.real_directory(root / "link" / "new", create=True)
            self.assertFalse((root / "real" / "new").exists())

    def test_password_mode_and_location(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            drive = root / "drive"
            drive.mkdir()
            password = root / "password"
            password.write_text("fixture password")
            password.chmod(0o644)
            with self.assertRaises(recovery.RecoveryError):
                recovery.password_file(password, drive)
            password.chmod(0o600)
            self.assertEqual(recovery.password_file(password, drive), password)
            inside = drive / "password"
            inside.write_text("fixture password")
            inside.chmod(0o600)
            with self.assertRaises(recovery.RecoveryError):
                recovery.password_file(inside, drive)

    def test_missing_or_wrong_mount_never_falls_back(self):
        with tempfile.TemporaryDirectory() as t:
            drive = Path(t)
            policy = {"mode": "server", "source_roots": ["/"]}
            with patch.object(recovery.os, "geteuid", return_value=0), patch.object(
                    recovery, "mount_info", return_value={"target": "/", "uuid": "source", "fstype": "ext4"}):
                with self.assertRaises(recovery.RecoveryError):
                    recovery.validate_drive(drive, policy, "backup")

    def test_lock_contention_and_release(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "lock"
            with recovery.locked(path):
                with self.assertRaises(recovery.RecoveryError):
                    with recovery.locked(path):
                        self.fail("lock acquired concurrently")
            with recovery.locked(path):
                pass

    def test_no_inherited_restic_password_command(self):
        with patch.dict(os.environ, {"RESTIC_PASSWORD_COMMAND": "never-run", "RESTIC_REPOSITORY": "wrong"}):
            self.assertNotIn("RESTIC_PASSWORD_COMMAND", recovery.clean_env())
            self.assertNotIn("RESTIC_REPOSITORY", recovery.clean_env())

    def test_unmanaged_or_incomplete_stage_never_removed(self):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t)
            (state / "current").mkdir()
            precious = state / "current" / "unrelated"
            precious.write_text("keep")
            with self.assertRaises(recovery.RecoveryError):
                recovery.managed_state(state)
            self.assertEqual(precious.read_text(), "keep")
            precious.unlink()
            (state / "current").rmdir()
            recovery.managed_state(state)
            capture = state / "current" / "captures"
            capture.mkdir(parents=True)
            recovery.write_json(capture / "manifest.json", {"status": "incomplete"})
            with self.assertRaises(recovery.RecoveryError):
                recovery.managed_state(state)
            self.assertTrue(capture.is_dir())

    def test_manifest_bounds_require_retained_payloads(self):
        report = {"errors": [], "scan_boundaries": [
            {"path": "/source/deep", "reason": "depth_limit", "incomplete_scope": True}]}
        recovery.validate_manifest(report, [Path("/source")], [])
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_manifest(report, [Path("/source")], ["/source/deep"])
        report["scan_boundaries"][0]["reason"] = "entry_limit"
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_manifest(report, [Path("/source")], [])

    def test_required_snap_dependency_not_excluded_by_parent_mount(self):
        mounts = {"filesystems": [{"target": "/", "fstype": "ext4"},
                                 {"target": "/snap/tool/42", "fstype": "squashfs"},
                                 {"target": "/snap/other/7", "fstype": "squashfs"}]}
        policy = {"mode": "server", "source_roots": ["/", "/snap/tool/42/bin/tool"]}
        with patch.object(recovery, "command", return_value=json.dumps(mounts)):
            excluded = recovery.exclusions_for(policy, Path("/mnt/drive"), Path("/root/password"),
                                               Path("/var/lib/recovery"), {})
        self.assertNotIn("/snap/tool/42", excluded)
        self.assertIn("/snap/other/7", excluded)


RESTIC = os.environ.get("FARM_TEST_RESTIC") or shutil.which("restic")


@unittest.skipUnless(RESTIC, "Set FARM_TEST_RESTIC to exercise real restic")
class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="farm-recovery-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.drive = self.root / "drive"
        self.drive.mkdir()
        self.password = self.root / "password"
        self.password.write_text("disposable fixture key\n")
        self.password.chmod(0o600)
        self.policy_path = self.root / "policy.json"
        self.policy = {"schema": "farm.recovery-policy/1", "mode": "fixture",
                       "fixture_root": str(self.root), "source_roots": [str(self.source)],
                       "manifest_roots": [str(self.source)], "collect_host_metadata": False,
                       "state_dir": str(self.root / "state"), "exclude_paths": [],
                       "host_id": "fixture", "sqlite_roots": [], "sqlite_paths": [],
                       "offline_stores": []}
        self.policy_path.write_text(json.dumps(self.policy))

    def run_cli(self, operation, *extra, expected=0, binary=None):
        cmd = [sys.executable, "-I", str(HERE / "farm_recovery.py"), "--policy", str(self.policy_path),
               "--restic", binary or RESTIC, operation, "--drive", str(self.drive),
               "--password-file", str(self.password), *map(str, extra)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def snapshots(self):
        return json.loads(self.run_cli("snapshots").stdout)

    def restored(self, target):
        return target / str(self.source).lstrip("/")

    def test_incremental_change_delete_old_restore_and_metadata(self):
        data = self.source / "large.bin"
        data.write_bytes(os.urandom(3 * 1024 * 1024))
        data.chmod(0o640)
        (self.source / "hardlink.bin").hardlink_to(data)
        (self.source / "relative-link").symlink_to("large.bin")
        (self.source / "remove.txt").write_text("old version")
        try:
            os.setxattr(data, "user.farm-fixture", b"metadata")
            has_xattr = True
        except OSError:
            has_xattr = False
        self.run_cli("init", "--uuid", "fixture")
        self.run_cli("backup")
        old = self.snapshots()[0]["id"]
        self.run_cli("backup")
        receipts = [json.loads(p.read_text()) for p in (self.root / "state/receipts").glob("*.json")]
        latest_receipt = max(receipts, key=lambda r: r["finished_at"])
        self.assertLess(latest_receipt["summary"]["data_added"], 200000)
        self.assertGreater(latest_receipt["summary"]["files_unmodified"], 0)
        (self.source / "remove.txt").unlink()
        (self.source / "new.txt").write_text("new version")
        self.run_cli("backup")
        target = self.root / "restored-latest"
        self.run_cli("restore", "--target", target)
        restored = self.restored(target)
        self.assertFalse((restored / "remove.txt").exists())
        self.assertEqual((restored / "new.txt").read_text(), "new version")
        self.assertEqual((restored / "large.bin").read_bytes(), data.read_bytes())
        self.assertEqual((restored / "large.bin").stat().st_mode & 0o777, 0o640)
        self.assertEqual((restored / "hardlink.bin").stat().st_ino, (restored / "large.bin").stat().st_ino)
        self.assertEqual(os.readlink(restored / "relative-link"), "large.bin")
        if has_xattr:
            self.assertEqual(os.getxattr(restored / "large.bin", "user.farm-fixture"), b"metadata")
        old_target = self.root / "restored-old"
        self.run_cli("restore", "--target", old_target, "--snapshot", old)
        self.assertEqual((self.restored(old_target) / "remove.txt").read_text(), "old version")
        self.assertFalse((self.restored(old_target) / "new.txt").exists())
        self.run_cli("check", "--read-data")
        before = self.snapshots()
        self.run_cli("retention-plan")
        self.assertEqual(self.snapshots(), before)
        self.run_cli("restore", "--target", target, expected=1)
        self.run_cli("init", expected=1)

    def test_incomplete_restic_exit_not_promoted(self):
        (self.source / "value").write_text("old")
        self.run_cli("init", "--uuid", "fixture")
        self.run_cli("backup")
        before = self.snapshots()
        (self.source / "value").write_text("new")
        wrapper = self.root / "fail-restic"

        wrapper.write_text("#!/usr/bin/python3\nimport subprocess,sys\n"
                           f"result=subprocess.run([{RESTIC!r},*sys.argv[1:]])\n"
                           "raise SystemExit(3 if 'backup' in sys.argv else result.returncode)\n")
        wrapper.chmod(0o700)
        self.run_cli("backup", expected=1, binary=str(wrapper))
        self.assertEqual(self.snapshots(), before)
        target = self.root / "restore-after-failure"
        self.run_cli("restore", "--target", target)
        self.assertEqual((self.restored(target) / "value").read_text(), "old")

    def test_sqlite_capture_applies_using_drive_kit_without_farm(self):
        database = self.source / "live.db"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE recovered(value TEXT)")
            db.execute("INSERT INTO recovered VALUES ('consistent state')")
        self.policy["sqlite_paths"] = [str(database)]
        self.policy_path.write_text(json.dumps(self.policy))
        self.run_cli("init", "--uuid", "fixture")
        self.run_cli("backup")
        target = self.root / "restored-sqlite"
        self.run_cli("restore", "--target", target)
        restored_db = self.restored(target) / "live.db"
        self.assertFalse(restored_db.exists())
        captures = target / str(self.root).lstrip("/") / "state/current/captures"
        result = subprocess.run([str(self.drive / "rescue-kit/farm-recovery"), "apply-captures",
                                 "--target", str(target), "--capture-dir", str(captures)],
                                capture_output=True, text=True, timeout=60, cwd="/")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "applied")
        with sqlite3.connect(restored_db) as db:
            self.assertEqual(db.execute("SELECT value FROM recovered").fetchall(), [("consistent state",)])
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        self.assertTrue((target / "FARM-APPLY-RECEIPT.json").is_file())


if __name__ == "__main__":
    unittest.main()
