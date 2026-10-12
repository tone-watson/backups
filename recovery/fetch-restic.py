#!/usr/bin/python3
"""Fetch one pinned upstream Linux amd64 binary; does not install system-wide."""
import argparse
import bz2
import hashlib
from pathlib import Path
import platform
import urllib.request

VERSION = "0.19.1"
ARCHIVE_SHA256 = "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c"
BINARY_SHA256 = "20d4142678d0d95ec11a4759def1b73fd9190abc9ca19e4b62d067c0b387e639"
URL = f"https://github.com/restic/restic/releases/download/v{VERSION}/restic_{VERSION}_linux_amd64.bz2"

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, help="New binary path; existing files are refused")
    args = p.parse_args()
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        p.error("Pinned binary supports Linux x86_64 only")
    raw = urllib.request.urlopen(URL, timeout=60).read()
    if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
        p.error("Archive checksum mismatch")
    binary = bz2.decompress(raw)
    if hashlib.sha256(binary).hexdigest() != BINARY_SHA256:
        p.error("Binary checksum mismatch")
    output = Path(args.output)
    with output.open("xb") as f:
        f.write(binary)
    output.chmod(0o700)
    print(f"Verified restic {VERSION}: {output}")
