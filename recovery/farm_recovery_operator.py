#!/usr/bin/python3
"""Server-only drive/job operations. No formatting, initialization or automatic retry."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import io
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import farm_recovery as recovery

PROFILE = HERE / "operator.server.json"
RUNTIME = Path("/run/farm-recovery-operator")
PUBLIC = Path("/var/lib/farm-recovery-status")
UNIT = re.compile(r"farm-recovery[A-Za-z0-9_.@:-]*\.service\Z")
HEX32 = re.compile(r"[a-f0-9]{32}\Z")
HEX64 = re.compile(r"[a-f0-9]{64}\Z")
MAX_OUTPUT = 2 * 1024 * 1024


class OperatorError(RuntimeError):
    """Only controlled, non-secret text is safe for operator output."""


def _command(argv, *, timeout=30):
    """Bound output and elapsed time; never expose native stderr."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "TZ": "UTC"})
    output, stderr = bytearray(), bytearray()
    started = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, output)
            selector.register(proc.stderr, selectors.EVENT_READ, stderr)
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    raise OperatorError("System command timed out; inspect status before retrying")
                for key, _ in selector.select(0.1):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(block)
                        if len(key.data) > MAX_OUTPUT:
                            raise OperatorError("System command output exceeded its limit")
            proc.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
        if proc.returncode or stderr.strip():
            raise OperatorError("System command failed or observation is unavailable")
        return output.decode("utf-8", "strict")
    except (UnicodeError, subprocess.TimeoutExpired):
        raise OperatorError("System command timed out or returned unsupported output") from None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def load_profile():
    profile = json.loads(PROFILE.read_text())
    if profile.get("schema") != "farm.recovery-operator-profile/1":
        raise OperatorError("Unsupported operator profile")
    for key in ("mountpoint", "device_by_id", "partition_by_id", "password_file", "policy", "state_dir"):
        recovery.absolute(profile[key])
    if not re.fullmatch(r"[a-f0-9-]{36}", profile["filesystem_uuid"]) or not HEX64.fullmatch(profile["repository_id"]):
        raise OperatorError("Invalid registered drive identity")
    return profile


def _require_root():
    if os.geteuid() != 0:
        raise OperatorError("This action requires sudo through farm backup")


def _private(path):
    path = recovery.private_directory(path)
    return path


def _read_json(path, *, public=False):
    st = path.lstat()
    allowed = 0o022 if public else 0o077
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & allowed
            or st.st_size > 65536 or path.resolve() != path):
        raise OperatorError("Unsafe operator state file")
    return json.loads(path.read_text())


def _reservation():
    path = RUNTIME / "launch.json"
    return _read_json(path) if path.exists() else None


def _publish(value):
    if PUBLIC.resolve() != PUBLIC:
        raise OperatorError("Unsafe public status directory")
    PUBLIC.mkdir(mode=0o755, parents=True, exist_ok=True)
    st = PUBLIC.stat()
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise OperatorError("Unsafe public status directory")


    PUBLIC.chmod(0o755)
    old = _read_json(PUBLIC / "latest.json", public=True) if (PUBLIC / "latest.json").exists() else {}
    if value.get("status") != "complete":
        value["last_backup"] = old.get("last_backup")

    allowed = {"schema", "status", "unit", "run_id", "phase", "bytes_done", "files_done",
               "updated_at", "started_at", "finished_at", "last_backup", "snapshot_id", "invocation_id", "pid"}
    value = {key: item for key, item in value.items() if key in allowed}
    value.update(schema="farm.recovery-operator-state/1", updated_at=recovery.now())
    recovery.write_json(PUBLIC / "latest.json", value)
    (PUBLIC / "latest.json").chmod(0o644)


def _lifecycle(*, wait=False):
    return recovery.lifecycle_lock(wait=wait)


