"""Capture application tests operate only on temporary alternate-root fixtures."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("apply_capture_test_module", Path(__file__).parents[1] / "apply_captures.py")
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)


class ApplyCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm-apply-captures-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "restore"
        self.captures = self.target / "var/lib/farm-recovery/current/captures"
        self.captures.mkdir(parents=True)
        (self.target / "data").mkdir()
        (self.target / "FARM-RESTORE-RECEIPT.json").write_text(json.dumps({"status": "extracted-and-verified"}))
        self.entries = []

    def artifact(self, name, kind, source, data=None):
        path = self.captures / name
        if data is not None:
            path.write_bytes(data)
        entry = {"kind": kind, "source": source, "artifact": name,
                 "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                 "uid": os.getuid(), "gid": os.getgid(), "mode": 0o640, "xattrs": {}}
        self.entries.append(entry)
        return entry

    def sqlite(self, name="sqlite.db", source="/data/database.db"):
        path = self.captures / name
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE fixture(value TEXT)")
            db.execute("INSERT INTO fixture VALUES ('restored fixture')")
        return self.artifact(name, "sqlite", source)

    def manifest(self, **updates):
        value = {"schema": "farm.recovery-captures/1", "status": "complete", "captures": self.entries,
                 "errors": []}
        value.update(updates)
        (self.captures / "manifest.json").write_text(json.dumps(value))

    def tree(self):
        return sorted(str(p.relative_to(self.target)) for p in self.target.rglob("*"))

    def rejected_without_writes(self):
        before = self.tree()
        with self.assertRaises(apply.ApplyError):
            apply.apply_captures(self.target, self.captures)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.target / "data/database.db").exists())

    def tar(self, members=None):
        path = self.captures / "offline.tar"
        root = tarfile.TarInfo("offline")
        root.type, root.mode, root.uid, root.gid = tarfile.DIRTYPE, 0o750, os.getuid(), os.getgid()
        with tarfile.open(path, "w") as archive:
            archive.addfile(root)
            for member, data in members or []:
                member.uid, member.gid = os.getuid(), os.getgid()
                archive.addfile(member, io.BytesIO(data) if data else None)
        entry = self.artifact(path.name, "tar", "/data/offline")
        entry["archive_parent"] = "/data"
        return entry

    def test_sqlite_and_redis_restore_data_mode_ownership_and_xattrs(self):
        entry = self.sqlite()
        entry["xattrs"] = {"user.farm-fixture": base64.b64encode(b"attribute fixture").decode()}
        self.artifact("redis.rdb", "redis-rdb", "/data/dump.rdb", b"fixture RDB bytes")
        self.manifest()
        result = apply.apply_captures(self.target, self.captures)
        self.assertEqual(result["status"], "applied")
        restored = self.target / "data/database.db"
        with sqlite3.connect(restored) as db:
            self.assertEqual(db.execute("SELECT value FROM fixture").fetchall(), [("restored fixture",)])
        self.assertEqual(restored.stat().st_mode & 0o777, 0o640)
        self.assertEqual((restored.stat().st_uid, restored.stat().st_gid), (os.getuid(), os.getgid()))
        self.assertEqual(os.getxattr(restored, "user.farm-fixture"), b"attribute fixture")
        self.assertEqual((self.target / "data/dump.rdb").read_bytes(), b"fixture RDB bytes")

    def test_real_tar_preserves_file_hardlink_symlink_mode_and_xattrs(self):
        source = self.root / "source/offline"
        source.mkdir(parents=True)
        data = source / "data.txt"
        data.write_text("offline fixture")
        data.chmod(0o640)
        os.setxattr(data, "user.farm-fixture", b"tar attribute")
        os.link(data, source / "hardlink")
        (source / "relative-link").symlink_to("data.txt")
        artifact = self.captures / "offline.tar"
        subprocess.run(["/usr/bin/tar", "--acls", "--xattrs", "--numeric-owner", "--sparse",
                        "-cf", str(artifact), "-C", str(source.parent), "offline"], check=True)
        entry = self.artifact(artifact.name, "tar", "/data/offline")
        entry["archive_parent"] = "/data"
        self.manifest()
        result = apply.apply_captures(self.target, self.captures)
        restored = self.target / "data/offline"
        self.assertEqual(result["status"], "applied")
        self.assertEqual((restored / "data.txt").read_text(), "offline fixture")
        self.assertEqual((restored / "data.txt").stat().st_mode & 0o777, 0o640)
        self.assertEqual((restored / "data.txt").stat().st_ino, (restored / "hardlink").stat().st_ino)
        self.assertEqual(os.readlink(restored / "relative-link"), "data.txt")
        self.assertEqual(os.getxattr(restored / "data.txt", "user.farm-fixture"), b"tar attribute")

    def test_postgres_is_validated_but_never_imported(self):
        self.artifact("postgres.sql", "pg_dumpall", "cluster", b"-- SQL fixture\n")
        self.manifest()
        before = self.tree()
        with mock.patch.object(apply.subprocess, "run", side_effect=AssertionError("SQL must not execute")):
            result = apply.apply_captures(self.target, self.captures)
        self.assertEqual(result["status"], "applied-imports-pending")
        self.assertEqual(result["applied"], [])
        self.assertEqual(result["manual_imports"][0]["status"], "import-required")
        self.assertEqual(self.tree(), before)

    def test_bad_later_artifact_prevents_earlier_sqlite_write(self):
        self.sqlite()
        self.artifact("bad.rdb", "redis-rdb", "/data/dump.rdb", b"original")
        (self.captures / "bad.rdb").write_bytes(b"modified")
        self.manifest()
        self.rejected_without_writes()

    def test_corrupt_sqlite_with_matching_hash_fails_preflight(self):
        self.artifact("broken.db", "sqlite", "/data/database.db", b"not sqlite")
        self.manifest()
        self.rejected_without_writes()

    def test_incomplete_or_invalid_manifest_and_extraction_receipts_fail(self):
        self.sqlite()
        for changes in ({"status": "incomplete"}, {"schema": "unknown"}, {"errors": ["failure"]}):
            with self.subTest(changes=changes):
                self.manifest(**changes)
                self.rejected_without_writes()
        self.manifest()
        (self.target / "FARM-RESTORE-RECEIPT.json").write_text('{"status":"incomplete"}')
        self.rejected_without_writes()

    def test_stale_database_sidecars_are_not_removed(self):
        self.sqlite()
        self.manifest()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self.target / ("data/database.db" + suffix)
            sidecar.write_text("keep")
            with self.subTest(suffix=suffix):
                self.rejected_without_writes()
                self.assertEqual(sidecar.read_text(), "keep")
            sidecar.unlink()

    def test_existing_destination_is_not_overwritten(self):
        self.sqlite()
        self.manifest()
        path = self.target / "data/database.db"
        path.write_text("keep")
        with self.assertRaises(apply.ApplyError):
            apply.apply_captures(self.target, self.captures)
        self.assertEqual(path.read_text(), "keep")

    def test_symlinked_destination_ancestor_cannot_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.target / "data").rmdir()
        (self.target / "data").symlink_to(outside, target_is_directory=True)
        self.sqlite()
        self.manifest()
        self.rejected_without_writes()
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_traversal_and_relative_paths_fail(self):
        entry = self.sqlite()
        for source in ("/data/../escape", "data/relative", "/", "//data/file", "/data//file"):
            with self.subTest(source=source):
                entry["source"] = source
                self.manifest()
                self.rejected_without_writes()

    def test_capture_directory_must_be_inside_target_without_symlinks(self):
        self.sqlite()
        self.manifest()
        alias = self.target / "alias"
        alias.symlink_to(self.captures, target_is_directory=True)
        for path in (alias, self.root, self.target):
            with self.subTest(path=str(path)), self.assertRaises(apply.ApplyError):
                apply.apply_captures(self.target, path)
        with self.assertRaises(apply.ApplyError):
            apply.apply_captures(Path("/"), self.captures)

    def test_symlink_artifact_and_symlink_receipt_fail(self):
        entry = self.sqlite()
        self.manifest()
        source = self.captures / entry["artifact"]
        original = self.root / "original.db"
        source.rename(original)
        source.symlink_to(original)
        self.rejected_without_writes()
        source.unlink()
        original.rename(source)
        receipt = self.target / "FARM-RESTORE-RECEIPT.json"
        original = self.root / "receipt.json"
        receipt.rename(original)
        receipt.symlink_to(original)
        self.rejected_without_writes()

    def test_unknown_kind_bad_attributes_and_artifact_path_fail(self):
        entry = self.sqlite()
        for key, bad in (("kind", "unknown"), ("uid", -1), ("mode", 0o10000),
                         ("xattrs", {"user.fixture": "invalid!"}), ("artifact", "../outside.db")):
            before = entry[key]
            with self.subTest(key=key):
                entry[key] = bad
                self.manifest()
                self.rejected_without_writes()
            entry[key] = before

    def test_tar_traversal_absolute_and_wrong_roots_fail_before_sqlite_write(self):
        self.sqlite()
        for name in ("../escape", "/absolute", "elsewhere/data", "offline/../../escape"):
            with self.subTest(name=name):
                member = tarfile.TarInfo(name)
                self.tar([(member, b"")])
                self.manifest()
                self.rejected_without_writes()
                self.entries.pop()

    def test_tar_escaping_links_and_link_ancestor_fail(self):
        self.sqlite()
        for target, child in (("../../outside", False), ("/etc/passwd", False), ("data", True)):
            with self.subTest(target=target, child=child):
                member = tarfile.TarInfo("offline/link")
                member.type, member.linkname = tarfile.SYMTYPE, target
                members = [(member, b"")]
                if child:
                    members.append((tarfile.TarInfo("offline/link/file"), b""))
                self.tar(members)
                self.manifest()
                self.rejected_without_writes()
                self.entries.pop()

    def test_tar_hardlink_to_unarchived_file_and_special_files_fail(self):
        self.sqlite()
        hardlink = tarfile.TarInfo("offline/link")
        hardlink.type, hardlink.linkname = tarfile.LNKTYPE, "offline/absent"
        device = tarfile.TarInfo("offline/device")
        device.type = tarfile.CHRTYPE
        for member in (hardlink, device):
            with self.subTest(type=member.type):
                self.tar([(member, b"")])
                self.manifest()
                self.rejected_without_writes()
                self.entries.pop()

    def test_tar_hardlink_parent_traversal_fails_even_when_lexically_inside(self):
        self.sqlite()
        data = tarfile.TarInfo("offline/data")
        hardlink = tarfile.TarInfo("offline/link")
        hardlink.type, hardlink.linkname = tarfile.LNKTYPE, "offline/dir/../data"
        self.tar([(data, b""), (hardlink, b"")])
        self.manifest()
        self.rejected_without_writes()

    def test_tar_chained_symlink_traversal_is_rejected(self):
        self.sqlite()
        directory = tarfile.TarInfo("offline/dir")
        directory.type = tarfile.DIRTYPE
        up = tarfile.TarInfo("offline/dir/up")
        up.type, up.linkname = tarfile.SYMTYPE, ".."
        escaped = tarfile.TarInfo("offline/link")
        # Lexical normalization is inside offline/, but following dir/up first
        # makes the remaining '..' escape offline/.
        escaped.type, escaped.linkname = tarfile.SYMTYPE, "dir/up/../outside"
        self.tar([(directory, b""), (up, b""), (escaped, b"")])
        self.manifest()
        self.rejected_without_writes()

    def test_tar_safe_parent_relative_symlink_is_preserved(self):
        directory = tarfile.TarInfo("offline/dir")
        directory.type, directory.mode = tarfile.DIRTYPE, 0o750
        data = tarfile.TarInfo("offline/data")
        data.size = 4
        link = tarfile.TarInfo("offline/dir/link")
        link.type, link.linkname = tarfile.SYMTYPE, "../data"
        self.tar([(directory, b""), (data, b"data"), (link, b"")])
        self.manifest()
        apply.apply_captures(self.target, self.captures)
        self.assertEqual((self.target / "data/offline/dir/link").read_bytes(), b"data")

    def test_tar_duplicate_members_and_wrong_archive_parent_fail(self):
        self.sqlite()
        member = tarfile.TarInfo("offline/file")
        self.tar([(member, b""), (member, b"")])
        self.manifest()
        self.rejected_without_writes()
        self.entries.pop()
        entry = self.tar()
        entry["archive_parent"] = "/elsewhere"
        self.manifest()
        self.rejected_without_writes()

    def test_late_failure_keeps_existing_files_and_does_not_claim_success(self):
        self.sqlite()
        self.manifest()
        with mock.patch.object(apply.os, "setxattr", side_effect=OSError("fixture")):
            # No attrs means no setxattr call; instead fail publication itself.
            with mock.patch.object(apply.os, "link", side_effect=OSError("fixture")):
                with self.assertRaises(apply.ApplyError):
                    apply.apply_captures(self.target, self.captures)
        self.assertFalse((self.target / "data/database.db").exists())
        self.assertFalse(list((self.target / "data").glob(".farm-restore-*")))


if __name__ == "__main__":
    unittest.main()
