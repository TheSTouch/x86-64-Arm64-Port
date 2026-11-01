"""Starting a game under the Porting Kit, and the report folder each run writes."""

import json
import platform
import subprocess

from . import config, kettle, runtime, steam, wine
from .util import Fail, step, sysctl, timestamp


def ready(game):
    missing = game.resolve()
    if missing:
        raise Fail(
            f"{game.name} is not ready: {', '.join(missing)} unknown. "
            f"Is it in {game.library or '(no library set)'}? Check games/{game.id}.toml"
        )


def start(game, *, debug="-all", extra_env=None, log=None, fresh=True):
    """Launch the game; returns the Popen of what was started (Steam or the game).

    Through Steam, the game is Steam's child and inherits Steam's environment,
    so Steam is restarted for each run that sets its own variables (fresh=True).
    With Kettle, the game starts as Steam would start it, without Windows Steam:
    the prefix is stopped first (fresh=True), so a Windows Steam signed in there
    does not play beside it.
    """
    env = {**game.env, **(extra_env or {})}
    if game.launch == "steam":
        if fresh:
            step(f"Stopping prefix {game.prefix} (Steam included) so the game gets this run's settings")
            steam.stop(game.prefix)
        step(f"Starting {game.name} through Steam (app {game.steam_appid})")
        return steam.start(game.prefix, ["-applaunch", game.steam_appid, *game.args], debug, env, log)
    if game.launch == "kettle":
        if fresh:
            step(f"Stopping prefix {game.prefix} (Windows Steam included): Kettle stands in for it")
            steam.stop(game.prefix)
        root = kettle.build_bridge()
        steam_log = log.parent / "steam_api.log" if log else None
        env.update(kettle.environment(game, root, steam_log, env))
        exe = game.launcher_path
        step(f"Starting {exe} with our steam_api64.dll (Steam through Kettle, app {game.steam_appid})")
        return wine.spawn(game.prefix, [exe, *game.args], debug, env, log, cwd=exe.parent)
    exe = game.in_run_root(game.exe_path)
    step(f"Starting {exe}")
    return wine.spawn(game.prefix, [exe, *game.args], debug, env, log, cwd=exe.parent)


def report_dir(game, kind, label=""):
    name = f"{timestamp()}-{kind}" + (f"-{label}" if label else "")
    path = config.REPORTS / game.id / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def machine(game):
    def cmd(args):
        try:
            return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    gpu_cores = ""
    for line in cmd(["system_profiler", "SPDisplaysDataType"]).splitlines():
        if "Total Number of Cores" in line:
            gpu_cores = line.split(":", 1)[1].strip()
            break
    return {
        "chip": sysctl("machdep.cpu.brand_string"),
        "memory_gb": round(int(sysctl("hw.memsize") or 0) / 2**30),
        "performance_cores": sysctl("hw.perflevel0.physicalcpu"),
        "efficiency_cores": sysctl("hw.perflevel1.physicalcpu"),
        "gpu_cores": gpu_cores,
        "macos": platform.mac_ver()[0],
        "runtime": runtime.describe(game.runtime),
    }


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
