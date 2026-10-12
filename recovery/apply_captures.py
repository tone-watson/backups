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


def validate_tar(artifact, source):
    """Reject tar layouts the alternate-root applicator cannot restore safely."""
    members = {}
    try:
        # Capture writes uncompressed GNU/PAX tar archives; no auto-detection.
        with tarfile.open(artifact, "r:") as archive:
            for member in archive:
                path = _member_path(member.name)
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
    for path, member in members.items():
        for parent in path.parents:
            if parent in members and not members[parent].isdir():
                raise ApplyError("Tar member has a file or link ancestor")
        if member.issym() or member.islnk():
            if member.islnk():
                # GNU tar records hardlink names relative to the archive root;
                # unlike symlinks, parent traversal is never needed here.
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
                validate_tar(artifact, source)
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
        # New files can inherit a parent's default ACL; retain only attributes
        # recorded for the captured source, not incidental restore-tree ACLs.
        for key in os.listxattr(temporary, follow_symlinks=False):
            if key not in job["xattrs"]:
                os.removexattr(temporary, key, follow_symlinks=False)
        for key, value in job["xattrs"].items():
            os.setxattr(temporary, key, value, follow_symlinks=False)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        # A hardlink publishes without replacing any destination created since preflight.
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
              "applied": [], "manual_imports": []}
    for job in jobs:
        entry = job["entry"]
        if entry["kind"] == "pg_dumpall":
            result["manual_imports"].append({"kind": "pg_dumpall", "artifact": str(job["artifact"]),
                                              "status": "import-required"})
            continue
        # Recheck path boundaries immediately before writing; callers still must
        # exclude concurrent access to the offline restore tree.
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
    if result["manual_imports"]:
        result["status"] = "applied-imports-pending"
    return result
