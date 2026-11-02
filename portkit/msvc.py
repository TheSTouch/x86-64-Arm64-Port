"""MSVC on the Mac: the game's compiler run under the Porting Kit's Wine, so one unity blob compiles and
matches without UBT or the Windows PC.

- The toolchain (cl.exe of MSVC 14.38.33130 and Windows SDK 10.0.22621) is copied from the owner's PC,
  the same bits under the owner's licence, to config.MSVC_DIR; it runs in a Wine prefix of its own.
- The flags: a /Z7 object records its command line (S_BUILDINFO in .debug$S, pointing at LF_BUILDINFO in
  .debug$T), without its include directories, definitions and forced includes; UBT's response file for
  each compile has them all (`flags`).
- The inputs: the engine headers come from the Mac's clone of the PC's engine tag, the project from a
  local staging; what exists only on the PC (UHT's generated headers, UBT's response files, definitions
  and the shared PCH's header) is mirrored at its PC path under config.MSVC_MIRROR/pc (`mirror`).
- A compile replays the PC's response file with every path mapped to the Mac (`PathMap`), with the shared
  PCH built on the Mac from the PC's response file for it (without it, some inlining changes).
"""

import concurrent.futures
import hashlib
import json
import os
import posixpath
import re
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import coff, config, decomp, runtime, wine
from .util import Fail, run, step

def wine_path(path):
    return wine.windows_path(Path(path))


# ---------------------------------------------------------------------------------------------- Wine

def cl_exe():
    path = config.MSVC_DIR / "MSVC" / config.MSVC_VERSION / "bin" / "Hostx64" / "x64" / "cl.exe"
    if not path.is_file():
        raise Fail(f"{path} not found: copy MSVC {config.MSVC_VERSION} from the PC (docs/SATISFACTORY-RECOMP.md)")
    return path


def _env():
    return wine.env(config.DEFAULT_RUNTIME, extra={"WINEPREFIX": str(config.MSVC_PREFIX), "INCLUDE": "", "LIB": "",
                                                   "LIBPATH": "", "CL": "", "_CL_": ""})


def _wineserver():
    """The prefix, made on first use, and its wineserver started apart, kept for a minute after the last
    compile: one Wine starts, and it inherits nobody's output (a pipe would never close)."""
    wine.require_runtime(config.DEFAULT_RUNTIME)
    with open(os.devnull, "wb") as null:
        if not (config.MSVC_PREFIX / "system.reg").exists():
            step(f"Creating the MSVC Wine prefix at {config.MSVC_PREFIX}")
            config.MSVC_PREFIX.mkdir(parents=True, exist_ok=True)
            run([runtime.wine_bin(config.DEFAULT_RUNTIME), "wineboot", "--init"], env=_env(), stdout=null, stderr=null)
        run([runtime.wineserver_bin(config.DEFAULT_RUNTIME), "-p60"], env=_env(), stdout=null, stderr=null, check=False)


def cl(args, cwd=None, output=None):
    """cl.exe under Wine. Its output goes to the file `output`, else to ours."""
    _wineserver()
    command = [runtime.wine_bin(config.DEFAULT_RUNTIME), wine_path(cl_exe()), *args]
    if output is None:
        return run(command, env=_env(), cwd=cwd, check=False)
    with open(output, "wb") as out:
        return run(command, env=_env(), cwd=cwd, check=False, stdout=out, stderr=subprocess.STDOUT)
