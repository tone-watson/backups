"""Run only synthetic temporary-file recovery captures; no root or live snapshots."""
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import tarfile
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

spec = importlib.util.spec_from_file_location("capture_api_uv", Path(__file__).with_name("capture-api-uv.py"))
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.managed_python = mock.patch.object(capture, "capture_managed_python", return_value={"status": "passed", "fixture": True})
        self.managed_capture = self.managed_python.start()
        self.addCleanup(self.managed_python.stop)
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
        self.upstream_roots = {name: self.root / name / "wheels"
                               for name in capture.UPSTREAM_WHEEL_ROOTS}
        patcher = mock.patch.dict(capture.UPSTREAM_WHEEL_ROOTS, self.upstream_roots, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.upstream_wheels = {}
        for profile, wheel_root in self.upstream_roots.items():
            prefix = {"comfyui": "mode-preserved/", "ace-step": "retained/", "live-portrait": "retained/", "flood-map": "retained/"}.get(profile, "")
            relative = prefix + "example-2.0-py3-none-any.whl"
            wheel = wheel_root / relative
            wheel.parent.mkdir(parents=True)
            wheel.write_bytes((profile + " inert bytes").encode())
            wheel.chmod(0o644)
            self.upstream_wheels[profile] = wheel
            sha256 = hashlib.sha256(wheel.read_bytes()).hexdigest()
            entry = "example @ " + wheel.as_uri() + " --hash=sha256:" + sha256 + "\n"
            if profile == "comfyui":
                entry = entry.replace(" --hash", " \\\n    --hash")
            remote = "remote==1.0 --hash=sha256:" + "a" * 64 + "\n"
            prefix = "" if profile == "graphiti" else profile + "/"
            for filename in capture.UPSTREAM_LOCKS[profile]:
                lock = self.repo / "deploy/upstream" / (prefix + filename)
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.write_text(entry if filename == "requirements-overlays.lock"
                                else "# fixture\n" + entry + remote)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], check=True)

    def commit(self):
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "update"], check=True)

    def test_managed_python_failure_keeps_overall_capture_incomplete(self):
        self.managed_capture.side_effect = FileNotFoundError("fixture missing interpreter archive")
        with self.assertRaises(FileNotFoundError):
            capture.capture(self.repo, self.destination)
        self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")
        self.managed_capture.assert_called_once_with(self.destination / "managed-python/cpython-3.9.18-build-20240224")

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

    def test_upstream_capture_archives_recipes_and_copies_only_locked_wheels(self):
        for wheel in self.upstream_wheels.values():
            wheel.with_name("unlisted-1.0-py3-none-any.whl").write_bytes(b"not needed")
        result = capture.capture(self.repo, self.destination)
        with tarfile.open(self.destination / "api-recipes.tar") as archive:
            self.assertIn("scripts/upstream_runtime.py", archive.getnames())
            self.assertIn("deploy/upstream/comfyui/requirements.lock", archive.getnames())
            self.assertIn("deploy/upstream/flood-map/requirements.lock", archive.getnames())
        for profile, source in self.upstream_wheels.items():
            item = result["upstream"][profile]
            self.assertEqual(item["artifact_count"], 1)
            self.assertEqual(item["copied_wheels"], 1)
            self.assertEqual(item["artifacts"][0]["locks"], list(capture.UPSTREAM_LOCKS[profile]))
            target = self.destination / "upstream" / profile / "wheelhouse" / item["artifacts"][0]["filename"]
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(source.stat().st_mode & 0o777, 0o644)
            self.assertFalse(target.with_name("unlisted-1.0-py3-none-any.whl").exists())
        second = capture.capture(self.repo, self.destination)
        self.assertTrue(all(item["reused_wheels"] == 1 for item in second["upstream"].values()))

    def test_graphiti_profiles_share_one_bundle_and_keep_distinct_wheels(self):
        for profile in ("graphiti-root", "graphiti-mcp"):
            source = self.upstream_roots["graphiti"] / (profile + "-1.0-py3-none-any.whl")
            source.write_bytes((profile + " unique fixture").encode())
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            lock = self.repo / "deploy/upstream" / profile / "requirements.lock"
            with lock.open("a") as stream:
                stream.write(profile + " @ " + source.as_uri() + " --hash=sha256:" + digest + "\n")
        self.commit()
        result = capture.capture(self.repo, self.destination)
        graphiti = result["upstream"]["graphiti"]
        self.assertEqual(graphiti["artifact_count"], 3)
        self.assertEqual(graphiti["copied_wheels"], 3)
        shared = next(item for item in graphiti["artifacts"]
                      if item["filename"] == self.upstream_wheels["graphiti"].name)
        self.assertEqual(shared["locks"], ["graphiti-root/requirements.lock",
                                            "graphiti-mcp/requirements.lock"])
        wheelhouse = self.destination / "upstream/graphiti/wheelhouse"
        self.assertEqual(len(list(wheelhouse.glob("*.whl"))), 3)
        with tarfile.open(self.destination / "api-recipes.tar") as archive:
            for profile in ("graphiti-root", "graphiti-mcp"):
                self.assertIn("deploy/upstream/" + profile + "/requirements.lock", archive.getnames())
        second = capture.capture(self.repo, self.destination)
        self.assertEqual(second["upstream"]["graphiti"]["reused_wheels"], 3)
        self.assertEqual(second["copied_wheels"], 0)
        self.assertEqual(second["upstream"]["comfyui"]["artifact_count"], 1)
        self.assertEqual(second["upstream"]["stable-diffusion"]["artifact_count"], 1)

    def test_graphiti_conflicting_committed_shared_hash_fails_closed(self):
        lock = self.repo / "deploy/upstream/graphiti-mcp/requirements.lock"
        source_hash = hashlib.sha256(self.upstream_wheels["graphiti"].read_bytes()).hexdigest()
        lock.write_text(lock.read_text().replace(source_hash, "0" * 64))
        self.assertEqual(capture.capture(self.repo, self.destination)["status"], "passed")
        self.commit()
        with self.assertRaisesRegex(ValueError, "Conflicting upstream wheel hashes"):
            capture.capture(self.repo, self.destination)
        self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")

    def test_graphiti_requires_both_locks_in_committed_api_revision(self):
        lock = self.repo / "deploy/upstream/graphiti-mcp/requirements.lock"
        content = lock.read_bytes()
        lock.unlink()
        self.commit()
        lock.write_bytes(content)  # Prepared but uncommitted is not recoverable HEAD.
        with self.assertRaises(subprocess.CalledProcessError):
            capture.capture(self.repo, self.destination)
        self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")

    def test_ace_step_retained_wheel_selected_without_unreviewed_original(self):
        source = self.upstream_wheels["ace-step"]
        original = self.upstream_roots["ace-step"] / source.name
        original.write_bytes(b"unpatched registry wheel must not be substituted")
        second = self.upstream_roots["ace-step"] / "dependency-1.0-py3-none-any.whl"
        second.write_bytes(b"small synthetic indexed dependency")
        sha256 = hashlib.sha256(second.read_bytes()).hexdigest()
        lock = self.repo / "deploy/upstream/ace-step/requirements.lock"
        with lock.open("a") as stream:
            stream.write("dependency @ " + second.as_uri() + " --hash=sha256:" + sha256 + "\n")
        self.commit()
        result = capture.capture(self.repo, self.destination)
        ace = result["upstream"]["ace-step"]
        self.assertEqual(ace["artifact_count"], 2)
        self.assertEqual(ace["copied_wheels"], 2)
        self.assertEqual(ace["lock_sha256"]["requirements.lock"],
                         hashlib.sha256(lock.read_bytes()).hexdigest())
        wheelhouse = self.destination / "upstream/ace-step/wheelhouse"
        retained = wheelhouse / "retained" / source.name
        self.assertEqual(retained.read_bytes(), source.read_bytes())
        self.assertNotEqual(retained.stat().st_ino, source.stat().st_ino)
        self.assertEqual(retained.stat().st_mode & 0o777, 0o600)
        self.assertFalse((wheelhouse / original.name).exists())
        self.assertEqual((wheelhouse / second.name).read_bytes(), second.read_bytes())
        with tarfile.open(self.destination / "api-recipes.tar") as archive:
            self.assertEqual(archive.extractfile("deploy/upstream/ace-step/requirements.lock").read(),
                             lock.read_bytes())
        repeated = capture.capture(self.repo, self.destination)
        self.assertEqual(repeated["upstream"]["ace-step"]["reused_wheels"], 2)
        self.assertEqual(repeated["copied_wheels"], 0)
        for profile in ("comfyui", "stable-diffusion", "graphiti"):
            self.assertEqual(repeated["upstream"][profile]["artifact_count"], 1)
            self.assertEqual(repeated["upstream"][profile]["reused_wheels"], 1)

    def test_ace_step_corrupt_retained_payload_invalidates_previous_capture(self):
        capture.capture(self.repo, self.destination)
        self.upstream_wheels["ace-step"].write_bytes(b"unexpected payload change")
        with self.assertRaisesRegex(ValueError, "hash/size"):
            capture.capture(self.repo, self.destination)
        self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")

    def test_ace_step_lock_must_exist_in_committed_revision(self):
        lock = self.repo / "deploy/upstream/ace-step/requirements.lock"
        prepared = lock.read_bytes()
        lock.unlink()
        self.commit()
        lock.write_bytes(prepared)
        with self.assertRaises(subprocess.CalledProcessError):
            capture.capture(self.repo, self.destination)
        self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")

    def test_upstream_hardlink_is_replaced_and_corrupt_backup_repaired(self):
        source = self.upstream_wheels["comfyui"]
        target = self.destination / "upstream/comfyui/wheelhouse/mode-preserved" / source.name
        target.parent.mkdir(parents=True)
        os.link(source, target)
        result = capture.capture(self.repo, self.destination)
        self.assertEqual(result["upstream"]["comfyui"]["copied_wheels"], 1)
        self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)
        self.assertEqual(source.stat().st_mode & 0o777, 0o644)
        target.write_bytes(b"wrong")
        capture.capture(self.repo, self.destination)
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_upstream_corruption_or_missing_wheel_invalidates_capture(self):
        source = self.upstream_wheels["stable-diffusion"]
        for missing in (False, True):
            if missing:
                source.unlink()
            else:
                source.write_bytes(b"wrong")
            with self.subTest(missing=missing), self.assertRaises((ValueError, FileNotFoundError)):
                capture.capture(self.repo, self.destination)
            self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")

    def test_upstream_dirty_lock_is_ignored_and_committed_lock_conflicts_fail(self):
        path = self.repo / "deploy/upstream/stable-diffusion/requirements-overlays.lock"
        prefix, _ = path.read_text().rsplit("sha256:", 1)
        path.write_text(prefix + "sha256:" + "0" * 64 + "\n")
        self.assertEqual(capture.capture(self.repo, self.destination)["status"], "passed")
        self.commit()
        with self.assertRaisesRegex(ValueError, "Conflicting upstream wheel hashes"):
            capture.capture(self.repo, self.destination)

    def test_upstream_lock_requires_reviewed_path_hash_and_supported_syntax(self):
        source = self.upstream_wheels["stable-diffusion"]
        sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        root = self.upstream_roots["stable-diffusion"]
        entries = [
            "example @ " + self.wheel.as_uri() + " --hash=sha256:" + sha256,
            "example @ file://host" + str(source) + " --hash=sha256:" + sha256,
            "example @ " + source.as_uri() + "?query=1 --hash=sha256:" + sha256,
            "example @ " + source.as_uri(),
            "example @ " + source.as_uri() + " --hash=sha256:" + sha256 + " --hash=sha256:" + sha256,
            "-r elsewhere.txt",
            str(source) + " --hash=sha256:" + sha256,
        ]
        for entry in entries:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                list(capture.local_wheels(entry.encode(), root))

    def test_upstream_symlink_source_parent_or_backup_directory_fails(self):
        source = self.upstream_wheels["comfyui"]
        actual = source.parent.with_name("actual")
        source.parent.rename(actual)
        source.parent.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, "canonical root"):
            capture.capture(self.repo, self.destination)
        source.parent.unlink()
        actual.rename(source.parent)
        wheelhouse = self.destination / "upstream/comfyui/wheelhouse"
        wheelhouse.mkdir(parents=True, exist_ok=True)
        (wheelhouse / "mode-preserved").symlink_to(source.parent)
        with self.assertRaisesRegex(ValueError, "real backup directory"):
            capture.capture(self.repo, self.destination)


class ManagedPythonRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.destination = self.root / "backup"
        manifest = b'{"schema":1,"entries":{}}\n'
        archive = b"inert accepted archive fixture; never extracted"
        receipt = {"schema": 1, "status": "passed", "build": "20240224",
                   "source_prefix": "/srv/farm/.uv/python/cpython-3.9.18-linux-x86_64-gnu",
                   "before_after_source_match": True, "archive_verified": True,
                   "manifest": {"filename": "source-manifest.json", "sha256": hashlib.sha256(manifest).hexdigest()},
                   "archive": {"filename": "python-3.9.18-build-20240224.tar.gz", "sha256": hashlib.sha256(archive).hexdigest(), "bytes": len(archive)}}
        self.payloads = {"source-manifest.json": manifest,
                         "python-3.9.18-build-20240224.tar.gz": archive,
                         "receipt.json": (json.dumps(receipt) + "\n").encode()}
        for name, payload in self.payloads.items():
            (self.bundle / name).write_bytes(payload)
            (self.bundle / name).chmod(0o644)
        for name, value in (("MANAGED_PYTHON_BUNDLE", self.bundle),
                            ("MANAGED_PYTHON_RECEIPT_SHA256", hashlib.sha256(self.payloads["receipt.json"]).hexdigest())):
            patcher = mock.patch.object(capture, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_copy_and_reuse_independent_verified_private_files(self):
        first = capture.capture_managed_python(self.destination)
        self.assertEqual(first["copied_files"], 3)
        self.assertEqual(first["status"], "passed")
        inodes = {}
        for name, payload in self.payloads.items():
            source, target = self.bundle / name, self.destination / name
            self.assertEqual(target.read_bytes(), payload)
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
            self.assertEqual(target.stat().st_nlink, 1)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(source.stat().st_mode & 0o777, 0o644)
            inodes[name] = target.stat().st_ino
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        repeated = capture.capture_managed_python(self.destination)
        self.assertEqual(repeated["copied_files"], 0)
        self.assertEqual(repeated["reused_files"], 3)
        self.assertEqual(inodes, {name: (self.destination / name).stat().st_ino for name in self.payloads})

    def test_hardlinked_destination_is_replaced_without_changing_source(self):
        self.destination.mkdir()
        for name in self.payloads:
            os.link(self.bundle / name, self.destination / name)
        result = capture.capture_managed_python(self.destination)
        self.assertEqual(result["copied_files"], 3)
        for name in self.payloads:
            self.assertEqual((self.bundle / name).stat().st_mode & 0o777, 0o644)
            self.assertNotEqual((self.bundle / name).stat().st_ino, (self.destination / name).stat().st_ino)

    def test_corrupted_or_missing_artifact_invalidates_previous_success(self):
        for name, payload in self.payloads.items():
            for missing in (False, True):
                with self.subTest(name=name, missing=missing):
                    capture.capture_managed_python(self.destination)
                    source = self.bundle / name
                    if missing:
                        source.unlink()
                    else:
                        source.write_bytes(b"corrupted retained artifact")
                    with self.assertRaises((OSError, ValueError)):
                        capture.capture_managed_python(self.destination)
                    self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")
                    source.write_bytes(payload)

    def test_symlink_bundle_or_artifact_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.bundle)
        with mock.patch.object(capture, "MANAGED_PYTHON_BUNDLE", alias):
            with self.assertRaisesRegex(ValueError, "canonical"):
                capture.capture_managed_python(self.destination)
        for name, payload in self.payloads.items():
            source = self.bundle / name
            other = self.root / "outside"
            other.write_bytes(payload)
            source.unlink()
            source.symlink_to(other)
            with self.subTest(name=name), self.assertRaises(OSError):
                capture.capture_managed_python(self.destination)
            source.unlink()
            source.write_bytes(payload)


class PrivateEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source" / "service.env"
        self.source.parent.mkdir()
        self.data = b"FARM_SD_API_AUTH=inert-fixture-only\n"
        self.source.write_bytes(self.data)
        self.source.chmod(0o600)
        self.destination = self.root / "backup/private/stable-diffusion"
        patcher = mock.patch.object(capture, "SD_ENV_PATH", self.source)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_private_copy_is_independent_0600_and_does_not_disclose_contents(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = capture.capture_sd_environment(self.destination)
        target = self.destination / "service.env"
        self.assertEqual(result["status"], "passed")
        self.assertEqual(target.read_bytes(), self.data)
        self.assertNotEqual(target.stat().st_ino, self.source.stat().st_ino)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(target.stat().st_uid, os.geteuid())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.destination.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(self.data.decode().strip(), json.dumps(result))
        self.assertNotIn("FARM_SD_API_AUTH", (self.destination / "capture.json").read_text())

    def test_private_existing_hardlink_replaced_without_source_changes(self):
        self.destination.mkdir(parents=True)
        target = self.destination / "service.env"
        os.link(self.source, target)
        old = self.source.stat()
        capture.capture_sd_environment(self.destination)
        self.assertNotEqual(target.stat().st_ino, old.st_ino)
        self.assertEqual(self.source.stat().st_mode, old.st_mode)
        self.assertEqual(self.source.stat().st_uid, old.st_uid)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_private_missing_insecure_or_nonregular_source_leaves_incomplete(self):
        self.source.unlink()
        for kind in ("missing", "insecure", "fifo", "empty", "oversized"):
            if kind == "insecure":
                self.source.write_bytes(self.data)
                self.source.chmod(0o644)
            elif kind == "fifo":
                os.mkfifo(self.source, 0o600)
            elif kind in ("empty", "oversized"):
                self.source.write_bytes(b"" if kind == "empty" else b"x" * 65537)
                self.source.chmod(0o600)
            with self.subTest(kind=kind), self.assertRaises((OSError, ValueError)):
                capture.capture_sd_environment(self.destination)
            self.assertEqual(json.loads((self.destination / "capture.json").read_text())["status"], "incomplete")
            self.assertFalse((self.destination / "service.env").exists())
            if self.source.exists():
                self.source.unlink()

    def test_private_symlink_source_or_directory_rejected(self):
        actual = self.source.with_name("actual.env")
        self.source.rename(actual)
        self.source.symlink_to(actual)
        with self.assertRaisesRegex(ValueError, "canonical regular"):
            capture.capture_sd_environment(self.destination)
        self.source.unlink()
        actual.rename(self.source)
        actual_directory = self.source.parent.with_name("actual-directory")
        self.source.parent.rename(actual_directory)
        self.source.parent.symlink_to(actual_directory)
        with self.assertRaisesRegex(ValueError, "canonical regular"):
            capture.capture_sd_environment(self.destination)
        self.source.parent.unlink()
        actual_directory.rename(self.source.parent)
        self.destination.joinpath("capture.json").unlink()
        self.destination.rmdir()
        self.destination.symlink_to(self.source.parent)
        with self.assertRaisesRegex(ValueError, "real backup directory"):
            capture.capture_sd_environment(self.destination)
        self.assertFalse(self.source.parent.joinpath("capture.json").exists())


if __name__ == '__main__':
    unittest.main()
