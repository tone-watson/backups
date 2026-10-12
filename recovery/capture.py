"""Stage consistent application state; this module performs no work on import.

Policy keys: sqlite_roots, sqlite_paths; postgres/redis dictionaries with enabled,
version/user/data_path (PostgreSQL) or host/port/data_path (Redis); offline_stores
entries with id, path, units, and an optional exact runtime_recipe.
timeout_seconds defaults to 900 for captures
and state queries. Service stop/start waits have no independent client timeout:
systemd owns their unit deadlines, execution phases and dependencies. An infinite
unit deadline can therefore wait until operator intervention. Missing timing
properties (including simple fixtures) also leave systemd in control. The caller
must reject status=incomplete. Native stderr is deliberately absent from receipts.
PostgreSQL logical dumps do not replace/exclude their raw cluster directory:
local cluster configuration is not reproduced by pg_dumpall. Prefer the guarded
offline tar for a complete cluster directory backup. Captures are individually
consistent, not one transaction across different databases/applications.
"""
from contextlib import closing
import base64
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import time


ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "TZ": "UTC"}
PROTECTED_UNITS = {
    "ssh.service", "sshd.service", "tailscaled.service", "tailscale.service",
    "NetworkManager.service", "networking.service", "systemd-networkd.service",
    "systemd-resolved.service", "dbus.service", "cloudflared.service",
    "wpa_supplicant.service", "systemd-logind.service",
}
PROTECTED_BASES = {unit.rsplit('.', 1)[0] for unit in PROTECTED_UNITS}
UNIT_TIMEOUT_PROPERTIES = ("TimeoutStartUSec", "TimeoutStopUSec", "TimeoutUSec")


class CaptureError(RuntimeError):
    pass


