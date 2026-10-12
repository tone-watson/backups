"""Portable capture tests use scratch SQLite/tar data and fake native services."""
import hashlib
import base64
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("farm_capture", Path(__file__).parents[1] / "capture.py")
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm-state-capture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stage = self.root / "stage"
        self.sources = self.root / "sources"
        self.sources.mkdir()

    def database(self, name="example.db"):
        path = self.sources / name
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE payload(value TEXT)")
            db.execute("INSERT INTO payload VALUES ('fixture value')")
        return path

    def unit_output(self, unit, active):
        return (f"Id={unit}\nNames={unit}\nLoadState=loaded\n"
                f"ActiveState={'active' if active else 'inactive'}\n"
                f"SubState={'running' if active else 'dead'}\n"
                f"MainPID={123 if active else 0}\nControlPID=0\n").encode()

    def test_sqlite_backup_includes_live_wal_and_restores_rows(self):
        source = self.database()
        connection = sqlite3.connect(source)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO payload VALUES ('uncheckpointed fixture')")
        connection.commit()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        with mock.patch.object(capture, "_run", side_effect=AssertionError("unexpected external command")):
            receipt = capture.capture_state({"sqlite_roots": [str(self.sources)]}, self.stage)
        self.assertEqual(receipt["status"], "complete", receipt)
        artifact = self.stage / receipt["captures"][0]["artifact"]
        with sqlite3.connect(artifact) as restored:
            self.assertEqual(restored.execute("SELECT value FROM payload ORDER BY rowid").fetchall(),
                             [("fixture value",), ("uncheckpointed fixture",)])
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertEqual(receipt["exclusions"], sorted([str(source)] +
                         [str(source) + suffix for suffix in ("-wal", "-shm", "-journal")]))
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.stage.stat().st_mode & 0o777, 0o700)

    def test_bad_database_is_not_excluded_or_published(self):
        good = self.database()
        bad = self.sources / "corrupt.db"
        bad.write_bytes(b"not a SQLite database; inert fixture")
        receipt = capture.capture_state({"sqlite_roots": [str(self.sources)]}, self.stage)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(len(receipt["captures"]), 1)
        self.assertIn(str(good), receipt["exclusions"])
        self.assertNotIn(str(bad), receipt["exclusions"])
        self.assertFalse(list(self.stage.glob("*.partial")))
        self.assertEqual(json.loads((self.stage / "manifest.json").read_text()), receipt)

    def test_nonempty_stage_and_source_symlinks_are_rejected(self):
        source = self.database()
        alias = self.sources / "alias.db"
        alias.symlink_to(source)
        receipt = capture.capture_state({"sqlite_paths": [str(alias)]}, self.stage)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["exclusions"], [])
        with self.assertRaises(capture.CaptureError):
            capture.capture_state({}, self.stage)

    def test_symlink_ancestors_rejected_for_source_stage_and_discovery(self):
        source = self.database()
        alias = self.root / "alias"
        alias.symlink_to(self.sources, target_is_directory=True)
        receipt = capture.capture_state({"sqlite_paths": [str(alias / source.name)]}, self.stage)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["exclusions"], [])
        with self.assertRaises(capture.CaptureError):
            capture.capture_state({}, alias / "new-stage")
        self.assertFalse((self.sources / "new-stage").exists())
        with self.assertRaises(capture.CaptureError):
            capture._sqlite_paths({"sqlite_roots": [str(alias)]})

    def test_all_sqlite_extensions_discovered_and_missing_explicit_path_fails(self):
        sources = [self.database(name) for name in ("one.db", "two.sqlite", "three.sqlite3")]
        receipt = capture.capture_state({"sqlite_roots": [str(self.sources)],
                                         "sqlite_paths": [str(self.sources / "missing.sqlite")]}, self.stage)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(len(receipt["captures"]), 3)
        self.assertTrue(all(str(source) in receipt["exclusions"] for source in sources))

    def test_file_xattrs_preserved_and_unreadable_xattrs_fail_capture(self):
        source = self.database()
        os.setxattr(source, "user.farm-fixture", b"arbitrary\x00bytes")
        receipt = capture.capture_state({"sqlite_paths": [str(source)]}, self.stage)
        self.assertEqual(base64.b64decode(receipt["captures"][0]["xattrs"]["user.farm-fixture"]),
                         b"arbitrary\x00bytes")
        with mock.patch.object(capture.os, "listxattr", side_effect=PermissionError("private value")):
            denied = capture.capture_state({"sqlite_paths": [str(source)]}, self.root / "denied-stage")
        self.assertEqual(denied["status"], "incomplete")
        self.assertEqual(denied["exclusions"], [])
        self.assertNotIn("private value", json.dumps(denied))

    def test_offline_tar_round_trip_with_metadata_and_state_guards(self):
        source = self.sources / "offline"
        source.mkdir()
        data = source / "data.txt"
        data.write_text("independent fixture content")
        data.chmod(0o640)
        (source / "alias").symlink_to("data.txt")
        actual_run = capture._run
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "/usr/bin/systemctl":
                return self.unit_output("fixture.service", False)
            return actual_run(argv, **kwargs)
        with mock.patch.object(capture, "_run", side_effect=run):
            receipt = capture.capture_state({"offline_stores": [
                {"id": "offline", "path": str(source), "units": ["fixture.service"]}]}, self.stage)
        self.assertEqual(receipt["status"], "complete", receipt)
        with tarfile.open(self.stage / "offline.tar") as archive:
            member = archive.getmember("offline/data.txt")
            self.assertEqual(member.mode, 0o640)
            self.assertEqual(archive.extractfile(member).read(), b"independent fixture content")
            self.assertEqual(archive.getmember("offline/alias").linkname, "data.txt")
        self.assertEqual(sum(argv[1] == "show" for argv in calls), 3)
        self.assertEqual(receipt["exclusions"], [str(source)])

    def test_running_store_without_quiesce_never_stops(self):
        calls = []
        def run(argv, **_kwargs):
            calls.append(argv)
            return self.unit_output("fixture.service", True)
        with mock.patch.object(capture, "_run", side_effect=run):
            receipt = capture.capture_state({"offline_stores": [
                {"id": "offline", "path": str(self.sources), "units": ["fixture.service"]}]}, self.stage)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(all(argv[1] == "show" for argv in calls))
        self.assertEqual(receipt["exclusions"], [])

    def test_offline_archive_unsupported_links_and_special_files_fail_before_promotion(self):
        actual_run = capture._run
        def run(argv, **kwargs):
            if argv[0] == "/usr/bin/systemctl":
                return self.unit_output("fixture.service", False)
            return actual_run(argv, **kwargs)
        for index, kind in enumerate(("absolute-link", "escaping-link", "fifo")):
            with self.subTest(kind=kind):
                source = self.sources / kind
                source.mkdir()
                if kind == "fifo":
                    os.mkfifo(source / "special")
                else:
                    (source / "link").symlink_to("/fixture/host-path" if kind == "absolute-link" else "../outside")
                stage = self.root / f"unsafe-stage-{index}"
                with mock.patch.object(capture, "_run", side_effect=run):
                    receipt = capture.capture_state({"offline_stores": [{"id": "fixture",
                        "path": str(source), "units": ["fixture.service"]}]}, stage)
                self.assertEqual(receipt["status"], "incomplete")
                self.assertEqual(receipt["captures"], [])
                self.assertEqual(receipt["exclusions"], [])
                self.assertIn("manual recovery recipe", receipt["errors"][0]["error"])
                self.assertFalse((stage / "fixture.tar").exists())
                self.assertFalse(list(stage.glob("*.partial")))

    def test_quiesce_restores_initial_running_state_after_capture_failure(self):
        calls, state = [], {"active": True}
        def run(argv, **_kwargs):
            calls.append(argv)
            if argv[0] == "/usr/bin/tar":
                raise capture.CaptureError("fixture tar failed")
            if argv[1] == "stop":
                state["active"] = False
                return b""
            if argv[1] == "start":
                state["active"] = True
                return b""
            return self.unit_output("fixture.service", state["active"])
        with mock.patch.object(capture, "_run", side_effect=run):
            receipt = capture.capture_state({"offline_stores": [
                {"id": "offline", "path": str(self.sources), "units": ["fixture.service"]}]},
                self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(state["active"])
        self.assertEqual(receipt["service_restoration"], [{"unit": "fixture.service", "status": "restored"}])
        self.assertEqual([argv[1] for argv in calls if argv[1] in ("start", "stop")], ["stop", "start"])

    def test_protected_service_rejected_without_any_external_command(self):
        with mock.patch.object(capture, "_run", side_effect=AssertionError("unexpected external command")):
            receipt = capture.capture_state({"offline_stores": [
                {"id": "network", "path": str(self.sources), "units": ["tailscaled.service"]}]},
                self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertIn("SSH/network/Tailscale", receipt["errors"][0]["error"])

    def test_protected_service_alias_is_rejected(self):
        fields = self.unit_output("ssh.service", True)
        with mock.patch.object(capture, "_run", return_value=fields) as run:
            receipt = capture.capture_state({"offline_stores": [
                {"id": "alias", "path": str(self.sources), "units": ["safe-alias.service"]}]},
                self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(run.call_count, 1)

    def test_protected_socket_and_alias_rejected(self):
        with mock.patch.object(capture, "_run", side_effect=AssertionError("must not query")):
            with self.assertRaises(capture.CaptureError):
                capture._unit_state("ssh.socket", 1)
        fields = b"Id=ssh.socket\nNames=safe.socket ssh.socket\nLoadState=loaded\nActiveState=active\nSubState=listening\n"
        with mock.patch.object(capture, "_run", return_value=fields):
            with self.assertRaises(capture.CaptureError):
                capture._unit_state("safe.socket", 1)

    def test_socket_quiesce_journal_and_initially_inactive_daemon(self):
        active, actions = {"fixture.socket": True, "fixture.service": False}, []
        actual_run = capture._run
        def run(argv, **kwargs):
            if argv[0] == "/usr/bin/tar":
                return actual_run(argv, **kwargs)
            unit = argv[2]
            if argv[1] in ("stop", "start"):
                journal = json.loads((self.stage / "manifest.json").read_text())
                self.assertEqual(journal["service_actions"][-1],
                                 {"unit": unit, "action": argv[1], "status": "intended"})
                self.assertEqual(journal["initial_unit_states"]["fixture.socket"]["SubState"], "listening")
                actions.append((argv[1], unit))
                active[unit] = argv[1] == "start"
                return b""
            if unit.endswith(".service"):
                return self.unit_output(unit, active[unit])
            return (f"Id={unit}\nNames={unit}\nLoadState=loaded\n"
                    f"ActiveState={'active' if active[unit] else 'inactive'}\n"
                    f"SubState={'listening' if active[unit] else 'dead'}\n").encode()
        with mock.patch.object(capture, "_run", side_effect=run):
            receipt = capture.capture_state({"offline_stores": [{"id": "fixture", "path": str(self.sources),
                "units": ["fixture.socket", "fixture.service"]}]}, self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "complete", receipt)
        self.assertEqual(actions, [("stop", "fixture.socket"), ("start", "fixture.socket")])
        self.assertFalse(active["fixture.service"])

    def test_interrupt_restores_service_and_durably_records_incomplete(self):
        active = True
        def run(argv, **_kwargs):
            nonlocal active
            if argv[0] == "/usr/bin/tar":
                raise KeyboardInterrupt()
            if argv[1] in ("stop", "start"):
                active = argv[1] == "start"
                return b""
            return self.unit_output("fixture.service", active)
        with mock.patch.object(capture, "_run", side_effect=run):
            with self.assertRaises(KeyboardInterrupt):
                capture.capture_state({"offline_stores": [{"id": "fixture", "path": str(self.sources),
                    "units": ["fixture.service"]}]}, self.stage, quiesce=True)
        receipt = json.loads((self.stage / "manifest.json").read_text())
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(active)
        self.assertEqual(receipt["service_restoration"], [{"unit": "fixture.service", "status": "restored"}])
        self.assertEqual(receipt["errors"][0]["capture"], "interruption")

    def test_native_dump_artifacts_and_exact_exclusions(self):
        postgres = self.sources / "postgres"
        postgres.mkdir()
        redis = self.sources / "dump.rdb"
        redis.write_bytes(b"old fixture rdb")
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "/usr/sbin/runuser":
                if argv[4].endswith("/psql"):
                    return str(postgres).encode() + b"\n"
                kwargs["stdout"].write(b"-- fixture SQL\nCREATE DATABASE fixture;\n")
            elif argv[0] == "/usr/bin/redis-cli":
                if "CONFIG" in argv:
                    values = {"dir": str(redis.parent), "dbfilename": redis.name, "appendonly": "no"}
                    return f"{argv[-1]}\n{values[argv[-1]]}\n".encode()
                Path(argv[-1]).write_bytes(b"validated-by-fake-checker RDB fixture")
            return b""
        with mock.patch.object(capture, "_run", side_effect=run):
            receipt = capture.capture_state({"postgres": {"enabled": True, "data_path": str(postgres)},
                                             "redis": {"enabled": True, "data_path": str(redis)}}, self.stage)
        self.assertEqual(receipt["status"], "complete", receipt)
        self.assertEqual({x["kind"] for x in receipt["captures"]}, {"pg_dumpall", "redis-rdb"})
        self.assertEqual(receipt["exclusions"], [str(redis)])
        pg_capture = next(item for item in receipt["captures"] if item["kind"] == "pg_dumpall")
        self.assertTrue(pg_capture["raw_source_retained"])
        self.assertTrue(any(argv[0] == "/usr/bin/redis-check-rdb" for argv in calls))

    def test_postgres_mismatched_cluster_never_dumps_or_excludes(self):
        source = self.sources / "postgres"
        source.mkdir()
        with mock.patch.object(capture, "_run", return_value=b"/different/cluster\n") as run:
            receipt = capture.capture_state({"postgres": {"enabled": True, "data_path": str(source)}}, self.stage)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["exclusions"], [])
        self.assertEqual(run.call_count, 1)
        self.assertTrue(run.call_args.args[0][4].endswith("/psql"))

    def test_redis_aof_and_mismatched_path_never_dump_or_exclude(self):
        source = self.sources / "dump.rdb"
        source.write_bytes(b"fixture")
        for index, overrides in enumerate(({"appendonly": "yes"}, {"dir": "/different/redis"})):
            with self.subTest(overrides=overrides):
                values = {"dir": str(source.parent), "dbfilename": source.name, "appendonly": "no", **overrides}
                def run(argv, **_kwargs):
                    self.assertIn("CONFIG", argv)
                    return f"{argv[-1]}\n{values[argv[-1]]}\n".encode()
                with mock.patch.object(capture, "_run", side_effect=run):
                    receipt = capture.capture_state({"redis": {"enabled": True, "data_path": str(source)}},
                                                    self.root / f"redis-stage-{index}")
                self.assertEqual(receipt["status"], "incomplete")
                self.assertEqual(receipt["exclusions"], [])

    def test_failed_native_command_does_not_expose_stderr(self):
        result = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"PRIVATE_NATIVE_VALUE")
        with mock.patch.object(capture.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(capture.CaptureError, "exit 1") as error:
                capture._run(["/fixture/tool", "argument"], timeout=1)
        self.assertNotIn("PRIVATE_NATIVE_VALUE", str(error.exception))

    def test_restart_failure_keeps_overall_receipt_incomplete(self):
        state = {"active": True}
        def run(argv, **_kwargs):
            if argv[0] == "/usr/bin/tar":
                Path(argv[argv.index("-cf") + 1]).write_bytes(b"inert tar fixture")
            elif argv[1] == "stop":
                state["active"] = False
            elif argv[1] == "start":
                raise capture.CaptureError("fixture restart failed")
            else:
                return self.unit_output("fixture.service", state["active"])
            return b""
        with mock.patch.object(capture, "_run", side_effect=run):
            receipt = capture.capture_state({"offline_stores": [
                {"id": "offline", "path": str(self.sources), "units": ["fixture.service"]}]},
                self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(any(x["capture"] == "service_restoration" for x in receipt["errors"]))


if __name__ == "__main__":
    unittest.main()
