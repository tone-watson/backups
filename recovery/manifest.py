"""Refresh recovery metadata without copying payloads or executing project code.

The collector deliberately does not establish backup or download coverage. Scan
limits are reported, not converted into backup exclusions. Git working-file
signals use index stat metadata, never content filters or a working-tree refresh.
"""

from __future__ import annotations

import ast
import datetime
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import urllib.parse


VIRTUAL_ROOTS = ("/proc", "/sys", "/dev", "/run")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "TZ": "UTC",
       "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
       "GIT_PAGER": "cat", "SYSTEMD_PAGER": "cat", "SYSTEMD_COLORS": "0"}
RUNTIME_NAMES = {"uv.lock", "pyproject.toml", "requirements.txt", "requirements.lock",
                 "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Pipfile.lock",
                 "environment.yml", "environment.yaml", ".python-version", "pyvenv.cfg"}
MODEL_NAMES = {"training.json", "model_index.json", "adapter_config.json"}
SAFE_UNIT_PROPERTIES = ("Id,LoadState,ActiveState,SubState,UnitFileState,Result,"
                        "User,Group,WorkingDirectory,FragmentPath,DropInPaths")


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _paths(values, name):
    if not isinstance(values, list) or any(not isinstance(v, str) or not os.path.isabs(v)
                                           or "\0" in v for v in values):
        raise ValueError(name + " must be a list of absolute paths")
    return list(dict.fromkeys(os.path.normpath(v) for v in values))


def sanitize_remote(value):
    """Keep remote identity without URL userinfo, query strings or fragments."""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", value):


        return "[remote helper specification redacted]"
    if "://" in value:
        try:
            parts = urllib.parse.urlsplit(value)
            if parts.scheme not in {"http", "https", "ssh", "git", "file", "ftp", "ftps"}:
                return "[unsupported remote transport redacted]"
            host = parts.hostname or ""
            if ":" in host:
                host = "[" + host + "]"
            if parts.port:
                host += ":" + str(parts.port)
            return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
        except ValueError:
            return "[invalid remote URL redacted]"
    return value.split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1]


