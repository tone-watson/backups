"""Redis regression checks; native processes use only disposable local data."""
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


capture = load("redis_capture_regression", "capture.py")


class RedisCaptureTests(unittest.TestCase):
    def test_failed_redis_preflight_never_stops_an_offline_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dump.rdb"
            source.write_bytes(b"fixture")
            offline = root / "offline"
            offline.mkdir()
            state = {"ActiveState": "active", "SubState": "running"}
            with mock.patch.object(capture, "_unit_state", return_value=state), mock.patch.object(
                    capture, "_verify_redis_persistence", side_effect=capture.CaptureError("fixture Redis failure")), mock.patch.object(
                    capture, "_control_unit") as control:
                receipt = capture.capture_state({
                    "redis": {"enabled": True, "data_path": str(source)},
                    "offline_stores": [{"id": "offline", "path": str(offline), "units": ["fixture.service"]}],
                }, root / "stage", quiesce=True)
            self.assertEqual(receipt["status"], "incomplete")
            self.assertEqual(receipt["service_actions"], [])
            self.assertEqual(receipt["errors"][0]["capture"], "preflight")
            control.assert_not_called()

    def test_redis_identity_drift_does_not_publish_or_exclude(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dump.rdb"
            source.write_bytes(b"fixture")
            stage = root / "stage"

            def run(argv, **kwargs):
                if "--rdb" in argv:
                    Path(argv[-1]).write_bytes(b"fixture capture")
                return b""

            with mock.patch.object(capture, "_verify_redis_persistence", side_effect=[
                    {"identity": "old"}, {"identity": "old"}, {"identity": "new"}]), mock.patch.object(
                    capture, "_run", side_effect=run):
                receipt = capture.capture_state({"redis": {"enabled": True, "data_path": str(source)}}, stage)
            self.assertEqual(receipt["status"], "incomplete")
            self.assertEqual(receipt["exclusions"], [])
            self.assertFalse((stage / "redis.rdb").exists())
            self.assertFalse((stage / ".redis.rdb.partial").exists())


@unittest.skipUnless(Path("/usr/bin/redis-server").exists() and Path("/usr/bin/redis-cli").exists(),
                     "native Redis binaries are required")
class NativeRedisCaptureTests(unittest.TestCase):
    def cli(self, port, *args):
        result = subprocess.run(["/usr/bin/redis-cli", "-h", "127.0.0.1", "-p", str(port), "--raw", *args],
                                check=True, capture_output=True, timeout=20, env=capture.ENV)
        return result.stdout

    def start(self, root):
        root.mkdir(exist_ok=True)
        # No production port or socket is used. Verify PID/config before writes.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = root / "redis.conf"
        config.write_text(f'bind 127.0.0.1\nport {port}\ndaemonize no\nsave ""\n'
                          f'dir {root}\ndbfilename dump.rdb\nappendonly no\n'
                          'rename-command CONFIG ""\n')
        started_ns = time.time_ns()
        process = subprocess.Popen(["/usr/bin/redis-server", str(config)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=capture.ENV)

        def cleanup():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("scratch Redis exited before readiness")
            try:
                info = self.cli(port, "INFO", "server").decode()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                time.sleep(0.05)
                continue
            fields = dict(line.split(":", 1) for line in info.splitlines() if ":" in line)
            self.assertEqual(fields.get("process_id"), str(process.pid), "refuse to write to another process")
            self.assertEqual(fields.get("config_file"), str(config))
            return process, port, config, started_ns
        self.fail("scratch Redis readiness timed out")

    def test_disabled_config_captures_live_data_and_restores_with_native_redis(self):
        helper = load("native_redis_state", "redis_state.py")
        temporary = tempfile.TemporaryDirectory(prefix="farm-native-redis-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        process, port, config_file, started_ns = self.start(root / "source")
        self.assertIn(b"unknown command", self.cli(port, "CONFIG", "GET", "dir"))
        self.assertEqual(self.cli(port, "SET", "backup-fixture", "old-disk-value"), b"OK\n")
        self.assertEqual(self.cli(port, "SAVE"), b"OK\n")
        self.assertEqual(self.cli(port, "SET", "backup-fixture", "fresh-live-value"), b"OK\n")
        config = {"enabled": True, "host": "127.0.0.1", "port": port,
                  "data_path": str(config_file.parent / "dump.rdb"),
                  "persistence_verification": "disabled-config", "config_file": str(config_file),
                  "service_unit": "fixture-redis.service"}
        context = {"pid": process.pid, "started_ns": started_ns,
                   "started_monotonic_us": int((Path(f"/proc/{process.pid}/stat").read_text()
                                                .rsplit(")", 1)[1].split())[19]) * 1_000_000 // os.sysconf("SC_CLK_TCK"),
                   "unit_file": {"fixture": True}, "executable": "/usr/bin/redis-server",
                   "invocation_id": "fixture"}

        def verify(config, source, timeout):
            try:
                return helper.verify_disabled_config(config, source, capture._run, timeout)
            except helper.RedisVerificationError as exc:
                raise capture.CaptureError(str(exc)) from None

        with mock.patch.object(helper, "_systemd_context", return_value=context), mock.patch.object(
                capture, "_verify_redis_persistence", side_effect=verify):
            receipt = capture.capture_state({"redis": config}, root / "stage")
        self.assertEqual(receipt["status"], "complete", receipt)
        self.assertEqual(receipt["exclusions"], [config["data_path"]])
        artifact = root / "stage" / "redis.rdb"
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(receipt["captures"]), 1)
        self.assertIn("persistence_verification", receipt["captures"][0])
        restored = root / "restored"
        restored.mkdir()
        shutil.copy2(artifact, restored / "dump.rdb")
        _, restored_port, _, _ = self.start(restored)
        self.assertEqual(self.cli(restored_port, "GET", "backup-fixture"), b"fresh-live-value\n")
        self.assertIn(b"unknown command", self.cli(port, "CONFIG", "GET", "dir"))


if __name__ == "__main__":
    unittest.main()
