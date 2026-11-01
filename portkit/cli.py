import argparse
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime

from . import bench, config, games, inspect, kettle, runtime, session, steam, trace, wine
from .util import Fail, free_gb, step, sysctl


def _env_pairs(pairs):
    env = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise Fail(f"--env wants KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


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


def cmd_run(a):
    g = games.load(a.game)
    if a.game_args:
        g.args = a.game_args
    session.ready(g)
    env = {"MTL_HUD_ENABLED": "1"} if a.hud else {}
    env.update(_env_pairs(a.env))
    log = session.report_dir(g, "run") / "wine.log"
    session.start(g, extra_env=env, log=log, fresh=not a.keep_steam)
    step(f"Output of Steam and the game: {log}")
    return 0


def cmd_bench(a):
    g = games.load(a.game)
    if a.game_args:
        g.args = a.game_args
    bench.run(g, duration=a.duration, warmup=a.warmup, label=a.label, note=a.note, hud=not a.no_hud,
              instruments=not a.no_instruments, template=a.template, timeout=a.timeout, quit=a.quit,
              extra_env=_env_pairs(a.env), attach=a.attach)
    return 0


def cmd_trace(a):
    g = games.load(a.game)
    if a.game_args:
        g.args = a.game_args
    out = trace.run(g, channels=a.channels, relay=a.relay, dump_metal=a.dump_metal,
                    sample_seconds=a.sample, sample_at=a.sample_at, timeout=a.timeout,
                    extra_env=_env_pairs(a.env))
    print((out / "summary.md").read_text())
    return 0


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
    sp.add_argument("action", choices=["install", "start", "stop", "library", "signin", "status", "inventory"])
    sp.add_argument("args", nargs="*", help="library: the library folder; start: extra Steam arguments; "
                    "signin: an account name (else a QR code), for the Kettle launch mode; "
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

    def game_cmd(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("game")
        sp.add_argument("--env", action="append", metavar="KEY=VALUE", help="extra environment, repeatable")
        sp.add_argument("--arg", action="append", dest="game_args", metavar="ARG",
                        help="game argument, repeatable; replaces the game file's args")
        return sp

    sp = game_cmd("run", "play a game under the Porting Kit")
    sp.add_argument("--hud", action="store_true", help="show the Metal performance HUD")
    sp.add_argument("--keep-steam", action="store_true", help="use the running Steam (its environment applies)")
    sp.set_defaults(fn=cmd_run)

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
