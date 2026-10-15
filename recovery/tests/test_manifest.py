"""Recovery metadata tests use isolated repositories; never inspect host secrets."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("recovery_manifest", Path(__file__).parents[1] / "manifest.py")
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="farm-manifest-test-")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "source"
        self.root.mkdir()

    def collect(self, **overrides):
        policy = {"source_roots": [str(self.root)], "manifest_roots": [str(self.root)],
                  "collect_host_metadata": False, "scan_max_depth": 12, "scan_max_entries": 10000}
        policy.update(overrides)
        return manifest.collect_manifest(policy)

    def git(self, path, *args):
        return subprocess.run(["/usr/bin/git", "-c", "user.name=Recovery fixture",
                               "-c", "user.email=fixture@example.invalid", "-C", str(path), *args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                              env={**manifest.ENV, "GIT_ALLOW_PROTOCOL": "file"}).stdout

    def repository(self):
        self.git(self.root, "init", "--initial-branch=main")
        (self.root / "tracked.txt").write_text("original\n")
        (self.root / "deleted.txt").write_text("original\n")
        (self.root / ".gitignore").write_text("ignored.bin\n")
        (self.root / ".gitattributes").write_text("tracked.txt filter=fixture\n")
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-m", "fixture")

    def test_symlinks_are_mapped_without_traversal_and_targets_need_coverage(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret-child").mkdir()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        (self.root / "broken").symlink_to(self.base / "missing")
        (self.root / "virtual").symlink_to("/proc", target_is_directory=True)
        result = self.collect()
        entries = {entry["path"]: entry for entry in result["topology"]["entries"]}
        self.assertEqual(entries[str(self.root / "link")]["symlink_target"], str(outside))
        self.assertIn("uid", entries[str(self.root)])
        self.assertNotIn(str(outside / "secret-child"), entries)
        self.assertEqual(result["required_roots"], [str(outside)])
        broken = next(r for r in result["external_references"] if r["owner"].endswith("/broken"))
        self.assertEqual(broken["status"], "FileNotFoundError")

    def test_explicit_exclusion_does_not_exclude_other_same_named_directories(self):
        excluded = self.root / "a" / "cache"
        included = self.root / "b" / "cache"
        (excluded / "hidden").mkdir(parents=True)
        (included / "visible").mkdir(parents=True)
        result = self.collect(scan_exclusions=[str(excluded)])
        paths = {item["path"] for item in result["topology"]["entries"]}
        self.assertNotIn(str(excluded / "hidden"), paths)
        self.assertIn(str(included / "visible"), paths)
        self.assertTrue(any(item["path"] == str(excluded) and item["reason"] == "explicit_scan_exclusion"
                            for item in result["scan_boundaries"]))

    def test_excluded_runtime_keeps_interpreter_metadata_but_not_command_values(self):
        env = self.root / "runtime"
        (env / "lib" / "package").mkdir(parents=True)
        (env / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.11.2\ncommand = uv --password SECRET\n")
        result = self.collect(scan_exclusions=[str(env)])
        runtime = result["runtime"]["recipe_files"][0]
        self.assertEqual(runtime["version"], "3.11.2")
        self.assertEqual(runtime["interpreter_home"], "/usr/bin")
        self.assertNotIn("SECRET", json.dumps(result))
        node = next(item for item in result["topology"]["entries"] if item["path"] == str(env))
        self.assertIn("lib", node["immediate_children"])
        self.assertNotIn(str(env / "lib"), {item["path"] for item in result["topology"]["entries"]})

    def test_same_device_bind_mount_is_not_traversed_implicitly(self):
        nested = self.root / "mounted"
        (nested / "payload").mkdir(parents=True)
        with mock.patch.object(manifest, "_mount_points", return_value={"/", str(nested)}):
            result = self.collect()
        paths = {item["path"] for item in result["topology"]["entries"]}
        self.assertNotIn(str(nested / "payload"), paths)
        self.assertTrue(any(item["path"] == str(nested) and item["reason"] == "mount_boundary"
                            for item in result["scan_boundaries"]))

    def test_explicit_root_can_be_rescanned_beyond_an_earlier_depth_boundary(self):
        deep = self.root / "child"
        (deep / "leaf").mkdir(parents=True)
        result = self.collect(manifest_roots=[str(self.root), str(deep)], scan_max_depth=1)
        paths = {item["path"] for item in result["topology"]["entries"]}
        self.assertIn(str(deep / "leaf"), paths)

    def test_missing_permission_and_limits_are_explicit(self):
        denied = self.root / "denied"
        denied.mkdir()
        scan = os.scandir

        def checked(path):
            if str(path) == str(denied):
                raise PermissionError("fixture")
            return scan(path)

        with mock.patch.object(manifest.os, "scandir", side_effect=checked):
            result = self.collect(manifest_roots=[str(self.root), str(self.base / "missing")])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual({e["status"] for e in result["errors"]}, {"PermissionError", "FileNotFoundError"})
        limited = self.collect(scan_max_entries=1)
        self.assertEqual(limited["status"], "incomplete")
        self.assertTrue(any(item["reason"] == "entry_limit" for item in limited["scan_boundaries"]))

    def test_missing_mount_topology_fails_closed(self):
        (self.root / "child").mkdir()
        with mock.patch.object(manifest, "_mount_points", side_effect=PermissionError("fixture")):
            result = self.collect()
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn(str(self.root / "child"), {e["path"] for e in result["topology"]["entries"]})

    def test_repository_signals_ignore_rules_and_no_project_commands_or_secret_contents(self):
        self.repository()
        marker = self.base / "executed-project-code"
        command = "/usr/bin/touch " + str(marker)
        self.git(self.root, "config", "core.fsmonitor", command)
        self.git(self.root, "config", "filter.fixture.clean", command)
        self.git(self.root, "config", "filter.fixture.process", command)
        (self.root / "tracked.txt").write_text("modified working file of a different size\n")
        (self.root / "deleted.txt").unlink()
        (self.root / "new.txt").write_text("UNTRACKED_SECRET_VALUE")
        (self.root / "ignored.bin").write_text("IGNORED_SECRET_VALUE")
        (self.root / "training.json").write_text('{"credential":"MODEL_SECRET_VALUE"}')
        result = self.collect()
        self.assertFalse(marker.exists())
        self.assertFalse(result["errors"], result["errors"])
        checkout = result["git"]["checkouts"][0]
        signals = {s["path"]: s["signal"] for s in checkout["tracked_metadata_signals"]}
        self.assertEqual(signals["tracked.txt"], "stat_differs_from_index")
        self.assertEqual(signals["deleted.txt"], "FileNotFoundError")
        self.assertIn("new.txt", checkout["untracked_groups"])
        self.assertIn("ignored.bin", checkout["immediate_ignored_entries"])
        text = json.dumps(result)
        for secret in ["UNTRACKED_SECRET_VALUE", "IGNORED_SECRET_VALUE", "MODEL_SECRET_VALUE"]:
            self.assertNotIn(secret, text)
        self.assertEqual(result["models"]["reference_files"][0]["qualification"], "unverified_preserve_payload")

    def test_empty_git_markers_are_recorded_without_becoming_repository_errors(self):
        (self.root / ".git").mkdir()
        cached = self.root / "cache"
        cached.mkdir()
        (cached / ".git").touch()
        result = self.collect(mode="fixture", fixture_root=str(self.root))
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual(result["git"]["repositories"], [])
        self.assertEqual({item["path"] for item in result["git"]["discovery_notes"]},
                         {str(self.root), str(cached)})
        self.assertTrue(all(item["kind"] == "empty_git_marker" for item in result["git"]["discovery_notes"]))

    def test_nonempty_broken_git_marker_remains_a_hard_error(self):
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("[core]\nrepositoryformatversion = 0\n")
        result = self.collect()
        self.assertTrue(any(item["operation"] == "git_checkout_identity" for item in result["errors"]))

    def test_initialized_and_uninitialized_submodules_do_not_scan_git_object_stores(self):
        self.repository()
        upstream = self.base / "upstream"
        upstream.mkdir()
        self.git(upstream, "init", "--initial-branch=main")
        (upstream / "payload.txt").write_text("source payload\n")
        self.git(upstream, "add", ".")
        self.git(upstream, "commit", "-m", "upstream fixture")
        self.git(self.root, "submodule", "add", str(upstream), "nested")
        upstream_head = self.git(upstream, "rev-parse", "HEAD").decode().strip()
        self.git(self.root, "update-index", "--add", "--cacheinfo", "160000," + upstream_head + ",missing")
        (self.root / "missing").mkdir()
        result = self.collect()
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual({item["path"] for item in result["git"]["checkouts"]},
                         {str(self.root), str(self.root / "nested")})
        checkout = next(item for item in result["git"]["checkouts"] if item["path"] == str(self.root))
        missing = next(item for item in checkout["submodule_gitlinks"] if item["path"] == "missing")
        self.assertEqual(missing["checkout_status"], "uninitialized_or_missing_git_marker")
        store = str(self.root / ".git" / "modules" / "nested")
        self.assertNotIn(store, {item["path"] for item in result["topology"]["roots"]})
        stores = [item for item in result["git"]["registered_worktrees"] if item["worktree"] == store]

        self.assertTrue(all(item["entry_kind"] == "git_metadata_directory" for item in stores))

    def test_worktree_common_store_and_object_alternates_outside_source(self):
        self.repository()
        worktree = self.base / "outside worktree"
        self.git(self.root, "worktree", "add", "-b", "working", str(worktree))
        pool = self.base / "object-pool"
        pool.mkdir()
        (self.root / ".git" / "objects" / "info" / "alternates").write_text(str(pool) + "\n")
        (worktree / "tracked.txt").write_text("uncommitted worktree work\n")
        result = self.collect()
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual(len(result["git"]["repositories"]), 1)
        self.assertEqual(len(result["git"]["checkouts"]), 2)
        self.assertIn(str(worktree), result["required_roots"])
        self.assertIn(str(pool), result["required_roots"])
        self.assertIn(str(worktree), {x["path"] for x in result["topology"]["roots"]})

    def test_fixture_mode_does_not_inspect_outside_registered_worktrees(self):
        self.repository()
        outside = self.base / "outside"
        self.git(self.root, "worktree", "add", "-b", "outside", str(outside))
        result = self.collect(mode="fixture", fixture_root=str(self.root))
        self.assertEqual(len(result["git"]["checkouts"]), 1)
        self.assertNotIn(str(outside), {item["path"] for item in result["topology"]["roots"]})
        self.assertTrue(any(e["status"] == "outside_fixture_not_inspected" for e in result["errors"]))

    def test_fixture_gitdir_escape_is_rejected_before_git_runs(self):
        self.repository()
        fixture = self.base / "fixture"
        fixture.mkdir()
        (fixture / ".git").write_text("gitdir: " + str(self.root / ".git") + "\n")
        with mock.patch.object(manifest, "_run", wraps=manifest._run) as runner:
            result = self.collect(mode="fixture", fixture_root=str(fixture),
                                  source_roots=[str(fixture)], manifest_roots=[str(fixture)])
        runner.assert_not_called()
        self.assertTrue(any(e["operation"] == "fixture_git_boundary" for e in result["errors"]))

    def test_fixture_mode_never_queries_host_package_or_service_tools(self):
        with mock.patch.object(manifest, "_run", wraps=manifest._run) as runner:
            result = self.collect(mode="fixture", fixture_root=str(self.root), collect_host_metadata=True)
        runner.assert_not_called()
        self.assertEqual(result["host"]["status"], "not_requested")

    def test_host_identity_storage_and_service_metadata_omit_private_fields(self):
        contents = {"/etc/passwd": "worker:PRIVATE_PASSWORD:1200:1300:PRIVATE_GECOS:/home/worker:/bin/sh\n",
                    "/etc/group": "workers:PRIVATE_GROUP_PASSWORD:1300:worker,helper\n"}
        commands = []
        def execute(argv, timeout):
            commands.append(argv)
            output = (b"worker.service loaded active running PRIVATE_DESCRIPTION\n"
                      if "list-units" in argv else b"{}\n")
            return {"status": "ok", "returncode": 0, "output": output}
        collector = manifest._Collector({"source_roots": [str(self.root)]}, None)
        with mock.patch.object(manifest, "_small_regular", side_effect=lambda p, maximum: contents[p]), \
                mock.patch.object(manifest, "_run", side_effect=execute):
            host = collector.host()
        self.assertEqual(host["accounts"]["entries"],
                         [{"name": "worker", "uid": 1200, "gid": 1300, "home": "/home/worker", "shell": "/bin/sh"}])
        self.assertEqual(host["groups"]["entries"], [{"name": "workers", "gid": 1300, "members": ["worker", "helper"]}])
        self.assertEqual(host["current_service_states"]["entries"],
                         [{"unit": "worker.service", "load": "loaded", "active": "active", "sub": "running"}])
        self.assertNotIn("PRIVATE", json.dumps(host))
        self.assertTrue(any(a[0] == "/usr/bin/lsblk" and a[-1] == "NAME,TYPE,SIZE,FSTYPE,UUID,MOUNTPOINTS" for a in commands))
        self.assertTrue(any(a[0] == "/usr/bin/findmnt" and a[-1] == "TARGET,SOURCE,FSTYPE,UUID" for a in commands))
        self.assertFalse(collector.result["errors"])

    def test_identity_permission_and_malformed_records_remain_errors(self):
        collector = manifest._Collector({"source_roots": [str(self.root)]}, None)
        def metadata(path, maximum):
            if path == "/etc/passwd":
                raise PermissionError("PRIVATE_ERROR_TEXT")
            return "invalid:PRIVATE_PASSWORD:bad-id:member\n"
        with mock.patch.object(manifest, "_small_regular", side_effect=metadata), \
                mock.patch.object(manifest, "_run", return_value={"status": "ok", "returncode": 0, "output": b""}):
            host = collector.host()
        self.assertEqual(host["accounts"]["status"], "PermissionError")
        self.assertEqual(host["groups"]["status"], "ValueError")
        self.assertEqual(len(collector.result["errors"]), 2)
        self.assertNotIn("PRIVATE", json.dumps(host))

    def test_sanitized_remotes_never_return_authentication_fields(self):
        self.repository()
        self.git(self.root, "remote", "add", "origin", "https://person:SECRET@example.invalid/team/repo.git?token=SECRET#fragment")
        result = self.collect()
        remote = result["git"]["repositories"][0]["remotes"][0]["urls"][0]
        self.assertEqual(remote, "https://example.invalid/team/repo.git")
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(manifest.sanitize_remote("git@example.invalid:team/repo.git?secret=yes"), "example.invalid:team/repo.git")
        self.assertEqual(manifest.sanitize_remote("ssh://user@[::1]:22/team/repo"), "ssh://[::1]:22/team/repo")
        self.assertNotIn("SECRET", manifest.sanitize_remote("ext::helper --password SECRET"))

    def test_invalid_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            self.collect(scan_exclusions=["relative"])
        with self.assertRaises(ValueError):
            self.collect(scan_max_entries=True)


class CommandTests(unittest.TestCase):
    def test_output_limit_and_timeout_are_not_success(self):
        result = manifest._run([sys.executable, "-c", "print('x'*10000)"], max_bytes=128)
        self.assertEqual(result["status"], "output_limit")
        self.assertEqual(len(result["output"]), 128)
        start = time.monotonic()
        result = manifest._run([sys.executable, "-c", "import os,time;os.close(1);time.sleep(10)"], timeout=0.1)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, 2)

    def test_stderr_is_not_captured_and_input_is_streamed_without_deadlock(self):
        result = manifest._run([sys.executable, "-c", "import sys;sys.stderr.write('CREDENTIAL');sys.exit(2)"])
        self.assertEqual(result["status"], "error")
        self.assertNotIn("CREDENTIAL", str(result))
        payload = b"a" * 200000
        result = manifest._run([sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"], input_bytes=payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output"], payload)


if __name__ == "__main__":
    unittest.main()
