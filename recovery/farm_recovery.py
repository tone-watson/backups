#!/usr/bin/python3
"""Portable Farm recovery coordinator. No imports from Farm or installed apps."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
TAG = "farm-recovery"
COMPLETE = "farm-recovery-complete"
MARKER = ".farm-recovery-drive.json"
LIFECYCLE_LOCK = Path("/run/lock/farm-recovery.lifecycle.lock")


class RecoveryError(RuntimeError):
    pass


class ResticError(RecoveryError):
    """A native failure with private evidence, never native text in its message."""

    def __init__(self, operation, exit_code, *, summary=None, diagnostics_path=None, error_count=0):
        self.exit_code = exit_code
        self.summary = summary
        self.diagnostics_path = diagnostics_path
        self.error_count = error_count
        message = (f"restic {operation} failed (exit {exit_code}; reported errors: {error_count}); "
                   "no complete recovery set published")
        if diagnostics_path is not None:
            message += f"; private diagnostics: {diagnostics_path}"
        super().__init__(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with open(temp, "x", encoding="utf-8") as f:
        os.chmod(temp, 0o600)
        json.dump(value, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def absolute(value):
    p = Path(value)
    if not p.is_absolute() or ".." in p.parts or "\0" in str(p):
        raise RecoveryError(f"Expected a normalized absolute path: {p}")
    return p


def beneath(path, root):
    return path == root or root in path.parents


def real_directory(path, *, create=False):
    path = absolute(path)

    if path.resolve() != path:
        raise RecoveryError(f"Symlinked directory is not allowed: {path}")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise RecoveryError(f"Directory not found: {path}")
    return path


def private_directory(path):
    path = real_directory(path, create=True)
    st = path.stat()
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise RecoveryError(f"Directory must be owned by current user and mode 0700: {path}")
    return path


def managed_state(state):
    """Claim only an empty directory; never clear an unrelated current/ tree."""
    marker = state / ".farm-recovery-state.json"
    if marker.is_symlink():
        raise RecoveryError("Symlinked recovery state marker")
    if marker.exists():
        if json.loads(marker.read_text()).get("schema") != "farm.recovery-state/1":
            raise RecoveryError("Unknown recovery state marker")
    else:
        if any(p.name != "operation.lock" for p in state.iterdir()):
            raise RecoveryError("Refusing populated, unmanaged recovery state directory")
        write_json(marker, {"schema": "farm.recovery-state/1", "created_at": now()})
    capture_dir = state / "current" / "captures"
    if capture_dir.exists():
        real_directory(capture_dir)
        receipt = capture_dir / "manifest.json"
        if receipt.is_symlink() or not receipt.is_file() or json.loads(receipt.read_text()).get("status") != "complete":
            raise RecoveryError("Previous capture is incomplete; review its service journal and move current/ aside before retrying")


def validate_manifest(report, roots, exclusions):
    if report.get("errors"):
        raise RecoveryError("Manifest has discovery errors; resolve them before a server backup")
    for boundary in report.get("scan_boundaries", []):
        if not boundary.get("incomplete_scope"):
            continue
        path = absolute(boundary["path"])


        covered = any(beneath(path, p) for p in roots)
        excluded = any(beneath(path, Path(p)) for p in exclusions)
        if boundary.get("reason") != "depth_limit" or not covered or excluded:
            raise RecoveryError("Manifest discovery was truncated or an unmapped path is excluded; review scan bounds")


def load_policy(path):
    data = json.loads(Path(path).read_text())
    if data.get("schema") != "farm.recovery-policy/1":
        raise RecoveryError("Unsupported recovery policy schema")
    if data.get("mode") not in {"server", "fixture"}:
        raise RecoveryError("Policy mode must be server or fixture")
    roots = [absolute(p) for p in data.get("source_roots", [])]
    if not roots:
        raise RecoveryError("Policy has no source roots")
    for key in ("exclude_paths", "manifest_roots", "scan_exclusions"):
        for p in data.get(key, []):
            absolute(p)
    absolute(data["state_dir"])
    if data["mode"] == "fixture":
        base = real_directory(data["fixture_root"])
        if base == Path("/") or len(base.parts) < 3:
            raise RecoveryError("Fixture root must be a dedicated scratch directory")
        fixture_paths = roots + [absolute(data["state_dir"])]
        for key in ("manifest_roots", "sqlite_roots", "sqlite_paths", "model_reference_paths"):
            fixture_paths.extend(absolute(p) for p in data.get(key, []))
        for p in fixture_paths:
            if not beneath(p.resolve(), base):
                raise RecoveryError("Fixture sources and state must stay inside fixture_root")
        if data.get("postgres") or data.get("redis") or data.get("offline_stores"):
            raise RecoveryError("Fixture policy cannot query or stop host services")
    return data


def validate_source_mounts(policy):
    for target, expected in policy.get("required_mounts", {}).items():
        info = mount_info(target)
        if info.get("target") != target or info.get("uuid") != expected:
            raise RecoveryError(f"Required source filesystem missing or changed: {target}")


def preflight(policy):
    checks = []
    errors = []
    for path in policy["source_roots"]:
        p = Path(path)
        checks.append({"source": path, "exists": p.exists(), "readable": os.access(p, os.R_OK | os.X_OK)})
        if not checks[-1]["exists"] or not checks[-1]["readable"]:
            errors.append(f"Required source unavailable: {path}")
    try:
        validate_source_mounts(policy)
    except RecoveryError as exc:
        errors.append(str(exc))
    tools = {name: shutil.which(name) for name in ["git", "findmnt", "tar", "getfacl", "sqlite3", "redis-cli", "redis-check-rdb"]}
    return {"schema": "farm.recovery-preflight/1", "captured_at": now(), "sources": checks,
            "tools": tools, "errors": errors, "effective_uid": os.geteuid(),
            "status": "review-required" if errors or os.geteuid() != 0 else "sources-observed",
            "notes": ["No payload capture, drive initialization or service changes performed.",
                      "Required captures and destination checks are repeated by backup.",
                      "A missing getfacl limits inventory; restic metadata restoration needs acceptance testing."]}


def clean_env():
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def command(argv, *, timeout=60):
    result = subprocess.run([str(a) for a in argv], env=clean_env(),
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:

        raise RecoveryError(f"{Path(str(argv[0])).name} failed (exit {result.returncode})")
    return result.stdout


def mount_info(path):
    data = json.loads(command(["findmnt", "--json", "--target", str(path),
                               "--output", "TARGET,SOURCE,FSTYPE,UUID"]))
    rows = data.get("filesystems", [])
    if len(rows) != 1:
        raise RecoveryError("Cannot establish destination mount identity")
    return rows[0]


def validate_drive(drive, policy, expected_uuid):
    drive = real_directory(drive)
    if policy["mode"] == "fixture":
        base = real_directory(policy["fixture_root"])
        if not beneath(drive, base):
            raise RecoveryError("Fixture repository must stay inside fixture_root")
        for root in policy["source_roots"]:
            if beneath(drive, Path(root)) or beneath(Path(root), drive):
                raise RecoveryError("Fixture source/repository overlap")
        return {"target": str(drive), "uuid": "fixture", "fstype": "fixture"}
    if os.geteuid() != 0:
        raise RecoveryError("Server backup commands require root; preparation does not")
    info = mount_info(drive)
    if not expected_uuid or expected_uuid == "REPLACE_WITH_DRIVE_UUID":
        raise RecoveryError("An explicit backup drive filesystem UUID is required")
    if Path(info["target"]) != drive or info.get("uuid") != expected_uuid:
        raise RecoveryError("Drive is not the expected mounted filesystem; refusing local-disk fallback")
    if info.get("fstype") not in {"ext4", "xfs", "btrfs"}:
        raise RecoveryError("Use an ext4, XFS or Btrfs backup filesystem")
    for root in policy["source_roots"]:
        p = Path(root)
        if p.exists() and p.stat().st_dev == drive.stat().st_dev:
            raise RecoveryError(f"Backup drive shares the source filesystem: {root}")
    return info


def password_file(path, drive):
    path = absolute(path)
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.geteuid():
        raise RecoveryError("Password file must be a regular, current-user-owned mode 0600 file")
    if path.resolve() != path or beneath(path, drive) or not st.st_size:
        raise RecoveryError("Password file must be nonempty, outside the drive and not symlinked")
    return path


@contextlib.contextmanager
def locked(path):
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError("Another recovery operation holds the lock") from exc
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def lifecycle_lock(*, wait=False):
    """Serialize host drive operations outside the detachable filesystem."""
    parent = real_directory(LIFECYCLE_LOCK.parent)
    if parent.stat().st_uid != 0:
        raise RecoveryError("Recovery lifecycle lock directory must be root-owned")
    fd = os.open(LIFECYCLE_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077 or st.st_nlink != 1:
            raise RecoveryError("Unsafe recovery lifecycle lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise RecoveryError("Another recovery or drive operation is running") from exc
        yield
    finally:
        os.close(fd)


class Restic:
    def __init__(self, binary, repo, password):
        self.binary = str(Path(binary).resolve())
        self.repo, self.password = Path(repo), Path(password)
        version = command([self.binary, "version"])
        match = re.search(r"restic (\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (0, 17, 0):
            raise RecoveryError("Restic 0.17 or newer is required")
        self.version = version.strip()

    def run(self, args, *, timeout=None, diagnostics_path=None):
        """Drain both pipes; retain bounded native error details only privately."""
        if diagnostics_path is not None:
            diagnostics_path = absolute(diagnostics_path)
            private_directory(diagnostics_path.parent)
            if diagnostics_path.is_symlink():
                raise RecoveryError("Symlinked restic diagnostics destination")
        report = {"schema": "farm.recovery-restic-diagnostics/1", "operation": str(args[0]),
                  "started_at": now(), "exit_code": None, "error_count": 0, "errors": [],
                  "errors_dropped": 0, "malformed_stderr_records": 0, "summary": None}
        reported_errors = 0
        summary_numbers = {"files_new", "files_changed", "files_unmodified", "dirs_new", "dirs_changed",
                           "dirs_unmodified", "data_blobs", "tree_blobs", "data_added", "data_added_packed",
                           "total_files_processed", "total_bytes_processed", "total_duration"}

        def counter(value):
            return value if type(value) is int and 0 <= value <= 2 ** 64 - 1 else 0

        def retain_error(event):
            nonlocal reported_errors
            kind = event.get("message_type")
            if kind == "error":
                reported_errors += 1
                report["error_count"] = max(report["error_count"], reported_errors)
                details = {"message_type": kind}
                for field, limit in (("during", 128), ("item", 4096)):
                    if isinstance(event.get(field), str):
                        details[field] = event[field][:limit]
                error = event.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    details["error"] = {"message": error["message"][:4096]}
            elif kind == "exit_error":
                details = {"message_type": kind}
                if type(event.get("code")) is int:
                    details["code"] = event["code"]
                if isinstance(event.get("message"), str):
                    details["message"] = event["message"][:4096]
            else:
                return False
            if len(report["errors"]) < 128:
                report["errors"].append(details)
            else:
                report["errors_dropped"] += 1
            return True

        def backup_event(line):
            nonlocal last_progress
            try:
                event = json.loads(line)
            except (ValueError, RecursionError) as exc:
                raise RecoveryError("Invalid restic backup progress record") from exc
            if not isinstance(event, dict):
                raise RecoveryError("Invalid restic backup progress record")
            if event.get("message_type") == "summary":
                summary = {"message_type": "summary"}
                for key in summary_numbers:
                    value = event.get(key)
                    if type(value) in (int, float) and 0 <= value <= 2 ** 64 - 1:
                        summary[key] = value
                snapshot = event.get("snapshot_id")
                if isinstance(snapshot, str) and re.fullmatch(r"[0-9a-f]{64}", snapshot):
                    summary["snapshot_id"] = snapshot
                if type(event.get("dry_run")) is bool:
                    summary["dry_run"] = event["dry_run"]
                for key in ("backup_start", "backup_end"):
                    value = event.get(key)
                    if isinstance(value, str) and len(value) <= 128:
                        try:
                            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                            if stamp.tzinfo is not None:
                                summary[key] = value
                        except ValueError:
                            pass
                report["summary"] = summary
                output.extend(json.dumps(summary).encode("utf-8") + b"\n")
                if len(output) > 1024 * 1024:
                    raise RecoveryError("Restic summary response exceeded the bounded output limit")
            elif event.get("message_type") == "status":
                report["error_count"] = max(report["error_count"], counter(event.get("error_count")))
                if time.monotonic() - last_progress > 30:
                    print(json.dumps({"phase": "backup", "bytes_done": counter(event.get("bytes_done")),
                                      "files_done": counter(event.get("files_done")),
                                      "error_count": report["error_count"]}), flush=True)
                    last_progress = time.monotonic()
            else:
                retain_error(event)

        def stderr_event(line):
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                report["malformed_stderr_records"] += 1
                return
            if not isinstance(event, dict) or not retain_error(event):
                report["malformed_stderr_records"] += 1

        argv = [self.binary, "--repo", str(self.repo), "--password-file", str(self.password),
                "--no-cache", "--json", *map(str, args)]

        proc = subprocess.Popen(argv, env=clean_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, pending, stderr_pending = bytearray(), bytearray(), bytearray()
        stderr_discarding = False
        started = last_progress = time.monotonic()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
                selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    if timeout and time.monotonic() - started > timeout:
                        raise RecoveryError("Restic operation timed out")
                    for key, _ in selector.select(1):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == "stderr":


                            stderr_pending.extend(chunk)
                            while b"\n" in stderr_pending:
                                line, _, rest = stderr_pending.partition(b"\n")
                                stderr_pending = bytearray(rest)
                                if stderr_discarding:
                                    stderr_discarding = False
                                elif len(line) > 1024 * 1024:
                                    report["malformed_stderr_records"] += 1
                                elif line.strip():
                                    stderr_event(line)
                            if len(stderr_pending) > 1024 * 1024:
                                if not stderr_discarding:
                                    report["malformed_stderr_records"] += 1
                                stderr_discarding = True
                                stderr_pending.clear()
                            continue
                        if args[0] != "backup":
                            output.extend(chunk)
                            if len(output) > 32 * 1024 * 1024:
                                raise RecoveryError("Restic response exceeded the bounded output limit")
                            continue
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if not line:
                                continue
                            if len(line) > 1024 * 1024:
                                raise RecoveryError("Restic progress record exceeded the bounded output limit")
                            backup_event(line)
                        if len(pending) > 1024 * 1024:
                            raise RecoveryError("Restic progress record exceeded the bounded output limit")
                proc.wait()
            if pending.strip():
                backup_event(pending)
            if stderr_pending.strip() and not stderr_discarding:
                stderr_event(stderr_pending)
        except BaseException:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
        finally:
            proc.stdout.close()
            proc.stderr.close()
            report.update(finished_at=now(), exit_code=proc.returncode)
            if diagnostics_path is not None:
                write_json(diagnostics_path, report)
        if proc.returncode:
            raise ResticError(args[0], proc.returncode, summary=report["summary"],
                              diagnostics_path=diagnostics_path, error_count=report["error_count"])
        return output.decode("utf-8")

    def json(self, args):
        return json.loads(self.run(args))


def kit(destination, restic_binary=None):
    destination = real_directory(destination, create=True)
    def copy_regular(source, target, mode=None):
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RecoveryError(f"Unsafe existing rescue-kit entry: {target.name}")
        temp = target.with_name("." + target.name + ".tmp-" + uuid.uuid4().hex)
        with open(source, "rb") as origin, open(temp, "xb") as output:
            shutil.copyfileobj(origin, output)
            output.flush()
            os.fsync(output.fileno())
        temp.chmod(mode or (source.stat().st_mode & 0o777))
        os.replace(temp, target)
    for name in ["farm-recovery", "farm_recovery.py", "manifest.py", "capture.py", "redis_state.py", "apply_captures.py",
                 "policy.server.json", "README.md", "RESTORE.md", "fetch-restic.py"]:
        source = HERE / name
        if source.exists():
            copy_regular(source, destination / name)
    if restic_binary:
        copy_regular(Path(restic_binary), destination / "restic", 0o700)
    hashes = {p.name: digest(p) for p in destination.iterdir()
              if not p.is_symlink() and p.is_file() and p.name != "SHA256SUMS.json"}
    write_json(destination / "SHA256SUMS.json", hashes)


def prepare(policy, output):
    from manifest import collect_manifest
    output = private_directory(output)
    if any(output.iterdir()):
        raise RecoveryError("Preparation output must be empty")
    last_progress = 0.0
    def progress(event):
        nonlocal last_progress
        if time.monotonic() - last_progress >= 15:
            print(json.dumps({"phase": event.get("phase"), "entries": event.get("entries")}), flush=True)
            last_progress = time.monotonic()
    report = collect_manifest(policy, progress=progress)
    write_json(output / "manifest.json", report)
    write_json(output / "policy.json", policy)
    kit(output / "rescue-kit")
    return {"status": report.get("status"), "output": str(output),
            "errors": len(report.get("errors", [])),
            "note": "Metadata preparation only; no server payload backup or service change"}


def escape_pattern(path):
    return "".join("\\" + c if c in "\\*?[" else c for c in str(path))


def exclusions_for(policy, drive, password, state, captures):
    result = set(policy.get("exclude_paths", []))
    result.update([str(drive), str(password), str(state / "operation.lock"), str(state / "receipts"),
                   str(state / "diagnostics")])
    result.update(captures.get("exclusions", []))
    if policy["mode"] == "server":

        mounts = json.loads(command(["findmnt", "--json", "--list", "--output", "TARGET,FSTYPE"]))
        allowed = set(policy["source_roots"])
        for row in mounts.get("filesystems", []):
            target = row["target"]
            if target not in allowed and target != "/":
                virtual = any(beneath(Path(target), Path(p)) for p in ("/dev", "/proc", "/sys", "/run"))
                if not virtual and Path(target) != drive and row.get("fstype") != "squashfs":
                    raise RecoveryError(f"Unclassified mount {target}; add it as a source or explicitly review the mount policy")




                explicit_descendant = any(beneath(Path(p), Path(target)) for p in allowed)
                if row.get("fstype") != "squashfs" or not explicit_descendant:
                    result.add(target)
    return sorted(result)


def initialize(args, policy):
    drive = real_directory(args.drive)
    info = validate_drive(drive, policy, args.uuid)
    password = password_file(args.password_file, drive)
    with locked(drive / ".farm-recovery.lock"):
        if (drive / MARKER).exists() or (drive / "repository").exists():
            raise RecoveryError("Drive already contains recovery state; initialization will not overwrite it")
        engine = Restic(args.restic, drive / "repository", password)
        engine.run(["init", "--repository-version", "2"])
        config = engine.json(["cat", "config"])
        kit(drive / "rescue-kit", engine.binary)
        marker = {"schema": "farm.recovery-drive/1", "filesystem_uuid": info["uuid"],
                  "repository_id": config["id"], "created_at": now()}
        write_json(drive / MARKER, marker)
    return {"status": "initialized", "drive": str(drive), "repository_id": marker["repository_id"]}


def open_repository(args, policy=None):
    drive = real_directory(args.drive)
    marker_path = drive / MARKER
    if marker_path.is_symlink():
        raise RecoveryError("Symlinked drive marker")
    marker = json.loads(marker_path.read_text())
    if marker.get("schema") != "farm.recovery-drive/1":
        raise RecoveryError("Unknown drive marker")
    if policy:
        validate_drive(drive, policy, args.uuid or marker["filesystem_uuid"])
    else:
        info = mount_info(drive)
        if marker["filesystem_uuid"] != "fixture" and (
                Path(info["target"]) != drive or info.get("uuid") != marker["filesystem_uuid"]):
            raise RecoveryError("Restore drive identity mismatch")
    real_directory(drive / "repository")
    engine = Restic(args.restic, drive / "repository", password_file(args.password_file, drive))
    if engine.json(["cat", "config"])["id"] != marker["repository_id"]:
        raise RecoveryError("Restic repository identity mismatch")
    return drive, engine


def backup(args, policy):
    from manifest import collect_manifest
    from capture import capture_state
    drive, engine = open_repository(args, policy)
    state = private_directory(policy["state_dir"])
    run_id = uuid.uuid4().hex
    with locked(drive / ".farm-recovery.lock"), locked(state / "operation.lock"):
        roots = [absolute(p) for p in policy["source_roots"]]
        for p in roots:
            if not p.exists() or p.is_symlink():
                raise RecoveryError(f"Required source root is missing or symlinked: {p}")
        validate_source_mounts(policy)
        if beneath(state, drive):
            raise RecoveryError("Plaintext capture staging must not be on the backup drive")
        total, used, free = shutil.disk_usage(drive)
        reserve = policy.get("minimum_free_bytes", 0)
        if free < reserve:
            raise RecoveryError("Drive free space is below the configured reserve")
        managed_state(state)
        stage = state / "current"
        if stage.exists():
            real_directory(stage)
            shutil.rmtree(stage)
        stage.mkdir(mode=0o700)
        receipts = private_directory(state / "receipts")
        receipt_path = receipts / (run_id + ".json")
        receipt = {"schema": "farm.recovery-run/1", "id": run_id, "started_at": now(), "status": "incomplete"}
        diagnostics_path = state / "diagnostics" / (run_id + ".json")
        write_json(receipt_path, receipt)
        try:
            print(json.dumps({"phase": "manifest", "run_id": run_id}), flush=True)
            manifest_policy = dict(policy)
            manifest_policy["scan_exclusions"] = list(set(policy.get("scan_exclusions", []) + [str(drive), str(state)]))
            report = collect_manifest(manifest_policy)
            for p in report.get("required_roots", []):
                if isinstance(p, dict):
                    p = p.get("path")
                if p and Path(p) not in roots:
                    if policy["mode"] == "fixture" and not beneath(Path(p).resolve(), Path(policy["fixture_root"])):
                        raise RecoveryError("Manifest discovered a source outside the fixture root")
                    roots.append(absolute(p))
            for p in roots:
                if not p.exists() or p.is_symlink() or beneath(p, drive):
                    raise RecoveryError(f"Discovered source is unavailable or overlaps the destination: {p}")
            write_json(stage / "manifest.json", report)
            write_json(stage / "policy.json", policy)
            print(json.dumps({"phase": "capture", "run_id": run_id}), flush=True)

            effective_policy = dict(policy, source_roots=list(map(str, roots)))
            preliminary_exclusions = exclusions_for(effective_policy, drive, engine.password, state, {})
            for root in roots:
                if any(beneath(root, Path(ex)) for ex in preliminary_exclusions):
                    raise RecoveryError(f"Required source root excluded by policy: {root}")
            validate_manifest(report, roots, preliminary_exclusions)
            receipt["manifest_status"] = report.get("status")
            receipt["manifest_depth_boundaries"] = sum(b.get("reason") == "depth_limit" for b in report.get("scan_boundaries", []))
            captured = capture_state(policy, stage / "captures", quiesce=args.quiesce)
            if captured.get("status") != "complete":
                raise RecoveryError("One or more consistent captures failed")
            write_json(stage / "capture.json", captured)
            kit(stage / "rescue-kit")
            excluded = exclusions_for(effective_policy, drive, engine.password, state, captured)
            write_json(stage / "selection.json", {"roots": list(map(str, roots)), "excluded_exact_paths": excluded})
            for root in roots:
                if any(beneath(root, Path(ex)) for ex in excluded):
                    raise RecoveryError(f"Required source root excluded by policy: {root}")
            argv = ["backup", "--host", policy.get("host_id", "farm-server"), "--one-file-system",
                    "--tag", TAG, "--tag", "pending", "--tag", "run-" + run_id]
            for path in excluded:
                argv.extend(["--exclude", escape_pattern(path)])
            argv.extend(["--", *map(str, roots), str(stage)])
            print(json.dumps({"phase": "backup", "run_id": run_id}), flush=True)
            events = [json.loads(line) for line in engine.run(argv, diagnostics_path=diagnostics_path).splitlines()
                      if line.strip()]
            summaries = [v for v in events if v.get("message_type") == "summary"]
            if len(summaries) != 1 or not summaries[0].get("snapshot_id"):
                raise RecoveryError("No unique successful restic snapshot summary")
            snapshot_id = summaries[0]["snapshot_id"]
            engine.run(["check"])
            kit(drive / "rescue-kit", engine.binary)

            engine.run(["tag", "--remove", "pending", "--add", COMPLETE, snapshot_id])
            complete = engine.json(["snapshots", "--tag", COMPLETE + ",run-" + run_id])
            if len(complete) != 1:
                raise RecoveryError("Cannot resolve completed snapshot identity")
            receipt.update(status="complete", snapshot_id=complete[0]["id"], finished_at=now(),
                           summary=summaries[0], restic=engine.version)
            write_json(receipt_path, receipt)
            return receipt
        except BaseException as exc:
            receipt.update(finished_at=now(), error=type(exc).__name__)
            if isinstance(exc, ResticError):
                receipt.update(restic_exit_code=exc.exit_code, restic_summary=exc.summary,
                               restic_error_count=exc.error_count,
                               diagnostics_path=str(exc.diagnostics_path) if exc.diagnostics_path else None)
            write_json(receipt_path, receipt)
            raise


def select_snapshot(engine, requested):
    snapshots = engine.json(["snapshots", "--tag", COMPLETE])
    if not snapshots:
        raise RecoveryError("No complete recovery snapshots")
    if requested == "latest":
        return max(snapshots, key=lambda s: (s["time"], s["id"]))
    matches = [s for s in snapshots if s["id"].startswith(requested)]
    if not requested or len(matches) != 1:
        raise RecoveryError("Snapshot must uniquely identify a complete recovery set")
    return matches[0]


def restore(args):
    drive, engine = open_repository(args)
    target = real_directory(args.target, create=True)
    if target == Path("/") or any(target.iterdir()) or beneath(target, drive) or beneath(drive, target):
        raise RecoveryError("Restore target must be empty, separate from the repository, and not /")
    with locked(drive / ".farm-recovery.lock"):
        snapshot = select_snapshot(engine, args.snapshot)
        engine.run(["check"])
        engine.run(["restore", snapshot["id"], "--target", str(target), "--verify"])

        result = {"schema": "farm.recovery-restore/1", "status": "extracted-and-verified",
                  "snapshot_id": snapshot["id"], "target": str(target), "finished_at": now(),
                  "next": "Follow rescue-kit/RESTORE.md to apply consistent captures and rebuild the fresh host."}
        write_json(target / "FARM-RESTORE-RECEIPT.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=str(HERE / "policy.server.json"))
    bundled = next((p for p in [HERE / "restic", HERE / ".tools/restic"] if p.is_file()), None)
    parser.add_argument("--restic", default=str(bundled) if bundled else shutil.which("restic"))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Refresh metadata/manifests only; no backups or service changes")
    p.add_argument("--output", required=True)
    sub.add_parser("preflight", help="Read-only source and tool checks; no drive required")
    p = sub.add_parser("apply-captures", help="Apply verified captures inside an extracted alternate root")
    p.add_argument("--target", required=True)
    p.add_argument("--capture-dir", required=True)
    for name in ["init", "backup", "snapshots", "check", "retention-plan", "restore"]:
        p = sub.add_parser(name)
        p.add_argument("--drive", required=True)
        p.add_argument("--password-file", required=True)
        p.add_argument("--uuid")
        if name == "backup":
            p.add_argument("--quiesce", action="store_true", help="Permit configured offline-store service stop/start during capture")
        if name == "check":
            p.add_argument("--read-data", action="store_true", help="Read and verify every stored data pack")
        if name == "restore":
            p.add_argument("--snapshot", default="latest")
            p.add_argument("--target", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.command == "prepare":
            result = prepare(load_policy(args.policy), args.output)
        elif args.command == "preflight":
            result = preflight(load_policy(args.policy))
        elif args.command == "apply-captures":
            from apply_captures import apply_captures, ApplyError
            target = real_directory(args.target)
            if target == Path("/"):
                raise RecoveryError("Capture application requires an alternate root")
            with locked(target / ".farm-recovery-apply.lock"):
                try:
                    result = apply_captures(target, Path(args.capture_dir))
                except ApplyError as exc:
                    raise RecoveryError(str(exc)) from exc
                result["finished_at"] = now()
                write_json(target / "FARM-APPLY-RECEIPT.json", result)
        else:


            server = load_policy(args.policy)["mode"] == "server"
            gate = lifecycle_lock() if server and os.geteuid() == 0 else contextlib.nullcontext()
            with gate:
                if server and os.geteuid() == 0 and os.path.lexists("/run/farm-recovery-operator/launch.json"):
                    raise RecoveryError("An operator backup launch is pending; inspect its status before direct recovery operations")
                if not args.restic:
                    raise RecoveryError("restic is unavailable; use fetch-restic.py or an installed binary")
                if args.command == "restore":
                    result = restore(args)
                else:
                    policy = load_policy(args.policy)
                    if args.command == "init":
                        result = initialize(args, policy)
                    elif args.command == "backup":
                        result = backup(args, policy)
                    else:
                        drive, engine = open_repository(args, policy)
                        with locked(drive / ".farm-recovery.lock"):
                            if args.command == "snapshots":
                                result = engine.json(["snapshots", "--tag", COMPLETE])
                            elif args.command == "check":
                                engine.run(["check", *(["--read-data"] if args.read_data else [])])
                                result = {"status": "checked", "read_data": args.read_data}
                            else:
                                result = engine.json(["forget", "--dry-run", "--tag", COMPLETE,
                                                      "--group-by", "host", "--keep-within", "90d", "--keep-last", "3"])
        print(json.dumps(result, indent=2))
        return 0
    except (RecoveryError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "note": "No automatic unlock, prune or success receipt"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
