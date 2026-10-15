"""Apply verified captures only within an already extracted, offline alternate root.

No database server is contacted or started. PostgreSQL SQL is left for reviewed
manual import. The caller must keep this private restore tree exclusive for the
whole operation; it must never be used as a running system's root.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile


class ApplyError(RuntimeError):
    pass


RUNTIME_RECIPES = {
    "docker-snap-v1": {
        "source": "/var/snap/docker/common/var-lib-docker",
        "entries": [{"path": "volumes/backingFsBlockDev", "kind": "block_device",
                     "action": "daemon-recreates"}],
    },
    "lxd-snap-v1": {
        "source": "/var/snap/lxd/common/lxd",
        "entries": [
            {"path": "shmounts", "kind": "symlink", "action": "recreate-symlink",
             "target": "/var/snap/lxd/common/shmounts/instances"},
            {"path": "unix.socket", "kind": "socket", "action": "systemd-recreates"},
        ],
    },
}


def _runtime_recipe(source, recipe_id):
    if not isinstance(recipe_id, str) or recipe_id not in RUNTIME_RECIPES:
        raise ApplyError("Runtime recipe is unsupported")
    recipe = RUNTIME_RECIPES[recipe_id]
    if str(_original(str(source))) != recipe["source"]:
        raise ApplyError("Runtime recipe does not match its exact source")
    return recipe


def _runtime_receipt(source, receipt):
    if not isinstance(receipt, dict) or set(receipt) != {"recipe", "entries"}:
        raise ApplyError("Runtime receipt is invalid")
    recipe = _runtime_recipe(source, receipt["recipe"])
    entries = receipt["entries"]
    if not isinstance(entries, list) or len(entries) != len(recipe["entries"]):
        raise ApplyError("Runtime receipt must contain exactly its recipe entries")
    for entry, expected in zip(entries, recipe["entries"]):
        if (not isinstance(entry, dict) or type(entry.get("present")) is not bool
                or any(entry.get(key) != value for key, value in expected.items())):
            raise ApplyError("Runtime entry does not match its recipe")
        metadata = {"mode", "uid", "gid", "dev", "inode", "nlink"} if entry["present"] else set()
        if entry["present"] and expected["kind"] == "block_device":
            metadata.add("rdev")
        if set(entry) != set(expected) | {"present"} | metadata:
            raise ApplyError("Runtime entry metadata is invalid")
        if entry["present"]:
            for key in metadata:
                maximum = {"mode": 0o7777, "uid": 2**32 - 2, "gid": 2**32 - 2}.get(key, 2**64 - 1)
                minimum = 1 if key in {"inode", "nlink"} else 0
                if type(entry[key]) is not int or not minimum <= entry[key] <= maximum:
                    raise ApplyError("Runtime entry metadata is invalid")
            if expected["kind"] == "block_device" and entry["rdev"] != entry["dev"]:
                raise ApplyError("Runtime block device does not identify its source filesystem")
    return entries


def inspect_runtime_entries(source: Path, recipe_id: str) -> dict:
    """Observe only exact known runtime entries; payload lookalikes fail closed."""
    recipe = _runtime_recipe(source, recipe_id)
    source = _plain(source)
    if not source.is_dir():
        raise ApplyError("Runtime source must be an existing directory")
    source_device = source.stat().st_dev
    entries = []
    for expected in recipe["entries"]:
        entry = dict(expected, present=False)
        path = source / expected["path"]
        missing = False
        for parent in reversed(path.relative_to(source).parents):
            candidate = source / parent
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                missing = True
                break
            if not stat.S_ISDIR(info.st_mode):
                raise ApplyError("Runtime entry has a file or symlink ancestor")
        if not missing:
            try:
                info = path.lstat()
            except FileNotFoundError:
                missing = True
        if not missing:
            valid_type = {"block_device": stat.S_ISBLK, "socket": stat.S_ISSOCK,
                          "symlink": stat.S_ISLNK}[expected["kind"]]
            if not valid_type(info.st_mode):
                raise ApplyError("Runtime entry has an unexpected type; refusing to omit payload")
            if expected["kind"] == "symlink" and os.readlink(path) != expected["target"]:
                raise ApplyError("Runtime symlink has an unexpected target")
            if expected["kind"] == "block_device" and (info.st_rdev != source_device or info.st_dev != source_device):
                raise ApplyError("Runtime block device does not identify its source filesystem")
            entry.update(present=True, mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid,
                         dev=info.st_dev, inode=info.st_ino, nlink=info.st_nlink)
            if expected["kind"] == "block_device":
                entry["rdev"] = info.st_rdev
        entries.append(entry)
    receipt = {"recipe": recipe_id, "entries": entries}
    _runtime_receipt(source, receipt)
    return receipt


def runtime_exclude_paths(source, receipt):
    """Return only recipe-bound, archive-relative paths, including absent entries."""
    return [str(PurePosixPath(source.name) / entry["path"])
            for entry in _runtime_receipt(source, receipt)]


def _plain(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.resolve() != path:
        raise ApplyError("Restore paths must be absolute and have no symlink ancestors or traversal")
    return path


def _regular(path):
    path = _plain(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ApplyError("Required restore artifact is not a regular file")
    return path


def _json(path):
    try:
        with _regular(path).open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ApplyError("Restore receipt is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise ApplyError("Restore receipt must be an object")
    return value


def _original(value):
    if not isinstance(value, str) or "\0" in value:
        raise ApplyError("Capture source must be a normalized absolute path")
    path = PurePosixPath(value)
    if path.root != "/" or str(path) != value or ".." in path.parts or path == PurePosixPath("/"):
        raise ApplyError("Capture source must be a normalized absolute path below /")
    return path


def _destination(target, source):
    destination = target.joinpath(*source.parts[1:])
    _plain(destination)
    if os.path.lexists(destination):
        raise ApplyError("Capture destination already exists; nothing will be overwritten")
    if not destination.parent.is_dir():
        raise ApplyError("Restore destination parent is missing; extract its filesystem metadata first")
    return destination


def _digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _attributes(entry):
    for key, maximum in (("uid", 2**32 - 2), ("gid", 2**32 - 2), ("mode", 0o7777)):
        if type(entry.get(key)) is not int or not 0 <= entry[key] <= maximum:
            raise ApplyError("Capture ownership or mode metadata is invalid")
    values = entry.get("xattrs", {})
    if not isinstance(values, dict):
        raise ApplyError("Capture extended attributes must be a mapping")
    decoded = {}
    for name, value in values.items():
        if not isinstance(name, str) or not name or "\0" in name or not isinstance(value, str):
            raise ApplyError("Capture extended attribute metadata is invalid")
        try:
            decoded[name] = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ApplyError("Capture extended attribute encoding is invalid") from exc
    return decoded


def _member_path(value):
    path = PurePosixPath(value)
    if not value or "\0" in value or path.is_absolute() or ".." in path.parts or not path.parts:
        raise ApplyError("Tar contains an unsafe member path")
    return path


def _link_target(member):
    value = member.linkname
    if not value or "\0" in value or PurePosixPath(value).is_absolute():
        raise ApplyError("Tar contains an absolute or empty link target")
    parts = list(PurePosixPath(member.name).parent.parts) if member.issym() else []
    for part in PurePosixPath(value).parts:
        if part == "..":
            if not parts:
                raise ApplyError("Tar link escapes its archive root")
            parts.pop()
        elif part != ".":
            parts.append(part)
    return PurePosixPath(*parts)


def _resolve_tar_link(member, members, root):
    """Follow archived links before interpreting subsequent '..' components."""
    pending = list(PurePosixPath(member.name).parent.parts) + list(PurePosixPath(member.linkname).parts)
    resolved, followed = [], 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            if len(resolved) <= 1:
                raise ApplyError("Tar link chain escapes its declared source")
            resolved.pop()
            continue
        if part == ".":
            continue
        resolved.append(part)
        if resolved[0] != root:
            raise ApplyError("Tar link chain escapes its declared source")
        linked = members.get(PurePosixPath(*resolved))
        if linked and linked.issym():
            followed += 1
            if followed > 40 or PurePosixPath(linked.linkname).is_absolute():
                raise ApplyError("Tar link chain is cyclic or absolute")
            resolved.pop()
            pending = list(PurePosixPath(linked.linkname).parts) + pending


def validate_tar(artifact, source, runtime_entries=None):
    """Reject tar layouts the alternate-root applicator cannot restore safely."""
    omitted = [PurePosixPath(path) for path in runtime_exclude_paths(source, runtime_entries)] if runtime_entries is not None else []
    members = {}
    try:

        with tarfile.open(artifact, "r:") as archive:
            for member in archive:
                path = _member_path(member.name)
                if any(path == other or path.is_relative_to(other) for other in omitted):
                    raise ApplyError("Tar contains a declared runtime omission")
                if path.parts[0] != source.name or path in members:
                    raise ApplyError("Tar has duplicate members or members outside its declared source")
                if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                    raise ApplyError("Tar contains unsupported special files")
                if not (0 <= member.uid <= 2**32 - 2 and 0 <= member.gid <= 2**32 - 2
                        and 0 <= member.mode <= 0o7777):
                    raise ApplyError("Tar contains unsupported ownership or mode metadata")
                members[path] = member
    except (OSError, tarfile.TarError) as exc:
        raise ApplyError("Capture tar archive is invalid") from exc
    root = PurePosixPath(source.name)
    if root not in members or not members[root].isdir():
        raise ApplyError("Tar must contain its declared source directory")
    for path in omitted:
        if any(parent in members and not members[parent].isdir() for parent in path.parents):
            raise ApplyError("Tar runtime entry has a file or link ancestor")
    for path, member in members.items():
        for parent in path.parents:
            if parent in members and not members[parent].isdir():
                raise ApplyError("Tar member has a file or link ancestor")
        if member.issym() or member.islnk():
            if member.islnk():


                _member_path(member.linkname)
            linked = _link_target(member)
            if not linked.parts or linked.parts[0] != source.name:
                raise ApplyError("Tar link escapes its declared source")
            if member.islnk() and (linked not in members or not members[linked].isfile()):
                raise ApplyError("Tar hardlink must reference an archived regular file")
            if member.issym():
                _resolve_tar_link(member, members, source.name)


def _preflight(target, capture_dir):
    target = _plain(target)
    if target == Path("/") or not target.is_dir():
        raise ApplyError("Restore target must be an existing alternate root, never /")
    restored = _json(target / "FARM-RESTORE-RECEIPT.json")
    if restored.get("status") != "extracted-and-verified":
        raise ApplyError("Target does not have a verified extraction receipt")
    capture_dir = _plain(capture_dir)
    if capture_dir == target or not capture_dir.is_relative_to(target) or not capture_dir.is_dir():
        raise ApplyError("Capture directory must be strictly inside the restored alternate root")
    manifest = _json(capture_dir / "manifest.json")
    if (manifest.get("schema") != "farm.recovery-captures/1" or manifest.get("status") != "complete"
            or manifest.get("errors") or not isinstance(manifest.get("captures"), list)):
        raise ApplyError("Captures do not have a complete supported manifest")
    jobs, destinations, artifacts = [], [], set()
    for entry in manifest["captures"]:
        if not isinstance(entry, dict) or entry.get("kind") not in {"sqlite", "redis-rdb", "tar", "pg_dumpall"}:
            raise ApplyError("Capture kind is unsupported")
        if "runtime_entries" in entry and (entry["kind"] != "tar" or entry["runtime_entries"] is None):
            raise ApplyError("Runtime receipt is valid only for an explicit tar recipe")
        name = entry.get("artifact")
        if (not isinstance(name, str) or "\0" in name or name in {"", ".", "..", "manifest.json"}
                or Path(name).name != name):
            raise ApplyError("Capture artifact must be a single filename")
        artifact = _regular(capture_dir / name)
        if artifact in artifacts:
            raise ApplyError("Capture artifact is listed more than once")
        artifacts.add(artifact)
        if (type(entry.get("bytes")) is not int or entry["bytes"] <= 0
                or artifact.stat().st_size != entry["bytes"]
                or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or _digest(artifact) != entry["sha256"]):
            raise ApplyError("Capture artifact size or SHA-256 verification failed")
        job = {"entry": entry, "artifact": artifact}
        if entry["kind"] != "pg_dumpall":
            source = _original(entry.get("source"))
            destination = _destination(target, source)
            for other in destinations:
                if destination.is_relative_to(other) or other.is_relative_to(destination):
                    raise ApplyError("Capture destinations overlap")
            destinations.append(destination)
            job.update(source=source, destination=destination)
            if entry["kind"] == "tar":
                if entry.get("archive_parent") != str(source.parent):
                    raise ApplyError("Tar archive parent does not match its declared source")
                validate_tar(artifact, source, runtime_entries=entry.get("runtime_entries"))
                if not Path("/usr/bin/tar").is_file():
                    raise ApplyError("GNU tar is required to apply offline captures")
            else:
                job["xattrs"] = _attributes(entry)
                for suffix in ("-wal", "-shm", "-journal"):
                    if os.path.lexists(str(destination) + suffix):
                        raise ApplyError("Capture destination has a stale database sidecar")
                if entry["kind"] == "sqlite":
                    try:
                        with closing(sqlite3.connect(artifact.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                                raise ApplyError("SQLite capture failed integrity_check")
                    except sqlite3.Error as exc:
                        raise ApplyError("SQLite capture cannot be validated") from exc
        jobs.append(job)
    return target, jobs


def _apply_file(job):
    destination, entry = job["destination"], job["entry"]
    descriptor, name = tempfile.mkstemp(prefix=".farm-restore-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream, job["artifact"].open("rb") as source:
            shutil.copyfileobj(source, stream, length=1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, entry["uid"], entry["gid"], follow_symlinks=False)
        os.chmod(temporary, entry["mode"], follow_symlinks=False)


        for key in os.listxattr(temporary, follow_symlinks=False):
            if key not in job["xattrs"]:
                os.removexattr(temporary, key, follow_symlinks=False)
        for key, value in job["xattrs"].items():
            os.setxattr(temporary, key, value, follow_symlinks=False)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())

        os.link(temporary, destination, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)


def apply_captures(target: Path, capture_dir: Path) -> dict:
    """Validate every capture, then apply missing files within an offline restore tree.

    Validation failure performs no writes. An I/O failure during application can
    leave already applied files; retry from a fresh extraction, never overwrite.
    """
    target, jobs = _preflight(target, capture_dir)
    result = {"schema": "farm.recovery-apply/1", "status": "applied", "target": str(target),
              "applied": [], "manual_imports": [], "runtime_recovery": []}
    for job in jobs:
        entry = job["entry"]
        if entry["kind"] == "pg_dumpall":
            result["manual_imports"].append({"kind": "pg_dumpall", "artifact": str(job["artifact"]),
                                              "status": "import-required"})
            continue


        _destination(target, job["source"])
        try:
            if entry["kind"] == "tar":
                run = subprocess.run(["/usr/bin/tar", "--extract", "--file", str(job["artifact"]),
                                      "--directory", str(job["destination"].parent), "--acls", "--xattrs",
                                      "--xattrs-include=*", "--numeric-owner", "--same-owner",
                                      "--same-permissions", "--delay-directory-restore", "--keep-old-files"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, check=False)
                if run.returncode:
                    raise ApplyError("Tar application failed; the alternate root may be partially restored")
            else:
                _apply_file(job)
        except OSError as exc:
            raise ApplyError("Capture application failed; retry from a fresh alternate-root extraction") from exc
        result["applied"].append({"kind": entry["kind"], "source": entry["source"],
                                  "target": str(job["destination"])})

    for job in jobs:
        receipt = job["entry"].get("runtime_entries")
        if receipt is None:
            continue
        for runtime in _runtime_receipt(job["source"], receipt):
            action = {"source": str(job["source"]), "path": runtime["path"],
                      "kind": runtime["kind"], "action": runtime["action"],
                      "present_at_capture": runtime["present"], "service_activation": "manual"}
            if runtime["kind"] == "symlink" and runtime["present"]:
                destination = job["destination"] / runtime["path"]
                _plain(destination.parent)
                if not destination.parent.is_dir() or os.path.lexists(destination):
                    raise ApplyError("Runtime symlink parent is unsafe or destination already exists")
                try:
                    os.symlink(runtime["target"], destination)
                    os.chown(destination, runtime["uid"], runtime["gid"], follow_symlinks=False)
                except OSError as exc:
                    raise ApplyError("Runtime symlink application failed; retry from a fresh extraction") from exc
                action["status"] = "recreated"
            else:
                action["status"] = "absent-at-capture" if runtime["kind"] == "symlink" else "activation-required"
            result["runtime_recovery"].append(action)
    if result["manual_imports"]:
        result["status"] = "applied-imports-pending"
    return result
