"""Private diagnostics and incomplete-snapshot handling on disposable fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import farm_recovery as recovery


class ResticDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="farm-restic-diagnostics-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.drive = self.root / "drive"
        self.drive.mkdir()
        self.repo = self.drive / "repository"
        self.repo.mkdir()
        self.password = self.root / "password"
        self.password.write_text("disposable fixture key\n")
        self.password.chmod(0o600)
        self.calls = self.root / "invocations.jsonl"
        self.binary = self.root / "fake-restic"
        self.diagnostics = self.root / "private-diagnostics" / "backup.json"
        self.secret_item = str(self.root / "sensitive-source" / "private-token-file")
        self.secret_message = "fixture private credential contents"
        self.summary = {"message_type": "summary", "snapshot_id": "a" * 64,
                        "total_bytes_processed": 73, "total_files_processed": 2,
                        "data_added": 47}

    def fake_restic(self, body, *, exit_code=3):
        self.binary.write_text(
            "#!/usr/bin/python3\nimport json, os, sys\n"
            "if sys.argv[1:] == ['version']:\n"
            "    print('restic 0.18.1 compiled with fixture')\n"
            "    raise SystemExit(0)\n"
            "operation = sys.argv[7]\n"
            f"with open({str(self.calls)!r}, 'a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if operation == 'cat':\n"
            "    print(json.dumps({'id': 'fixture-repository'}))\n"
            "    raise SystemExit(0)\n"
            "if operation != 'backup':\n"
            "    raise SystemExit('Unexpected fixture operation: ' + operation)\n"
            + body + "\n"
            + f"raise SystemExit({exit_code})\n"
        )
        self.binary.chmod(0o700)
        return recovery.Restic(self.binary, self.repo, self.password)

    def error_event(self, item=None):
        return {"message_type": "error", "during": "archival",
                "item": item or self.secret_item,
                "error": {"message": self.secret_message}}

    def error_body(self):
        return (f"print(json.dumps({self.error_event()!r}), file=sys.stderr, flush=True)\n"
                f"print(json.dumps({self.summary!r}), flush=True)")

    def run_failure(self, engine, *, path=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        kwargs = {"timeout": 10}
        if path:
            kwargs["diagnostics_path"] = self.diagnostics
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(recovery.ResticError) as caught:
                engine.run(["backup", "--", str(self.root / "source")], **kwargs)
        public = stdout.getvalue() + stderr.getvalue() + str(caught.exception)
        self.assertNotIn(self.secret_item, public)
        self.assertNotIn(self.secret_message, public)
        return caught.exception, public

    def report(self):
        self.assertEqual(stat.S_IMODE(self.diagnostics.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.diagnostics.parent.stat().st_mode), 0o700)
        self.assertEqual(self.diagnostics.stat().st_uid, os.geteuid())
        return json.loads(self.diagnostics.read_text())

    def test_exit_three_retains_private_error_and_partial_summary(self):
        engine = self.fake_restic(self.error_body())
        failure, public = self.run_failure(engine)
        report = self.report()
        self.assertEqual(report["schema"], "farm.recovery-restic-diagnostics/1")
        self.assertEqual(report["operation"], "backup")
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual(report["error_count"], 1)
        self.assertEqual(report["errors"][0]["item"], self.secret_item)
        self.assertEqual(report["errors"][0]["error"]["message"], self.secret_message)
        self.assertEqual(report["summary"]["snapshot_id"], self.summary["snapshot_id"])
        self.assertEqual(failure.exit_code, 3)
        self.assertEqual(failure.error_count, 1)
        self.assertEqual(failure.diagnostics_path, self.diagnostics)
        self.assertEqual(failure.summary["snapshot_id"], self.summary["snapshot_id"])
        self.assertIn("exit 3", public)
        self.assertNotIn("disposable fixture key", self.diagnostics.read_text())

    def test_large_malformed_stderr_is_drained_and_error_samples_are_bounded(self):
        body = ("os.write(2, b'x' * (2 * 1024 * 1024) + b'\\n')\n"
                "os.write(2, b'not json\\n\\xff\\xfe\\n')\n"
                f"event = {self.error_event()!r}\n"
                "for number in range(300):\n"
                "    event['item'] = event['item'].split('#')[0] + '#' + str(number)\n"
                "    print(json.dumps(event), file=sys.stderr, flush=True)\n"
                f"print(json.dumps({self.summary!r}), flush=True)")
        engine = self.fake_restic(body)
        failure, _ = self.run_failure(engine)
        report = self.report()
        self.assertEqual(failure.exit_code, 3)
        self.assertEqual(report["error_count"], 300)
        self.assertLessEqual(len(report["errors"]), 128)
        self.assertEqual(len(report["errors"]) + report["errors_dropped"], 300)
        self.assertGreaterEqual(report["malformed_stderr_records"], 3)
        self.assertLess(self.diagnostics.stat().st_size, 2 * 1024 * 1024)

    def test_trailing_error_without_newline_and_unexpected_fields_stay_private(self):
        event = self.error_event()
        event["unrecognized_secret"] = "never copy this field"
        event["error"]["extra_secret"] = "never copy nested field"
        event["during"] = "d" * 1024
        event["item"] = "i" * 32768
        event["error"]["message"] = "m" * 32768
        body = (f"os.write(2, json.dumps({event!r}).encode())\n"
                f"print(json.dumps({self.summary!r}), flush=True)")
        engine = self.fake_restic(body)
        self.run_failure(engine)
        report = self.report()
        error = report["errors"][0]
        self.assertLessEqual(len(error["during"]), 128)
        self.assertLessEqual(len(error["item"]), 4096)
        self.assertLessEqual(len(error["error"]["message"]), 4096)
        self.assertNotIn("never copy", self.diagnostics.read_text())

    def test_missing_diagnostics_destination_still_reports_incomplete_without_native_output(self):
        engine = self.fake_restic(self.error_body())
        failure, _ = self.run_failure(engine, path=False)
        self.assertEqual(failure.exit_code, 3)
        self.assertIsNone(failure.diagnostics_path)
        self.assertFalse(self.diagnostics.exists())

    def test_diagnostics_refuse_symlinked_parent_without_writing_elsewhere(self):
        engine = self.fake_restic(self.error_body())
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.diagnostics.parent.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(recovery.RecoveryError):
            engine.run(["backup"], timeout=10, diagnostics_path=self.diagnostics)
        self.assertFalse((elsewhere / self.diagnostics.name).exists())

    def test_backup_exit_three_never_checks_tags_or_publishes_complete_receipt(self):
        engine = self.fake_restic(self.error_body())
        source = self.root / "source"
        source.mkdir()
        (source / "value").write_text("disposable payload")
        state = self.root / "state"
        policy = {"schema": "farm.recovery-policy/1", "mode": "fixture",
                  "fixture_root": str(self.root), "source_roots": [str(source)],
                  "manifest_roots": [str(source)], "collect_host_metadata": False,
                  "state_dir": str(state), "exclude_paths": [], "host_id": "fixture",
                  "sqlite_roots": [], "sqlite_paths": [], "offline_stores": []}
        args = SimpleNamespace(quiesce=False)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(recovery, "open_repository", return_value=(self.drive, engine)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                self.assertRaises(recovery.ResticError) as caught:
            recovery.backup(args, policy)
        receipts = list((state / "receipts").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["restic_exit_code"], 3)
        self.assertEqual(receipt["restic_error_count"], 1)
        self.assertEqual(receipt["restic_summary"]["snapshot_id"], self.summary["snapshot_id"])
        self.assertNotIn("snapshot_id", receipt)
        self.assertEqual(Path(receipt["diagnostics_path"]).parent, state / "diagnostics")
        private_report = json.loads(Path(receipt["diagnostics_path"]).read_text())
        self.assertEqual(private_report["errors"][0]["item"], self.secret_item)
        invocations = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(invocations), 1, "Failure must stop before check or tagging")
        self.assertEqual(invocations[0][6], "backup")
        selection = json.loads((state / "current" / "selection.json").read_text())
        self.assertIn(str(state / "diagnostics"), selection["excluded_exact_paths"])
        public = stdout.getvalue() + stderr.getvalue() + str(caught.exception)
        self.assertNotIn(self.secret_item, public)
        self.assertNotIn(self.secret_message, public)
        self.assertNotIn('"status": "complete"', public)


if __name__ == "__main__":
    unittest.main()