def _units():
    rows = json.loads(_command(["/usr/bin/systemctl", "list-units", "farm-recovery*.service", "--all",
                               "--plain", "--no-legend", "--no-pager", "--output=json"]))
    if not isinstance(rows, list) or len(rows) > 128:
        raise OperatorError("Recovery unit observation is unavailable")
    units = {}
    for row in rows:
        name = row.get("unit", "")
        if not UNIT.fullmatch(name) or name in units:
            raise OperatorError("Recovery unit identity is unavailable")
        output = _command(["/usr/bin/systemctl", "show", name, "--no-pager",
                           "--property=Id,LoadState,ActiveState,SubState,Result,MainPID,ControlPID,"
                           "ExecMainPID,ExecMainCode,ExecMainStatus,InvocationID,"
                           "ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic,"
                           "ExecMainStartTimestamp,ExecMainExitTimestamp"])
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if fields.get("Id") != name or fields.get("LoadState") != "loaded":
            raise OperatorError("Recovery unit identity is unavailable")
        units[name] = fields
    return units


def _running(unit):
    return (unit.get("ActiveState") in {"activating", "deactivating", "reloading"}
            or unit.get("ActiveState") == "active" and unit.get("SubState") != "exited"
            or unit.get("MainPID", "0") != "0" or unit.get("ControlPID", "0") != "0")


def _successful(unit):
    return (not _running(unit) and unit.get("ActiveState") in {"active", "inactive"}
            and unit.get("SubState") in {"exited", "dead"} and unit.get("Result") == "success"
            and unit.get("ExecMainCode") in {"1", "exited"} and unit.get("ExecMainStatus") == "0")


def _mounted(profile):
    rows = json.loads(_command(["/usr/bin/findmnt", "--json", "--bytes", "--output",
                               "TARGET,SOURCE,FSTYPE,UUID,AVAIL,SIZE,OPTIONS"]))["filesystems"]
    found = []
    def visit(items):
        for item in items:
            if item.get("target") == profile["mountpoint"]:
                found.append(item)
            visit(item.get("children", []))
    visit(rows)
    if len(found) > 1:
        raise OperatorError("Ambiguous destination mount")
    return found[0] if found else None


def _device(profile):
    """Bind UUID to the registered whole USB disk, never an observed sdX name."""
    by_id = Path(profile["device_by_id"])
    if not by_id.exists():
        return None
    disk_path = str(by_id.resolve(strict=True))
    rows = json.loads(_command(["/usr/bin/lsblk", "--json", "--bytes", "--paths", "--output",
                               "NAME,TYPE,PKNAME,UUID,SERIAL,TRAN,MOUNTPOINTS,RO", disk_path]))["blockdevices"]
    if len(rows) != 1:
        raise OperatorError("Ambiguous backup disk identity")
    disk = rows[0]
    if (disk.get("name") != disk_path or disk.get("type") != "disk" or disk.get("tran") != "usb"
            or disk.get("serial") != profile["serial"] or disk.get("ro") not in (False, 0)):
        raise OperatorError("Attached device does not match the registered writable USB disk")
    descendants = []
    def visit(node):
        descendants.append(node)
        for child in node.get("children", []):
            visit(child)
    visit(disk)
    matches = [item for item in descendants if item.get("uuid") == profile["filesystem_uuid"]]
    if len(matches) != 1 or matches[0].get("type") != "part" or matches[0].get("pkname") != disk_path:
        raise OperatorError("Registered backup filesystem is missing or ambiguous")
    if str(Path(profile["partition_by_id"]).resolve(strict=True)) != matches[0]["name"]:
        raise OperatorError("Backup partition hardware identity changed")

    if len(descendants) != 2:
        raise OperatorError("Backup disk layout changed; review before operating")
    return {"disk": disk_path, "device": matches[0]["name"], "nodes": descendants}


def drive_state(profile):
    mounted = _mounted(profile)
    if mounted:
        correct = mounted.get("uuid") == profile["filesystem_uuid"] and mounted.get("fstype") == "ext4"
        if correct:
            device = _device(profile)
            correct = device is not None and str(Path(mounted.get("source", "")).resolve()) == device["device"]
        return {"drive_state": "mounted" if correct else "wrong-drive",
                "drive_free_bytes": _number(mounted.get("avail")) if correct else None}
    return {"drive_state": "attached" if _device(profile) else "disconnected", "drive_free_bytes": None}


