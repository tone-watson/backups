"""Run only synthetic temporary-file recovery captures; no root or live snapshots."""
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("capture_api_uv", Path(__file__).with_name("capture-api-uv.py"))
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "api"
        self.bundle = self.root / "bundle"
        self.destination = self.root / "backup"
        self.repo.mkdir()
        (self.bundle / "wheelhouse").mkdir(parents=True)
        for relative in capture.RECIPES:
            path = self.repo / relative
            if relative.endswith(('.sh', '.py')) or relative == '.python-version':
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            else:
                path.mkdir(parents=True, exist_ok=True)
                (path / "fixture.txt").write_text("fixture\n")
        self.wheel = self.bundle / "wheelhouse/example-1.0-py3-none-any.whl"
        self.wheel.write_bytes(b"inert fixture bytes")
        self.wheel.chmod(0o644)
        self.item = {"name": "example", "version": "1.0", "filename": self.wheel.name,
                     "bytes": self.wheel.stat().st_size, "sha256": hashlib.sha256(self.wheel.read_bytes()).hexdigest()}
        receipt = {"status": "passed", "artifact_count": 1, "artifacts": {"example": self.item}}
        receipt_bytes = json.dumps(receipt).encode()
        (self.bundle / "receipt.json").write_bytes(receipt_bytes)
        lock = b"example==1.0\n"
        (self.bundle / "requirements.lock").write_bytes(lock)
        (self.repo / "deploy/runtime/requirements.lock").write_bytes(lock)
        manifest = {"artifact_bundle": str(self.bundle), "artifacts": [self.item],
                    "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                    "lock_sha256": hashlib.sha256(lock).hexdigest()}
        (self.repo / "deploy/runtime/artifacts.json").write_text(json.dumps(manifest))
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], check=True)

    def test_copy_and_daily_reuse_preserve_source_inode_and_permissions(self):
        first = capture.capture(self.repo, self.destination)
        self.assertEqual(first["copied_wheels"], 1)
        copied = self.destination / "wheelhouse" / self.wheel.name
        self.assertNotEqual(copied.stat().st_ino, self.wheel.stat().st_ino)
        self.assertEqual(copied.stat().st_nlink, 1)
        inode = copied.stat().st_ino
        copied.chmod(0o600)
        self.assertEqual(self.wheel.stat().st_mode & 0o777, 0o644)
        second = capture.capture(self.repo, self.destination)
        self.assertEqual(second["copied_wheels"], 0)
        self.assertEqual(second["reused_wheels"], 1)
        self.assertEqual(copied.stat().st_ino, inode)

    def test_existing_source_hardlink_is_replaced_before_permission_changes(self):
        (self.destination / "wheelhouse").mkdir(parents=True)
        copied = self.destination / "wheelhouse" / self.wheel.name
        os.link(self.wheel, copied)
        self.assertEqual(capture.capture(self.repo, self.destination)["copied_wheels"], 1)
        copied.chmod(0o600)
        self.assertEqual(self.wheel.stat().st_mode & 0o777, 0o644)
        self.assertNotEqual(copied.stat().st_ino, self.wheel.stat().st_ino)

    def test_corrupt_source_fails_without_success_receipt(self):
        self.wheel.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "hash/size"):
            capture.capture(self.repo, self.destination)
        self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")
        self.assertFalse((self.destination / "receipt.json").exists())

    def test_corrupt_destination_is_repaired(self):
        capture.capture(self.repo, self.destination)
        copied = self.destination / "wheelhouse" / self.wheel.name
        copied.write_bytes(b"corrupt")
        self.assertEqual(capture.capture(self.repo, self.destination)["copied_wheels"], 1)
        self.assertEqual(copied.read_bytes(), self.wheel.read_bytes())

    def test_symlink_wheel_and_destination_fail_closed(self):
        original = self.wheel.with_suffix('.source')
        self.wheel.rename(original)
        self.wheel.symlink_to(original)
        with self.assertRaises(OSError):
            capture.capture(self.repo, self.destination)
        self.wheel.unlink()
        original.rename(self.wheel)
        self.destination.joinpath('wheelhouse').rmdir()
        self.destination.joinpath('wheelhouse').symlink_to(self.bundle / 'wheelhouse')
        with self.assertRaises(ValueError):
            capture.capture(self.repo, self.destination)

    def test_uncommitted_runtime_recipe_is_not_captured(self):
        (self.repo / "deploy/runtime/artifacts.json").write_text("not committed")
        result = capture.capture(self.repo, self.destination)
        self.assertEqual(result["status"], "passed")
        self.assertNotEqual((self.destination / "artifacts.json").read_text(), "not committed")

    def test_receipt_and_bundle_lock_must_match_committed_hashes(self):
        for filename in ('receipt.json', 'requirements.lock'):
            path = self.bundle / filename
            old = path.read_bytes()
            path.write_bytes(b"wrong")
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                capture.capture(self.repo, self.destination)
            path.write_bytes(old)


if __name__ == '__main__':
    unittest.main()