def _run(argv, timeout=15, max_bytes=4 * 1024 * 1024, allowed=(0,), input_bytes=None):
    """Bound command output/time; never retain stderr that may contain secrets."""
    start = time.monotonic()
    output = bytearray()
    process = None
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                   env=ENV)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            pending_input = memoryview(input_bytes or b"")
            if process.stdin:
                if pending_input:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
            status = None
            while selector.get_map():
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    status = "timeout"
                    process.kill()
                    break
                for key, _ in selector.select(min(remaining, 0.2)):
                    if key.data == "stdin":
                        try:
                            count = os.write(key.fd, pending_input[:65536])
                            pending_input = pending_input[count:]
                        except BrokenPipeError:
                            pending_input = memoryview(b"")
                        if not pending_input:
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, min(65536, max_bytes + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > max_bytes:
                            status = "output_limit"
                            process.kill()
                            break
                if status:
                    break
            try:
                code = process.wait(timeout=max(0.01, timeout - (time.monotonic() - start)))
            except subprocess.TimeoutExpired:
                status = "timeout"
                process.kill()
                code = process.wait()
        return {"status": status or ("ok" if code in allowed else "error"),
                "returncode": code, "output": bytes(output[:max_bytes])}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": type(exc).__name__, "output": b""}
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout:
                process.stdout.close()
            if process.stdin:
                process.stdin.close()


def _metadata(path):
    result = {"path": path}
    try:
        value = os.lstat(path)
        result.update(status="observed", uid=value.st_uid, gid=value.st_gid,
                      mode=stat.filemode(value.st_mode), mode_bits=stat.S_IMODE(value.st_mode),
                      size_bytes=value.st_size, allocated_bytes=value.st_blocks * 512,
                      device=value.st_dev, inode=value.st_ino, mtime_ns=value.st_mtime_ns)
        if stat.S_ISLNK(value.st_mode):
            result["symlink_target"] = os.readlink(path)
    except OSError as exc:
        result["status"] = type(exc).__name__
    return result


def _small_regular(path, maximum=65536):
    """Read only explicitly selected bounded metadata, without following a link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("not a bounded regular metadata file")
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise ValueError("metadata size limit")
        return data.decode("utf-8", "replace")
    finally:
        os.close(fd)


def _mount_points():

    data = _small_regular("/proc/self/mountinfo", 1024 * 1024)
    decode = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), s)
    return {os.path.normpath(decode(line.split()[4])) for line in data.splitlines()
            if len(line.split()) >= 6}


class _Collector:
    def __init__(self, policy, progress):
        self.policy = policy
        self.progress = progress
        self.sources = _paths(policy.get("source_roots", []), "source_roots")
        if not self.sources:
            raise ValueError("at least one source root is required")
        self.roots = _paths(policy.get("manifest_roots", self.sources), "manifest_roots")
        self.exclusions = _paths(policy.get("scan_exclusions", []), "scan_exclusions")
        self.fixture = None
        if policy.get("mode") == "fixture":
            self.fixture = _paths([policy.get("fixture_root")], "fixture_root")[0]
            if any(not _inside(os.path.realpath(path), self.fixture) for path in self.roots + self.sources):
                raise ValueError("fixture roots must remain inside fixture_root")
        self.depth = self._limit("scan_max_depth", 8, 0, 64)
        self.limit = self._limit("scan_max_entries", 100000, 1, 2000000)
        self.timeout = self._limit("command_timeout_seconds", 15, 1, 120)
        self.result = {"schema": "farm.recovery-manifest/1", "started_at": _now(),
                       "source_roots": self.sources, "manifest_roots": self.roots,
                       "status": "complete", "errors": [], "scan_boundaries": [],
                       "topology": {"entries": [], "roots": []},
                       "git": {"repositories": [], "checkouts": [], "registered_worktrees": [],
                               "discovery_notes": []},
                       "external_references": [], "required_roots": [],
                       "runtime": {"recipe_files": []}, "models": {"reference_files": []},
                       "limits": ["Metadata inventory, not source payload or proof of recovery coverage.",
                                  "Live observation; no filesystem snapshot or payload hash.",
                                  "Git change signals are stat-based, not a clean/content verification.",
                                  "Remote URLs/refs are local metadata; no network verification.",
                                  "Model reference files are identified, not trusted download recipes.",
                                  "Scan exclusions and bounds never authorize backup exclusions."]}
        self.visited = {}
        self.candidates = set()
        self.common = set()
        self.checkouts = set()
        self.reference_keys = set()
        self.count = 0
        try:
            self.mounts = _mount_points()
        except (OSError, ValueError) as exc:
            self.mounts = set()
            self.error("/proc/self/mountinfo", "mount_topology", type(exc).__name__)
        self.result["mount_points"] = sorted(self.mounts)

    def _limit(self, name, default, minimum, maximum):
        value = self.policy.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(name + " is outside its permitted integer range")
        return value

    def error(self, path, operation, status):
        self.result["errors"].append({"path": path, "operation": operation, "status": status})
        self.result["status"] = "incomplete"

    def boundary(self, path, reason, incomplete=False):
        self.result["scan_boundaries"].append({"path": path, "reason": reason,
                                                "incomplete_scope": incomplete})
        if incomplete:
            self.result["status"] = "incomplete"

    def covered(self, path):
        if any(_inside(path, root) for root in VIRTUAL_ROOTS):
            return False
        for source in self.sources:
            if _inside(path, source) and not any(_inside(mount, source) and mount != source
                                                and _inside(path, mount) for mount in self.mounts):
                return True
        return False

    def reference(self, owner, path, kind):
        path = os.path.normpath(path)
        key = (owner, path, kind)
        if key in self.reference_keys:
            return
        self.reference_keys.add(key)
        resolved = os.path.realpath(path)
        if self.fixture and not _inside(resolved, self.fixture):
            self.result["external_references"].append({"owner": owner, "path": path,
                "resolved_path": resolved, "kind": kind, "status": "outside_fixture_not_inspected",
                "covered_by_source_roots": False})
            self.error(path, "fixture_reference_boundary", "outside_fixture_not_inspected")
            return
        target = _metadata(resolved)
        virtual = any(_inside(resolved, root) for root in VIRTUAL_ROOTS)
        item = {"owner": owner, "path": path, "resolved_path": resolved, "kind": kind,
                "status": target["status"], "covered_by_source_roots": self.covered(resolved),
                "virtual_filesystem": virtual}
        self.result["external_references"].append(item)
        if target["status"] not in {"observed", "FileNotFoundError"}:
            self.error(resolved, "reference_metadata", target["status"])
        elif target["status"] == "FileNotFoundError" and kind == "git_object_alternate":
            self.error(resolved, "git_object_alternate", "FileNotFoundError")
        if target["status"] == "observed" and not item["covered_by_source_roots"] and not virtual:
            self.result["required_roots"].append(resolved)

    def scan(self, root):
        if self.fixture and not _inside(os.path.realpath(root), self.fixture):
            self.error(root, "fixture_scan_boundary", "outside_fixture_not_inspected")
            return
        root_meta = _metadata(root)
        self.result["topology"]["roots"].append(root_meta)
        if root_meta["status"] != "observed":
            self.error(root, "source_root_metadata", root_meta["status"])
            return
        pending = [(root, 0)]
        while pending:
            path, depth = pending.pop()
            budget = self.depth - depth
            if self.visited.get(path, -1) >= budget:
                continue
            if self.count >= self.limit:
                self.boundary(path, "entry_limit", True)
                for remaining, _ in pending:
                    self.boundary(remaining, "entry_limit", True)
                return
            self.visited[path] = budget
            meta = _metadata(path)
            self.result["topology"]["entries"].append(meta)
            self.count += 1
            if meta["status"] != "observed":
                self.error(path, "directory_metadata", meta["status"])
                continue
            if "symlink_target" in meta:
                self.reference(path, os.path.join(os.path.dirname(path), meta["symlink_target"]), "symlink")
                continue
            if not meta["mode"].startswith("d"):
                continue
            if path != root and (path in self.mounts or meta["device"] != root_meta["device"]):
                self.boundary(path, "mount_boundary")
                continue
            if not self.mounts:
                self.boundary(path, "mount_topology_unavailable", True)
                continue
            if any(_inside(path, excluded) for excluded in (*VIRTUAL_ROOTS, *self.exclusions)):
                self.boundary(path, "explicit_scan_exclusion")
                if not any(_inside(path, excluded) for excluded in VIRTUAL_ROOTS):
                    self.excluded_directory_metadata(path, meta)
                continue
            if os.path.lexists(os.path.join(path, ".git")):
                self.candidates.add(path)
            if depth >= self.depth:
                self.boundary(path, "depth_limit", True)
                continue
            try:
                with os.scandir(path) as children:
                    for child in children:
                        self.count += 1
                        if self.count > self.limit:
                            self.boundary(path, "entry_limit", True)
                            break
                        child_path = os.path.join(path, child.name)
                        if child.name == ".git":
                            self.candidates.add(path)
                            self.boundary(child_path, "git_metadata_collected_separately")
                            continue
                        if child.is_dir(follow_symlinks=False) or child.is_symlink():
                            pending.append((child_path, depth + 1))
                        elif child.name in RUNTIME_NAMES:
                            self.runtime_reference(child_path)
                        elif child.name in MODEL_NAMES:
                            self.model_reference(child_path)
            except OSError as exc:
                self.error(path, "list_directory", type(exc).__name__)
            if self.progress and self.count % 1000 < 20:
                self.progress({"phase": "manifest_scan", "entries": self.count, "path": path})

    def excluded_directory_metadata(self, path, meta):
        """Map the explicit boundary and current children, never package internals."""
        children = []
        complete = True
        try:
            with os.scandir(path) as scan:
                for child in scan:
                    if len(children) == 128:
                        complete = False
                        break
                    children.append(child.name)


            for name in ("pyvenv.cfg", ".python-version"):
                target = os.path.join(path, name)
                if os.path.lexists(target):
                    self.runtime_reference(target)
        except OSError as exc:
            self.error(path, "explicit_boundary_metadata", type(exc).__name__)
            complete = False
        meta["immediate_children"] = sorted(children)
        meta["immediate_children_complete"] = complete

    def runtime_reference(self, path):
        item = _metadata(path)
        name = os.path.basename(path)
        if name in {".python-version", "pyvenv.cfg"} and item.get("mode", "").startswith("-"):
            try:
                contents = _small_regular(path, 8192)
                if name == ".python-version":
                    version = contents.strip()
                    if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
                        item["python_version"] = version
                    else:
                        item["version_status"] = "unsupported_version_format"
                else:
                    for line in contents.splitlines():
                        key, separator, value = line.partition("=")
                        if not separator:
                            continue
                        key, value = key.strip(), value.strip()
                        if key in {"version", "version_info"} and re.fullmatch(r"[0-9.A-Za-z-]{1,80}", value):
                            item[key] = value
                        elif key == "home" and os.path.isabs(value):
                            item["interpreter_home"] = value
                        elif key == "include-system-site-packages" and value in {"true", "false"}:
                            item[key] = value
            except (OSError, ValueError) as exc:
                self.error(path, "runtime_marker", type(exc).__name__)
        item["reconstruction_verified"] = False
        self.result["runtime"]["recipe_files"].append(item)

    def model_reference(self, path):
        if self.fixture and not _inside(os.path.realpath(path), self.fixture):
            self.error(path, "fixture_model_boundary", "outside_fixture_not_inspected")
            return
        item = _metadata(path)
        item.update(qualification="unverified_preserve_payload", contents_read=False)
        self.result["models"]["reference_files"].append(item)
        if item["status"] != "observed":
            self.error(path, "model_reference_metadata", item["status"])

    def git(self, path, *args, allowed=(0,), required=True, input_bytes=None):
        argv = ["/usr/bin/git", "-c", "safe.directory=" + path,
                "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                "-c", "core.quotePath=false", "-c", "submodule.recurse=false", "-C", path, *args]
        result = _run(argv, self.timeout, allowed=allowed, input_bytes=input_bytes)
        if result["status"] != "ok":
            if required:
                self.error(path, "git_" + args[0], result["status"])
            return None
        return result["output"]

    def git_text(self, path, *args, **kwargs):
        raw = self.git(path, *args, **kwargs)
        return raw.decode("utf-8", "replace").strip() if raw is not None else None

    def inspect_git(self, path):
        if path in self.checkouts:
            return
        self.checkouts.add(path)
        marker = os.path.join(path, ".git")
        marker_meta = _metadata(marker)
        marker_kind = None
        if marker_meta["status"] == "FileNotFoundError":


            marker_kind = "no_checkout_marker"
        elif marker_meta["status"] != "observed":
            self.error(marker, "git_marker_metadata", marker_meta["status"])
            return
        elif marker_meta["mode"].startswith("-") and marker_meta["size_bytes"] == 0:

            marker_kind = "empty_git_marker"
        elif marker_meta["mode"].startswith("d"):
            try:
                with os.scandir(marker) as children:
                    if next(children, None) is None:
                        marker_kind = "empty_git_marker"
            except OSError as exc:
                self.error(marker, "git_marker_metadata", type(exc).__name__)
                return
        if marker_kind:
            self.result["git"]["discovery_notes"].append({"path": path, "kind": marker_kind,
                                                        "marker_metadata": marker_meta,
                                                        "payload_disposition": "preserve"})
            return
        if self.fixture and not self.fixture_git_storage(path):
            return
        top = self.git_text(path, "rev-parse", "--show-toplevel")
        if top != os.path.realpath(path):
            self.error(path, "git_checkout_identity", "unavailable_or_mismatch")
            return
        common = self.git_text(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not common:
            return
        common = os.path.realpath(common)
        index = self.git(path, "ls-files", "--stage", "-z")
        entries = []
        gitlinks = []
        if index is not None:
            for entry in index.split(b"\0"):
                if not entry:
                    continue
                try:
                    header, name = entry.split(b"\t", 1)
                    mode, oid, stage = header.decode("ascii").split()
                    name = os.fsdecode(name)
                except (ValueError, UnicodeError):
                    self.error(path, "git_index", "invalid_output")
                    continue
                item = {"path": name, "mode": mode, "object": oid, "stage": int(stage)}
                if mode == "160000":
                    nested = os.path.join(path, name)
                    item["checkout_status"] = ("git_marker_present" if os.path.lexists(os.path.join(nested, ".git"))
                                               else "uninitialized_or_missing_git_marker")
                    gitlinks.append(item)
                    if os.path.isdir(nested) and item["checkout_status"] == "git_marker_present":
                        self.candidates.add(nested)
                entries.append(item)


        debug = self.git(path, "ls-files", "--debug", "-z")
        changes = []
        if debug is not None:
            remaining = debug
            pattern = re.compile(rb"  ctime: (\d+):(\d+)\n  mtime: (\d+):(\d+)\n"
                                 rb"  dev: (\d+)\tino: (\d+)\n  uid: (\d+)\tgid: (\d+)\n"
                                 rb"  size: (\d+)\tflags: ([0-9a-fA-F]+)\n")
            while remaining:
                name, separator, rest = remaining.partition(b"\0")
                match = pattern.match(rest)
                if not separator or not match:
                    self.error(path, "git_index_stat", "unsupported_debug_output")
                    break
                remaining = rest[match.end():]
                filename = os.path.join(path, os.fsdecode(name))
                try:
                    value = os.lstat(filename)
                    if not stat.S_ISDIR(value.st_mode) and (
                        value.st_size != int(match[9]) or
                        value.st_mtime_ns != int(match[3]) * 10**9 + int(match[4]) or
                        value.st_ctime_ns != int(match[1]) * 10**9 + int(match[2])
                    ):
                        changes.append({"path": os.fsdecode(name), "signal": "stat_differs_from_index"})
                except OSError as exc:
                    changes.append({"path": os.fsdecode(name), "signal": type(exc).__name__})
        others = self.git(path, "ls-files", "--others", "--exclude-standard", "--directory", "-z")
        ignored = []
        try:
            with os.scandir(path) as scan:
                children = []
                for child in scan:
                    if len(children) >= 1000:
                        self.boundary(path, "immediate_ignore_probe_limit", True)
                        break
                    if child.name != ".git":
                        children.append(child.name + ("/" if child.is_dir(follow_symlinks=False) else ""))
            if children:
                raw = self.git(path, "check-ignore", "-z", "--stdin", allowed=(0, 1),
                               input_bytes=b"\0".join(os.fsencode(name) for name in children) + b"\0")
                if raw is not None:
                    ignored = [os.fsdecode(item) for item in raw.split(b"\0") if item]
        except OSError as exc:
            self.error(path, "git_ignore_probe", type(exc).__name__)
        staged = self.git(path, "diff-index", "--cached", "--raw", "-z", "--no-ext-diff",
                          "--ignore-submodules=all", "HEAD", required=False)
        checkout = {"path": path, "common_git_dir": common,
                    "head": self.git_text(path, "rev-parse", "HEAD", required=False),
                    "branch": self.git_text(path, "symbolic-ref", "--quiet", "--short", "HEAD", required=False),
                    "index_entries": entries, "submodule_gitlinks": gitlinks,
                    "tracked_metadata_signals": changes,
                    "staged_diff_raw": staged.decode("utf-8", "replace") if staged is not None else None,
                    "staged_diff_status": "observed" if staged is not None else "unavailable_or_unborn_head",
                    "untracked_groups": [os.fsdecode(item) for item in (others or b"").split(b"\0") if item],
                    "immediate_ignored_entries": ignored,
                    "content_cleanliness": "not_verified_metadata_only"}
        self.result["git"]["checkouts"].append(checkout)
        self.reference(path, common, "git_common_directory")
        if common in self.common:
            return
        self.common.add(common)
        remotes = []
        for name in (self.git_text(path, "remote") or "").splitlines():
            urls = self.git_text(path, "remote", "get-url", "--all", name)
            push = self.git_text(path, "remote", "get-url", "--push", "--all", name)
            remotes.append({"name": name, "urls": [sanitize_remote(u) for u in (urls or "").splitlines()],
                            "push_urls": [sanitize_remote(u) for u in (push or "").splitlines()]})
        raw_refs = self.git_text(path, "for-each-ref", "--format=%(refname)\t%(objectname)\t%(upstream)")
        refs = []
        for line in (raw_refs or "").splitlines():
            fields = line.split("\t")
            if len(fields) >= 2:
                refs.append({"ref": fields[0], "object": fields[1], "upstream": fields[2] if len(fields) > 2 else ""})
        repository = {"common_git_dir": common, "common_directory_metadata": _metadata(common),
                      "inspection_checkout": path,
                      "remotes": remotes, "refs": refs, "submodule_gitlinks": gitlinks,
                      "lfs_objects_metadata": _metadata(os.path.join(common, "lfs", "objects")),
                      "alternates": []}
        alternate_file = os.path.join(common, "objects", "info", "alternates")
        try:
            for line in _small_regular(alternate_file).splitlines():
                if line.startswith('"'):
                    line = json.loads(line)
                if not isinstance(line, str) or "\0" in line:
                    raise ValueError("invalid alternate path")
                target = os.path.normpath(os.path.join(common, "objects", line))
                repository["alternates"].append(target)
                self.reference(path, target, "git_object_alternate")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.error(alternate_file, "git_alternates", type(exc).__name__)
        self.result["git"]["repositories"].append(repository)
        worktrees = self.git(path, "worktree", "list", "--porcelain", "-z", required=False)
        fields = None
        if worktrees is not None:
            fields = [os.fsdecode(field) for field in worktrees.split(b"\0")]
        else:


            worktrees = self.git(path, "worktree", "list", "--porcelain")
            if worktrees is not None:
                fields = worktrees.decode("utf-8", "surrogateescape").splitlines()
        if fields is not None:
            item = {}
            for field in fields:
                if not field:
                    if item:
                        self.add_worktree(path, common, item)
                    item = {}
                    continue
                key, _, value = field.partition(" ")
                if key == "worktree" and value.startswith('"'):
                    try:
                        value = ast.literal_eval(value)
                    except (SyntaxError, ValueError):
                        self.error(path, "git_worktrees", "unsupported_quoted_path")
                        value = ""
                if key in {"worktree", "HEAD", "branch", "detached", "bare", "locked", "prunable"}:

                    item[key] = True if key in {"locked", "prunable", "detached", "bare"} else value
            if item:
                self.add_worktree(path, common, item)

    def add_worktree(self, owner, common, item):
        target = item.get("worktree")
        if not isinstance(target, str) or not os.path.isabs(target):
            self.error(owner, "git_worktrees", "invalid_path")
            return
        if self.fixture and not _inside(os.path.realpath(target), self.fixture):
            item.update(common_git_dir=common, metadata={"path": target, "status": "outside_fixture_not_inspected"})
            self.result["git"]["registered_worktrees"].append(item)
            self.reference(owner, target, "git_worktree")
            return
        item.update(common_git_dir=common, metadata=_metadata(target))



        metadata_store = item.get("bare") or os.path.realpath(target) == common
        item["entry_kind"] = "git_metadata_directory" if metadata_store else "checkout"
        self.result["git"]["registered_worktrees"].append(item)
        self.reference(owner, target, "git_common_directory" if metadata_store else "git_worktree")
        if item["metadata"]["status"] == "observed" and not metadata_store:
            self.candidates.add(target)
            self.scan(target)

    def fixture_git_storage(self, path):
        """Reject escaped gitdir/commondir pointers before running Git at all."""
        if not _inside(os.path.realpath(path), self.fixture):
            self.error(path, "fixture_git_boundary", "outside_fixture_not_inspected")
            return False
        marker = os.path.join(path, ".git")
        gitdir = os.path.realpath(marker)
        if not _inside(gitdir, self.fixture):
            self.error(marker, "fixture_git_boundary", "outside_fixture_not_inspected")
            return False
        try:
            if not os.path.isdir(marker):
                content = _small_regular(marker, 8192).strip()
                if not content.startswith("gitdir: "):
                    raise ValueError("unsupported gitdir pointer")
                gitdir = os.path.realpath(os.path.join(path, content[8:]))
            if not _inside(gitdir, self.fixture):
                self.error(marker, "fixture_git_boundary", "outside_fixture_not_inspected")
                return False
            try:
                pointer = _small_regular(os.path.join(gitdir, "commondir"), 8192).strip()
                common = os.path.realpath(os.path.join(gitdir, pointer))
            except FileNotFoundError:
                common = gitdir
            if not _inside(common, self.fixture):
                self.error(gitdir, "fixture_git_boundary", "outside_fixture_not_inspected")
                return False
        except (OSError, ValueError) as exc:
            self.error(marker, "fixture_git_storage", type(exc).__name__)
            return False
        return True

    def host(self):
        host = {"running_python": {"version": sys.version.split()[0], "executable": sys.executable},
                "kernel": dict(zip(("system", "node", "release", "version", "machine"), os.uname()))}
        for label, path, count in (("accounts", "/etc/passwd", 7), ("groups", "/etc/group", 4)):
            try:
                rows = []
                for line in _small_regular(path, 1024 * 1024).splitlines():
                    if not line or line.startswith("#"):
                        continue
                    fields = line.split(":")
                    if len(fields) != count or not fields[0]:
                        raise ValueError("unsupported identity record")
                    numeric = fields[2:4] if label == "accounts" else fields[2:3]
                    if any(not value.isascii() or not value.isdecimal() or int(value) > 2**32 - 2
                           for value in numeric):
                        raise ValueError("unsupported numeric identity")
                    if label == "accounts":
                        rows.append({"name": fields[0], "uid": int(fields[2]), "gid": int(fields[3]),
                                     "home": fields[5], "shell": fields[6]})
                    else:
                        rows.append({"name": fields[0], "gid": int(fields[2]),
                                     "members": fields[3].split(",") if fields[3] else []})
                host[label] = {"status": "observed", "source": path, "entries": rows}
            except (OSError, ValueError) as exc:
                host[label] = {"status": type(exc).__name__, "source": path, "entries": []}
                self.error(path, "host_" + label, type(exc).__name__)
        commands = {
            "packages": ["/usr/bin/dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\n"],
            "manual_packages": ["/usr/bin/apt-mark", "showmanual"],
            "service_unit_files": ["/usr/bin/systemctl", "list-unit-files", "--no-pager", "--no-legend"],
            "current_service_states": ["/usr/bin/systemctl", "list-units", "--all", "--type=service",
                                       "--no-pager", "--no-legend", "--plain"],
            "block_devices": ["/usr/bin/lsblk", "--json", "--bytes", "--output", "NAME,TYPE,SIZE,FSTYPE,UUID,MOUNTPOINTS"],
            "filesystems": ["/usr/bin/findmnt", "--json", "--output", "TARGET,SOURCE,FSTYPE,UUID"],
        }
        units = self.policy.get("service_units", [])
        if not isinstance(units, list) or any(not isinstance(u, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]+", u) for u in units):
            raise ValueError("service_units must contain literal unit names")
        if units:
            commands["service_states"] = ["/usr/bin/systemctl", "show", *units, "--no-pager", "--property=" + SAFE_UNIT_PROPERTIES]
        for name, argv in commands.items():
            result = _run(argv, self.timeout)
            host[name] = {"status": result["status"], "returncode": result.get("returncode"),
                          "output": result["output"].decode("utf-8", "replace") if result["status"] == "ok" else ""}
            if name == "current_service_states" and result["status"] == "ok":


                output = host[name].pop("output")
                entries = []
                for line in output.splitlines():
                    fields = line.split(None, 4)
                    if len(fields) < 4:
                        self.error(argv[0], name, "unsupported_unit_state_output")
                        host[name]["status"] = "unsupported_unit_state_output"
                        continue
                    entries.append(dict(zip(("unit", "load", "active", "sub"), fields[:4])))
                host[name]["entries"] = entries
            if result["status"] != "ok":
                self.error(argv[0], name, result["status"])
        return host

    def collect(self):
        for root in self.roots:
            self.scan(root)
        while self.candidates - self.checkouts:
            for path in sorted(self.candidates - self.checkouts):
                if self.progress:
                    self.progress({"phase": "manifest_git", "path": path})
                self.inspect_git(path)
        for path in _paths(self.policy.get("model_reference_paths", []), "model_reference_paths"):
            self.model_reference(path)
        self.result["host"] = (self.host() if self.policy.get("collect_host_metadata", True) and not self.fixture
                               else {"status": "not_requested"})
        self.result["required_roots"] = sorted(set(self.result["required_roots"]))
        self.result["topology"]["entries"] = list({entry["path"]: entry for entry in self.result["topology"]["entries"]}.values())
        self.result["scanned_entries"] = self.count
        self.result["finished_at"] = _now()
        return self.result


def collect_manifest(policy: dict, progress=None) -> dict:
    """Collect bounded metadata; errors/limits return an incomplete report.

    ``source_roots`` describes backup coverage, while ``manifest_roots`` limits
    metadata discovery. ``scan_exclusions`` names exact paths (and their subtree),
    never glob patterns or a name-based omission rule. Existing uncovered Git,
    alternate and symlink targets appear in ``required_roots`` for the caller.
    The caller owns persistence and any decision to proceed despite limitations.
    """
    if not isinstance(policy, dict):
        raise ValueError("policy must be a dictionary")
    return _Collector(policy, progress).collect()
