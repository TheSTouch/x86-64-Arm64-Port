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
DROPPED = ("/experimental:log",)
PCH_FLAGS = ("/Yu", "/Yc", "/Fp")
DEBUG_FLAGS = ("/Z7", "/Zi", "/ZI", "/Zo")   # debug information only: the same code without (A-9: 5,742 of 5,742)
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


def _ssh(command, **kw):
    return run(["ssh", "-o", "ServerAliveInterval=30", config.PC_HOST, command], **kw)


def _fetch(drive_paths, dest, patterns=()):
    """tar over ssh: paths of one PC drive ("D:/a/b"), each a folder (whole) or with patterns (files in it)."""
    by_drive = {}
    for p in drive_paths:
        by_drive.setdefault(p[0].upper(), []).append(p[3:])
    for drive, rels in by_drive.items():
        target = Path(dest) / drive
        target.mkdir(parents=True, exist_ok=True)
        for i in range(0, len(rels), 20):   # cmd.exe's line limit
            names = " ".join(f'"{r}/{pat}"' if patterns else f'"{r}"' for r in rels[i:i + 20] for pat in (patterns or [""]))
            ssh = f"cd /d {drive}:\\ && tar -cf - {names}"
            with open(os.devnull, "wb") as null:
                src = subprocess.Popen(["ssh", config.PC_HOST, ssh], stdout=subprocess.PIPE, stderr=null)
                untar = run(["tar", "-xf", "-", "-C", target], stdin=src.stdout, check=False)
                if src.wait() or untar.returncode:
                    raise Fail(f"fetching from {config.PC_HOST} failed: {ssh}")


def read_pc_text(path):
    if not Path(path).is_file():
        raise Fail(f"{path} not found: run ./port msvc mirror")
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", "replace")


def mirror(module="FactoryGameRecomp", project=None, check=True):
    """Fetch from the PC what a compile of `module` needs and only the PC has: UBT's response files,
    definitions and source dependencies for the module; the shared PCH's header and definitions; every
    generated include folder (UHT's Inc trees, whole). Then, with check, hash the engine headers the PC's
    compiles read (their .dep.json) on both sides."""
    m = PathMap(project or ".")
    started = time.monotonic()
    inter = intermediate(module)
    local = m.mirrored(inter)
    local.mkdir(parents=True, exist_ok=True)
    for old in list(local.glob("*.rsp")) + list(local.glob("*.dep.json")):
        old.unlink()
    _fetch([inter], m.mirror, ("*.rsp", "*.h", "*.dep.json"))
    shared = local / f"{module}.Shared.rsp"
    if not shared.is_file():
        raise Fail(f"{shared.name} not fetched: has {module} been built on the PC?")
    cmd = Command.parse(split_args(read_pc_text(shared)))
    folders, headers = set(), set()
    for d in cmd.values("/I") + cmd.values("/external:I"):
        p = m.pc(d)
        if m.generated(p):
            parts = p.split("/")
            low = [x.lower() for x in parts]
            if "inc" in low:
                folders.add("/".join(parts[:low.index("inc") + 1]))
            else:
                headers.add(p)   # the shared PCH's folder: its headers, not the PCH
    for rsp in local.glob("*.cpp.obj.rsp"):
        for f in Command.parse(split_args(read_pc_text(rsp)), read=lambda p: read_pc_text(m.mac(p))).values("/FI"):
            if m.generated(f) and not m.pc(f).lower().startswith(m.pc(inter).lower() + "/"):
                headers.add(posixpath.dirname(m.pc(f)))
    step(f"Mirroring {len(folders)} generated include folders and the headers of {len(headers)} more")
    _fetch(sorted(folders), m.mirror)
    # The shared PCH's folder also holds its own response file and dependencies (not the PCH: gigabytes).
    _fetch(sorted(headers), m.mirror, ("*.h", "*.cpp", "*.rsp", "*.dep.json"))
    took = time.monotonic() - started
    report = {"module": module, "folders": len(folders), "seconds": round(took)}
    if check:
        deps = list(local.glob("*.dep.json")) + [d for h in headers for d in m.mirrored(h).glob("*.dep.json")]
        report.update(check_engine(deps, m))
    return report


def _hashes(data):
    """SHA-1 of a file as it is, and as it would be with CRLF or LF line ends."""
    lf = data.replace(b"\r\n", b"\n")
    return {hashlib.sha1(x).hexdigest().upper() for x in (data, lf, lf.replace(b"\n", b"\r\n"))}


