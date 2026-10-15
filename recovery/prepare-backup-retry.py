#!/usr/bin/python3
"""Inspect one reviewed failed backup; --execute preserves its local capture stage.

No backup start, service control, source scan, repair, repository update or
payload copy is performed. The existing encrypted repository is authenticated
and its one pending snapshot is inspected without taking a Restic write lock.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys

RUN_ID = "3beac83e7baa422c842a159dcf41731a"
PRIOR_RUN_ID = "5ad7ea30773c4772b438b89e21b1f524"
UNIT = "farm-recovery-first-backup-retry1.service"
BAD_SOURCE = Path("/home/gradywoodruff/.vscode-server/extensions/"
                  "rvest.vs-code-prettier-eslint-6.0.0/coverage/lcov-report/dist/extension.js.html")
BASELINE = {"media_errors": 26, "error_log_entries": 123, "unsafe_shutdowns": 16}
MAX_METADATA = 16 * 1024 * 1024
PREPARATION = "retry-preparation.json"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def private(path, directory=False):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and ".." not in path.parts and path.resolve() == path,
            "Metadata must use an absolute, non-symlinked path")
    require(info.st_uid == os.geteuid() and not stat.S_IMODE(info.st_mode) & 0o077,
            "Metadata must be private and owned by the invoking user")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            "Unexpected metadata type")
    if not directory:
        require(info.st_nlink == 1, "Hard-linked metadata is not allowed")
    return path


def read_json(path):
    path = private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        require(os.fstat(stream.fileno()).st_size <= MAX_METADATA, "Metadata exceeds the bounded read limit")
        data = stream.read(MAX_METADATA + 1)
    require(len(data) <= MAX_METADATA, "Metadata exceeds the bounded read limit")
    return json.loads(data)


@contextmanager
def existing_lock(path):
    """Hold an existing lock without creating or changing any drive files."""
    private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077 and info.st_nlink == 1, "Unsafe existing recovery lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("A recovery operation holds an existing lock") from None
        yield
    finally:
        os.close(fd)


def unit_state(unit, operator):
    require(unit == UNIT, "Unexpected recovery unit")
    output = operator._command(["/usr/bin/systemctl", "show", unit, "--no-pager",
                                "--property=Id,LoadState,ActiveState,SubState,MainPID,ControlPID,Job,"
                                "Result,ExecMainCode,ExecMainStatus"], timeout=30)
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    require(fields.get("Id") == unit and fields.get("LoadState") == "loaded"
            and fields.get("Job") in {"", "0"}, "Failed unit identity or pending job differs from reviewed attempt")
    require(tuple(fields.get(key) for key in ("ActiveState", "SubState", "MainPID", "ControlPID"))
            == ("failed", "failed", "0", "0") and fields.get("Result") == "exit-code"
            and fields.get("ExecMainCode") in {"1", "exited"} and fields.get("ExecMainStatus") == "1",
            "The reviewed failed backup unit is not fully stopped with its original failure")
    return fields


def timestamp(value):
    """Compare RFC3339 timestamps exactly, including Restic's nanoseconds."""
    require(isinstance(value, str), "Missing metadata timestamp")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
                         r"(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value, re.ASCII)
    require(match is not None, "Invalid metadata timestamp; expected timezone-qualified RFC3339")
    seconds, fraction, zone = match.groups()
    require(zone == "Z" or (int(zone[1:3]) < 24 and int(zone[4:6]) < 60),
            "Invalid metadata timestamp timezone")
    try:
        whole = dt.datetime.fromisoformat(seconds + ("+00:00" if zone == "Z" else zone))
    except (ValueError, OverflowError):
        raise RuntimeError("Invalid metadata timestamp calendar value") from None


    return whole, int((fraction or "").ljust(9, "0"))


def failure_message(exc):
    if isinstance(exc, RuntimeError):
        return str(exc)

    message, frame = type(exc).__name__, exc.__traceback__
    while frame is not None:
        code = frame.tb_frame.f_code
        if code.co_filename == __file__:
            message = f"{type(exc).__name__} in {code.co_name} ({Path(__file__).name}:{frame.tb_lineno})"
        frame = frame.tb_next
    return message


def capture_receipt(current, policy):
    private(current / "captures", directory=True)
    captured = read_json(current / "captures" / "manifest.json")
    require(captured == read_json(current / "capture.json"), "Capture receipts differ")
    require(captured.get("schema") == "farm.recovery-captures/1" and captured.get("status") == "complete"
            and captured.get("errors") == [], "Consistent captures were not all completed")
    initial = captured.get("initial_unit_states", {})
    require(set(initial) == {unit for store in policy.get("offline_stores", []) for unit in store["units"]},
            "Initial service inventory differs from the reviewed capture policy")
    active = {unit for unit, value in initial.items() if value.get("ActiveState") == "active"}
    actions = captured.get("service_actions", [])
    require(len(actions) == 2 * len(active) and all(action.get("status") == "confirmed" for action in actions)
            and {(action.get("unit"), action.get("action")) for action in actions}
            == {(unit, action) for unit in active for action in ("stop", "start")},
            "Capture service actions were not all confirmed")
    restored = captured.get("service_restoration", [])
    require(len(restored) == len(active) and {row.get("unit") for row in restored} == active
            and all(row.get("status") == "restored" for row in restored), "Service restoration was incomplete")


