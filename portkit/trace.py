"""Watch a game while it runs under the Porting Kit, for the recompilation plan.

Records which DLLs the game loads (Wine's builtin stand-ins or the game's own),
the Windows functions Wine reports as unimplemented (fixme) or failing (err),
D3DMetal's log lines and, if asked, call-stack samples and the Metal shaders
D3DMetal produced. With --relay, every Windows API call the game makes and the
address in the game's code it was called from: the API surface a native port
has to provide, and known call sites to seed the recompiler's code search.
Everything lands in the run's report folder.
"""

import re
import subprocess
import threading
import time
from collections import Counter, defaultdict
from pathlib import PureWindowsPath

from . import session, wine
from .util import step, warn

LINE = re.compile(
    r"^(?:(?P<pid>[0-9a-f]{4,8}):)?(?P<tid>[0-9a-f]{4,8}):(?P<cls>trace|err|warn|fixme|info):(?P<ch>[\w-]+):(?P<msg>.*)$"
)
LOADED = re.compile(r'Loaded L"(?P<path>[^"]+)".*?:\s*(?P<kind>builtin|native)\s*$')
RELAY = re.compile(
    r"^(?:(?P<pid>[0-9a-f]{4,8}):)?(?P<tid>[0-9a-f]{4,8}):Call (?P<fn>[\w.@]+)\(.*\) ret=(?P<ret>[0-9a-f]+)\s*$"
)
ADDRESS = re.compile(r"\b(?:0x)?[0-9a-fA-F]{6,16}\b")


def analyse(log_path, game_process=None):
    """Summarise a Wine log written with WINEDEBUG=+pid,+loaddll."""
    modules = defaultdict(dict)  # pid -> {dll: kind}
    names = {}  # pid -> first .exe it loaded
    problems = defaultdict(Counter)  # pid -> Counter("fixme:channel:message")
    calls = defaultdict(Counter)  # pid -> Counter("DLL.Function")
    call_sites = defaultdict(set)  # pid -> return addresses
    d3dm = []
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if "D3DM" in line:
                d3dm.append(line)
            if ":Call " in line and (r := RELAY.match(line)):
                pid = r.group("pid") or "?"
                calls[pid][r.group("fn")] += 1
                call_sites[pid].add(int(r.group("ret"), 16))
                continue
            m = LINE.match(line)
            if not m:
                continue
            pid = m.group("pid") or "?"
            if m.group("ch") == "loaddll" and (hit := LOADED.search(m.group("msg"))):
                path = PureWindowsPath(hit.group("path").replace("\\\\", "\\"))
                if path.suffix.lower() == ".exe":
                    names.setdefault(pid, path.name)
                modules[pid][str(path)] = hit.group("kind")
            elif m.group("cls") in ("fixme", "err"):
                msg = ADDRESS.sub("…", m.group("msg").strip())
                problems[pid][f"{m.group('cls')}:{m.group('ch')}:{msg[:160]}"] += 1

    processes = []
    for pid in modules.keys() | problems.keys() | calls.keys():
        mods = modules.get(pid, {})
        processes.append({
            "pid": pid,
            "exe": names.get(pid),
            "builtin": sorted(p for p, k in mods.items() if k == "builtin"),
            "native": sorted(p for p, k in mods.items() if k == "native"),
            "problems": problems[pid].most_common(60),
            "api_calls": dict(calls[pid].most_common()),
            "call_sites": [f"{a:#x}" for a in sorted(call_sites[pid])],
        })
    game = None
    if game_process:
        game = next((p for p in processes if (p["exe"] or "").lower() == game_process.lower()), None)
    return {"game": game, "processes": processes, "d3dm_lines": len(d3dm)}, d3dm


def format_summary(game, result):
    g = result["game"]
    lines = [f"# {game.name}: Porting Kit trace", ""]
    if not g:
        lines += [f"{game.process} was not found in the log; the per-process lists below cover everything.", ""]
        targets = result["processes"]
    else:
        targets = [g]
    for p in targets:
        lines += [f"## {p['exe'] or 'unnamed process'} (Wine pid {p['pid']})", ""]
        lines.append(f"**Wine builtin DLLs: {len(p['builtin'])}.** These Windows parts are what a native "
                     "port has to provide itself:")
        lines += ["", ", ".join(PureWindowsPath(x).name for x in p["builtin"]) or "none", ""]
        lines.append(f"**The game's own DLLs: {len(p['native'])}.** These must be recompiled or replaced:")
        lines += ["", ", ".join(PureWindowsPath(x).name for x in p["native"]) or "none", ""]
        if p["api_calls"]:
            lines += [f"**Windows API calls seen: {len(p['api_calls'])} functions, "
                      f"{sum(p['api_calls'].values())} calls, from {len(p['call_sites'])} call sites** "
                      "(full lists in `modules.json`). Most frequent:", ""]
            lines += ["| Calls | Function |", "|---|---|"]
            lines += [f"| {n} | `{fn}` |" for fn, n in list(p["api_calls"].items())[:30]]
            lines.append("")
        if p["problems"]:
            lines += ["**Unimplemented or failing Windows calls (most frequent first):**", "",
                      "| Count | Message |", "|---|---|"]
            lines += [f"| {n} | `{msg.replace('|', '/')}` |" for msg, n in p["problems"][:30]]
            lines.append("")
    lines.append(f"D3DMetal log lines: {result['d3dm_lines']} (in `d3dm.log`).")
    return "\n".join(lines) + "\n"


def _sample_later(pid, out, at, seconds):
    time.sleep(at)
    if wine.alive(pid):
        step(f"Sampling call stacks for {seconds} s")
        subprocess.run(["/usr/bin/sample", str(pid), str(seconds), "-file", str(out / "sample.txt")],
                       capture_output=True)


def run(game, *, channels="+loaddll", relay=False, dump_metal=False, sample_seconds=0, sample_at=60,
        timeout=900, extra_env=None):
    session.ready(game)
    out = session.report_dir(game, "trace")
    env = dict(extra_env or {})
    if dump_metal:
        # Metal's own dump switches (found in macOS's Metal framework). Experimental:
        # what they write, if anything, under D3DMetal is still to be seen.
        (out / "metal-libraries").mkdir()
        env["MTL_DUMP_LIBRARIES_TO_DIRECTORY"] = str(out / "metal-libraries")
        env["MTL_DUMP_PIPELINES_TO_JSON_FILE"] = str(out / "pipelines.json")
    if relay:
        # Logs every call between Windows modules: the game runs many times slower.
        warn("relay tracing slows the game a lot; trace menus and a short race, not a benchmark")
        channels += ",+relay"
    log = out / "wine.log"
    session.start(game, debug=f"+pid,{channels}", extra_env=env, log=log)
    step(f"Waiting for {game.process}")
    pid = wine.wait_for_process(game.process, timeout)
    step(f"{game.process} running as pid {pid}. Play normally; quit the game (or Ctrl-C) to finish")
    if sample_seconds:
        threading.Thread(target=_sample_later, args=(pid, out, sample_at, sample_seconds), daemon=True).start()
    try:
        while wine.alive(pid):
            time.sleep(2)
    except KeyboardInterrupt:
        warn("stopped watching; the game keeps running")

    result, d3dm = analyse(log, game.process)
    (out / "d3dm.log").write_text("\n".join(d3dm) + "\n", encoding="utf-8")
    session.write_json(out / "modules.json", result)
    (out / "summary.md").write_text(format_summary(game, result), encoding="utf-8")
    step(f"Report in {out}")
    return out