def check_engine(deps, m):
    """The engine headers the PC's compiles read (their .dep.json files), hashed on the PC and on the Mac's
    clone: the clone must be the PC's engine."""
    files = set()
    for dep in deps:
        data = json.loads(read_pc_text(dep)).get("Data", {})
        for f in data.get("Includes", []):
            p = m.pc(f)
            if p.lower().startswith(config.PC_ENGINE.lower() + "/") and not m.generated(p):
                files.add(p)
    if not files:
        return {"engine_files": 0}
    script = "$input | ForEach-Object { if (Test-Path -LiteralPath $_) { (Get-FileHash -Algorithm SHA1 -LiteralPath $_).Hash + ' ' + $_ } else { 'MISSING ' + $_ } }"
    out = _ssh(f'powershell -NoProfile -Command "{script}"', input="\n".join(sorted(files)) + "\n",
               capture_output=True, text=True).stdout
    same = endings = differ = absent = 0
    differing = []
    for line in out.splitlines():
        digest, _, path = line.strip().partition(" ")
        if not path or digest == "MISSING":
            continue
        mac = m.mac(path)
        if not mac.is_file():
            absent += 1
            differing.append(f"not on the Mac: {path}")
            continue
        data = mac.read_bytes()
        if hashlib.sha1(data).hexdigest().upper() == digest:
            same += 1
        elif digest in _hashes(data):
            endings += 1
        else:
            differ += 1
            differing.append(f"differs: {path}")
    return {"engine_files": len(files), "identical": same, "line_endings": endings, "different": differ,
            "absent": absent, "differing": differing}


# ---------------------------------------------------------------------------------------------- compiling

def patch_definitions(text, hooks=False):
    """A module's Definitions header for a matching compile (RECOMP_HOOKS 0) or a hook compile (1), whatever
    the PC's last build was."""
    return re.sub(r"^#define RECOMP_HOOKS \d+", f"#define RECOMP_HOOKS {1 if hooks else 0}", text, flags=re.M)


def blob_name(target, project, module="FactoryGameRecomp"):
    """A blob number (101), a blob (Module.FactoryGame.101.cpp) or a recovered file (FGFoo.cpp) to its blob; a
    file of the module itself (RecompHooks.cpp) stays itself."""
    target = Path(str(target)).name
    if target.isdigit():
        return f"Module.FactoryGame.{target}.cpp"
    if target.startswith("Module."):
        return target if target.endswith(".cpp") else target + ".cpp"
    if (Path(project) / "Source" / module / "Private" / target).is_file():
        return target
    recovered = Path(project) / "Source" / module / "Private" / "Recovered"
    for blob in sorted(recovered.glob("*.cpp")):
        if f'#include "{target}"' in blob.read_text(encoding="utf-8", errors="replace"):
            return blob.name
    raise Fail(f"{target}: in no blob of {recovered} (is it staged?)")


def response_file(module, source_name):
    """The PC's response file for one compile; for a blob the PC has not compiled yet, a sibling's with the
    names changed (UBT writes them all alike)."""
    local = PathMap(".").mirrored(intermediate(module))
    rsp = local / f"{source_name}.obj.rsp"
    if rsp.is_file():
        return read_pc_text(rsp)
    for sibling in sorted(local.glob("Module.*.cpp.obj.rsp")):
        return read_pc_text(sibling).replace(sibling.name[:-len(".obj.rsp")], source_name)
    raise Fail(f"no response file for {source_name} in {local}: run ./port msvc mirror first")


def command_for(project, module, source_name, out_dir, hooks=False, mapper=None, text=None, pch=None,
                debug_info=True, include_first=None):
    """The Mac compile of one source: the PC's response file with its paths mapped, the definitions patched for
    this kind of build, the object written to out_dir. pch: the PCH built on the Mac (`build_pch`) for /Fp, or
    None to compile without one (its header is forced with /FI either way). debug_info False leaves out /Z7
    and /Zo: the code is the same, the back end a third as long (matching reads no debug information).
    include_first: a folder searched before every include folder, so a file there stands in for the staged one
    of the same name (a permuter worker's candidate); the blob itself is unchanged."""
    m = mapper or PathMap(project)
    work = Path(out_dir)
    work.mkdir(parents=True, exist_ok=True)
    cmd = Command.parse(split_args(text if text is not None else response_file(module, source_name)),
                        read=lambda p: read_pc_text(m.mac(p)))
    items = []
    for o, v in cmd.items:
        if o is not None and (o in DROPPED or (o in PCH_FLAGS and pch is None) or
                              (o in DEBUG_FLAGS and not debug_info)):
            continue
        if o == "/Fp":
            v = wine_path(pch)
        elif o is None:
            v = wine_path(m.mac(v))
        elif o == "/Fo":
            v = wine_path(work / f"{source_name}.obj")
        elif o.lower() == "/sourcedependencies":
            v = wine_path(work / f"{source_name}.dep.json")
        elif o == "/FI" and Path(v.replace("\\", "/")).name.startswith("Definitions."):
            defs = work / Path(v.replace("\\", "/")).name
            defs.write_text(patch_definitions(read_pc_text(m.mac(v)), hooks), encoding="utf-8")
            v = wine_path(defs)
        elif v is not None:
            v = wine_path(m.mac(v))
        items.append([o, v])
    if include_first is not None:
        at = next((i for i, (o, _) in enumerate(items) if o is not None and o.lower() == "/i"), len(items))
        items.insert(at, ["/I", wine_path(include_first)])
    return Command(items)


