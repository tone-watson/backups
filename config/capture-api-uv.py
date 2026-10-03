"""Capture API recovery artifacts and the exact private SD environment safely."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

MANAGED_PYTHON_BUNDLE = Path("/srv/farm/.uv/migrations/2026-10-03-managed-python-recovery")
MANAGED_PYTHON_RECEIPT_SHA256 = "a5b616f544ca695793321b4f72e3e6859ac7aee0aa109f476a3659832e1e1a16"
ADDITIONAL_MANAGED_PYTHON_ROOT = Path("/srv/farm/.uv/migrations/2026-10-03-managed-python-extra")
PYTHON_31014_OWNERSHIP_POLICY_FILENAME = "python_31014_host_ownership_policy.json"
PYTHON_31014_OWNERSHIP_POLICY_SHA256 = "d4b0636012ff5af87edb7adcbfff6e273b9dec4b14ce1ec4b99a7d6cf852e5ef"
ADDITIONAL_MANAGED_PYTHON_PROFILES = {
    "3.9.19": {"build": "20240814",
               "receipt_sha256": "da96e37f8107b863886040350a3f82e2fbcb24741a4e295b658f626f6dfbd5a3"},
    "3.10.14": {"build": "20240814",
                "receipt_sha256": "81f40a19834ddf4f8410a1d3d1e1d38dc4b93ea86e12b92a5a6ea9612a85d4bf"},
    "3.10.16": {"build": "20250317",
                "receipt_sha256": "8ffd8a92c40f4535edf04374f974c64b2400d637599c3b37032c59939b49b9a2"},
    "3.10.18": {"build": "20250918",
                "receipt_sha256": "4fc113f33c526dd10df775ca70dd7f522ff915a06646a168017611a444485ef7"},
    "3.10.19": {"build": "20251031",
                "receipt_sha256": "b5904fb0d5c5bccffcc40f4d2da03ec105f711afa5edabcd970aad6eaf81c5c2"},
    "3.12.11": {"build": "20250918",
                "receipt_sha256": "863a41d609b648e7d909a0e8f8eab8248b71cbb1a25ea547fa12042fb2f8d671"},
}
API_ROOT = Path("/srv/farm/sys/api")
SD_ENV_PATH = Path("/srv/farm/private/stable-diffusion/service.env")
RECIPES = (
    ".python-version", "scripts/runtime.sh", "scripts/runtime.py", "deploy/runtime",
    "deploy/uv", "deploy/systemd", "deploy/uv-preview", "lib/testing",
    "docs/deployment", "scripts/upstream_runtime.sh", "scripts/upstream_runtime.py",
    "deploy/upstream",
)
UPSTREAM_WHEEL_ROOTS = {
    "comfyui": Path("/srv/farm/.uv/migrations/2026-10-02-comfyui/wheels"),
    "stable-diffusion": Path("/srv/farm/.uv/migrations/2026-10-02-stable-diffusion/wheels"),
    "graphiti": Path("/srv/farm/.uv/migrations/2026-10-03-graphiti/wheels"),
    "ace-step": Path("/srv/farm/.uv/migrations/2026-10-03-ace-step/wheels"),
    "live-portrait": Path("/srv/farm/.uv/migrations/2026-10-03-live-portrait/wheels"),
    "flood-map": Path("/srv/farm/.uv/migrations/2026-10-03-flood-map/wheels"),
    "tiktok-scraper": Path("/srv/farm/.uv/migrations/2026-10-03-tiktok-scraper/wheels"),
    "hardware": Path("/srv/farm/.uv/migrations/2026-10-03-hardware/wheels"),
    "noise-agent": Path("/srv/farm/.uv/migrations/2026-10-03-noise-agent/wheels"),
    "instantmesh": Path("/srv/farm/.uv/migrations/2026-10-03-instantmesh/wheels"),
    "riffusion": Path("/srv/farm/.uv/migrations/2026-10-03-riffusion/wheels"),
}
UPSTREAM_LOCKS = {
    "comfyui": ("requirements.lock",),
    "stable-diffusion": ("requirements.lock", "requirements-overlays.lock"),
    "graphiti": ("graphiti-root/requirements.lock", "graphiti-mcp/requirements.lock"),
    "ace-step": ("requirements.lock",),
    "live-portrait": ("requirements.lock",),
    "flood-map": ("requirements.lock",),
    "tiktok-scraper": ("requirements.lock",),
    "hardware": ("requirements.lock",),
    "noise-agent": ("requirements.lock",),
    "instantmesh": ("requirements.lock",),
    "riffusion": ("requirements.lock",),
}


def local_wheels(lock, wheel_root):
    """Read the reviewed lock syntax without importing pip or following includes."""
    if wheel_root.resolve() != wheel_root or not wheel_root.is_dir():
        raise ValueError("Missing canonical upstream wheel root")
    logical = ""
    for raw in lock.decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        logical += line[:-1].rstrip() + " " if line.endswith("\\") else line
        if line.endswith("\\"):
            continue
        requirement, logical = logical, ""
        # Current recipes use version pins, HTTPS wheels or file:// wheels.
        # Refuse extra include/options and unsupported local path syntax.
        match = re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*(?:==[^\s]+|\s+@\s+(\S+))"
            r"(?:\s+--hash=sha256:[0-9a-f]{64})+", requirement,
        )
        if not match:
            raise ValueError("Unsupported upstream lock requirement")
        url = match.group(1)
        if url is None:
            continue
        parsed = urlsplit(url)
        if parsed.scheme == "https":
            continue
        if (parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment):
            raise ValueError("Unsupported upstream wheel URL")
        source = Path(unquote(parsed.path))
        if (not source.is_absolute() or source.resolve() != source
                or not source.is_relative_to(wheel_root)
                or not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", source.name)):
            raise ValueError("Upstream wheel must stay inside its reviewed canonical root")
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", requirement)
        if len(hashes) != 1:
            raise ValueError("Local upstream wheel needs exactly one reviewed hash")
        yield source, hashes[0]
    if logical:
        raise ValueError("Unfinished upstream lock continuation")


def capture_upstream(repo, revision, destination):
    result = {}
    for profile, wheel_root in UPSTREAM_WHEEL_ROOTS.items():
        artifacts = {}
        locks = {}
        for filename in UPSTREAM_LOCKS[profile]:
            # Graphiti's two profiles share one bundle; copy shared wheels once.
            prefix = "" if profile == "graphiti" else profile + "/"
            relative = "deploy/upstream/" + prefix + filename
            lock = git(repo, "show", revision + ":" + relative)
            locks[filename] = hashlib.sha256(lock).hexdigest()
            for source, sha256 in local_wheels(lock, wheel_root):
                name = source.relative_to(wheel_root).as_posix()
                previous = artifacts.get(name)
                if previous:
                    if previous["sha256"] != sha256:
                        raise ValueError("Conflicting upstream wheel hashes")
                    previous["locks"].append(filename)
                    continue
                with regular(source) as stream:
                    size = os.fstat(stream.fileno()).st_size
                artifacts[name] = {"source": str(source), "filename": name,
                                   "sha256": sha256, "bytes": size, "locks": [filename]}
        wheelhouse = destination / "upstream" / profile / "wheelhouse"
        directory(destination / "upstream")
        directory(wheelhouse.parent)
        directory(wheelhouse)
        copied = 0
        for item in artifacts.values():
            target = wheelhouse / item["filename"]
            directory(target.parent)
            copied += copy_wheel(Path(item["source"]), target, item["sha256"], item["bytes"])
        result[profile] = {"wheel_root": str(wheel_root), "lock_sha256": locks,
                           "artifact_count": len(artifacts),
                           "wheel_bytes": sum(item["bytes"] for item in artifacts.values()),
                           "copied_wheels": copied, "reused_wheels": len(artifacts) - copied,
                           "artifacts": list(artifacts.values())}
    return result


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


def capture_sd_environment(destination):
    """Capture only the reviewed private SD environment; never return its bytes."""
    directory(destination.parent)
    directory(destination)
    write_bytes(destination / "capture.json", b'{"status":"incomplete"}\n')
    if SD_ENV_PATH.resolve() != SD_ENV_PATH:
        raise ValueError("Stable Diffusion environment must be a canonical regular file")
    with regular(SD_ENV_PATH) as source:
        if stat.S_IMODE(os.fstat(source.fileno()).st_mode) != 0o600:
            raise ValueError("Stable Diffusion environment must have mode 0600")
        data = source.read(65537)
    if not data or len(data) > 65536:
        raise ValueError("Stable Diffusion environment is empty or unexpectedly large")
    target = destination / "service.env"
    # Atomic replacement creates an independent 0600 file, including when an
    # earlier destination is a hardlink. Source ownership/mode never changes.
    write_bytes(target, data)
    with regular(target) as saved:
        if saved.read(65537) != data:
            raise ValueError("Private environment copy failed verification")
    result = {"status": "passed", "source": str(SD_ENV_PATH),
              "filename": "service.env", "mode": "0600"}
    write_bytes(destination / "capture.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


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


def capture_managed_python(destination):
    """Copy the accepted installed-byte interpreter archive; never execute/extract it."""
    directory(destination)
    write_bytes(destination / "capture.json", b'{"status":"incomplete"}\n')
    bundle = MANAGED_PYTHON_BUNDLE
    if bundle.resolve() != bundle or not bundle.is_dir():
        raise ValueError("Missing canonical managed Python recovery bundle")
    with regular(bundle / "receipt.json") as stream:
        receipt_bytes = stream.read(65537)
    if (len(receipt_bytes) > 65536
            or hashlib.sha256(receipt_bytes).hexdigest() != MANAGED_PYTHON_RECEIPT_SHA256):
        raise ValueError("Managed Python recovery receipt differs from reviewed pin")
    receipt = json.loads(receipt_bytes)
    if (receipt.get("schema") != 1 or receipt.get("status") != "passed"
            or receipt.get("source_prefix") != "/srv/farm/.uv/python/cpython-3.9.18-linux-x86_64-gnu"
            or receipt.get("build") != "20240224"
            or receipt.get("before_after_source_match") is not True
            or receipt.get("archive_verified") is not True):
        raise ValueError("Managed Python recovery receipt is not accepted")
    artifacts = []
    for key, filename, maximum in (
            ("manifest", "source-manifest.json", 8 * 1024 * 1024),
            ("archive", "python-3.9.18-build-20240224.tar.gz", 256 * 1024 * 1024)):
        item = receipt[key]
        if (item["filename"] != filename
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("Invalid managed Python recovery artifact")
        source = bundle / filename
        with regular(source) as stream:
            size = os.fstat(stream.fileno()).st_size
        if not 0 < size <= maximum or (key == "archive" and size != item["bytes"]):
            raise ValueError("Unexpected managed Python recovery artifact size")
        copied = copy_wheel(source, destination / filename, item["sha256"], size)
        artifacts.append({"filename": filename, "sha256": item["sha256"],
                          "bytes": size, "copied": copied})
    copied = copy_wheel(bundle / "receipt.json", destination / "receipt.json",
                        MANAGED_PYTHON_RECEIPT_SHA256, len(receipt_bytes))
    artifacts.append({"filename": "receipt.json", "sha256": MANAGED_PYTHON_RECEIPT_SHA256,
                      "bytes": len(receipt_bytes), "copied": copied})
    result = {"status": "passed", "source_bundle": str(bundle),
              "source_prefix": receipt["source_prefix"], "build": receipt["build"],
              "artifacts": artifacts, "copied_files": sum(item["copied"] for item in artifacts),
              "reused_files": sum(not item["copied"] for item in artifacts),
              "scope": "Exact accepted installed-byte archive; no extraction or upstream provenance claim"}
    write_bytes(destination / "capture.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


def capture_additional_managed_python_profile(version, profile, destination):
    """Copy one explicitly pinned additional interpreter bundle without extracting it."""
    build, receipt_sha = profile["build"], profile["receipt_sha256"]
    if (not re.fullmatch(r"3\.[0-9]+\.[0-9]+", version)
            or not re.fullmatch(r"[0-9]{8}", build)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha)):
        raise ValueError("Invalid reviewed additional managed Python profile")
    directory(destination)
    write_bytes(destination / "capture.json", b'{"status":"incomplete"}\n')
    bundle = ADDITIONAL_MANAGED_PYTHON_ROOT / version
    if bundle.resolve() != bundle or not bundle.is_dir():
        raise ValueError("Missing canonical additional managed Python recovery bundle")
    with regular(bundle / "receipt.json") as stream:
        receipt_bytes = stream.read(65537)
    if (len(receipt_bytes) > 65536
            or hashlib.sha256(receipt_bytes).hexdigest() != receipt_sha):
        raise ValueError("Additional managed Python receipt differs from reviewed pin")
    receipt = json.loads(receipt_bytes)
    prefix = "/srv/farm/.uv/python/cpython-" + version + "-linux-x86_64-gnu"
    if (not isinstance(receipt, dict) or type(receipt.get("schema")) is not int
            or receipt["schema"] != 1 or receipt.get("status") != "passed"
            or receipt.get("source_prefix") != prefix
            or receipt.get("python_version") != version or receipt.get("build") != build
            or receipt.get("before_after_source_match") is not True
            or receipt.get("archive_verified") is not True):
        raise ValueError("Additional managed Python recovery receipt is not accepted")
    selected = [
        ("manifest", "source-manifest.json", 8 * 1024 * 1024),
        ("archive", "python-" + version + "-build-" + build + ".tar.gz", 256 * 1024 * 1024),
    ]
    policy = receipt.get("ownership_policy")
    if version == "3.10.14":
        if (not isinstance(policy, dict)
                or policy.get("filename") != PYTHON_31014_OWNERSHIP_POLICY_FILENAME
                or policy.get("sha256") != PYTHON_31014_OWNERSHIP_POLICY_SHA256):
            raise ValueError("Managed Python 3.10.14 ownership policy differs from reviewed pin")
        selected.append(("ownership_policy", PYTHON_31014_OWNERSHIP_POLICY_FILENAME, 1024 * 1024))
    elif policy is not None:
        raise ValueError("Unexpected managed Python ownership qualification")
    artifacts = []
    for key, filename, maximum in selected:
        item = receipt.get(key)
        if (not isinstance(item, dict) or item.get("filename") != filename
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("Invalid additional managed Python recovery artifact")
        source = bundle / filename
        with regular(source) as stream:
            size = os.fstat(stream.fileno()).st_size
        if (not 0 < size <= maximum
                or ((key == "archive" or "bytes" in item)
                    and (type(item.get("bytes")) is not int or size != item["bytes"]))):
            raise ValueError("Unexpected additional managed Python recovery artifact size")
        copied = copy_wheel(source, destination / filename, item["sha256"], size)
        artifacts.append({"filename": filename, "sha256": item["sha256"],
                          "bytes": size, "copied": copied})
    copied = copy_wheel(bundle / "receipt.json", destination / "receipt.json",
                        receipt_sha, len(receipt_bytes))
    artifacts.append({"filename": "receipt.json", "sha256": receipt_sha,
                      "bytes": len(receipt_bytes), "copied": copied})
    result = {"status": "passed", "source_bundle": str(bundle),
              "source_prefix": prefix, "python_version": version, "build": build,
              "artifacts": artifacts, "copied_files": sum(item["copied"] for item in artifacts),
              "reused_files": sum(not item["copied"] for item in artifacts),
              "scope": "Exact accepted installed-byte archive; no extraction or upstream provenance claim"}
    write_bytes(destination / "capture.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


def capture_additional_managed_python(destination):
    """Retain only the six reviewed version/build bundles under distinct destinations."""
    directory(destination)
    results = {}
    for version, profile in ADDITIONAL_MANAGED_PYTHON_PROFILES.items():
        name = "cpython-" + version + "-build-" + profile["build"]
        results[version] = capture_additional_managed_python_profile(
            version, profile, destination / name)
    return results


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
    upstream = capture_upstream(repo, revision, destination)
    managed_python = capture_managed_python(
        destination / "managed-python/cpython-3.9.18-build-20240224")
    additional_managed_python = capture_additional_managed_python(
        destination / "managed-python")
    for name, value in (("api-recipes.tar", archive), ("artifacts.json", manifest_bytes),
                        ("requirements.lock", lock), ("receipt.json", receipt_bytes)):
        write_bytes(destination / name, value)
    result = {"status": "passed", "api_commit": revision, "artifact_bundle": str(bundle),
              "artifact_count": len(artifacts), "wheel_bytes": sum(item["bytes"] for item in artifacts),
              "copied_wheels": copied, "reused_wheels": len(artifacts) - copied,
              "recipe_paths": list(RECIPES), "recipes_sha256": hashlib.sha256(archive).hexdigest(),
              "receipt_sha256": manifest["receipt_sha256"], "lock_sha256": manifest["lock_sha256"],
              "upstream": upstream, "managed_python": managed_python,
              "additional_managed_python": additional_managed_python}
    write_bytes(destination / "capture.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--stable-diffusion-env":
        if os.geteuid() != 0:
            raise SystemExit("Private environment capture must run as root")
        print(json.dumps(capture_sd_environment(Path(sys.argv[2]))))
    elif len(sys.argv) == 2:
        print(json.dumps(capture(API_ROOT, Path(sys.argv[1]))))
    else:
        raise SystemExit("Usage: capture-api-uv.py [--stable-diffusion-env] SNAPSHOT_DIRECTORY")
