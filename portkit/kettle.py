"""The Kettle launch mode: Steam games under Wine without Windows Steam.

steam_api/ builds our own steam_api64.dll. The game loads it in place of Valve's, as a Wine
builtin: it lives under runtime/steam-api/<build>/ and Wine finds it through WINEDLLPATH, with
WINEDLLOVERRIDES=steam_api64=b, so the game's folder is never touched. Wine swaps a builtin in for
a file it finds (the game's own steam_api64.dll, which every Steam game ships); it does not load
a builtin of that name out of nothing. Two builds:

- "bridge": the Windows DLL (steam_api/pe) plus its Mac side (steam_api/unix, an x86-64 Wine
  unixlib) that signs in through Kettle (vendor/kettle) and has Steam sign the game's ownership
  ticket. Without a sign-in or a license, SteamAPI_Init fails;
- "inventory": the Windows DLL alone, no Steam at all, which logs every interface and method the
  game uses (steam_api/satisfactory-interfaces.md).
"""

import re
import struct
from collections import defaultdict
from pathlib import Path

from . import config, steam, wine
from .util import Fail, run, step

SOURCE = config.ROOT / "steam_api"
BUILD_DIR = config.RUNTIME_DIR / "steam-api"
DLL = "steam_api64.dll"
BUILTIN_SIGNATURE = b"Wine builtin DLL\0"   # what Wine's server looks for right after the DOS header


def mark_wine_builtin(path):
    """Write Wine's builtin signature into a PE's DOS stub, as winebuild --builtin does.

    Wine only loads a DLL found through WINEDLLPATH, and only gives it a unixlib, when it carries
    the signature at offset 0x40, before the PE header.
    """
    path = Path(path)
    data = bytearray(path.read_bytes())
    if data[:2] != b"MZ":
        raise Fail(f"{path} is not a PE file")
    pe_header = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_header < 0x40 + len(BUILTIN_SIGNATURE):
        raise Fail(f"{path}: no room for Wine's builtin signature (PE header at {pe_header:#x})")
    data[0x40:0x40 + len(BUILTIN_SIGNATURE)] = BUILTIN_SIGNATURE
    path.write_bytes(bytes(data))


def is_wine_builtin(path):
    with open(path, "rb") as f:
        return f.read(0x40 + len(BUILTIN_SIGNATURE))[0x40:] == BUILTIN_SIGNATURE


def build_root(name):
    """runtime/steam-api/<name>: a WINEDLLPATH entry (x86_64-windows/, x86_64-unix/)."""
    return BUILD_DIR / name


def _stale(target, sources):
    if not target.exists():
        return True
    newest = max(p.stat().st_mtime for p in sources)
    return target.stat().st_mtime < newest


def build_dll(name, defines=(), extra_sources=()):
    """Cross-compile steam_api/pe into runtime/steam-api/<name>/x86_64-windows/steam_api64.dll."""
    pe_dir = SOURCE / "pe"
    sources = [pe_dir / "steam_api.c", *extra_sources]
    target = build_root(name) / "x86_64-windows" / DLL
    if not _stale(target, [*sources, *pe_dir.glob("*.h")]):
        return target
    cc = steam.zig_cc()
    step(f"Building {DLL} ({name})")
    target.parent.mkdir(parents=True, exist_ok=True)
    run([*cc, "-shared", "-O2", "-s", "-Wall", "-Wno-unused-function", *[f"-D{d}" for d in defines],
         "-o", target, *sources], cwd=target.parent)
    for implib in target.parent.glob("*.lib"):
        implib.unlink()
    mark_wine_builtin(target)
    return target


def build_inventory():
    build_dll("inventory", ["STEAM_API_INVENTORY"])
    return build_root("inventory")


