"""Known daemon runtime entries are omitted, never generalized tar exceptions."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("runtime_restore_test_module", Path(__file__).parents[1] / "apply_captures.py")
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


class RuntimeRestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm-runtime-restore-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source/lxd"
        self.source.mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "sentinel").write_text("untouched")
        self.catalog = copy.deepcopy(apply.RUNTIME_RECIPES)
        self.catalog["lxd-snap-v1"]["source"] = str(self.source)
        self.catalog["lxd-snap-v1"]["entries"][0]["target"] = str(self.outside)
        self.catalog["docker-snap-v1"]["source"] = str(self.root / "source/docker")
        self.docker = Path(self.catalog["docker-snap-v1"]["source"])
        self.docker.mkdir()
        patcher = mock.patch.object(apply, "RUNTIME_RECIPES", self.catalog)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = self.root / "restore"
        self.captures = self.target / "captures"
        self.captures.mkdir(parents=True)
        (self.target / "FARM-RESTORE-RECEIPT.json").write_text(json.dumps({"status": "extracted-and-verified"}))
        self.destination = self.target / str(self.source).lstrip("/")
        self.destination.parent.mkdir(parents=True)

    def present_lxd(self):
        (self.source / "shmounts").symlink_to(self.outside)
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(self.source / "unix.socket"))
        return apply.inspect_runtime_entries(self.source, "lxd-snap-v1")

    def archive(self, receipt, members=()):
        artifact = self.captures / "lxd.tar"
        with tarfile.open(artifact, "w") as archive:
            root = tarfile.TarInfo("lxd")
            root.type, root.mode = tarfile.DIRTYPE, 0o700
            root.uid, root.gid = os.getuid(), os.getgid()
            archive.addfile(root)
            for member in members:
                member.uid, member.gid = os.getuid(), os.getgid()
                archive.addfile(member, io.BytesIO(b"x" * member.size) if member.size else None)
        entry = {"kind": "tar", "source": str(self.source), "archive_parent": str(self.source.parent),
                 "artifact": artifact.name, "bytes": artifact.stat().st_size,
                 "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
        if receipt != "omitted":
            entry["runtime_entries"] = receipt
        self.manifest([entry])
        return artifact, entry

    def manifest(self, entries):
        (self.captures / "manifest.json").write_text(json.dumps({"schema": "farm.recovery-captures/1",
            "status": "complete", "errors": [], "captures": entries}))

    def rejected_without_writes(self):
        before = sorted(str(path) for path in self.target.rglob("*"))
        with self.assertRaises(apply.ApplyError):
            apply.apply_captures(self.target, self.captures)
        self.assertEqual(sorted(str(path) for path in self.target.rglob("*")), before)
        self.assertEqual((self.outside / "sentinel").read_text(), "untouched")

    def test_missing_entries_and_missing_parent_are_recorded_and_excluded(self):
        receipt = apply.inspect_runtime_entries(self.docker, "docker-snap-v1")
        self.assertFalse(receipt["entries"][0]["present"])
        self.assertEqual(apply.runtime_exclude_paths(self.docker, receipt), ["docker/volumes/backingFsBlockDev"])
        receipt = apply.inspect_runtime_entries(self.source, "lxd-snap-v1")
        self.assertTrue(all(not entry["present"] for entry in receipt["entries"]))
        self.archive(receipt)
        result = apply.apply_captures(self.target, self.captures)
        self.assertFalse(os.path.lexists(self.destination / "shmounts"))
        self.assertEqual(result["runtime_recovery"][0]["status"], "absent-at-capture")
        self.assertEqual(result["runtime_recovery"][1]["action"], "systemd-recreates")

    def test_real_symlink_and_socket_metadata_and_change_detection(self):
        receipt = self.present_lxd()
        self.assertTrue(all(entry["present"] for entry in receipt["entries"]))
        link = receipt["entries"][0]
        info = (self.source / "shmounts").lstat()
        self.assertEqual((link["mode"], link["uid"], link["gid"], link["inode"], link["dev"], link["nlink"]),
                         (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, info.st_ino, info.st_dev, info.st_nlink))
        self.assertEqual(receipt, apply.inspect_runtime_entries(self.source, "lxd-snap-v1"))
        (self.source / "unix.socket").chmod(0o600)
        self.assertNotEqual(receipt, apply.inspect_runtime_entries(self.source, "lxd-snap-v1"))

    def test_real_payload_files_and_directories_are_never_omitted(self):
        for name in ("shmounts", "unix.socket"):
            path = self.source / name
            for directory in (False, True):
                with self.subTest(name=name, directory=directory):
                    path.mkdir() if directory else path.write_text("payload")
                    with self.assertRaises(apply.ApplyError):
                        apply.inspect_runtime_entries(self.source, "lxd-snap-v1")
                    path.rmdir() if directory else path.unlink()
        (self.docker / "volumes").mkdir()
        (self.docker / "volumes/backingFsBlockDev").write_text("real volume data")
        with self.assertRaises(apply.ApplyError):
            apply.inspect_runtime_entries(self.docker, "docker-snap-v1")

    def test_wrong_symlink_target_recipe_and_source_fail(self):
        (self.source / "shmounts").symlink_to("../different")
        for source, recipe in ((self.source, "lxd-snap-v1"), (self.source, "docker-snap-v1"),
                               (self.source, "unknown"), (self.source, None), (self.docker, "lxd-snap-v1")):
            with self.subTest(source=source, recipe=recipe), self.assertRaises(apply.ApplyError):
                apply.inspect_runtime_entries(source, recipe)

    def test_symlink_or_file_ancestors_fail_even_when_leaf_missing(self):
        parent = self.docker / "volumes"
        for symlink in (False, True):
            parent.symlink_to(self.outside) if symlink else parent.write_text("payload")
            with self.assertRaises(apply.ApplyError):
                apply.inspect_runtime_entries(self.docker, "docker-snap-v1")
            parent.unlink()
        alias = self.root / "alias"
        alias.symlink_to(self.docker)
        self.catalog["docker-snap-v1"]["source"] = str(alias)
        with self.assertRaises(apply.ApplyError):
            apply.inspect_runtime_entries(alias, "docker-snap-v1")

    def test_block_device_is_verified_against_source_filesystem_without_mknod(self):
        (self.docker / "volumes").mkdir()
        path = self.docker / "volumes/backingFsBlockDev"
        device = self.docker.stat().st_dev
        info = SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_uid=os.getuid(), st_gid=os.getgid(),
                               st_dev=device, st_rdev=device, st_ino=123, st_nlink=1)
        original = Path.lstat
        with mock.patch.object(Path, "lstat", lambda current: info if current == path else original(current)):
            receipt = apply.inspect_runtime_entries(self.docker, "docker-snap-v1")
            self.assertEqual(receipt["entries"][0]["rdev"], device)
            info.st_rdev = device + 1
            with self.assertRaises(apply.ApplyError):
                apply.inspect_runtime_entries(self.docker, "docker-snap-v1")

    def test_verified_block_device_is_not_replayed_on_restore(self):
        receipt = apply.inspect_runtime_entries(self.docker, "docker-snap-v1")
        receipt["entries"][0].update(present=True, mode=0o600, uid=os.getuid(), gid=os.getgid(),
                                      dev=123, rdev=123, inode=456, nlink=1)
        artifact = self.captures / "docker.tar"
        with tarfile.open(artifact, "w") as archive:
            for name in ("docker", "docker/volumes"):
                member = tarfile.TarInfo(name)
                member.type, member.mode, member.uid, member.gid = tarfile.DIRTYPE, 0o700, os.getuid(), os.getgid()
                archive.addfile(member)
        entry = {"kind": "tar", "source": str(self.docker), "archive_parent": str(self.docker.parent),
                 "artifact": artifact.name, "bytes": artifact.stat().st_size,
                 "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "runtime_entries": receipt}
        self.manifest([entry])
        with mock.patch.object(apply.os, "mknod", side_effect=AssertionError("No stale device replay")):
            result = apply.apply_captures(self.target, self.captures)
        destination = self.target / str(self.docker).lstrip("/") / "volumes/backingFsBlockDev"
        self.assertFalse(os.path.lexists(destination))
        self.assertEqual(result["runtime_recovery"][0]["action"], "daemon-recreates")
        self.assertEqual(result["runtime_recovery"][0]["status"], "activation-required")
        receipt["entries"][0]["rdev"] += 1
        with self.assertRaises(apply.ApplyError):
            apply.runtime_exclude_paths(self.docker, receipt)

    def test_malformed_receipts_fail_before_restore_writes(self):
        original = self.present_lxd()
        changes = [("recipe", "unknown"), ("recipe", "docker-snap-v1"), ("entries", []), ("extra", True)]
        receipts = [None, {}, [], "lxd-snap-v1"]
        for key, value in changes:
            receipt = copy.deepcopy(original)
            receipt[key] = value
            receipts.append(receipt)
        for key, value in (("path", "other"), ("kind", "file"), ("action", "ignore"), ("present", 1),
                           ("target", "/different"), ("mode", 0o10000), ("uid", True), ("gid", -1),
                           ("inode", 0), ("nlink", 0), ("dev", -1), ("extra", "injected")):
            receipt = copy.deepcopy(original)
            receipt["entries"][0][key] = value
            receipts.append(receipt)
        receipt = copy.deepcopy(original)
        del receipt["entries"][0]["uid"]
        receipts.append(receipt)
        receipt = copy.deepcopy(original)
        receipt["entries"][0]["present"] = False
        receipts.append(receipt)
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                self.archive(receipt)
                self.rejected_without_writes()

    def test_archived_runtime_entries_and_descendants_are_forbidden_even_if_absent(self):
        receipt = apply.inspect_runtime_entries(self.source, "lxd-snap-v1")
        for name in ("lxd/shmounts", "lxd/shmounts/escape", "lxd/unix.socket", "lxd/unix.socket/child"):
            with self.subTest(name=name):
                self.archive(receipt, [tarfile.TarInfo(name)])
                self.rejected_without_writes()

    def test_runtime_ancestor_symlink_is_rejected_before_payload_writes(self):
        receipt = apply.inspect_runtime_entries(self.docker, "docker-snap-v1")
        artifact = self.captures / "docker.tar"
        with tarfile.open(artifact, "w") as archive:
            for name, kind, target in (("docker", tarfile.DIRTYPE, ""),
                                       ("docker/actual", tarfile.DIRTYPE, ""),
                                       ("docker/volumes", tarfile.SYMTYPE, "actual")):
                member = tarfile.TarInfo(name)
                member.type, member.linkname = kind, target
                archive.addfile(member)
        entry = {"kind": "tar", "source": str(self.docker), "archive_parent": str(self.docker.parent),
                 "artifact": artifact.name, "bytes": artifact.stat().st_size,
                 "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "runtime_entries": receipt}
        self.manifest([entry])
        self.rejected_without_writes()

    def test_restore_recreates_only_literal_link_after_payloads_without_touching_target(self):
        receipt = self.present_lxd()
        (self.source / "payload").write_text("stored data")
        artifact = self.captures / "lxd.tar"
        command = ["/usr/bin/tar", "-cf", str(artifact), "-C", str(self.source.parent)]
        command += ["--exclude=" + path for path in apply.runtime_exclude_paths(self.source, receipt)]
        subprocess.run(command + [self.source.name], check=True)
        entry = {"kind": "tar", "source": str(self.source), "archive_parent": str(self.source.parent),
                 "artifact": artifact.name, "bytes": artifact.stat().st_size,
                 "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "runtime_entries": receipt}
        later_source = self.root / "source/later"
        later_destination = self.target / str(later_source).lstrip("/")
        later = self.captures / "later.rdb"
        later.write_bytes(b"later payload")
        self.manifest([entry, {"kind": "redis-rdb", "source": str(later_source), "artifact": later.name,
            "bytes": later.stat().st_size, "sha256": hashlib.sha256(later.read_bytes()).hexdigest(),
            "uid": os.getuid(), "gid": os.getgid(), "mode": 0o600}])
        original = os.symlink
        def create_link(target, destination):
            self.assertEqual((self.destination / "payload").read_text(), "stored data")
            self.assertEqual(later_destination.read_bytes(), b"later payload")
            return original(target, destination)
        with mock.patch.object(apply.os, "symlink", side_effect=create_link), \
                mock.patch.object(apply.os, "chown", wraps=os.chown) as chown:
            result = apply.apply_captures(self.target, self.captures)
        self.assertEqual(os.readlink(self.destination / "shmounts"), str(self.outside))
        self.assertFalse(os.path.lexists(self.destination / "unix.socket"))
        self.assertEqual((self.outside / "sentinel").read_text(), "untouched")
        self.assertEqual(list(self.outside.iterdir()), [self.outside / "sentinel"])
        self.assertEqual(chown.call_args, mock.call(self.destination / "shmounts", os.getuid(), os.getgid(), follow_symlinks=False))
        self.assertEqual(result["runtime_recovery"][0]["status"], "recreated")
        self.assertEqual(result["runtime_recovery"][1]["status"], "activation-required")

    def test_legacy_tar_without_recipe_keeps_strict_validation(self):
        self.archive("omitted")
        result = apply.apply_captures(self.target, self.captures)
        self.assertEqual(result["runtime_recovery"], [])
        absolute = tarfile.TarInfo("lxd/anything")
        absolute.type, absolute.linkname = tarfile.SYMTYPE, str(self.outside)
        artifact, _ = self.archive("omitted", [absolute])
        with self.assertRaises(apply.ApplyError):
            apply.validate_tar(artifact, self.source)


if __name__ == "__main__":
    unittest.main()
