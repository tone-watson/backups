"""Exercise a relocated snapshot script with synthetic sources and fake host tools."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm-snapshot-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.snapshot = self.root / "snapshot"
        self.stamp = self.root / "success.stamp"
        self.sources = self.root / "sources"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.sources.mkdir()
        nginx = self.sources / "nginx"
        for name in ("sites-available", "sites-enabled", "conf.d"):
            (nginx / name).mkdir(parents=True)
        (nginx / "nginx.conf").write_text("fixture nginx config\n")
        cloudflared = self.sources / "cloudflared"
        cloudflared.mkdir()
        (cloudflared / "config.yml").write_text("fixture: true\n")
        (cloudflared / "fixture.json").write_text('{"fixture":true}\n')
        systemd = self.sources / "systemd"
        (systemd / "fixture.service.d").mkdir(parents=True)
        (systemd / "fixture.service").write_text("[Service]\nType=oneshot\n")
        (systemd / "fixture.service.d" / "override.conf").write_text("[Service]\n")
        (self.sources / "farm").symlink_to(systemd / "fixture.service")

        script = Path(__file__).with_name("snapshot.sh").read_text()
        paths = {"SNAP_DIR": self.snapshot, "LOG_FILE": self.root / "capture.log",
                 "STAMP_FILE": self.stamp, "NGINX_SRC": nginx,
                 "CLOUDFLARED_SRC": cloudflared, "SYSTEMD_SRC": systemd,
                 "FARM_LINK_SRC": self.sources / "farm"}
        lines = script.splitlines()
        for index, line in enumerate(lines):
            key = line.split("=", 1)[0]
            if key in paths:
                lines[index] = key + "=" + shlex.quote(str(paths[key]))
        script = "\n".join(lines) + "\n"
        guard = 'if [ "${EUID:-$(id -u)}" -ne 0 ]; then'
        self.assertIn(guard, script)
        # Only the temporary copy bypasses root; production has no test-mode switch.
        script = script.replace(guard, "if false; then", 1)
        self.script = self.root / "snapshot.sh"
        self.script.write_text(script)
        (self.root / "capture-api-uv.py").write_text('''
import json, os, pathlib, sys
private = sys.argv[1] == "--stable-diffusion-env"
destination = pathlib.Path(sys.argv[-1])
destination.mkdir(parents=True, exist_ok=True)
if os.environ.get("FIXTURE_PRIVATE_FAIL" if private else "FIXTURE_UV_FAIL"):
    if not private:
        (destination / "capture.json").write_text('{"status":"incomplete"}')
    raise SystemExit(1)
payload = destination / ("service.env" if private else "retained.whl")
if not payload.exists():
    payload.write_bytes(b"inert fixture bytes")
    payload.chmod(0o600)
if not private:
    (destination / "capture.json").write_text('{"status":"passed"}')
print(json.dumps({"status":"passed"}))
''')
        self.fake("chown", "raise SystemExit(0)")
        self.fake("apt", '''
if os.environ.get("FIXTURE_APT_FAIL"):
    raise SystemExit(1)
print("fixture/stable 1.0 amd64 [installed]")
''')
        self.fake("crontab", '''
user = sys.argv[-1]
if os.environ.get("FIXTURE_CRON_FAIL"):
    print("permission denied", file=sys.stderr)
    raise SystemExit(1)
if user == "root" or os.environ.get("FIXTURE_NO_OWNER_CRON"):
    print("no crontab for " + user, file=sys.stderr)
    raise SystemExit(1)
print("# inert fixture schedule")
''')
        self.fake("cp", '''
match = os.environ.get("FIXTURE_CP_FAIL", "")
if match and any(match in arg for arg in sys.argv[1:]):
    raise SystemExit(1)
os.execv("/bin/cp", ["/bin/cp", *sys.argv[1:]])
''')
        self.fake("chmod", '''
if os.environ.get("FIXTURE_CHMOD_FAIL") and any("fixture.json" in arg for arg in sys.argv[1:]):
    raise SystemExit(1)
os.execv("/bin/chmod", ["/bin/chmod", *sys.argv[1:]])
''')

    def fake(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/python3\nimport os, sys\n" + body + "\n")
        path.chmod(0o755)

    def run_snapshot(self, **changes):
        env = dict(os.environ, BASH_ENV="/dev/null", PATH=str(self.bin) + ":/usr/bin:/bin")
        env.update(changes)
        return subprocess.run(["/bin/bash", str(self.script)], env=env,
                              text=True, capture_output=True, timeout=20)

    def receipt(self):
        return json.loads((self.snapshot / "manifest.json").read_text())

    def test_success_and_repeat_reuse_keep_runtime_inode(self):
        result = self.run_snapshot()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        receipt = self.receipt()
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["failure_count"], 0)
        self.assertEqual(receipt["warnings"], [])
        self.assertTrue(self.stamp.read_text().strip().isdigit())
        self.assertEqual(self.stamp.stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.snapshot / "manifest.json").stat().st_mode & 0o777, 0o600)
        wheel = self.snapshot / "uv/api/retained.whl"
        before = wheel.stat().st_ino
        self.assertEqual(self.run_snapshot().returncode, 0)
        self.assertEqual(wheel.stat().st_ino, before)

    def test_required_failures_leave_incomplete_receipt_and_prior_stamp(self):
        for failure in ({"FIXTURE_CP_FAIL": "nginx.conf"},
                        {"FIXTURE_CP_FAIL": "fixture.service"},
                        {"FIXTURE_UV_FAIL": "1"}, {"FIXTURE_PRIVATE_FAIL": "1"},
                        {"FIXTURE_APT_FAIL": "1"}, {"FIXTURE_CRON_FAIL": "1"},
                        {"FIXTURE_CHMOD_FAIL": "1"}):
            with self.subTest(failure=failure):
                self.stamp.write_text("old-success\n")
                result = self.run_snapshot(**failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.receipt()["status"], "incomplete")
                self.assertGreater(self.receipt()["failure_count"], 0)
                self.assertEqual(self.stamp.read_text(), "old-success\n")
                self.assertNotIn("Config snapshot complete", result.stdout)

    def test_missing_required_nginx_file_is_not_silent_success(self):
        (self.sources / "nginx/nginx.conf").unlink()
        self.assertNotEqual(self.run_snapshot().returncode, 0)
        self.assertIn("nginx/nginx.conf not found", self.receipt()["failures"])
        self.assertFalse(self.stamp.exists())

    def test_optional_absent_crontab_is_a_successful_warning(self):
        result = self.run_snapshot(FIXTURE_NO_OWNER_CRON="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.receipt()["status"], "passed")
        self.assertEqual(self.receipt()["failure_count"], 0)
        self.assertEqual(self.receipt()["warning_count"], 1)

    def test_runtime_failure_does_not_chmod_source_hardlink(self):
        source = self.sources / "source.whl"
        source.write_bytes(b"fixture")
        source.chmod(0o644)
        destination = self.snapshot / "uv/api"
        destination.mkdir(parents=True)
        os.link(source, destination / "retained.whl")
        self.assertNotEqual(self.run_snapshot(FIXTURE_UV_FAIL="1").returncode, 0)
        self.assertEqual(source.stat().st_mode & 0o777, 0o644)
        self.assertEqual(source.read_bytes(), b"fixture")

    def test_atomic_manifest_replacement_does_not_mutate_hardlink(self):
        source = self.sources / "previous-receipt"
        source.write_text("previous bytes")
        self.snapshot.mkdir()
        os.link(source, self.snapshot / "manifest.json")
        self.assertEqual(self.run_snapshot().returncode, 0)
        self.assertEqual(source.read_text(), "previous bytes")
        self.assertNotEqual(source.stat().st_ino, (self.snapshot / "manifest.json").stat().st_ino)

    def test_failure_receipt_escapes_quoted_unit_filename(self):
        path = self.sources / 'systemd/quoted"unit.service'
        path.write_text("fixture")
        result = self.run_snapshot(FIXTURE_CP_FAIL='quoted"unit')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('systemd/quoted"unit.service copy failed', self.receipt()["failures"])

    def test_concurrent_capture_refuses_without_replacing_receipt(self):
        self.snapshot.mkdir()
        receipt = self.snapshot / "manifest.json"
        receipt.write_text('{"previous":true}')
        descriptor = os.open(self.snapshot, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertNotEqual(self.run_snapshot().returncode, 0)
        self.assertEqual(json.loads(receipt.read_text()), {"previous": True})
        self.assertFalse(self.stamp.exists())

    def test_heartbeat_publication_failure_marks_capture_incomplete(self):
        self.stamp.mkdir()
        result = self.run_snapshot()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.receipt()["status"], "incomplete")
        self.assertIn("success heartbeat could not be published", self.receipt()["failures"])


if __name__ == "__main__":
    unittest.main()
