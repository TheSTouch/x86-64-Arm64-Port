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

LF_PRECOMP, LF_BUILDINFO, LF_SUBSTR_LIST, LF_STRING_ID = 0x1509, 0x1603, 0x1604, 0x1605
BUILDINFO_FIELDS = ("cwd", "compiler", "source", "pdb", "command")   # CV_BuildInfo_e order

# Options whose value is the next argument in UBT's response files (/I "dir"); others carry it joined (/FI"file").
SEPARATE = {"/i", "/external:i", "/experimental:log", "/sourcedependencies"}
JOINED_PATHS = ("/FI", "/Yu", "/Yc", "/Fp", "/Fo")
# Dropped on the Mac: the SARIF log; without a PCH (pch=False) also /Yu and /Fp, its header staying forced with /FI.
SHIPPED = {"FactoryGameRecomp": "FactoryGame", "FactoryPreEarlyLoadingScreen": "FactoryPreEarlyLoadingScreen"}


# ---------------------------------------------------------------------------------------------- recorded flags

@dataclass
class BuildInfo:
    cwd: str = ""
    compiler: str = ""
    source: str = ""
    pdb: str = ""
    command: str = ""


def build_info(data):
    """The command line a /Z7 object records: LF_BUILDINFO's five strings, each an LF_STRING_ID whose
    LF_SUBSTR_LIST (long strings are cut in pieces) comes first. Compiled with a PCH, the object's records
    are numbered after the PCH object's (LF_PRECOMP: their first index and count)."""
    try:
        o = coff.parse(data)
    except coff.NotCOFF as e:
        raise Fail(str(e))
    strings, lists, info = {}, {}, None
    for s in o.sections:
        if s.name != ".debug$T" or len(s.data) < 4:
            continue
        d, i, index = s.data, 4, 0x1000
        while i + 4 <= len(d):
            length, kind = struct.unpack_from("<HH", d, i)
            body = d[i + 4:i + 2 + length]
            if kind == LF_PRECOMP:
                start, count = struct.unpack_from("<II", body)
                i, index = i + 2 + length, start + count
                continue
            if kind == LF_STRING_ID:
                strings[index] = (struct.unpack_from("<I", body)[0], body[4:].split(b"\0", 1)[0].decode("utf-8", "replace"))
            elif kind == LF_SUBSTR_LIST:
                n = struct.unpack_from("<I", body)[0]
                lists[index] = struct.unpack_from(f"<{n}I", body, 4)
            elif kind == LF_BUILDINFO:
                n = struct.unpack_from("<H", body)[0]
                info = struct.unpack_from(f"<{n}I", body, 2)
            i += 2 + length
            index += 1
    if info is None:
        raise Fail("no LF_BUILDINFO record: not compiled with /Z7?")

    def text(sid):
        if sid not in strings:
            return ""
        sub, own = strings[sid]
        return "".join(text(p) for p in lists.get(sub, ())) + own

    return BuildInfo(**{name: text(sid) for name, sid in zip(BUILDINFO_FIELDS, info)})


# ---------------------------------------------------------------------------------------------- response files