def environment(game, root, log=None, base=None):
    """What a run with our steam_api64.dll from `root` adds to the game's environment."""
    base = base or {}
    paths = [str(root)] + [p for p in base.get("WINEDLLPATH", "").split(":") if p]
    overrides = [o for o in base.get("WINEDLLOVERRIDES", "").split(";") if o and not o.startswith("steam_api64=")]
    env = {
        "WINEDLLPATH": ":".join(paths),
        "WINEDLLOVERRIDES": ";".join(["steam_api64=b", *overrides]),
        # How Steam tells a game its app id when it starts it.
        "SteamAppId": str(game.steam_appid),
        "SteamGameId": str(game.steam_appid),
    }
    if game.root:
        env["PORTKIT_GAME_DIR"] = wine.windows_path(game.root)
        manifest = Path(game.library, "steamapps", f"appmanifest_{game.steam_appid}.acf")
        if manifest.exists():
            from . import vdf
            env["PORTKIT_GAME_BUILDID"] = vdf.load(manifest).get("AppState", {}).get("buildid", "")
    env["PORTKIT_GAME_NAME"] = game.name[:60]
    if log:
        env["PORTKIT_STEAM_LOG"] = wine.windows_path(log)
    return {k: v for k, v in env.items() if v}


# --- the inventory -----------------------------------------------------------------------------

LINE = re.compile(r"\[steam_api\] (.*)")


def parse_log(text):
    """What the inventory build logged: exports, interfaces, methods with their call counts, callbacks."""
    found = {"exports": [], "interfaces": [], "missing": [], "methods": {}, "callbacks": {}, "counts": {}}
    for raw in text.splitlines():
        m = LINE.search(raw)
        if not m:
            continue
        line = m.group(1)
        if mm := re.fullmatch(r"export (\w+)", line):
            if mm.group(1) not in found["exports"]:   # each process logs its own first calls
                found["exports"].append(mm.group(1))
        elif mm := re.fullmatch(r"interface (\S+) \(([^)]*)\)(: (.*))?", line):
            version, how, problem = mm.group(1), mm.group(2), mm.group(4)
            if problem:
                found["missing"].append((version, how, problem))
            elif version not in [v for v, _ in found["interfaces"]]:
                found["interfaces"].append((version, how))
        elif mm := re.fullmatch(r"call (\w+)::(\w+) \((\S+) slot (\d+)\)(, stub)?", line):
            found["methods"].setdefault((mm.group(1), mm.group(2)), (mm.group(3), int(mm.group(4))))
        elif mm := re.fullmatch(r"register callback (\d+) (\S+)( \(game server\))?", line):
            found["callbacks"][int(mm.group(1))] = mm.group(2) + (mm.group(3) or "")
        elif mm := re.fullmatch(r"\s*(\w+)::(\w+) (\d+)( \(stub\))?", line):
            found["counts"][(mm.group(1), mm.group(2))] = int(mm.group(3))
    return found


def inventory_report(text, title):
    """The inventory as Markdown, from the inventory build's log."""
    f = parse_log(text)
    out = [f"# {title}", ""]
    out += ["## Exports called", ""] + [f"- `{e}`" for e in f["exports"]] + [""]
    out += ["## Interfaces handed out", "", "| Version | Asked through |", "|---|---|"]
    out += [f"| `{v}` | {how} |" for v, how in f["interfaces"]] + [""]
    if f["missing"]:
        out += ["Asked for and not handed out:", ""]
        seen = set()
        for v, how, problem in f["missing"]:
            if (v, problem) not in seen:
                seen.add((v, problem))
                out.append(f"- `{v}` ({how}): {problem}")
        out.append("")
    by_class = defaultdict(list)
    for (klass, method), (version, slot) in f["methods"].items():
        by_class[(klass, version)].append((slot, method, f["counts"].get((klass, method))))
    out += ["## Methods called", "", "Counts are calls until the game quit (blank: the game did not quit cleanly).", ""]
    for (klass, version), methods in sorted(by_class.items()):
        out += [f"### {klass} (`{version}`)", "", "| Slot | Method | Calls |", "|---|---|---|"]
        out += [f"| {slot} | `{method}` | {count if count is not None else ''} |" for slot, method, count in sorted(methods)]
        out.append("")
    out += ["## Callbacks registered", "", "| Id | Struct |", "|---|---|"]
    out += [f"| {i} | `{name}` |" for i, name in sorted(f["callbacks"].items())] + [""]
    return "\n".join(out)


