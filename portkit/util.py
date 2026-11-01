import hashlib
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path


def step(msg):
    print(f"==> {msg}", flush=True)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr, flush=True)


class Fail(Exception):
    """An error meant for the user: printed without a traceback."""


def run(cmd, **kw):
    kw.setdefault("check", True)
    return subprocess.run([str(c) for c in cmd], **kw)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, expected_sha256=None):
    dest = Path(dest)
    if dest.exists() and (expected_sha256 is None or sha256(dest) == expected_sha256):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    step(f"Downloading {url}")
    with urllib.request.urlopen(url) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total and sys.stdout.isatty():
                print(f"\r    {done >> 20} / {total >> 20} MB", end="", flush=True)
    if total and sys.stdout.isatty():
        print()
    if expected_sha256 and (got := sha256(part)) != expected_sha256:
        part.unlink()
        raise Fail(f"checksum mismatch for {url}: got {got}, expected {expected_sha256}")
    part.rename(dest)
    return dest


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def free_gb(path):
    path = Path(path)
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free / 1e9


def sysctl(name):
    try:
        return run(["sysctl", "-n", name], capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
