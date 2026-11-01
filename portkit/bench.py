"""Benchmark a game under the Porting Kit.

You drive the game; the tool records. Each run samples the game's memory and
CPU and the Mac's swap and free memory once a second, and records an
Instruments trace (Game Performance template) for frame rate and GPU time.
Run the same scene with the same settings each time so runs compare.
"""

import csv
import re
import statistics
import subprocess
import time

from . import session, wine
from .util import Fail, sysctl, step, warn


def _swap_used_mb():
    m = re.search(r"used = ([0-9.]+)([MG])", sysctl("vm.swapusage"))
    if not m:
        return None
    return float(m.group(1)) * (1024 if m.group(2) == "G" else 1)


def _process(pid):
    for p, rss, cpu, _ in wine.processes():
        if p == pid:
            return rss / 1024, cpu
    return None


def sample(pid, duration, interval=1.0):
    rows, start = [], time.monotonic()
    while (t := time.monotonic() - start) < duration:
        proc = _process(pid)
        if proc is None:
            warn("the game exited during the benchmark")
            break
        rows.append({
            "t": round(t, 1),
            "rss_mb": round(proc[0]),
            "cpu_percent": proc[1],
            "swap_used_mb": _swap_used_mb(),
            "free_memory_percent": int(sysctl("kern.memorystatus_level") or 0),
        })
        time.sleep(interval)
    return rows


def summarise(rows):
    if not rows:
        return {}
    rss = [r["rss_mb"] for r in rows]
    cpu = [r["cpu_percent"] for r in rows]
    swap = [r["swap_used_mb"] for r in rows if r["swap_used_mb"] is not None]
    free = [r["free_memory_percent"] for r in rows]
    return {
        "seconds": rows[-1]["t"],
        "rss_mb_mean": round(statistics.mean(rss)),
        "rss_mb_peak": max(rss),
        "cpu_percent_mean": round(statistics.mean(cpu)),
        "cpu_percent_peak": max(cpu),
        "swap_growth_mb": round(swap[-1] - swap[0]) if swap else None,
        "free_memory_percent_min": min(free),
    }


def run(game, *, duration=120, warmup=45, label="", note="", hud=True, instruments=True,
        template="Game Performance", timeout=900, quit=False, extra_env=None, attach=False):
    session.ready(game)
    out = session.report_dir(game, "bench", label)
    env = {"MTL_HUD_ENABLED": "1"} if hud else {}
    env.update(extra_env or {})
    if attach:
        pid = wine.find_process(game.process)
        if not pid:
            raise Fail(f"{game.process} is not running; start it with ./port run {game.id}")
        env = {}  # the running game keeps the environment it was started with
    else:
        session.start(game, extra_env=env, log=out / "wine.log")
        step(f"Waiting for {game.process} (first starts compile shaders and can take minutes)")
        pid = wine.wait_for_process(game.process, timeout)
    step(f"{game.process} running as pid {pid}. You have {warmup} s to reach the benchmark scene")
    time.sleep(warmup)

    trace, trace_path = None, out / "run.trace"
    if instruments:
        step(f"Recording Instruments '{template}' for {duration} s")
        trace = subprocess.Popen(
            ["xcrun", "xctrace", "record", "--template", template, "--attach", str(pid),
             "--time-limit", f"{duration}s", "--output", str(trace_path), "--no-prompt"],
            stdout=open(out / "xctrace.log", "wb"), stderr=subprocess.STDOUT,
        )
    step(f"Sampling for {duration} s: play the scene now")
    try:
        rows = sample(pid, duration)
    except KeyboardInterrupt:
        warn("stopped early: the report covers what was sampled")
        rows = []
    if trace:
        try:
            trace.wait(timeout=180)
        except subprocess.TimeoutExpired:
            trace.terminate()

    with open(out / "samples.csv", "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
    summary = summarise(rows)
    report = {
        "game": game.id,
        "label": label,
        "note": note,
        "duration": duration,
        "warmup": warmup,
        "attached": attach,
        "env": {**game.env, **env},
        "args": game.args,
        "machine": session.machine(game),
        "summary": summary,
        "trace": str(trace_path) if trace_path.exists() else None,
    }
    session.write_json(out / "report.json", report)
    (out / "summary.md").write_text(format_summary(game, report), encoding="utf-8")
    step(f"Report in {out}")
    print(format_summary(game, report))
    if quit:
        wine.kill(game.prefix)
    return out


def format_summary(game, r):
    s, m = r["summary"], r["machine"]
    lines = [
        f"# {game.name}: Porting Kit benchmark {r['label']}".rstrip(),
        "",
        f"{m['chip']} ({m['gpu_cores']} GPU cores), {m['memory_gb']} GB, macOS {m['macos']}",
        f"Runtime: {m['runtime']}",
        "",
    ]
    if r["note"]:
        lines += [r["note"], ""]
    if s:
        lines += [
            "| Measure | Value |",
            "|---|---|",
            f"| Sampled | {s['seconds']} s |",
            f"| Game memory (mean / peak) | {s['rss_mb_mean']} / {s['rss_mb_peak']} MB |",
            f"| Game CPU (mean / peak, 100 = one core) | {s['cpu_percent_mean']} / {s['cpu_percent_peak']} % |",
            f"| Swap growth | {s['swap_growth_mb']} MB |",
            f"| Lowest free memory | {s['free_memory_percent_min']} % |",
        ]
    else:
        lines.append("No samples: the game exited or the run was stopped before sampling.")
    lines += [
        "",
        "Frame rate and GPU time: open `run.trace` in Instruments (the Game Performance template's "
        "frame-rate and GPU tracks), and the Metal HUD values seen on screen.",
    ]
    return "\n".join(lines) + "\n"