def prepare(*, recovery, operator, policy, profile, execute=False, query=unit_state):
    require(policy.get("mode") == "server" and policy.get("state_dir") == profile.get("state_dir"),
            "Expected the enrolled server recovery policy")
    state = private(Path(profile["state_dir"]), directory=True)
    drive = Path(profile["mountpoint"])
    destination = state / ("failed-" + RUN_ID)
    with operator._lifecycle():
        operator._busy(profile)
        operator._validate_repository(profile)


        with existing_lock(drive / ".farm-recovery.lock"), existing_lock(state / "operation.lock"):
            query(UNIT, operator)

            try:
                BAD_SOURCE.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("The reviewed unreadable coverage path exists again")
            require(not os.path.lexists(destination), "Preserved stage already exists; do not repeat this preparation")
            require(read_json(state / ".farm-recovery-state.json").get("schema") == "farm.recovery-state/1",
                    "Unknown recovery state marker")
            receipts = private(state / "receipts", directory=True)


            with os.scandir(receipts) as entries:
                names = []
                for entry in entries:
                    names.append(entry.name)
                    require(len(names) <= 2, "Unexpected or newer recovery run receipts")
            require(set(names) == {PRIOR_RUN_ID + ".json", RUN_ID + ".json"},
                    "Unexpected or newer recovery run receipts")
            previous = read_json(receipts / (PRIOR_RUN_ID + ".json"))
            run = read_json(receipts / (RUN_ID + ".json"))
            require(run.get("schema") == "farm.recovery-run/1" and run.get("id") == RUN_ID
                    and run.get("status") == "incomplete" and run.get("error") == "RecoveryError"
                    and not run.get("snapshot_id"), "Failed run receipt differs from the reviewed attempt")
            started, finished = timestamp(run.get("started_at")), timestamp(run.get("finished_at"))
            require(previous.get("schema") == "farm.recovery-run/1" and previous.get("id") == PRIOR_RUN_ID
                    and previous.get("status") == "incomplete" and previous.get("error") == "RecoveryError"
                    and timestamp(previous.get("finished_at")) < started < finished,
                    "Recovery run chronology differs from the reviewed attempts")
            current = private(state / "current", directory=True)
            require(current.stat().st_dev == state.stat().st_dev, "Capture stage is on another filesystem")
            require(not os.path.lexists(current / PREPARATION), "Capture stage already has preparation evidence")
            require(read_json(current / "policy.json") == policy, "Saved capture policy differs from current policy")
            capture_receipt(current, policy)
            selection = read_json(current / "selection.json")
            roots = selection.get("roots")
            require(isinstance(roots, list) and roots and all(isinstance(root, str) for root in roots)
                    and len(set(roots)) == len(roots), "Invalid saved source selection")
            require(all(str(recovery.absolute(root)) == root for root in roots)
                    and set(policy["source_roots"]).issubset(roots)
                    and isinstance(selection.get("excluded_exact_paths"), list), "Saved source selection is inconsistent")
            opened_drive, engine = recovery.open_repository(operator._arguments(profile), policy)
            require(opened_drive == drive, "Authenticated repository drive differs from enrolled drive")
            snapshots = engine.json(["snapshots", "--no-lock", "--tag", "farm-recovery,run-" + RUN_ID])
            require(isinstance(snapshots, list) and len(snapshots) == 1, "No unique pending snapshot for the reviewed run")
            snapshot = snapshots[0]
            tags, paths = snapshot.get("tags", []), snapshot.get("paths", [])
            require(isinstance(snapshot.get("id"), str) and re.fullmatch(r"[a-f0-9]{64}", snapshot["id"]),
                    "Pending snapshot has no full identity")
            require({"farm-recovery", "pending", "run-" + RUN_ID}.issubset(tags)
                    and "farm-recovery-complete" not in tags
                    and snapshot.get("hostname") == policy.get("host_id", "farm-server"),
                    "Snapshot is not the reviewed host's incomplete recovery attempt")
            require(isinstance(paths, list) and len(paths) == len(roots) + 1
                    and set(paths) == set(roots + [str(current)])
                    and started <= timestamp(snapshot.get("time")) <= finished,
                    "Pending snapshot source selection or chronology differs from local evidence")
            evidence = {"schema": "farm.recovery-retry-preparation/1", "run_id": RUN_ID, "unit": UNIT,
                        "prepared_at": recovery.now(), "pending_snapshot_id": snapshot["id"],
                        "original_stage": str(current), "preserved_path": str(destination),
                        "smart_baseline": {"source": "user-reported", "reported_date": "2026-10-13",
                                           "collected_at": None, "device": "/dev/nvme0n1", **BASELINE},
                        "known_unreadable_path_absent": str(BAD_SOURCE), "backup_started": False}
            if execute:
                os.rename(current, destination)
                try:
                    recovery.write_json(destination / PREPARATION, evidence)
                except Exception:
                    raise RuntimeError("Stage was preserved, but its preparation evidence could not be saved; "
                                       "inspect the preserved stage before starting another backup") from None
                for path in (destination, state):
                    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            return {"status": "preserved-for-retry" if execute else "checks-passed-no-changes", "run_id": RUN_ID,
                    "pending_snapshot_id": snapshot["id"], "preserved_path": str(destination),
                    "backup_started": False, "reuse_existing_repository_ready": True,
                    "note": "Pending snapshot remains incomplete and has not been modified"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="preserve the reviewed stage by rename after all checks pass")
    args = parser.parse_args(argv)
    require(os.geteuid() == 0, "Run with sudo to inspect protected metadata")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import farm_recovery as recovery
    import farm_recovery_operator as operator
    profile = operator.load_profile()
    print(json.dumps(prepare(recovery=recovery, operator=operator, policy=recovery.load_policy(profile["policy"]),
                             profile=profile, execute=args.execute), indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit("STOPPED: " + failure_message(exc))