def _run(argv, *, timeout, stdout=None):
    try:
        result = subprocess.run(argv, stdout=stdout if stdout is not None else subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, env=ENV, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureError(f"{Path(argv[0]).name}: {type(exc).__name__}") from None
    if result.returncode:
        raise CaptureError(f"{Path(argv[0]).name}: exit {result.returncode}")
    return result.stdout or b""


def _write_receipt(stage, receipt):
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=stage)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, stage / "manifest.json")
        descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _plain_path(path):
    """Reject lexical traversal and symlink ancestors before privileged access."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=False) != path:
        raise CaptureError("path must be absolute without symlinks or parent traversal")
    return path


def _source(path, stage, *, directory=False):
    path = _plain_path(path)
    try:
        info = path.stat()
    except OSError as exc:
        raise CaptureError(f"source unavailable: {type(exc).__name__}") from None
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise CaptureError("source has an unsupported file type")
    if stage.resolve().is_relative_to(path.resolve()):
        raise CaptureError("capture stage must not be inside its source")
    return path, {"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}


def _file_xattrs(path):
    # These values can contain private application metadata; only the private
    # staged receipt is allowed to retain them. ACLs are represented as xattrs.
    try:
        return {name: base64.b64encode(os.getxattr(path, name, follow_symlinks=False)).decode("ascii")
                for name in os.listxattr(path, follow_symlinks=False)}
    except OSError as exc:
        raise CaptureError(f"source extended attributes unavailable: {type(exc).__name__}") from None


def _promote(stage, temporary, filename, *, kind, source, restore, metadata):
    if not temporary.is_file() or temporary.is_symlink() or temporary.stat().st_size == 0:
        raise CaptureError("capture produced no regular nonempty artifact")
    digest = hashlib.sha256()
    with temporary.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    size = temporary.stat().st_size
    os.replace(temporary, stage / filename)
    return {"kind": kind, "source": str(source), "artifact": filename,
            "sha256": digest.hexdigest(), "bytes": size, "restore": restore, **metadata}


def _protected(unit):
    return unit.rsplit('.', 1)[0] in PROTECTED_BASES or unit.startswith(("netplan-", "wpa_supplicant@"))


def _unit_state(unit, timeout):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*\.(service|socket)", unit):
        raise CaptureError("offline store requires explicit service or socket unit names")
    if _protected(unit):
        raise CaptureError("automatic capture cannot control SSH/network/Tailscale units")
    output = _run(["/usr/bin/systemctl", "show", unit, "--no-pager",
                   "--property=Id,Names,LoadState,ActiveState,SubState,MainPID,ControlPID,"
                   + ",".join(UNIT_TIMEOUT_PROPERTIES)], timeout=timeout)
    fields = dict(line.split("=", 1) for line in output.decode("utf-8", "replace").splitlines() if "=" in line)
    names = set(fields.get("Names", "").split()) | {fields.get("Id", unit)}
    if any(_protected(name) for name in names):
        raise CaptureError("offline unit aliases a protected SSH/network/Tailscale unit")
    if fields.get("LoadState") != "loaded":
        raise CaptureError("offline store unit is not loaded")
    return fields


def _is_inactive(unit, state):
    default_pid = "0" if unit.endswith(".socket") else None
    return (state.get("ActiveState") == "inactive" and state.get("SubState") == "dead"
            and (state.get("MainPID") or default_pid) == "0"
            and (state.get("ControlPID") or default_pid) == "0")


def _is_active(unit, state):
    substate = "listening" if unit.endswith(".socket") else "running"
    return state.get("ActiveState") == "active" and state.get("SubState") == substate


def _inactive(unit, timeout):
    state = _unit_state(unit, timeout)
    if not _is_inactive(unit, state):
        raise CaptureError("offline store unit is not inactive/dead with zero PIDs")


def _control_unit(unit, action, initial_state, stage, receipt):
    """Wait for systemd's job result without imposing the native-capture timeout.

    Unit deadlines can cover multiple command phases and may be extended by a
    notify service or dependency jobs. Killing only the systemctl client early
    leaves that manager-owned job running, so no finite client deadline is safe.
    Raw limits are evidence, not a guessed upper bound for the complete job.
    """
    if action not in {"stop", "start"}:
        raise CaptureError("unsupported service control action")
    wait = {"unit": unit, "action": action, "status": "waiting",
            "deadline_owner": "systemd", "client_timeout_seconds": None,
            "initial_manager_timeouts": {key: initial_state[key] for key in UNIT_TIMEOUT_PROPERTIES
                                         if key in initial_state}}
    receipt["service_waits"].append(wait)
    _write_receipt(stage, receipt)
    started = time.monotonic()
    try:
        _run(["/usr/bin/systemctl", action, unit], timeout=None)
        wait["status"] = "job_returned_success"
    except BaseException as exc:
        wait["status"] = "failed" if isinstance(exc, Exception) else "interrupted"
        raise
    finally:
        wait["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _write_receipt(stage, receipt)


def _sqlite_paths(policy):
    paths = list(policy.get("sqlite_paths", []))
    for value in policy.get("sqlite_roots", []):
        root = _plain_path(value)
        if not root.is_dir():
            raise CaptureError("SQLite root must be an existing absolute directory")
        def failed(_error):
            raise CaptureError("SQLite discovery encountered an unreadable directory")
        for directory, children, files in os.walk(root, followlinks=False, onerror=failed):
            if any((Path(directory) / child).is_symlink() for child in children):
                raise CaptureError("SQLite discovery requires explicit non-symlink directories")
            paths.extend(str(Path(directory) / name) for name in files
                         if name.lower().endswith((".db", ".sqlite", ".sqlite3")))
    return sorted(set(paths))


def _capture_sqlite(source, stage, index, timeout):
    source, metadata = _source(source, stage)
    metadata["xattrs"] = _file_xattrs(source)
    filename = f"sqlite-{index:04d}-{hashlib.sha256(str(source).encode()).hexdigest()[:12]}.db"
    temporary = stage / ("." + filename + ".partial")
    deadline = time.monotonic() + timeout
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise CaptureError("SQLite backup timed out")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as origin:
            with closing(sqlite3.connect(temporary)) as destination:
                origin.backup(destination, pages=256, progress=progress, sleep=0.05)
                if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise CaptureError("SQLite backup failed integrity_check")
        result = _promote(stage, temporary, filename, kind="sqlite", source=source,
                          restore="Restore to the original path with recorded ownership and xattrs; target must be empty and have no stale -wal/-shm/-journal files.",
                          metadata=metadata)
        return result, [str(source)] + [str(source) + suffix for suffix in ("-wal", "-shm", "-journal")]
    finally:
        temporary.unlink(missing_ok=True)


def _capture_postgres(config, stage, timeout):
    version, user = str(config.get("version", "14")), config.get("user", "postgres")
    if not re.fullmatch(r"[0-9]+", version) or not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise CaptureError("invalid PostgreSQL version or system user")
    source, metadata = _source(config.get("data_path", "/var/lib/postgresql/14/main"), stage, directory=True)
    port = int(config.get("port", 5432))
    if not 0 < port < 65536:
        raise CaptureError("invalid PostgreSQL port")
    def verify_cluster():
        value = _run(["/usr/sbin/runuser", "-u", user, "--", f"/usr/lib/postgresql/{version}/bin/psql",
                      "--no-password", "--no-psqlrc", "--host=/var/run/postgresql", f"--port={port}",
                      "--dbname=template1", "--tuples-only", "--no-align", "--command=SHOW data_directory"],
                     timeout=timeout).decode("utf-8", "strict").strip()
        if value != str(source):
            raise CaptureError("PostgreSQL live data_directory does not match the configured source")
    verify_cluster()
    temporary = stage / ".postgres.sql.partial"
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            _run(["/usr/sbin/runuser", "-u", user, "--", f"/usr/lib/postgresql/{version}/bin/pg_dumpall",
                  "--no-password", "--host=/var/run/postgresql", f"--port={port}"], timeout=timeout, stdout=stream)
        verify_cluster()
        metadata.update(target=str(source), postgres_version=version, raw_source_retained=True)
        result = _promote(stage, temporary, "postgres-cluster.sql", kind="pg_dumpall", source="cluster",
                          restore="Logical SQL only: install compatible PostgreSQL, preserve local configuration separately, then review and restore roles/globals and databases with psql. Raw cluster files remain unvalidated supplemental payload; never mix them into a logical restore or copy SQL into the data directory.",
                          metadata=metadata)
        return result, []
    finally:
        temporary.unlink(missing_ok=True)


def _verify_redis_persistence(config, source, timeout):
    """Read-only validation, also run before any offline-store service stops."""
    host, port = config.get("host", "127.0.0.1"), int(config.get("port", 6379))
    if not ipaddress.ip_address(host).is_loopback or not 0 < port < 65536:
        raise CaptureError("Redis capture requires an explicit loopback address and valid port")
    method = config.get("persistence_verification", "config-command")
    if method == "disabled-config":
        spec = importlib.util.spec_from_file_location(
            "farm_redis_state_verification", Path(__file__).with_name("redis_state.py"))
        if spec is None or spec.loader is None:
            raise CaptureError("Redis state verifier is unavailable")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        try:
            return helper.verify_disabled_config(config, source, _run, timeout)
        except helper.RedisVerificationError as exc:
            raise CaptureError(str(exc)) from None
    if method != "config-command":
        raise CaptureError("Unsupported Redis persistence verification method")
    values = {}
    for key in ("dir", "dbfilename", "appendonly"):
        lines = _run(["/usr/bin/redis-cli", "-h", host, "-p", str(port), "--raw", "CONFIG", "GET", key],
                     timeout=timeout).decode("utf-8", "strict").splitlines()
        if len(lines) != 2 or lines[0] != key:
            raise CaptureError(f"Redis CONFIG GET {key} could not be verified")
        values[key] = lines[1]
    if values["appendonly"] != "no":
        raise CaptureError("Redis AOF persistence requires a separately reviewed capture/restore policy")
    if Path(values["dir"]) / values["dbfilename"] != source:
        raise CaptureError("Redis live persistence path does not match the configured source")
    return {"method": method, **values}


def _capture_redis(config, stage, timeout):
    source, metadata = _source(config.get("data_path", "/var/lib/redis/dump.rdb"), stage)
    metadata["xattrs"] = _file_xattrs(source)
    host, port = config.get("host", "127.0.0.1"), int(config.get("port", 6379))
    verified = _verify_redis_persistence(config, source, timeout)
    temporary = stage / ".redis.rdb.partial"
    try:
        _run(["/usr/bin/redis-cli", "-h", host, "-p", str(port), "--rdb", str(temporary)], timeout=timeout)
        _run(["/usr/bin/redis-check-rdb", str(temporary)], timeout=timeout)
        if _verify_redis_persistence(config, source, timeout) != verified:
            raise CaptureError("Redis persistence identity changed during capture")
        metadata["persistence_verification"] = verified
        result = _promote(stage, temporary, "redis.rdb", kind="redis-rdb", source=source,
                          restore="Restore the validated RDB to the configured persistence path while Redis is stopped; reconcile persistence settings and queued work before enabling workers.",
                          metadata=metadata)
        return result, [str(source)]
    finally:
        temporary.unlink(missing_ok=True)


def _archive_helper():
    # Load the colocated helper directly, independent of CLI/test sys.path.
    spec = importlib.util.spec_from_file_location(
        "farm_capture_archive_validation", Path(__file__).with_name("apply_captures.py"))
    if spec is None or spec.loader is None:
        raise CaptureError("shared restore archive validator is unavailable")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def _runtime_entries(store, source, helper):
    if "runtime_recipe" not in store:
        return None
    try:
        return helper.inspect_runtime_entries(source, store["runtime_recipe"])
    except helper.ApplyError:
        raise CaptureError("offline runtime entries do not match the reviewed recovery recipe") from None


def _capture_offline(store, stage, timeout):
    identity = store["id"]
    source, metadata = _source(store["path"], stage, directory=True)
    for unit in store["units"]:
        _inactive(unit, timeout)
    helper = _archive_helper()
    runtime = _runtime_entries(store, source, helper)
    omit = helper.runtime_exclude_paths(source, runtime) if runtime is not None else []
    temporary = stage / ("." + identity + ".tar.partial")
    try:
        _run(["/usr/bin/tar", "--acls", "--xattrs", "--numeric-owner", "--sparse",
              "--anchored", "--no-wildcards", *["--exclude=" + path for path in omit],
              "-cf", str(temporary), "-C", str(source.parent), "--", source.name], timeout=timeout)
        for unit in store["units"]:
            _inactive(unit, timeout)
        if _runtime_entries(store, source, helper) != runtime:
            raise CaptureError("offline runtime entries changed during capture")
        # A successful tar command alone does not establish that our guarded
        # alternate-root restoration can safely apply the resulting archive.
        try:
            helper.validate_tar(temporary, source, runtime_entries=runtime)
        except helper.ApplyError:
            raise CaptureError("offline archive is unsupported by guarded restore; a reviewed manual recovery recipe is required") from None
        metadata.update(units=list(store["units"]), archive_parent=str(source.parent))
        if runtime is not None:
            metadata["runtime_entries"] = runtime
        result = _promote(stage, temporary, identity + ".tar", kind="tar", source=source,
                          restore="Keep recorded services stopped; extract with ACLs/xattrs/numeric ownership into the original parent and an empty target, then validate before starting services.",
                          metadata=metadata)
        return result, [str(source)]
    finally:
        temporary.unlink(missing_ok=True)


def capture_state(policy, stage: Path, *, quiesce=False) -> dict:
    """Capture configured stores into an empty private stage; return a truthful receipt.

    With quiesce=True, only explicitly listed, initially active offline-store
    services are stopped and restored in finally, sockets before services and
    reverse stop order within each category. Before/after state guards do not
    replace an operator-exclusive maintenance window. Forced
    process termination or host loss still requires operator service recovery.
    """
    stage = _plain_path(stage)
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(stage.iterdir()):
        raise CaptureError("capture stage must be empty")
    stage.chmod(0o700)
    receipt = {"schema": "farm.recovery-captures/1", "status": "incomplete",
               "captures": [], "exclusions": [], "errors": [], "service_restoration": [],
               "initial_unit_states": {}, "service_actions": [], "service_waits": []}
    _write_receipt(stage, receipt)
    stopped = []
    interrupted = None
    timeout = 900
    try:
        timeout = float(policy.get("timeout_seconds", 900))
        if not 0 < timeout <= 86400:
            raise CaptureError("capture timeout must be between 0 and 86400 seconds")
        stores = policy.get("offline_stores", [])
        identities, units = set(), []
        for store in stores:
            identity = store.get("id", "")
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", identity) or identity in identities:
                raise CaptureError("offline store IDs must be unique simple names")
            identities.add(identity)
            if not store.get("units") or not isinstance(store["units"], list):
                raise CaptureError("offline store requires explicit writer units")
            source, _ = _source(store["path"], stage, directory=True)
            if "runtime_recipe" in store:
                _runtime_entries(store, source, _archive_helper())
            for unit in store["units"]:
                if unit not in units:
                    units.append(unit)
        initial = {unit: _unit_state(unit, timeout) for unit in units}
        receipt["initial_unit_states"] = initial
        _write_receipt(stage, receipt)
        for unit, state in initial.items():
            if _is_active(unit, state):
                if not quiesce:
                    raise CaptureError("offline store is active; an explicit maintenance window is required")
            elif not _is_inactive(unit, state):
                raise CaptureError("offline store unit is in an unsupported transitional/failed state")
        if policy.get("redis", {}).get("enabled"):
            redis_config = policy["redis"]
            redis_source, _ = _source(redis_config.get("data_path", "/var/lib/redis/dump.rdb"), stage)
            _verify_redis_persistence(redis_config, redis_source, timeout)
        for unit, state in initial.items():
            if state.get("ActiveState") == "active":
                # Even a failed stop may have changed the original running state.
                stopped.append(unit)
                action = {"unit": unit, "action": "stop", "status": "intended"}
                receipt["service_actions"].append(action)
                _write_receipt(stage, receipt)
                _control_unit(unit, "stop", initial[unit], stage, receipt)
                _inactive(unit, timeout)
                action["status"] = "confirmed"
                _write_receipt(stage, receipt)
        jobs = [("sqlite", lambda value=value, index=index: _capture_sqlite(value, stage, index, timeout))
                for index, value in enumerate(_sqlite_paths(policy))]
        for key, method in (("postgres", _capture_postgres), ("redis", _capture_redis)):
            if policy.get(key, {}).get("enabled"):
                jobs.append((key, lambda key=key, method=method: method(policy[key], stage, timeout)))
        jobs.extend((store["id"], lambda store=store: _capture_offline(store, stage, timeout)) for store in stores)
        for name, job in jobs:
            try:
                capture, exclusions = job()
                receipt["captures"].append(capture)
                receipt["exclusions"].extend(exclusions)
            except Exception as exc:
                # Exceptions may contain database values or native stderr. Keep only controlled messages.
                receipt["errors"].append({"capture": name, "error": str(exc) if isinstance(exc, CaptureError) else type(exc).__name__})
    except Exception as exc:
        receipt["errors"].append({"capture": "preflight", "error": str(exc) if isinstance(exc, CaptureError) else type(exc).__name__})
    except BaseException as exc:
        receipt["errors"].append({"capture": "interruption", "error": "capture interrupted"})
        interrupted = exc
    finally:
        # Socket-activated daemons need their listening descriptors available
        # before startup, even when their unit does not declare that dependency.
        restore_units = [unit for suffix in (".socket", ".service")
                         for unit in reversed(stopped) if unit.endswith(suffix)]
        for unit in restore_units:
            try:
                action = {"unit": unit, "action": "start", "status": "intended"}
                receipt["service_actions"].append(action)
                _write_receipt(stage, receipt)
                _control_unit(unit, "start", initial[unit], stage, receipt)
                state = _unit_state(unit, timeout)
                if not _is_active(unit, state):
                    raise CaptureError("unit did not return to its original active substate")
                action["status"] = "confirmed"
                receipt["service_restoration"].append({"unit": unit, "status": "restored"})
            except Exception as exc:
                receipt["errors"].append({"capture": "service_restoration", "unit": unit,
                                          "error": str(exc) if isinstance(exc, CaptureError) else type(exc).__name__})
            except BaseException as exc:
                receipt["errors"].append({"capture": "service_restoration", "unit": unit,
                                          "error": "service restoration interrupted; inspect durable action journal"})
                interrupted = exc
        receipt["exclusions"] = sorted(set(receipt["exclusions"]))
        receipt["status"] = "incomplete" if receipt["errors"] else "complete"
        _write_receipt(stage, receipt)
    if interrupted is not None:
        raise interrupted
    return receipt