def split_args(text):
    """Arguments of a command line or response file, by the Windows rules cl.exe uses: whitespace separates,
    double quotes group, backslashes are literal unless they precede a quote."""
    args, cur, quoted, have, i = [], [], False, False, 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            n = len(text) - i - len(text[i:].lstrip("\\"))
            if i + n < len(text) and text[i + n] == '"':
                cur.append("\\" * (n // 2))
                if n % 2:
                    cur.append('"')
                    i += n + 1
                else:
                    i += n
                have = True
                continue
            cur.append("\\" * n)
            i += n
            have = True
            continue
        if c == '"':
            quoted, have = not quoted, True
        elif c.isspace() and not quoted:
            if have:
                args.append("".join(cur))
            cur, have = [], False
        else:
            cur.append(c)
            have = True
        i += 1
    if have:
        args.append("".join(cur))
    return args


def quote(arg):
    return f'"{arg}"' if any(c.isspace() for c in arg) or not arg else arg


@dataclass
class Command:
    """A compile, option by option: [option, value] pairs, value None for a flag; a source file is [None, path]."""
    items: list = field(default_factory=list)

    @classmethod
    def parse(cls, args, read=None):
        """args: the arguments of a response file. read(path) gives the text of one it names (@file)."""
        items, i = [], 0
        while i < len(args):
            a = args[i]
            if a.startswith("@"):
                if read is None:
                    raise Fail(f"{a}: nested response file, nothing to read it with")
                items += cls.parse(split_args(read(a[1:])), read).items
            elif a.lower() in SEPARATE and i + 1 < len(args):
                items.append([a, args[i + 1]])
                i += 1
            elif a[:1] in "/-":
                for opt in JOINED_PATHS:
                    if a.startswith(opt) and len(a) > len(opt):
                        items.append([opt, a[len(opt):]])
                        break
                else:
                    items.append([a, None])
            else:
                items.append([None, a])
            i += 1
        return cls(items)

    def values(self, option):
        return [v for o, v in self.items if o is not None and o.lower() == option.lower()]

    @property
    def sources(self):
        return [v for o, v in self.items if o is None]

    def flags(self):
        """The options that are not paths: what the object's recorded command line also holds."""
        skip = {s.lower() for s in SEPARATE} | {s.lower() for s in JOINED_PATHS} | {"/d"}
        return [o for o, v in self.items if o is not None and o.lower() not in skip and not o.lower().startswith("/d")]

    def lines(self):
        out = []
        for o, v in self.items:
            if o is None:
                out.append(quote(v))
            elif v is None:
                out.append(o)
            elif o.lower() in SEPARATE:
                out.append(f"{o} {quote(v)}")
            else:
                out.append(f'{o}"{v}"')
        return out


def _norm_flag(f):
    return "/" + f[1:].lower()


def missing_flags(recorded, command):
    """The flags of an object's recorded command line that the response file does not have. -X (no INCLUDE
    variable) is recorded although UBT does not pass it: UBT empties INCLUDE, and so does the Mac compile."""
    have = {_norm_flag(f) for f in command.flags()} | {"/x"}
    return [f for f in split_args(recorded) if f[:1] in "/-" and _norm_flag(f) not in have
            and not f.lower().startswith(("-i", "/i", "-d", "/d", "-fi", "/fi", "-external:i", "/external:i"))]


# ---------------------------------------------------------------------------------------------- paths

class PathMap:
    """PC paths to Mac paths: what the PC generated (an Intermediate folder) to the mirror, at its PC path;
    the project to its local staging; the engine to its Mac clone; MSVC and the SDK to their copies."""

    def __init__(self, project, cwd=None, mirror=None, engine=None, msvc=None):
        self.cwd = (cwd or f"{config.PC_ENGINE}/Engine/Source").replace("\\", "/")
        self.mirror = Path(mirror or config.MSVC_MIRROR) / "pc"
        msvc = Path(msvc or config.MSVC_DIR)
        self.roots = sorted([
            (config.PC_PROJECT, Path(project)),
            (config.PC_ENGINE, Path(engine or config.ENGINE_DIR)),
            (config.PC_MSVC, msvc / "MSVC" / config.MSVC_VERSION),
            (config.PC_WINSDK, msvc / "sdk"),
        ], key=lambda r: -len(r[0]))

    def pc(self, path):
        """An absolute PC path, forward slashes, '..' resolved (relative ones from the compile's directory)."""
        p = path.replace("\\", "/")
        if not re.match(r"^[A-Za-z]:/", p):
            p = self.cwd + "/" + p
        return p[:2].upper() + posixpath.normpath(p[2:])

    def generated(self, path):
        return "intermediate" in self.pc(path).lower().split("/")

    def mirrored(self, path):
        p = self.pc(path)
        return self.mirror / p[0] / p[3:]

    def mac(self, path):
        p = self.pc(path)
        if self.generated(p):
            return self.mirrored(p)
        for prefix, local in self.roots:
            if p.lower() == prefix.lower() or p.lower().startswith(prefix.lower().rstrip("/") + "/"):
                return local / p[len(prefix):].lstrip("/")
        raise Fail(f"{path}: no Mac folder for this PC path (config.PC_ENGINE, PC_PROJECT, PC_MSVC, PC_WINSDK)")


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


# ---------------------------------------------------------------------------------------------- the mirror

def intermediate(module):
    return f"{config.PC_PROJECT}/Intermediate/Build/Win64/x64/{config.PC_TARGET}/Shipping/{module}"


def read_pc_text(path):
    if not Path(path).is_file():
        raise Fail(f"{path} not found: run ./port msvc mirror")
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", "replace")
