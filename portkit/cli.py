import argparse
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import bench, config, decomp, games, hooks, msvc, pdb, inspect, kettle, overlay, runtime, session, steam, trace, wine
from .util import Fail, free_gb, step, sysctl


def _env_pairs(pairs):
    env = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise Fail(f"--env wants KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


def _game(a):
    """The game a run/bench/trace names, with this run's --arg and --kettle."""
    g = games.load(a.game)
    if a.game_args:
        g.args = a.game_args
    if a.kettle:
        g.launch = "kettle"
    return g


def cmd_doctor(a):
    checks = []

    def check(ok, label, detail="", fix=""):
        checks.append(ok)
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        print(f"[{mark}] {label}" + (f": {detail}" if detail else "") + (f"\n       -> {fix}" if fix and ok is not True else ""))

    check(platform.machine() == "arm64", "Apple Silicon", sysctl("machdep.cpu.brand_string"))
    mac = platform.mac_ver()[0]
    check(int(mac.split(".")[0] or 0) >= 14, "macOS 14 or later", mac)
    rosetta = subprocess.run(["arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode == 0
    check(rosetta, "Rosetta 2", fix="softwareupdate --install-rosetta --agree-to-license")
    mem = round(int(sysctl("hw.memsize") or 0) / 2**30)
    check(True if mem >= 16 else None, "Memory", f"{mem} GB", "under 16 GB, modern games swap heavily")
    xctrace = shutil.which("xcrun") and subprocess.run(["xcrun", "--find", "xctrace"], capture_output=True).returncode == 0
    check(True if xctrace else None, "Instruments (xctrace) for benchmarks", fix="install Xcode")
    free = free_gb(config.HOME)
    check(True if free >= 30 else None, f"Free space on {config.HOME}", f"{free:.0f} GB",
          "prefixes and shader caches take tens of GB")
    for name in config.RUNTIMES:
        default = name == config.DEFAULT_RUNTIME
        ok = True if runtime.installed(name) else (False if default else None)
        check(ok, f"Runtime {name}" + (" (default)" if default else ""),
              runtime.describe(name), f"./port setup --runtime {name}")
        if runtime.installed(name):
            check(True if (wine.prefix_path(name) / "system.reg").exists() else None,
                  f"  prefix {name}", fix=f"./port setup --runtime {name}")
            check(True if steam.installed(name) else None, f"  Windows Steam in {name}",
                  fix=f"./port setup --runtime {name}")
    info = kettle.token_info(kettle.stored_token())
    check(True if info else None, "Steam sign-in for the Kettle launch mode",
          f"SteamID {info['steamid']}" if info else "none", "./port steam signin")
    for gid in games.all_ids():
        g = games.load(gid)
        missing = g.resolve()
        check(True if not missing else None, f"Game {g.name} [{g.runtime}]",
              str(g.exe_path) if not missing else f"missing: {', '.join(missing)}",
              f"is it installed in {g.library}?")
    return 0 if all(c is not False for c in checks) else 1


def cmd_setup(a):
    runtime.install(a.runtime, force=a.force)
    wine.create_prefix(a.runtime)
    if not a.no_steam:
        steam.install(a.runtime)
    flag = "" if a.runtime == config.DEFAULT_RUNTIME else f" --runtime {a.runtime}"
    step(f"Done. Next: ./port steam start{flag}, sign in, quit Steam, then "
         f"./port steam library{flag} /Volumes/SMART/SteamLibraryWindows")
    return 0


def cmd_steam(a):
    if a.action == "install":
        steam.install(a.runtime)
    elif a.action == "start":
        log = config.REPORTS / f"steam-{a.runtime}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        steam.start(a.runtime, a.args, log=log)
        step(f"Steam is starting (the first start updates itself); its output goes to {log}")
    elif a.action == "stop":
        steam.stop(a.runtime)
    elif a.action == "library":
        if not a.args:
            raise Fail("usage: ./port steam library <path to a Windows Steam library>")
        steam.add_library(a.runtime, a.args[0])
    elif a.action == "signin":
        step("Signing in to Steam for the Kettle launch mode (QR code, or account and password if you named one)")
        code = kettle.signin(a.args[0] if a.args else None)
        if code:
            raise Fail(f"not signed in; Kettle's log: {config.REPORTS / 'steam-signin.log'}")
    elif a.action == "status":
        info = kettle.token_info(kettle.stored_token())
        if not info:
            raise Fail("no Steam sign-in on this Mac for the Kettle launch mode: run ./port steam signin")
        expires = datetime.fromtimestamp(info["expires"]).strftime("%Y-%m-%d") if info["expires"] else "?"
        print(f"Signed in to Steam as SteamID {info['steamid']} (Keychain \"{kettle.KEYCHAIN_SERVICE}\", "
              f"until {expires})")
    elif a.action == "check":
        if not a.args:
            raise Fail("usage: ./port steam check <game>")
        g = games.load(a.args[0])
        session.ready(g)
        out = session.report_dir(g, "steam-check")
        code, text = kettle.check(g, out)
        print(text, end="")
        if code:
            raise Fail(f"the bridge did not pass: {out / 'steam_api.log'} and {out / 'wine.log'}")
        step(f"Signed in and licensed for {g.name}, through our steam_api64.dll under Wine")
    elif a.action == "inventory":
        if not a.args:
            raise Fail("usage: ./port steam inventory <game>")
        g = games.load(a.args[0])
        session.ready(g)
        report = kettle.run_inventory(g, session.report_dir(g, "steam-inventory"))
        step(f"Inventory of the Steamworks API {g.name} uses: {report}")
    return 0


def cmd_wine(a):
    args = a.args[1:] if a.args[:1] == ["--"] else a.args
    if not args:
        raise Fail("usage: ./port wine [--runtime R] -- <program> [args] (e.g. winecfg, regedit)")
    return wine.wine(a.runtime, args, check=False).returncode


def cmd_games(a):
    ids = [a.game] if a.game else games.all_ids()
    for gid in ids:
        g = games.load(gid)
        missing = g.resolve()
        print(f"{g.id}: {g.name}")
        for key in ("library", "steam_appid", "install_dir", "exe", "process", "launch", "runtime"):
            print(f"  {key:<12} {getattr(g, key) or '-'}")
        if g.env:
            print(f"  {'env':<12} " + " ".join(f"{k}={v}" for k, v in g.env.items()))
        print(f"  {'status':<12} " + ("ready" if not missing else f"missing {', '.join(missing)}"))
    return 0


def cmd_inspect(a):
    target = a.target
    if target in games.all_ids():
        g = games.load(target)
        g.resolve()
        if not g.root or not g.root.exists():
            raise Fail(f"{g.name} is not installed yet")
        target, gid = g.root, g.id
    else:
        gid = None
    report = inspect.inspect_path(target)
    print(json.dumps(report, indent=2) if a.json else inspect.format_report(report))
    if gid:
        out = config.REPORTS / gid
        out.mkdir(parents=True, exist_ok=True)
        session.write_json(out / "inspect.json", report)
        (out / "inspect.txt").write_text(inspect.format_report(report) + "\n", encoding="utf-8")
        print(f"\nSaved to {out}/inspect.json")
    return 0


def cmd_decomp(a):
    if a.families:
        groups = decomp.families(a.dll, a.trace, a.source)
        for i, g in enumerate(groups, 1):
            print(f"family {i}: {g.kind}, {len(g.members)} functions, {g.bytes} bytes")
            for rva, length, names, where in g.members:
                folded = f" (+{len(names) - 1} folded at this address)" if len(names) > 1 else ""
                print(f"  {rva:#010x} {length:>6}  {where}  {names[0]}{folded}")
        big = [g for g in groups if len(g.members) >= 3]
        print(f"{len(groups)} families of 2 or more: {sum(len(g.members) for g in groups)} functions, "
              f"{sum(g.bytes for g in groups)} bytes; of 3 or more: {len(big)} families, "
              f"{sum(len(g.members) for g in big)} functions, {sum(g.bytes for g in big)} bytes", file=sys.stderr)
        return 0
    if a.trace:
        rows, files = decomp.trace(a.dll, a.trace)
        for r in rows:
            print(f"{r.ms:>8} {r.thread:>6} {r.rva:#010x} {r.length:>6}  {r.source or '-'}  {r.name}")
        print(f"\n{len(files)} source files, in order of first execution:")
        for name, n, size, first, spans, span_bytes in files:
            print(f"{first:>8} ms  {n:>5} functions {size:>8} bytes  {spans:>5} spans {span_bytes:>8} bytes  {name}")
        print(f"{len(rows)} functions run, {sum(r.length for r in rows)} bytes; from source files: "
              f"{sum(f[1] for f in files)} functions, {sum(f[2] for f in files)} bytes, {sum(f[4] for f in files)} "
              f"distinct spans, {sum(f[5] for f in files)} bytes", file=sys.stderr)
        return 0
    if a.match:
        results = decomp.match(a.dll, a.match, a.diff or ())
        for r in results:
            if a.diff and (r.name in a.diff or r.shipped in a.diff):
                print(decomp.diff_listing(r))
            if not a.diff or r.name in a.diff or r.shipped in a.diff:
                print(decomp.match_line(r))
        counts = {s: sum(r.state == s for r in results) for s in ("matched", "differs", "missing")}
        print(f"{len(results)} functions: {counts['matched']} matched, {counts['differs']} differing, "
              f"{counts['missing']} not in the shipped module", file=sys.stderr)
        return 1 if counts["differs"] else 0
    if a.out:
        root, fns = decomp.dump(a.dll, a.out, a.source)
        print(f"{len(fns)} functions ({sum(f.length for f in fns)} bytes) written to {root}")
        return 0
    if a.type:
        print(pdb.Types(decomp.Module.pdb_of(a.dll)).describe(a.type), end="")
        return 0
    m = decomp.Module(a.dll)
    if a.disasm:
        print(m.disassemble(m.function(a.disasm)), end="")
        return 0
    fns = m.functions if a.all else m.game_functions(a.source)
    for f in fns:
        source = f.source.replace("\\", "/").rsplit("/", 1)[-1] if f.source else "-"
        print(f"{m.rva(f.segment, f.offset):#010x} {f.length:>6}  {source}:{f.first_line}  {f.name}")
    print(f"{len(fns)} functions, {sum(f.length for f in fns)} bytes", file=sys.stderr)
    return 0


def cmd_hooks(a):
    objdir = Path(a.objects)
    objects = sorted(objdir.glob("*.obj"))
    recovered = [objdir / (n if n.endswith(".obj") else n + ".obj") for n in a.recovered]
    for r in recovered:
        if not r.is_file():
            raise Fail(f"{r} not found")
    skip = set()
    if a.skip:
        skip = {line.split()[0] for line in Path(a.skip).read_text().splitlines() if line.strip() and not line.startswith("#")}
    p = hooks.generate(a.dlls, objects, recovered, a.out, a.trace, skip)
    for h in p.hooks:
        print(f"hook     {h.rva:#010x} {h.length:>6}  {h.name}")
    for name, why in p.skipped:
        print(f"skipped  {name}: {why}")
    thunks = sum(s.thunk for s in p.slots)
    print(f"{len(p.hooks)} hooks ({sum(h.length for h in p.hooks)} bytes), {len(p.slots) - thunks} import slots, "
          f"{thunks} thunks, {len(p.left)} symbols left to the linker" + (", trace table" if a.trace else "")
          + f": {a.out}", file=sys.stderr)
    return 0


def cmd_overlay(a):
    g = games.load(a.game)
    if not g.root or not g.root.is_dir():
        raise Fail(f"{g.name} is not installed (games/{g.id}.toml)")
    replaced = overlay.build(g.root, a.dir, a.dll or [])
    for rel, src in replaced.items():
        print(f"  {rel}  <-  {src}")
    print(f"Overlay of {g.root} in {a.dir}: {len(replaced)} file(s) replaced, everything else linked")
    print(f"Start it with: ./port run {g.id} --overlay {a.dir} --kettle")
    return 0


def cmd_run(a):
    g = _game(a)
    session.ready(g)
    if a.overlay:
        overlay.use(g, a.overlay)
        step(f"Running from the overlay {g.overlay}")
    env = {"MTL_HUD_ENABLED": "1"} if a.hud else {}
    env.update(_env_pairs(a.env))
    log = session.report_dir(g, "run") / "wine.log"
    session.start(g, extra_env=env, log=log, fresh=not a.keep_steam)
    step(f"Output of {'the game' if g.launch == 'kettle' else 'Steam and the game'}: {log}")
    return 0


def cmd_bench(a):
    g = _game(a)
    bench.run(g, duration=a.duration, warmup=a.warmup, label=a.label, note=a.note, hud=not a.no_hud,
              instruments=not a.no_instruments, template=a.template, timeout=a.timeout, quit=a.quit,
              extra_env=_env_pairs(a.env), attach=a.attach)
    return 0


def cmd_trace(a):
    g = _game(a)
    out = trace.run(g, channels=a.channels, relay=a.relay, dump_metal=a.dump_metal,
                    sample_seconds=a.sample, sample_at=a.sample_at, timeout=a.timeout,
                    extra_env=_env_pairs(a.env))
    print((out / "summary.md").read_text())
    return 0


def cmd_msvc(a):
    if a.action == "cl":
        return msvc.cl(a.args).returncode
    if a.action == "flags":
        obj = Path(a.obj)
        info = msvc.build_info(obj.read_bytes())
        for name in msvc.BUILDINFO_FIELDS[:-1]:
            print(f"{name:<9} {getattr(info, name) or '(not recorded: /experimental:deterministic leaves it out)'}")
        print(f"{'recorded':<9} {info.command}")
        rsp = Path(a.rsp) if a.rsp else None
        if rsp is None:
            module = obj.parent.name if obj.parent.name in msvc.SHIPPED else "FactoryGameRecomp"
            guess = msvc.PathMap(".").mirrored(msvc.intermediate(module)) / (obj.name + ".rsp")
            rsp = guess if guess.is_file() else None
        if rsp is None:
            print("no response file (--rsp, or ./port msvc mirror): the include directories, definitions and forced "
                  "includes are not in the object", file=sys.stderr)
            return 0
        m = msvc.PathMap(".")
        cmd = msvc.Command.parse(msvc.split_args(msvc.read_pc_text(rsp)), read=lambda p: msvc.read_pc_text(m.mac(p)))
        print(f"{'response':<9} {rsp}: {len(cmd.items)} arguments, {len(cmd.values('/I'))} include directories, "
              f"{len(cmd.values('/external:I'))} external, {len(cmd.values('/FI'))} forced includes, "
              f"{sum(o is not None and o.startswith('/D') for o, _ in cmd.items)} definitions")
        missing = msvc.missing_flags(info.command, cmd)
        print(f"{'check':<9} " + (f"recorded but not in the response file: {' '.join(missing)}" if missing
                                  else "every recorded flag is in the response file"))
        if a.args_out:
            print("\n".join(cmd.lines()))
        return 1 if missing else 0
    if a.action == "mirror":
        report = msvc.mirror(a.module, check=not a.no_check)
        print(f"{report['folders']} generated include folders and {a.module}'s response files mirrored to "
              f"{config.MSVC_MIRROR / 'pc'} in {report['seconds']} s")
        if "engine_files" in report and report["engine_files"]:
            print(f"engine headers the PC's compiles read: {report['engine_files']}; identical on the Mac "
                  f"{report['identical']}, the same but for line ends {report['line_endings']}, different "
                  f"{report['different']}, not in the clone {report['absent']}")
            for line in report["differing"][:20]:
                print("  " + line)
            return 1 if report["different"] or report["absent"] else 0
        return 0
    if a.action == "compare":
        rows = msvc.compare_objects(a.obj, a.other)
        for name, state in rows:
            if state in ("differs", "only-a") or a.verbose:
                print(f"{state:<8} {name}")
        n = {s: sum(r == s for _, r in rows) for s in ("same", "differs", "only-a", "only-b")}
        print(f"{n['same'] + n['differs']} functions in both: {n['same']} with the same code, {n['differs']} not; "
              f"{n['only-a']} only in the first, {n['only-b']} only in the second", file=sys.stderr)
        return 1 if n["differs"] or n["only-a"] else 0
    if a.action == "match":
        project = Path(a.project)
        sources = [msvc.blob_name(t, project, a.module) for t in a.targets]
        out = Path(a.out) if a.out else config.MSVC_BUILDS / project.name / a.module
        if config.inside_repository(out):
            raise Fail(f"{out} is inside this repository; objects compiled from recovered code stay outside it")
        results = msvc.compile_many(project, a.module, sources, out, hooks=a.hooks, jobs=a.jobs, pch=not a.no_pch)
        dll = msvc.shipped_dll(a.module)
        differs = failed = 0
        for c in results:
            print(f"compiled {c.source} in {c.seconds:.0f} s" if c.ok else f"FAILED   {c.source} ({c.seconds:.0f} s)",
                  file=sys.stderr)
            if not c.ok:
                failed += 1
                print(c.output, file=sys.stderr)
                continue
            if a.no_match:
                continue
            for r in decomp.match(dll, c.obj):
                if r.state == "missing":
                    continue
                shipped = f"  [{r.shipped}]" if r.shipped else ""
                print(f"{r.state:<8} {r.name}{shipped}" + (f": {r.detail}" if r.detail else ""))
                differs += r.state == "differs"
        return 1 if failed or differs else 0
    return 0


def _msvc_args(a):
    """The positional ARGs of `msvc`, by action."""
    want = {"flags": 1, "compare": 2}
    if a.action in want and len(a.args) != want[a.action]:
        raise Fail(f"usage: ./port msvc {a.action} " + " ".join(["OBJ"] * want[a.action]))
    if a.action == "flags":
        a.obj = a.args[0]
    elif a.action == "compare":
        a.obj, a.other = a.args
    elif a.action == "match":
        if len(a.args) < 2:
            raise Fail("usage: ./port msvc match STAGED_PROJECT BLOB|File.cpp [...]")
        a.project, a.targets = a.args[0], a.args[1:]
    return cmd_msvc(a)



def parser():
    p = argparse.ArgumentParser(prog="port", description="Windows games on Apple Silicon: Porting Kit "
                                "(Game Porting Toolkit) now, native recompilation where a game has one.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def prefixed(sp):
        sp.add_argument("--runtime", default=config.DEFAULT_RUNTIME, choices=list(config.RUNTIMES),
                        help="Wine runtime, each with its own prefix (default: %(default)s)")
        return sp

    sub.add_parser("doctor", help="check this Mac and the setup").set_defaults(fn=cmd_doctor)

    sp = prefixed(sub.add_parser("setup", help="install a Wine runtime, its prefix and Windows Steam"))
    sp.add_argument("--force", action="store_true", help="reinstall the runtime")
    sp.add_argument("--no-steam", action="store_true")
    sp.set_defaults(fn=cmd_setup)

    sp = prefixed(sub.add_parser("steam", help="Windows Steam in the prefix"))
    sp.add_argument("action", choices=["install", "start", "stop", "library", "signin", "status", "check", "inventory"])
    sp.add_argument("args", nargs="*", help="library: the library folder; start: extra Steam arguments; "
                    "signin: an account name (else a QR code), for the Kettle launch mode; "
                    "check: a game, its sign-in and license through our steam_api64.dll; "
                    "inventory: a game, run with a logging steam_api64.dll and no Steam")
    sp.set_defaults(fn=cmd_steam)

    sp = prefixed(sub.add_parser("wine", help="run a program in the prefix (winecfg, regedit...)"))
    sp.add_argument("args", nargs=argparse.REMAINDER)
    sp.set_defaults(fn=cmd_wine)

    sp = sub.add_parser("games", help="list games and what is known about them")
    sp.add_argument("game", nargs="?")
    sp.set_defaults(fn=cmd_games)

    sp = sub.add_parser("inspect", help="static report on a game folder, .exe or .dll (reads files only)")
    sp.add_argument("target", help="a game id, a folder or a file")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_inspect)

    sp = sub.add_parser("decomp", help="a module's functions from its PDB, and their disassembly (reads files only)")
    sp.add_argument("dll", help="a .dll or .exe with its .pdb next to it")
    sp.add_argument("--source", help="only functions whose source path contains this (default: the game's own)")
    sp.add_argument("--all", action="store_true", help="list every function, engine templates included")
    sp.add_argument("--disasm", metavar="NAME", help="disassemble one function (name or decorated name)")
    sp.add_argument("--type", metavar="NAME", help="a class, struct or enum: layout, members, vtable slots")
    sp.add_argument("--out", metavar="DIR", help="write functions.tsv and one .asm per function (outside this repository)")
    sp.add_argument("--match", metavar="OBJ", help="compare each function of a compiled .obj with the shipped one "
                    "(relocated operands masked): matched, or the first difference; each line ends with a score, "
                    "the instructions not the same once both are aligned")
    sp.add_argument("--diff", action="append", metavar="NAME", help="with --match: this function's shipped and "
                    "compiled disassembly side by side (decorated or shipped name; may be repeated)")
    sp.add_argument("--families", action="store_true", help="group the game's functions (with --trace, those the "
                    "traces ran) whose code is the same once numbers are masked, or near it (lengths within 10 %%, "
                    "instructions 90 %% the same)")
    sp.add_argument("--trace", nargs="+", metavar="TSV", help="name the functions of traces from the hook loader's "
                    "trace mode (RecompTrace.tsv; each function once), and sum them by source file: functions and "
                    "distinct source spans (template instances and copies of one inline counted once)")
    sp.set_defaults(fn=cmd_decomp)

    sp = sub.add_parser("hooks", help="the tables object a hook DLL links in (windows/recomp-hooks): hooks over the "
                        "shipped functions, slots for what the recovered code takes from project modules")
    sp.add_argument("dlls", nargs="+", metavar="DLL", help="the hooked module first, then the project modules its "
                    "code references; each with its .pdb next to it")
    sp.add_argument("--objects", required=True, metavar="DIR", help="the hook DLL's compiled objects")
    sp.add_argument("--recovered", nargs="+", default=[], metavar="OBJ",
                    help="the objects compiled from recovered sources, by name in DIR (Foo.cpp or Foo.cpp.obj)")
    sp.add_argument("--out", required=True, metavar="FILE", help="the tables object to write (outside this repository)")
    sp.add_argument("--trace", action="store_true", help="also list every function of the hooked module, for trace mode")
    sp.add_argument("--skip", metavar="FILE", help="decorated names not to hook, one per line (those that do not match)")
    sp.set_defaults(fn=cmd_hooks)

    def game_cmd(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("game")
        sp.add_argument("--env", action="append", metavar="KEY=VALUE", help="extra environment, repeatable")
        sp.add_argument("--arg", action="append", dest="game_args", metavar="ARG",
                        help="game argument, repeatable; replaces the game file's args")
        sp.add_argument("--kettle", action="store_true",
                        help="launch = kettle for this run: our steam_api64.dll, no Windows Steam")
        return sp

    sp = game_cmd("run", "play a game under the Porting Kit")
    sp.add_argument("--hud", action="store_true", help="show the Metal performance HUD")
    sp.add_argument("--keep-steam", action="store_true", help="use the running Steam (its environment applies)")
    sp.add_argument("--overlay", metavar="DIR", help="start the game from an overlay folder (./port overlay); needs --kettle")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("overlay", help="an overlay game folder: the install linked, some DLLs replaced (never writes to the game)")
    sp.add_argument("game")
    sp.add_argument("dir", help="the overlay folder, beside the game and outside this repository")
    sp.add_argument("--dll", action="append", metavar="FILE",
                    help="a built file replacing the game's file of the same name (its .pdb too), repeatable")
    sp.set_defaults(fn=cmd_overlay)

    sp = game_cmd("bench", "benchmark a scene: memory, CPU, swap and an Instruments trace")
    sp.add_argument("--duration", type=int, default=120, help="seconds recorded (default: %(default)s)")
    sp.add_argument("--warmup", type=int, default=45, help="seconds to reach the scene (default: %(default)s)")
    sp.add_argument("--label", default="", help="short name for the run, e.g. 1080p-high")
    sp.add_argument("--note", default="", help="what was benchmarked, settings")
    sp.add_argument("--no-hud", action="store_true")
    sp.add_argument("--no-instruments", action="store_true")
    sp.add_argument("--template", default="Game Performance", help="Instruments template")
    sp.add_argument("--timeout", type=int, default=900, help="seconds to wait for the game to start")
    sp.add_argument("--quit", action="store_true", help="stop the game and Steam afterwards")
    sp.add_argument("--attach", action="store_true", help="measure the game already running instead of starting it")
    sp.set_defaults(fn=cmd_bench)

    sp = game_cmd("trace", "watch a run for the recompilation plan: DLLs, API calls, failures")
    sp.add_argument("--channels", default="+loaddll", help="Wine debug channels (default: %(default)s)")
    sp.add_argument("--relay", action="store_true", help="log every Windows API call and its call site (slow)")
    sp.add_argument("--dump-metal", action="store_true", help="ask Metal to dump libraries and pipelines (experimental)")
    sp.add_argument("--sample", type=int, default=0, metavar="SECS", help="sample call stacks for SECS seconds")
    sp.add_argument("--sample-at", type=int, default=60, metavar="SECS", help="when to sample (default: %(default)s)")
    sp.add_argument("--timeout", type=int, default=900)
    sp.set_defaults(fn=cmd_trace)

    sp = sub.add_parser("msvc", help="MSVC 14.38 under Wine: the PC's compiles replayed on the Mac, one unity "
                        "blob compiled and matched without UBT")
    sp.add_argument("action", choices=["cl", "flags", "mirror", "match", "compare"],
                    help="cl: run cl.exe with ARGS; flags: the command line an object records, and its response "
                    "file's; mirror: fetch what only the PC has; match: compile blobs of a staged project and match "
                    "them; compare: two objects' code, function by function")
    sp.add_argument("args", nargs="*", metavar="ARG", help="cl: its arguments; flags: OBJ; compare: OBJ OBJ; "
                    "match: STAGED_PROJECT then blob numbers, blob names or recovered File.cpp names")
    sp.add_argument("--rsp", metavar="FILE", help="flags: the response file (default: the mirrored one of the same name)")
    sp.add_argument("--args", dest="args_out", action="store_true", help="flags: print the response file's arguments")
    sp.add_argument("--module", default="FactoryGameRecomp", help="the build module (default: %(default)s)")
    sp.add_argument("--no-check", action="store_true", help="mirror: do not hash the engine headers on both sides")
    sp.add_argument("--hooks", action="store_true", help="match: compile as the hook build (RECOMP_HOOKS=1)")
    sp.add_argument("--jobs", type=int, default=2, help="match: parallel compiles (default: %(default)s)")
    sp.add_argument("--out", metavar="DIR", help="match: where the objects go (default: under config.MSVC_BUILDS)")
    sp.add_argument("--no-match", action="store_true", help="match: compile only")
    sp.add_argument("--no-pch", action="store_true", help="match: no PCH, its header forced (faster to start, but some "
                    "inlining differs from the PC's)")
    sp.add_argument("-v", "--verbose", action="store_true", help="compare: list the functions that are the same too")
    sp.set_defaults(fn=_msvc_args)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        return a.fn(a)
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