@dataclass
class Compiled:
    source: str
    obj: Path
    seconds: float
    ok: bool
    output: str


def pch_of(module, source_name):
    """The PC path of the PCH a compile uses (its /Fp), or None."""
    fp = Command.parse(split_args(response_file(module, source_name)), read=lambda p: "").values("/Fp")
    return fp[0] if fp else None


def build_pch(pc_pch, rebuild=False):
    """The shared PCH compiled on the Mac from the PC's own response file for it (mirrored beside its header),
    once: compiling with the PCH's header forced but no PCH changes some inlining (FKey::operator= is inlined
    with the PCH, called without). Kept in config.MSVC_MIRROR/work/pch, rebuilt when the mirror is newer."""
    m = PathMap(".")
    rsp_pc = re.sub(r"\.pch$", ".obj.rsp", m.pc(pc_pch))
    rsp = m.mirrored(rsp_pc)
    work = config.MSVC_MIRROR / "work" / "pch"
    work.mkdir(parents=True, exist_ok=True)
    pch = work / Path(m.pc(pc_pch)).name
    if pch.is_file() and not rebuild and pch.stat().st_mtime > rsp.stat().st_mtime:
        return pch
    step(f"Building the shared PCH on the Mac: {pch.name}")
    cmd = Command.parse(split_args(read_pc_text(rsp)))
    items = []
    for o, v in cmd.items:
        if o is not None and o in DROPPED:
            continue
        if o == "/Yc":
            pass   # the header's name as the .cpp includes it, not a path
        elif o == "/Fp":
            v = wine_path(pch)
        elif o == "/Fo":
            v = wine_path(work / (pch.stem + ".obj"))
        elif o is not None and o.lower() == "/sourcedependencies":
            v = wine_path(work / (pch.stem + ".dep.json"))
        elif v is not None:
            v = wine_path(m.mac(v))
        items.append([o, v])
    local_rsp = work / (pch.stem + ".rsp")
    local_rsp.write_text("\n".join(Command(items).lines()) + "\n", encoding="utf-8")
    log = work / (pch.stem + ".log")
    started = time.monotonic()
    r = cl(["@" + wine_path(local_rsp)], output=log)
    if r.returncode or not pch.is_file():
        raise Fail(f"the PCH did not build ({log}):\n" + log.read_text(encoding="utf-8", errors="replace")[-3000:])
    step(f"PCH built in {time.monotonic() - started:.0f} s")
    return pch


def compile_one(project, module, source_name, out_dir, hooks=False, pch=True, debug_info=False, include_first=None):
    started = time.monotonic()
    local_pch = None
    if pch and (pc_pch := pch_of(module, source_name)):
        local_pch = build_pch(pc_pch)
    cmd = command_for(project, module, source_name, out_dir, hooks, pch=local_pch, debug_info=debug_info,
                      include_first=include_first)
    rsp = Path(out_dir) / f"{source_name}.rsp"
    rsp.write_text("\n".join(cmd.lines()) + "\n", encoding="utf-8")
    obj = Path(out_dir) / f"{source_name}.obj"
    if obj.exists():
        obj.unlink()
    log = Path(out_dir) / f"{source_name}.log"
    r = cl(["@" + wine_path(rsp)], output=log)
    output = "\n".join(line for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
                       if line.strip() and line.strip() != source_name)
    return Compiled(source_name, obj, time.monotonic() - started, r.returncode == 0 and obj.is_file(), output)


def compile_many(project, module, sources, out_dir, hooks=False, jobs=2, pch=True, debug_info=False):
    if pch:   # built once, before the parallel compiles
        for pc_pch in {p for s in sources if (p := pch_of(module, s))}:
            build_pch(pc_pch)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return list(pool.map(lambda s: compile_one(project, module, s, out_dir, hooks, pch, debug_info), sources))


def shipped_dll(module):
    name = SHIPPED.get(module, module)
    return config.GAME_BINARIES / f"{config.PC_TARGET}-{name}-Win64-Shipping.dll"


# ---------------------------------------------------------------------------------------------- comparing objects

def compare_objects(a, b):
    """Two compiles of the same source, function by function: (name, state), state same, differs, only-a or
    only-b. Bytes a relocation fills in either are masked. A compile without the PCH also holds what the PCH's
    object holds otherwise: the static initializers and inline functions of the PCH's headers."""
    fa = {f.name: f for f in coff.parse(Path(a).read_bytes()).functions()}
    fb = {f.name: f for f in coff.parse(Path(b).read_bytes()).functions()}
    out = []
    for name in sorted(fa.keys() | fb.keys()):
        if name not in fa or name not in fb:
            out.append((name, "only-a" if name in fa else "only-b"))
            continue
        x, y = fa[name], fb[name]
        masked = x.masked | y.masked
        same = len(x.code) == len(y.code) and all(x.code[i] == y.code[i] for i in range(len(x.code)) if i not in masked)
        out.append((name, "same" if same else "differs"))
    return out
