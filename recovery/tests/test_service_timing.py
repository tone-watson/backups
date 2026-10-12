"""Disposable fixtures and mocked systemd jobs; no host services are queried."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("farm_capture_timing", Path(__file__).parents[1] / "capture.py")
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class ServiceTimingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm-service-timing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.stage = self.root / "stage"
        self.calls = []
        self.active = {}

    def policy(self, units=("fixture.service",), timeout=900):
        return {"timeout_seconds": timeout, "offline_stores": [
            {"id": "fixture", "path": str(self.source), "units": list(units)}]}

    def fake_systemd(self, limits, *, fail_action=None, interrupt_action=None):
        def run(argv, *, timeout, **_kwargs):
            self.assertEqual(argv[0], "/usr/bin/systemctl")
            action, unit = argv[1:3]
            self.calls.append((action, unit, timeout))
            if action == "show":
                for prop in capture.UNIT_TIMEOUT_PROPERTIES:
                    self.assertIn(prop, argv[-1])
                active = self.active.get(unit, True)
                substate = ("listening" if unit.endswith(".socket") else "running") if active else "dead"
                fields = {"Id": unit, "Names": unit, "LoadState": "loaded",
                          "ActiveState": "active" if active else "inactive", "SubState": substate,
                          "MainPID": "123" if active and unit.endswith(".service") else "0", "ControlPID": "0",
                          **limits}
                return "".join(f"{key}={value}\n" for key, value in fields.items()).encode()
            self.assertIn(action, {"stop", "start"})
            self.assertIsNone(timeout, "systemd jobs must not inherit the native-capture deadline")
            journal = json.loads((self.stage / "manifest.json").read_text())
            wait = journal["service_waits"][-1]
            self.assertEqual((wait["unit"], wait["action"], wait["status"]), (unit, action, "waiting"))
            self.assertEqual(wait["initial_manager_timeouts"], limits)
            self.assertIsNone(wait["client_timeout_seconds"])
            self.assertEqual(wait["deadline_owner"], "systemd")
            self.active[unit] = action == "start"
            if action == interrupt_action:
                raise KeyboardInterrupt()
            if action == fail_action:
                raise capture.CaptureError("fixture systemd job failed")
            return b""
        return run

    def successful_capture(self, store, stage, timeout):
        self.assertEqual(timeout, 900)
        return {"kind": "fixture", "artifact": "fixture.tar"}, [store["path"]]

    def test_hour_stop_and_infinite_start_do_not_inherit_native_900_seconds(self):
        limits = {"TimeoutStopUSec": "1h", "TimeoutStartUSec": "infinity"}
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd(limits)), \
                mock.patch.object(capture, "_capture_offline", side_effect=self.successful_capture):
            receipt = capture.capture_state(self.policy(), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "complete", receipt)
        self.assertTrue(self.active["fixture.service"])
        self.assertEqual([timeout for action, _, timeout in self.calls if action == "show"], [900, 900, 900])
        self.assertEqual([action for action, _, _ in self.calls if action != "show"], ["stop", "start"])
        self.assertEqual(receipt["initial_unit_states"]["fixture.service"]["TimeoutStopUSec"], "1h")
        for wait in receipt["service_waits"]:
            self.assertEqual(wait["status"], "job_returned_success")
            self.assertGreaterEqual(wait["elapsed_seconds"], 0)

    def test_socket_shared_timeout_is_recorded_without_guessing_job_duration(self):
        limits = {"TimeoutUSec": "1min 30s"}
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd(limits)), \
                mock.patch.object(capture, "_capture_offline", side_effect=self.successful_capture):
            receipt = capture.capture_state(self.policy(("fixture.socket",)), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "complete", receipt)
        self.assertEqual([wait["initial_manager_timeouts"] for wait in receipt["service_waits"]], [limits, limits])

    def test_missing_properties_still_let_systemd_own_deadline(self):
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd({})), \
                mock.patch.object(capture, "_capture_offline", side_effect=self.successful_capture):
            receipt = capture.capture_state(self.policy(), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "complete", receipt)
        self.assertEqual([wait["initial_manager_timeouts"] for wait in receipt["service_waits"]], [{}, {}])

    def test_initially_active_socket_is_restored_before_its_daemon(self):
        units = ("fixture.socket", "fixture.service")
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd({})), \
                mock.patch.object(capture, "_capture_offline", side_effect=self.successful_capture):
            receipt = capture.capture_state(self.policy(units), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "complete", receipt)
        self.assertEqual([(action, unit) for action, unit, _ in self.calls if action != "show"],
                         [("stop", "fixture.socket"), ("stop", "fixture.service"),
                          ("start", "fixture.socket"), ("start", "fixture.service")])

    def test_restoration_keeps_reverse_order_within_sockets_and_services(self):
        units = ("first.socket", "first.service", "second.socket", "second.service")
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd({})), \
                mock.patch.object(capture, "_capture_offline", side_effect=self.successful_capture):
            receipt = capture.capture_state(self.policy(units), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "complete", receipt)
        self.assertEqual([unit for action, unit, _ in self.calls if action == "start"],
                         ["second.socket", "first.socket", "second.service", "first.service"])

    def test_every_unit_is_validated_before_first_stop(self):
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd({})), \
                mock.patch.object(capture, "_capture_offline") as offline:
            receipt = capture.capture_state(self.policy(("fixture.service", "ssh.service")), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(self.calls, [("show", "fixture.service", 900)])
        self.assertEqual(receipt["service_waits"], [])
        self.assertEqual(receipt["service_actions"], [])
        offline.assert_not_called()

    def test_runtime_recipe_is_checked_before_any_service_command(self):
        policy = self.policy()
        policy["offline_stores"][0]["runtime_recipe"] = "fixture-invalid-recipe"
        with mock.patch.object(capture, "_archive_helper", return_value=object()), \
                mock.patch.object(capture, "_runtime_entries", side_effect=capture.CaptureError("invalid fixture recipe")), \
                mock.patch.object(capture, "_run") as run:
            receipt = capture.capture_state(policy, self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["service_actions"], [])
        run.assert_not_called()

    def test_invalid_native_timeout_fails_before_any_service_command(self):
        with mock.patch.object(capture, "_run") as run:
            receipt = capture.capture_state(self.policy(timeout=0), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        run.assert_not_called()

    def test_capture_failure_still_restores_using_manager_owned_wait(self):
        limits = {"TimeoutStopUSec": "1h", "TimeoutStartUSec": "infinity"}
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd(limits)), \
                mock.patch.object(capture, "_capture_offline", side_effect=capture.CaptureError("fixture capture failed")):
            receipt = capture.capture_state(self.policy(), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(self.active["fixture.service"])
        self.assertEqual(receipt["service_restoration"], [{"unit": "fixture.service", "status": "restored"}])
        self.assertEqual([wait["status"] for wait in receipt["service_waits"]],
                         ["job_returned_success", "job_returned_success"])

    def test_interrupted_stop_journals_interruption_and_attempts_restoration(self):
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd({}, interrupt_action="stop")):
            with self.assertRaises(KeyboardInterrupt):
                capture.capture_state(self.policy(), self.stage, quiesce=True)
        receipt = json.loads((self.stage / "manifest.json").read_text())
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(self.active["fixture.service"])
        self.assertEqual([wait["status"] for wait in receipt["service_waits"]], ["interrupted", "job_returned_success"])
        self.assertEqual(receipt["service_restoration"], [{"unit": "fixture.service", "status": "restored"}])

    def test_failed_restart_records_failed_wait_and_incomplete_capture(self):
        with mock.patch.object(capture, "_run", side_effect=self.fake_systemd({}, fail_action="start")), \
                mock.patch.object(capture, "_capture_offline", side_effect=self.successful_capture):
            receipt = capture.capture_state(self.policy(), self.stage, quiesce=True)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["service_waits"][-1]["status"], "failed")
        self.assertEqual(receipt["service_restoration"], [])
        self.assertEqual(receipt["errors"][0]["capture"], "service_restoration")


if __name__ == "__main__":
    unittest.main()
