"""Exercise backup serialization and failure handling without network or source data."""

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


HERE = Path(__file__).resolve().parent


class DropboxUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / "events.jsonl"
        self.lock = self.root / "upload.lock"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "snapshot.db").write_text("test fixture")
        for name in ("dropbox-common.sh", "production-to-dropbox.sh", "databases-to-dropbox.sh"):
            script = (HERE / name).read_text()
            for old, new in {
                "/srv/farm/logs/farm-dropbox-upload.lock": str(self.lock),
                "/srv/farm/production": str(self.source),
                "/var/backups/data/databases": str(self.source),
                "/var/log/farm-dropbox-sync.log": str(self.root / "production.log"),
                "/srv/farm/logs/farm-dropbox-db-sync.log": str(self.root / "database.log"),
                "/srv/farm/logs/farm-dropbox-sync.stamp": str(self.root / "production.stamp"),
                "/srv/farm/logs/farm-dropbox-db-sync.stamp": str(self.root / "database.stamp"),
            }.items():
                script = script.replace(old, new)
            (self.scripts / name).write_text(script)
        (self.scripts / "exclude.txt").write_text("")
        self.make_executable("rclone", """#!/usr/bin/python3
import json, os, sys, time
from pathlib import Path
if sys.argv[1] == 'listremotes':
    print('dropbox:')
    raise SystemExit(0)
assert sys.argv[1] == 'sync', sys.argv
def record(event):
    with open(os.environ['TEST_EVENTS'], 'a') as f:
        f.write(json.dumps({'event': event, 'job': os.environ['TEST_JOB'],
                            'pid': os.getpid(), 'args': sys.argv[1:]}) + '\\n')
record('start')
time.sleep(float(os.environ.get('TEST_UPLOAD_SECONDS', '0.1')))
record('end')
raise SystemExit(int(os.environ.get('TEST_UPLOAD_EXIT', '0')))
""")
        self.make_executable("redis-cli", """#!/usr/bin/python3
import sys
from pathlib import Path
assert sys.argv[1] == '--rdb', sys.argv
Path(sys.argv[2]).write_text('fake redis snapshot')
""")
        self.processes = []
        self.addCleanup(self.stop_processes)

    def make_executable(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def stop_processes(self):
        for proc in self.processes:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate(timeout=5)

    def launch(self, command, job, seconds="0.1", exit_code="0"):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   TEST_EVENTS=str(self.events), TEST_JOB=job,
                   TEST_UPLOAD_SECONDS=seconds, TEST_UPLOAD_EXIT=exit_code)
        proc = subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        self.processes.append(proc)
        return proc

    def script(self, name, job, **kwargs):
        return self.launch(["bash", str(self.scripts / name)], job, **kwargs)

    def helper(self, job, duration, **kwargs):
        # Arguments avoid shell interpolation of temporary paths.
        return self.launch([
            "bash", "-c",
            'set -euo pipefail; source "$1"; acquire_dropbox_lock "$2"; '
            'run_dropbox_upload "$3" source destination; touch "$4"',
            "test", str(self.scripts / "dropbox-common.sh"), str(self.lock),
            duration, str(self.root / f"{job}.stamp"),
        ], job, **kwargs)

    def recorded(self):
        if not self.events.exists():
            return []
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def wait_for_start(self, job):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(e["event"] == "start" and e["job"] == job for e in self.recorded()):
                return
            time.sleep(0.01)
        self.fail(f"{job} did not start")

    def finish(self, proc, expected=0):
        output = proc.communicate(timeout=6)[0]
        self.assertEqual(proc.returncode, expected, output)
        return output

    def test_real_scripts_serialize_and_preserve_versioning_and_budget(self):
        production = self.script("production-to-dropbox.sh", "production", seconds="0.4")
        self.wait_for_start("production")
        database = self.script("databases-to-dropbox.sh", "database")
        self.finish(production)
        self.finish(database)
        events = self.recorded()
        self.assertEqual([(e["job"], e["event"]) for e in events], [
            ("production", "start"), ("production", "end"),
            ("database", "start"), ("database", "end"),
        ])
        for event in events[::2]:
            args = event["args"]
            for flag, value in (("--bwlimit", "1M"), ("--transfers", "1"), ("--checkers", "2")):
                self.assertEqual(args[args.index(flag) + 1], value)
            archive = args[args.index("--backup-dir") + 1]
            self.assertTrue(archive.startswith("dropbox:farm/_archive/"))
        self.assertIn("--track-renames", events[0]["args"])
        self.assertIn("--exclude-from", events[0]["args"])
        self.assertTrue((self.root / "production.stamp").exists())
        self.assertTrue((self.root / "database.stamp").exists())

    def test_failed_upload_does_not_advance_either_success_stamp(self):
        for script, name in (("production-to-dropbox.sh", "production"),
                             ("databases-to-dropbox.sh", "database")):
            stamp = self.root / f"{name}.stamp"
            stamp.write_text("previous success")
            self.finish(self.script(script, name, exit_code="7"), expected=7)
            self.assertEqual(stamp.read_text(), "previous success")

    def test_queue_wait_does_not_consume_upload_timeout(self):
        first = self.helper("first", "2s", seconds="0.5")
        self.wait_for_start("first")
        second = self.helper("second", "0.2s", seconds="0.03")
        self.finish(first)
        self.finish(second)
        self.assertTrue((self.root / "second.stamp").exists())

    def test_upload_timeout_fails_without_success_and_releases_lock(self):
        timed_out = self.helper("slow", "0.1s", seconds="3")
        self.finish(timed_out, expected=124)
        self.assertFalse((self.root / "slow.stamp").exists())
        self.finish(self.helper("next", "2s"))

    def test_surviving_upload_retains_lock_after_wrapper_is_killed(self):
        first = self.helper("first", "3s", seconds="0.6")
        self.wait_for_start("first")
        first.kill()  # Kill the wrapper, deliberately leaving its upload alive.
        second = self.helper("second", "2s")
        self.finish(second)
        self.finish(first, expected=-signal.SIGKILL)
        self.assertEqual([(e["job"], e["event"]) for e in self.recorded()], [
            ("first", "start"), ("first", "end"),
            ("second", "start"), ("second", "end"),
        ])


if __name__ == "__main__":
    unittest.main()
