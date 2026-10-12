"""Exercise exact runtime omissions with real tar and disposable source trees."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import tarfile
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("runtime_capture", Path(__file__).parents[1] / "capture.py")
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class RuntimeCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm-runtime-capture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "lxd"
        self.source.mkdir()
        (self.source / "database").mkdir()
        (self.source / "database/state").write_bytes(b"retained application state")
        self.stage = self.root / "captures"
        self.helper = capture._archive_helper()
        recipes = copy.deepcopy(self.helper.RUNTIME_RECIPES)
        recipes["lxd-snap-v1"]["source"] = str(self.source)
        patch = mock.patch.object(self.helper, "RUNTIME_RECIPES", recipes)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(capture, "_archive_helper", return_value=self.helper)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = {"id": "lxd", "path": str(self.source),
                      "units": ["fixture.service"], "runtime_recipe": "lxd-snap-v1"}
        self.calls = []
        self.real_run = capture._run

    def native(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "/usr/bin/systemctl":
            self.assertEqual(argv[1], "show", "Fixtures must never call actual service controls")
            return (b"Id=fixture.service\nNames=fixture.service\nLoadState=loaded\n"
                    b"ActiveState=inactive\nSubState=dead\nMainPID=0\nControlPID=0\n")
        return self.real_run(argv, **kwargs)

    def run_capture(self, native=None):
        with mock.patch.object(capture, "_run", side_effect=native or self.native):
            return capture.capture_state({"offline_stores": [self.store]}, self.stage)

    def create_runtime(self):
        (self.source / "shmounts").symlink_to("/var/snap/lxd/common/shmounts/instances")
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.source / "unix.socket"))

    def test_real_runtime_omissions_preserve_state_and_restore_exact_link(self):
        self.create_runtime()
        receipt = self.run_capture()
        self.assertEqual(receipt["status"], "complete", receipt)
        entry = receipt["captures"][0]
        self.assertEqual(entry["runtime_entries"]["recipe"], "lxd-snap-v1")
        self.assertTrue(all(x["present"] for x in entry["runtime_entries"]["entries"]))
        with tarfile.open(self.stage / entry["artifact"]) as archive:
            self.assertEqual(archive.extractfile("lxd/database/state").read(), b"retained application state")
            self.assertNotIn("lxd/shmounts", archive.getnames())
            self.assertNotIn("lxd/unix.socket", archive.getnames())
        target = self.root / "restore"
        target.mkdir()
        (target / "FARM-RESTORE-RECEIPT.json").write_text(json.dumps({"status": "extracted-and-verified"}))
        restored_source = target / str(self.source).lstrip("/")
        restored_source.parent.mkdir(parents=True)
        restored_captures = target / "captures"
        self.stage.rename(restored_captures)
        applied = self.helper.apply_captures(target, restored_captures)
        self.assertEqual(applied["status"], "applied", applied)
        self.assertEqual((restored_source / "database/state").read_bytes(), b"retained application state")
        self.assertEqual(os.readlink(restored_source / "shmounts"), "/var/snap/lxd/common/shmounts/instances")
        self.assertFalse(os.path.lexists(restored_source / "unix.socket"))
        self.assertTrue(applied["runtime_recovery"])

    def test_missing_runtime_entries_are_recorded_without_losing_payload(self):
        receipt = self.run_capture()
        self.assertEqual(receipt["status"], "complete", receipt)
        runtime = receipt["captures"][0]["runtime_entries"]
        self.assertTrue(all(not entry["present"] for entry in runtime["entries"]))
        self.assertEqual(receipt["exclusions"], [str(self.source)])

    def test_wrong_runtime_type_fails_before_any_service_control_or_archive(self):
        (self.source / "shmounts").write_bytes(b"must never omit actual data")
        with mock.patch.object(capture, "_run", side_effect=AssertionError("must fail before commands")):
            receipt = capture.capture_state({"offline_stores": [self.store]}, self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertIn("runtime entries", receipt["errors"][0]["error"])
        self.assertEqual(receipt["captures"], [])
        self.assertEqual((self.source / "shmounts").read_bytes(), b"must never omit actual data")

    def test_source_change_during_archive_prevents_publication(self):
        (self.source / "shmounts").symlink_to("/var/snap/lxd/common/shmounts/instances")
        def changed(argv, **kwargs):
            result = self.native(argv, **kwargs)
            if argv[0] == "/usr/bin/tar":
                (self.source / "shmounts").unlink()
            return result
        receipt = self.run_capture(changed)
        self.assertEqual(receipt["status"], "incomplete", receipt)
        self.assertIn("changed during capture", receipt["errors"][0]["error"])
        self.assertEqual(receipt["exclusions"], [])
        self.assertFalse((self.stage / "lxd.tar").exists())

    def test_unreviewed_absolute_link_still_rejected(self):
        (self.source / "unreviewed").symlink_to("/outside/keep-guard-strict")
        receipt = self.run_capture()
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["captures"], [])
        self.assertIn("manual recovery recipe", receipt["errors"][0]["error"])

    def test_no_recipe_cannot_silently_omit_runtime_entries(self):
        self.store.pop("runtime_recipe")
        (self.source / "shmounts").symlink_to("/var/snap/lxd/common/shmounts/instances")
        receipt = self.run_capture()
        self.assertEqual(receipt["status"], "incomplete")
        self.assertFalse((self.stage / "lxd.tar").exists())


if __name__ == "__main__":
    unittest.main()