def run_inventory(game, out, timeout=900):
    """Run the game with the inventory build until it quits (or timeout s); write out/inventory.md."""
    import subprocess

    root = build_inventory()
    log = out / "steam_api.log"
    step(f"Stopping prefix {game.prefix} (Windows Steam included): the game runs without it")
    steam.stop(game.prefix)
    env = {**game.env, **environment(game, root, log, game.env)}
    exe = game.launcher_path
    step(f"Starting {exe} with the inventory {DLL}")
    proc = wine.spawn(game.prefix, [exe, *game.args], extra_env=env, log=out / "wine.log", cwd=exe.parent)
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        step(f"Still running after {timeout} s: stopping prefix {game.prefix}")
        wine.kill(game.prefix)
    except KeyboardInterrupt:
        wine.kill(game.prefix)
    text = log.read_text(errors="replace") if log.exists() else ""
    if not text:
        raise Fail(f"the game never loaded our {DLL}: see {out / 'wine.log'}")
    report = out / "inventory.md"
    report.write_text(inventory_report(text, f"{game.name}: the Steamworks API it uses") + "\n")
    return report


# --- signing in ----------------------------------------------------------------------------------

KETTLE = config.ROOT / "vendor/kettle/Sources/KettleExperimental"
SIGNIN = BUILD_DIR / "steam-signin"
# The Keychain item Kettle keeps the Steam client sign-in in on macOS (RecompSteamAuth.m).
KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "Recomp Steam", "steam-client-token"


def kettle_sources(*parts):
    """Kettle's core: the session, protobuf codec and token helpers, plus the files of `parts`."""
    if not (KETTLE / "KettleFeatures.h").exists():
        raise Fail("vendor/kettle is empty: run git submodule update --init vendor/kettle")
    files = {"session": "RecompSteamSession.m", "proto": "RecompSteamProto.c", "util": "RecompSteamUtil.c",
             "signin": "RecompSteamAuth.m", "cloud": "cloud_protocol.m"}
    return [KETTLE / files[p] for p in ("session", "proto", "util", "signin", *parts)]


def kettle_flags(*features):
    """Kettle built with only the parts named (KettleFeatures.h), and its headers."""
    return (["-DKETTLE_EXPERIMENTAL_CONFIGURED=1"] + [f"-DKETTLE_{f}=1" for f in features]
            + ["-I", KETTLE / "include", "-I", KETTLE])


def build_signin():
    """steam_api/signin/steam-signin.m with Kettle's sign-in, for this Mac (not under Wine)."""
    source = SOURCE / "signin/steam-signin.m"
    sources = [source, *kettle_sources()]
    if not _stale(SIGNIN, sources):
        return SIGNIN
    step("Building the Steam sign-in helper (Kettle)")
    SIGNIN.parent.mkdir(parents=True, exist_ok=True)
    run(["xcrun", "clang", "-fobjc-arc", "-fmodules", "-O1", "-Wall", "-mmacosx-version-min=12.0",
         *kettle_flags("SIGN_IN"), "-framework", "Foundation", "-framework", "Security",
         "-framework", "CoreImage", "-lz", "-o", SIGNIN, *sources])
    run(["codesign", "-s", "-", "-f", SIGNIN], capture_output=True)
    return SIGNIN


def signin(account=None, log=None):
    """Sign in interactively (QR code, or account and password); returns the helper's exit code."""
    import subprocess

    helper = build_signin()
    log = log or config.REPORTS / "steam-signin.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as err:
        return subprocess.run([str(helper), *([account] if account else [])], stderr=err).returncode


def _jwt_claims(token):
    import base64
    import json

    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def token_info(token):
    """{steamid, client, expires} of a Steam refresh token (a JWT), or None when it is not one."""
    claims = _jwt_claims(token or "")
    if not claims.get("sub"):
        return None
    aud = claims.get("aud", [])
    return {
        "steamid": int(claims["sub"]) if str(claims["sub"]).isdigit() else 0,
        "client": "client" in (aud if isinstance(aud, list) else [aud]),
        "expires": int(claims.get("exp", 0)),
    }


def stored_token(environ=None, security=None):
    """The sign-in Kettle would log on with, as RecompSteamAuth_StoredToken finds it on macOS:
    $RECOMP_STEAM_TOKEN, else the Keychain item. Only a Steam client token counts; None otherwise."""
    import os
    import subprocess

    environ = os.environ if environ is None else environ
    token = environ.get("RECOMP_STEAM_TOKEN") or None
    if not token:
        security = security or (lambda args: subprocess.run(args, capture_output=True, text=True))
        r = security(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                      "-a", KEYCHAIN_ACCOUNT, "-w"])
        token = r.stdout.strip() if r.returncode == 0 else None
    info = token_info(token)
    return token if info and info["client"] else None

