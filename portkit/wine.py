"""Wine prefixes, environment and processes.

A prefix is named after the runtime it belongs to (see runtime.py).
"""

import os
import re
import subprocess
import time

from . import config, runtime
from .util import Fail, run, step

# Applied to every launch; a game's [env] table and --env override them.
DEFAULT_ENV = {
    # Rosetta hides AVX/AVX2 from translated code unless asked; most 2020s
    # games check for them at start-up and refuse to run without.
    "ROSETTA_ADVERTISE_AVX": "1",
    # Event-based thread sync (this Wine has esync, not msync). It keeps a file
    # descriptor per sync object, hence the raised limit below.
    "WINEESYNC": "1",
}


def _raise_fd_limit():
    import resource  # Unix only: the decomp tools also run on the Windows PC, which never starts Wine

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        cap = int(subprocess.run(["sysctl", "-n", "kern.maxfilesperproc"],
                                 capture_output=True, text=True).stdout)
    except ValueError:
        cap = 10240
    want = cap if hard == resource.RLIM_INFINITY else min(hard, cap)
    if soft < want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))


def prefix_path(name):
    return config.PREFIXES / name


def windows_path(path):
    """Unix path as Wine sees it through drive Z: (mapped to /)."""
    return "Z:" + str(path).replace("/", "\\")


def env(prefix, debug="-all", extra=None):
    _raise_fd_limit()
    e = os.environ.copy()
    e.update(DEFAULT_ENV)
    e.update(runtime.spec(prefix).get("env", {}))
    e["WINEPREFIX"] = str(prefix_path(prefix))
    e["WINEDEBUG"] = debug
    if extra:
        e.update(extra)
    return e


def require_runtime(prefix):
    if not runtime.installed(prefix):
        raise Fail(f"runtime {prefix} is not installed: run ./port setup --runtime {prefix}")


def wine(prefix, args, debug="-all", extra_env=None, **kw):
    require_runtime(prefix)
    return run([runtime.wine_bin(prefix), *args], env=env(prefix, debug, extra_env), **kw)


def spawn(prefix, args, debug="-all", extra_env=None, log=None, cwd=None):
    """Start a Wine process in the background; its output goes to `log`."""
    require_runtime(prefix)
    out = open(log, "ab") if log else subprocess.DEVNULL
    return subprocess.Popen(
        [str(runtime.wine_bin(prefix)), *map(str, args)],
        env=env(prefix, debug, extra_env),
        stdout=out,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        start_new_session=True,
    )


def wait(prefix):
    """Wait for every process in the prefix to exit."""
    run([runtime.wineserver_bin(prefix), "-w"], env=env(prefix))


def kill(prefix):
    """Stop every process in the prefix (Steam and the game included)."""
    if runtime.installed(prefix) and prefix_path(prefix).exists():
        run([runtime.wineserver_bin(prefix), "-k"], env=env(prefix), check=False)


def create_prefix(name):
    path = prefix_path(name)
    if (path / "system.reg").exists():
        step(f"Prefix {name} already exists at {path}")
        return path
    path.mkdir(parents=True, exist_ok=True)
    step(f"Creating Wine prefix {name} at {path}")
    wine(name, ["wineboot", "--init"])
    wait(name)
    wine(name, ["winecfg", "-v", "win10"])
    wait(name)
    return path


def processes():
    """(pid, rss_kb, cpu_percent, args) for every process on the Mac."""
    out = run(["ps", "-axo", "pid=,rss=,%cpu=,args="], capture_output=True, text=True).stdout
    me = os.getpid()
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and int(parts[0]) != me:
            yield int(parts[0]), int(parts[1]), float(parts[2]), parts[3]


def find_process(name):
    """The pid of the process whose command line names `name` (an .exe), or None.

    Wine sets each Windows process's command line to its Windows path, so the
    game shows up in ps as C:\\...\\Game-Win64-Shipping.exe.
    """
    pattern = re.compile(r'(?:^|[/\\])' + re.escape(name) + r'(?:\s|"|$)', re.I)
    for pid, _, _, args in processes():
        if pattern.search(args):
            return pid
    return None


def wait_for_process(name, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid := find_process(name):
            return pid
        time.sleep(2)
    raise Fail(f"{name} did not start within {timeout} s")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
