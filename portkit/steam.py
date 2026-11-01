"""Windows Steam inside a Wine prefix."""

import shutil
import time
from pathlib import Path

from . import config, runtime, vdf, wine
from .util import Fail, download, run, step


def install_dir(prefix):
    return wine.prefix_path(prefix) / "drive_c/Program Files (x86)/Steam"


def exe(prefix):
    return install_dir(prefix) / "steam.exe"


def installed(prefix):
    return exe(prefix).exists()


def install(prefix):
    if installed(prefix):
        step(f"Steam already installed in prefix {prefix}")
        return
    wine.create_prefix(prefix)
    setup = download(config.STEAM_SETUP_URL, config.DOWNLOADS / "SteamSetup.exe")
    step("Installing Windows Steam (silent)")
    wine.wine(prefix, [setup, "/S"])
    wine.wait(prefix)
    if not installed(prefix):
        raise Fail(f"Steam setup finished but {exe(prefix)} is missing")


WRAPPER_SOURCE = config.ROOT / "wrapper/steamwebhelper.c"
WRAPPER_EXE = config.RUNTIME_DIR / "tools/steamwebhelper-wrapper.exe"
ZIG_VENV = config.RUNTIME_DIR / "tools/zig-venv"
WRAPPER_MAX_SIZE = 500_000  # the real helper is several MB


def build_wrapper():
    """Cross-compile wrapper/steamwebhelper.c with Zig (from pip, kept in runtime/tools)."""
    if WRAPPER_EXE.exists() and WRAPPER_EXE.stat().st_mtime >= WRAPPER_SOURCE.stat().st_mtime:
        return WRAPPER_EXE
    python = ZIG_VENV / "bin/python"
    if not python.exists():
        step("Installing the Zig compiler (pip, into runtime/tools) to build the web helper wrapper")
        run(["python3", "-m", "venv", ZIG_VENV])
        run([python, "-m", "pip", "-q", "install", "ziglang"])
    step("Building the Steam web helper wrapper")
    WRAPPER_EXE.parent.mkdir(parents=True, exist_ok=True)
    run([python, "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu", "-municode", "-Wl,--subsystem,windows",
         "-O2", "-s", "-o", WRAPPER_EXE, WRAPPER_SOURCE])
    return WRAPPER_EXE


def install_wrapper(prefix):
    """Put the wrapper in front of every steamwebhelper.exe (Steam updates replace it)."""
    wrapper = build_wrapper()
    for cef in sorted((install_dir(prefix) / "bin/cef").glob("cef.win*")):
        target, real = cef / "steamwebhelper.exe", cef / "steamwebhelper_real.exe"
        if not target.exists():
            continue
        if target.stat().st_size > WRAPPER_MAX_SIZE:
            shutil.copy2(target, real)  # Valve's helper, new or updated
        elif not real.exists():
            raise Fail(f"{cef.name}: Steam's own web helper is missing; reinstall Steam")
        if target.read_bytes() != wrapper.read_bytes():
            shutil.copy2(wrapper, target)
            step(f"Web helper wrapper installed in {cef.name}")


def start(prefix, args=(), debug="-all", extra_env=None, log=None):
    if not installed(prefix):
        raise Fail(f"Steam is not installed in prefix {prefix}: run ./port steam install")
    if runtime.spec(prefix).get("webhelper_wrapper"):
        install_wrapper(prefix)
    extra = runtime.spec(prefix).get("steam_args", [])
    return wine.spawn(prefix, [exe(prefix), *extra, *args], debug, extra_env, log)


def stop(prefix, timeout=60):
    """Ask Steam to quit (it saves its sign-in and library list), then stop the prefix."""
    if installed(prefix) and wine.find_process("steam.exe"):
        step("Asking Steam to shut down")
        wine.spawn(prefix, [exe(prefix), "-shutdown"])
        deadline = time.monotonic() + timeout
        while wine.find_process("steam.exe") and time.monotonic() < deadline:
            time.sleep(2)
    wine.kill(prefix)


def manifests(library):
    """{appid: AppState} for every game installed in a Steam library folder."""
    apps = {}
    for p in sorted(Path(library, "steamapps").glob("appmanifest_*.acf")):
        state = vdf.load(p).get("AppState", {})
        if "appid" in state:
            apps[state["appid"]] = state
    return apps


def add_library(prefix, library):
    """Register an existing Windows Steam library folder with the prefix's Steam.

    Steam writes libraryfolders.vdf on its first run and rewrites it on exit,
    so it must have run once and must be closed now.
    """
    library = Path(library).resolve()
    if not (library / "steamapps").is_dir():
        raise Fail(f"{library} is not a Steam library (no steamapps folder)")
    path = install_dir(prefix) / "steamapps/libraryfolders.vdf"
    if not path.exists():
        raise Fail("Start Steam once (./port steam start), sign in, quit it, then retry")
    if wine.find_process("steam.exe"):
        raise Fail("Quit Steam first: it rewrites libraryfolders.vdf when it exits")
    data = vdf.load(path)
    folders = data.setdefault("libraryfolders", {})
    win = wine.windows_path(library)
    if any(isinstance(f, dict) and f.get("path", "").lower() == win.lower() for f in folders.values()):
        step(f"{win} is already a library of prefix {prefix}")
        return
    meta = {}
    if (library / "libraryfolder.vdf").exists():
        meta = vdf.load(library / "libraryfolder.vdf").get("libraryfolder", {})
    apps = {appid: s.get("SizeOnDisk", "0") for appid, s in manifests(library).items()}
    key = str(max((int(k) for k in folders if k.isdigit()), default=-1) + 1)
    folders[key] = {
        "path": win,
        "label": meta.get("label", ""),
        "contentid": meta.get("contentid", "0"),
        "totalsize": "0",
        "update_clean_bytes_tally": "0",
        "time_last_update_verified": "0",
        "apps": apps,
    }
    path.write_text(vdf.dumps(data), encoding="utf-8")
    step(f"Added {win} ({len(apps)} games) to prefix {prefix}")