def _probe_lock(path):
    if not path.exists():
        return
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
            raise OperatorError("Unsafe existing recovery lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperatorError("A recovery operation is already running") from None
    finally:
        os.close(fd)


def _busy(profile, *, ignore_unit=None, ignore_reservation=False):
    if not ignore_reservation and _reservation():
        raise OperatorError("A backup launch is pending; inspect status before another operation")
    if any(_running(value) for name, value in _units().items() if name != ignore_unit):
        raise OperatorError("A recovery service is running or stopping")
    _probe_lock(Path(profile["state_dir"]) / "operation.lock")
    mounted = _mounted(profile)
    if mounted and mounted.get("uuid") == profile["filesystem_uuid"]:
        _probe_lock(Path(profile["mountpoint"]) / ".farm-recovery.lock")


def _mountpoint(profile):
    path = recovery.real_directory(profile["mountpoint"])

    for parent in [path, *path.parents]:
        st = parent.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise OperatorError("Mountpoint and ancestors must be root-owned and not writable by others")
    return path


def _validate_repository(profile):
    path = Path(profile["mountpoint"])
    mounted = _mounted(profile)
    if (not mounted or mounted.get("uuid") != profile["filesystem_uuid"] or mounted.get("fstype") != "ext4"
            or not {"rw", "nodev", "nosuid"}.issubset(set(mounted.get("options", "").split(",")))):
        raise OperatorError("Expected writable backup filesystem is not safely mounted")
    device = _device(profile)
    if device is None or str(Path(mounted.get("source", "")).resolve()) != device["device"]:
        raise OperatorError("Mounted filesystem is not the registered physical backup partition")
    marker = _read_json(path / recovery.MARKER)
    if (marker.get("schema") != "farm.recovery-drive/1" or marker.get("filesystem_uuid") != profile["filesystem_uuid"]
            or marker.get("repository_id") != profile["repository_id"]):
        raise OperatorError("Mounted drive repository marker does not match the registered repository")
    recovery.real_directory(path / "repository")


def _ensure_mount(profile):
    _mountpoint(profile)
    mounted = _mounted(profile)
    if mounted:
        _validate_repository(profile)
        return
    device = _device(profile)
    if device is None:
        raise OperatorError("Registered backup drive is disconnected")
    if any(any(p for p in node.get("mountpoints", [])) for node in device["nodes"]):
        raise OperatorError("Backup drive is mounted elsewhere; refusing another mount")


    _command(["/usr/bin/mount", "-o", "nodev,nosuid", profile["partition_by_id"], profile["mountpoint"]])
    _validate_repository(profile)


def _result(action, status="success", message="", **fields):
    return {"schema": "farm.recovery-operator/1", "action": action, "status": status, "message": message, **fields}


def _failure_message(exc):
    if isinstance(exc, (OperatorError, recovery.RecoveryError)):
        return re.sub(r"[\x00-\x1f\x7f]", " ", str(exc))[:512]
    return "Operation failed; inspect recovery status before retrying"


def mount(profile):
    _require_root()
    with _lifecycle():
        _busy(profile)
        _ensure_mount(profile)
    return _result("mount", message="Backup drive is mounted and its repository marker is verified", drive_state="mounted")


def _arguments(profile):
    binary = HERE / ".tools/restic"
    return argparse.Namespace(drive=profile["mountpoint"], password_file=profile["password_file"],
                              uuid=profile["filesystem_uuid"], restic=str(binary), quiesce=True)


def start(profile):
    _require_root()
    with _lifecycle():
        _busy(profile)
        _ensure_mount(profile)
        policy = recovery.load_policy(profile["policy"])
        if policy.get("mode") != "server" or policy.get("state_dir") != profile["state_dir"]:
            raise OperatorError("Operator policy state directory does not match its profile")
        recovery.validate_source_mounts(policy)
        recovery.managed_state(_private(Path(profile["state_dir"])))

        recovery.open_repository(_arguments(profile), policy)
        token = uuid.uuid4().hex
        unit = "farm-recovery-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + token[:8] + ".service"
        _private(RUNTIME)
        reservation = {"unit": unit, "token": token, "created_at": recovery.now()}
        recovery.write_json(RUNTIME / "launch.json", reservation)
        argv = ["/usr/bin/systemd-run", "--quiet", "--unit=" + unit, "--service-type=exec",
                "--property=KillMode=mixed", "--property=TimeoutStopSec=infinity", "--property=UMask=0077",
                "--property=Restart=no", "--property=StandardOutput=journal", "--property=StandardError=journal",
                "/usr/bin/python3", "-I", "-S", "-B", str(HERE / "farm_recovery_operator.py"), "run", "--token", token]
        try:
            _command(argv)
        except BaseException:


            try:
                observed = _command(["/usr/bin/systemctl", "show", unit, "--no-pager",
                                     "--property=LoadState,ActiveState,MainPID,ControlPID"])
                fields = dict(line.split("=", 1) for line in observed.splitlines() if "=" in line)
                if (fields.get("LoadState") == "not-found" and fields.get("ActiveState") == "inactive"
                        and fields.get("MainPID") == "0" and fields.get("ControlPID") == "0"
                        and _reservation() == reservation):
                    (RUNTIME / "launch.json").unlink()
                    raise OperatorError("Background unit did not launch; no backup started")
            except OperatorError as exc:
                if str(exc) == "Background unit did not launch; no backup started":
                    raise
            raise OperatorError("Launch was not confirmed; preserve launch state and inspect systemd before retrying") from None
    return _result("start", status="running", message="Background backup accepted; configured writers pause during capture", unit=unit)


def _poweroff_device(profile):
    device = _device(profile)
    if not device:
        raise OperatorError("Registered USB disk is no longer available")
    for node in device["nodes"]:
        if any(node.get("mountpoints", [])):
            raise OperatorError("A disk filesystem is still mounted")
        holders = Path("/sys/class/block") / Path(node["name"]).name / "holders"
        if not holders.is_dir() or any(holders.iterdir()):
            raise OperatorError("USB disk has holders or its holder state is unavailable")
    swap_paths = {line.split()[0] for line in Path("/proc/swaps").read_text().splitlines()[1:] if line.split()}
    if any(node["name"] in swap_paths for node in device["nodes"]):
        raise OperatorError("USB disk is in use as swap")
    return device["disk"]


def detach(profile, *, power_off=False):
    _require_root()
    action = "eject" if power_off else "unmount"
    with _lifecycle():
        _busy(profile)
        mounted = _mounted(profile)
        if mounted:
            _mountpoint(profile)
            _validate_repository(profile)

            _command(["/usr/bin/umount", "--", profile["mountpoint"]], timeout=120)
            if _mounted(profile):
                raise OperatorError("Backup filesystem is still mounted")
        device = _device(profile)
        if device and any(any(p for p in node.get("mountpoints", [])) for node in device["nodes"]):
            raise OperatorError("Backup disk remains mounted elsewhere")
        if device:
            _poweroff_device(profile)
        if power_off and device:
            try:
                _command(["/usr/bin/udisksctl", "power-off", "--block-device", profile["device_by_id"], "--no-user-interaction"], timeout=60)
            except (OperatorError, OSError):


                if _device(profile) is not None:
                    _poweroff_device(profile)
                return _result(action, status="partial", message="Drive is unmounted; power-off was not confirmed. It is safe to unplug",
                               drive_state="attached", powered_off=False, safe_to_unplug=True)
            return _result(action, message="Backup drive is unmounted and powered off; safe to unplug",
                           drive_state="disconnected", powered_off=True, safe_to_unplug=True)
    return _result(action, message="Backup drive is unmounted; safe to unplug", drive_state="attached" if device else "disconnected",
                   powered_off=False, safe_to_unplug=True)


def _events():
    output = _command(["/usr/bin/journalctl", "-q", "--no-pager", "-o", "json", "-u", "farm-recovery*.service",
                       "-n", "2048", "--output-fields=MESSAGE,_SYSTEMD_UNIT,_SYSTEMD_INVOCATION_ID,__REALTIME_TIMESTAMP,_PID"])
    buffers, events = {}, []
    for line in output.splitlines():
        row = json.loads(line)
        identity = (row.get("_SYSTEMD_UNIT", ""), row.get("_SYSTEMD_INVOCATION_ID", ""), row.get("_PID", ""))
        if not UNIT.fullmatch(identity[0]) or not HEX32.fullmatch(identity[1]) or not identity[2].isdigit():
            continue
        message = row.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        try:
            value = json.loads(message)
        except ValueError:
            value = None
        if isinstance(value, dict):
            buffers.pop(identity, None)
        elif message.strip() == "{":
            buffers[identity] = message
            continue
        elif identity in buffers:
            buffers[identity] += "\n" + message
            if len(buffers[identity]) > 65536:
                raise OperatorError("Journal receipt exceeds its limit")
            try:
                value = json.loads(buffers[identity])
            except ValueError:
                continue
            buffers.pop(identity, None)
        if isinstance(value, dict):
            events.append((identity, int(row["__REALTIME_TIMESTAMP"]) / 1e6, value))
    return events


def _number(value):
    return value if type(value) is int and 0 <= value < 2**63 else None


def _complete(value):
    return (value.get("schema") == "farm.recovery-run/1" and value.get("status") == "complete"
            and isinstance(value.get("id"), str) and HEX32.fullmatch(value["id"])
            and isinstance(value.get("snapshot_id"), str) and HEX64.fullmatch(value["snapshot_id"])
            and _timestamp(value.get("finished_at")) is not None)


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else None
    except ValueError:
        return None


def _unit_timestamp(value):
    for pattern in ("%a %Y-%m-%d %H:%M:%S.%f UTC", "%a %Y-%m-%d %H:%M:%S UTC"):
        try:
            return dt.datetime.strptime(value, pattern).replace(tzinfo=dt.timezone.utc).timestamp()
        except (TypeError, ValueError):
            pass
    return None


def status(profile):
    result = _result("status", status="unknown", message="Backup observation unavailable", phase=None,
                     bytes_done=None, files_done=None, progress_stale=False, last_backup=None)
    try:
        result.update(drive_state(profile))
    except (OperatorError, OSError, ValueError, KeyError):
        result["drive_state"] = "unknown"
    saved = {}
    try:
        saved = _read_json(PUBLIC / "latest.json", public=True) if (PUBLIC / "latest.json").exists() else {}
        if saved and saved.get("schema") != "farm.recovery-operator-state/1":
            saved = {}
        if _timestamp(saved.get("last_backup")) is not None:
            result["last_backup"] = saved["last_backup"]
    except (OperatorError, OSError, ValueError, KeyError, TypeError):
        saved = {}
    try:
        units, events = _units(), _events()
        running = [name for name, item in units.items() if _running(item)]
        if len(running) > 1:
            raise OperatorError("Multiple recovery services are active")
        name = running[0] if running else max(units, key=lambda name: int(units[name].get("ExecMainStartTimestampMonotonic", "0")), default=None)
        completions = []
        for identity, stamp, value in events:
            if not _complete(value):
                continue
            unit = units.get(identity[0])
            if unit and unit.get("InvocationID") == identity[1]:
                if not _successful(unit) or identity[2] != unit.get("ExecMainPID"):
                    continue
            completions.append((_timestamp(value["finished_at"]), value))
        if completions:
            finished, value = max(completions, key=lambda item: item[0])
            if finished > (_timestamp(result["last_backup"]) or 0):
                result["last_backup"] = value["finished_at"]
        if name is None:
            successful_at = _timestamp(result["last_backup"]) or 0
            failed_at = (_timestamp(saved.get("finished_at")) or 0) if saved.get("status") == "failed" else 0
            if failed_at > successful_at:
                result.update(status=saved["status"], unit=saved.get("unit"), run_id=saved.get("run_id"),
                              bytes_done=_number(saved.get("bytes_done")), files_done=_number(saved.get("files_done")),
                              progress_stale=True, message="Backup failed; preserve its stage and inspect recovery status")
            elif saved.get("status") == "running" and (_timestamp(saved.get("started_at")) or 0) >= successful_at:
                result.update(status="unknown", progress_stale=True, message="Previous backup has no visible service; inspect its recovery journal")
            elif successful_at:
                result.update(status="complete", message="A completed backup is recorded; no backup is running")
                if saved.get("status") == "complete" and _timestamp(saved.get("finished_at")) == successful_at:
                    result.update(unit=saved.get("unit"), run_id=saved.get("run_id"), bytes_done=_number(saved.get("bytes_done")),
                                  files_done=_number(saved.get("files_done")))
            else:
                result.update(status="idle", message="No backup is running")
            return result
        unit = units[name]
        identity = (name, unit.get("InvocationID"), unit.get("MainPID") if _running(unit) else unit.get("ExecMainPID"))
        selected = [(stamp, value) for ident, stamp, value in events if ident == identity]
        observed_times = [stamp for stamp in (_unit_timestamp(unit.get("ExecMainStartTimestamp")),
                          _unit_timestamp(unit.get("ExecMainExitTimestamp")), *(stamp for stamp, _ in selected)) if stamp is not None]
        if (not _running(unit) and saved.get("status") == "running" and saved.get("unit") != name
                and (_timestamp(saved.get("started_at")) or 0) > max([_timestamp(result["last_backup"]) or 0, *observed_times])):
            result.update(status="unknown", phase=saved.get("phase"), unit=saved.get("unit"),
                          progress_stale=True, message="Previous backup has no visible service; inspect its recovery journal")
            return result
        if (not _running(unit) and observed_times and saved.get("status") == "failed"
                and (_timestamp(saved.get("finished_at")) or 0) > max(*observed_times, _timestamp(result["last_backup"]) or 0)):
            result.update(status="failed", message="Backup failed; preserve its stage and inspect recovery status", unit=saved.get("unit"))
            return result
        if (not _running(unit) and observed_times and
                (_timestamp(result["last_backup"]) or 0) > max(observed_times)):
            result.update(status="complete", message="No backup is running; a newer successful backup is recorded")
            return result
        result.update(unit=name, status="running" if _running(unit) else "unknown", message="Backup is running" if _running(unit) else "Completion is not confirmed")
        if (saved.get("unit"), saved.get("invocation_id"), saved.get("pid")) == identity:


            result.update(phase=saved.get("phase"), run_id=saved.get("run_id"),
                          bytes_done=_number(saved.get("bytes_done")), files_done=_number(saved.get("files_done")),
                          progress_stale=time.time() - (_timestamp(saved.get("updated_at")) or 0) > 120)
            if (saved.get("status") == "complete" and _successful(unit)
                    and _timestamp(saved.get("finished_at")) is not None and HEX64.fullmatch(saved.get("snapshot_id", ""))):
                result.update(status="complete", phase=None, message="Backup and repository check completed")
        for stamp, value in selected:
            if value.get("phase") in {"manifest", "capture", "backup"}:
                result["phase"] = value["phase"]
                if isinstance(value.get("run_id"), str) and HEX32.fullmatch(value["run_id"]):
                    result["run_id"] = value["run_id"]
                if _number(value.get("bytes_done")) is not None:
                    result.update(bytes_done=value["bytes_done"], files_done=_number(value.get("files_done")),
                                  progress_stale=time.time() - stamp > 120)
            if _complete(value) and _successful(unit):
                summary = value.get("summary", {})
                if not isinstance(summary, dict):
                    summary = {}
                result.update(status="complete", phase=None, message="Backup and repository check completed", last_backup=value["finished_at"],
                              bytes_done=_number(summary.get("total_bytes_processed")), files_done=_number(summary.get("total_files_processed")))
        if not _running(unit) and (unit.get("ActiveState") == "failed" or unit.get("Result") not in (None, "success", "")):
            result.update(status="failed", message="Backup failed; preserve its stage and inspect recovery status",
                          progress_stale=result.get("bytes_done") is not None)
    except (OperatorError, OSError, ValueError, KeyError, TypeError):
        result.update(status="unknown", message="Backup observation unavailable; no completion inferred", progress_stale=True)
    return result


class _Progress(io.TextIOBase):
    def __init__(self, output, summary):
        self.output, self.summary, self.pending = output, summary, ""

    def write(self, text):
        self.output.write(text)
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("phase") in {"manifest", "capture", "backup"}:
                self.summary["phase"] = event["phase"]
                for key in ("run_id", "bytes_done", "files_done"):
                    if key in event:
                        self.summary[key] = event[key]
                _publish(self.summary)
        return len(text)

    def flush(self):
        self.output.flush()


def run(profile, token):
    _require_root()
    if not HEX32.fullmatch(token):
        raise OperatorError("Invalid launch identity")
    with _lifecycle(wait=True):
        reserved = _reservation()
        if not reserved or reserved.get("token") != token:
            raise OperatorError("Backup launch reservation does not match")
        unit = reserved["unit"]
        summary = {"status": "running", "unit": unit, "started_at": recovery.now(), "phase": "manifest"}
        summary.update(invocation_id=os.environ.get("INVOCATION_ID", ""), pid=str(os.getpid()))
        _publish(summary)
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt()
        previous = signal.signal(signal.SIGTERM, interrupted)
        try:
            _busy(profile, ignore_unit=unit, ignore_reservation=True)
            _validate_repository(profile)
            (RUNTIME / "launch.json").unlink()
            with contextlib.redirect_stdout(_Progress(sys.stdout, summary)):
                receipt = recovery.backup(_arguments(profile), recovery.load_policy(profile["policy"]))
            summary.update(status="complete", phase=None, run_id=receipt["id"], snapshot_id=receipt["snapshot_id"],
                           finished_at=receipt["finished_at"], last_backup=receipt["finished_at"])
            for key, native in (("bytes_done", "total_bytes_processed"), ("files_done", "total_files_processed")):
                summary[key] = _number(receipt.get("summary", {}).get(native))
            _publish(summary)
            print(json.dumps(receipt, indent=2), flush=True)
            return 0
        except BaseException:
            summary.update(status="failed", finished_at=recovery.now())
            _publish(summary)
            raise
        finally:
            signal.signal(signal.SIGTERM, previous)


def logs(profile, *, as_json=False):
    previous = None
    while True:
        value = status(profile)
        if as_json:
            rendered = json.dumps(value, sort_keys=True)
        else:
            parts = ["Status: " + value["status"]]
            if value.get("phase"):
                parts.append("Phase: " + value["phase"])
            if value.get("bytes_done") is not None:
                label = "Last reported" if value.get("progress_stale") else "Processed"
                files = f"{value['files_done']:,} files" if value.get("files_done") is not None else "file count unavailable"
                parts.append(f"{label}: {value['bytes_done']:,} bytes / {files}")
            parts.append(value["message"])
            rendered = " | ".join(parts)
        if rendered != previous:
            print(rendered, flush=True)
            previous = rendered
        if value["status"] != "running":
            return
        time.sleep(5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "mount", "start", "logs", "unmount", "eject", "run"])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--token", default="")
    args = parser.parse_args(argv)
    try:
        profile = load_profile()
        if args.action == "run":
            return run(profile, args.token)
        if args.action == "logs":
            logs(profile, as_json=args.json)
            return 0
        if args.action in {"unmount", "eject"}:
            result = detach(profile, power_off=args.action == "eject")
        else:
            result = {"status": status, "mount": mount, "start": start}[args.action](profile)
        print(json.dumps(result) if args.json else result["message"])
        return 0
    except KeyboardInterrupt:
        if args.action == "logs":
            return 0
        print(json.dumps(_result(args.action, "failed", "Operation interrupted; inspect status before retrying")))
        return 130
    except (OperatorError, recovery.RecoveryError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        message = _failure_message(exc)
        print(json.dumps(_result(args.action, "failed", message)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
