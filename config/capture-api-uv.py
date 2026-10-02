"""Back up committed API recovery recipes and verified wheels, without imports."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

API_ROOT = Path("/srv/farm/sys/api")
RECIPES = (
    ".python-version", "scripts/runtime.sh", "scripts/runtime.py", "deploy/runtime",
    "deploy/uv", "deploy/systemd", "deploy/uv-preview", "lib/testing",
    "docs/deployment",
)


def digest(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(block)
    return result.hexdigest()


def regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Expected regular file: " + str(path))
    return os.fdopen(fd, "rb")


def directory(path):
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Expected real backup directory: " + str(path))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Expected real backup directory: " + str(path))
    path.chmod(0o700)


def write_bytes(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def copy_wheel(source, destination, expected_sha, expected_bytes):
    """Verify bytes; reuse only an independent file, never a source hardlink."""
    with regular(source) as input_file:
        info = os.fstat(input_file.fileno())
        if info.st_size != expected_bytes or digest(input_file) != expected_sha:
            raise ValueError("Wheel source hash/size mismatch: " + source.name)
        if os.path.lexists(destination):
            with regular(destination) as old:
                saved = os.fstat(old.fileno())
                if (saved.st_nlink == 1 and saved.st_uid == os.geteuid() and saved.st_gid == os.getegid()
                        and (saved.st_dev, saved.st_ino) != (info.st_dev, info.st_ino)
                        and saved.st_size == expected_bytes and digest(old) == expected_sha):
                    os.fchmod(old.fileno(), 0o600)
                    return False
        fd, temporary = tempfile.mkstemp(prefix=".wheel-", dir=destination.parent)
        try:
            input_file.seek(0)
            with os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
            with regular(temporary) as output:
                if os.fstat(output.fileno()).st_size != expected_bytes or digest(output) != expected_sha:
                    raise ValueError("Copied wheel failed verification: " + source.name)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return True


def git(repo, *args):
    return subprocess.check_output(
        ["/usr/bin/git", "-c", "safe.directory=" + str(repo), "-C", str(repo), *args],
        timeout=60,
    )


def capture(repo, destination):
    directory(destination)
    directory(destination / "wheelhouse")
    write_bytes(destination / "capture.json", b'{"status":"incomplete"}\n')
    revision = git(repo, "rev-parse", "HEAD").decode().strip()
    manifest_bytes = git(repo, "show", revision + ":deploy/runtime/artifacts.json")
    manifest = json.loads(manifest_bytes)
    lock = git(repo, "show", revision + ":deploy/runtime/requirements.lock")
    bundle = Path(manifest["artifact_bundle"])
    if not bundle.is_absolute() or not bundle.is_dir() or bundle.is_symlink():
        raise ValueError("Missing reviewed API artifact bundle")
    with regular(bundle / "receipt.json") as stream:
        receipt_bytes = stream.read(32 * 1024 * 1024 + 1)
    if (len(receipt_bytes) > 32 * 1024 * 1024
            or hashlib.sha256(receipt_bytes).hexdigest() != manifest["receipt_sha256"]
            or hashlib.sha256(lock).hexdigest() != manifest["lock_sha256"]):
        raise ValueError("Reviewed receipt/lock hash mismatch")
    with regular(bundle / "requirements.lock") as stream:
        if stream.read(len(lock) + 1) != lock:
            raise ValueError("Artifact-bundle lock differs from committed API lock")
    receipt = json.loads(receipt_bytes)
    artifacts = manifest["artifacts"]
    if receipt.get("status") != "passed" or receipt.get("artifact_count") != len(artifacts):
        raise ValueError("Artifact receipt is not an accepted complete set")
    names = set()
    for item in artifacts:
        name = item["filename"]
        if not name.endswith(".whl") or Path(name).name != name or name in names:
            raise ValueError("Invalid or duplicate wheel filename")
        names.add(name)
        accepted = receipt["artifacts"][item["name"]]
        if any(item[key] != accepted[key] for key in ("name", "version", "filename", "bytes", "sha256")):
            raise ValueError("Wheel differs from accepted receipt")
    archive = git(repo, "archive", "--format=tar", revision, "--", *RECIPES)
    if len(archive) > 32 * 1024 * 1024:
        raise ValueError("Unexpectedly large API recovery recipe archive")
    copied = sum(copy_wheel(bundle / "wheelhouse" / item["filename"],
                            destination / "wheelhouse" / item["filename"],
                            item["sha256"], item["bytes"]) for item in artifacts)
    for old in (destination / "wheelhouse").iterdir():
        if old.name not in names:
            if old.is_symlink() or not old.is_file():
                raise ValueError("Unexpected entry in backup wheelhouse")
            old.unlink()
    for name, value in (("api-recipes.tar", archive), ("artifacts.json", manifest_bytes),
                        ("requirements.lock", lock), ("receipt.json", receipt_bytes)):
        write_bytes(destination / name, value)
    result = {"status": "passed", "api_commit": revision, "artifact_bundle": str(bundle),
              "artifact_count": len(artifacts), "wheel_bytes": sum(item["bytes"] for item in artifacts),
              "copied_wheels": copied, "reused_wheels": len(artifacts) - copied,
              "recipe_paths": list(RECIPES), "recipes_sha256": hashlib.sha256(archive).hexdigest(),
              "receipt_sha256": manifest["receipt_sha256"], "lock_sha256": manifest["lock_sha256"]}
    write_bytes(destination / "capture.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: capture-api-uv.py SNAPSHOT_DIRECTORY")
    print(json.dumps(capture(API_ROOT, Path(sys.argv[1]))))
