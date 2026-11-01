"""Benchmark a game under the Porting Kit.

You drive the game; the tool records. Each run samples the game's memory and
CPU and the Mac's swap and free memory once a second, and records an
Instruments trace (Game Performance template) for frame rate and GPU time.
Run the same scene with the same settings each time so runs compare.
"""

import csv
import ctypes
import re
import signal
import statistics
import struct
import subprocess
import time

from . import session, wine
from .util import Fail, sysctl, step, warn


def _swap_used_mb():
    m = re.search(r"used = ([0-9.]+)([MG])", sysctl("vm.swapusage"))
    if not m:
        return None
    return float(m.group(1)) * (1024 if m.group(2) == "G" else 1)


_libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
_RUSAGE_INFO_V2 = 2


def footprint_mb(pid):
    """The process's physical footprint (Activity Monitor's "Memory"), or None.

    ps's RSS leaves out compressed and swapped pages: for a game under Wine
    and Rosetta it can be a tenth of the real figure.
    """
    buf = ctypes.create_string_buffer(16 + 8 * 32)  # rusage_info_v2, with room to spare
    if _libproc.proc_pid_rusage(int(pid), _RUSAGE_INFO_V2, buf) != 0:
        return None
    # After the 16-byte UUID: user/system time, idle and interrupt wakeups,
    # pageins, wired size, resident size, then ri_phys_footprint.
    return round(struct.unpack_from("<8Q", buf.raw, 16)[7] / 2**20)


def trace_saved(log_path):
    try:
        return "Output file saved" in log_path.read_text(errors="replace")
    except OSError:
        return False


def _process(pid):
    for p, rss, cpu, _ in wine.processes():
        if p == pid:
            return rss / 1024, cpu
    return None


def sample(pid, duration, interval=1.0):
    """One row a second; Ctrl-C ends early and keeps the rows taken so far."""
    rows, start = [], time.monotonic()
    try:
        while (t := time.monotonic() - start) < duration:
            proc = _process(pid)
            if proc is None:
                warn("the game exited during the benchmark")
                break
            rows.append({
                "t": round(t, 1),
                "footprint_mb": footprint_mb(pid),
                "rss_mb": round(proc[0]),
                "cpu_percent": proc[1],
                "swap_used_mb": _swap_used_mb(),
                "free_memory_percent": int(sysctl("kern.memorystatus_level") or 0),
            })
            time.sleep(interval)
    except KeyboardInterrupt:
        warn("stopped early: the report covers what was sampled")
    return rows


def summarise(rows):
    if not rows:
        return {}
    mem = [r.get("footprint_mb") or r["rss_mb"] for r in rows]
    cpu = [r["cpu_percent"] for r in rows]
    swap = [r["swap_used_mb"] for r in rows if r["swap_used_mb"] is not None]
    free = [r["free_memory_percent"] for r in rows]
    return {
        "seconds": rows[-1]["t"],
        "memory_mb_mean": round(statistics.mean(mem)),
        "memory_mb_peak": max(mem),
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
    rows = sample(pid, duration)
    if trace:
        # Instruments processes the recording before it saves it, which can take
        # minutes; killing it then leaves a document Instruments cannot open.
        step("Waiting for Instruments to save the trace (can take minutes)")
        try:
            trace.wait(timeout=900)
        except subprocess.TimeoutExpired:
            trace.send_signal(signal.SIGINT)  # stops and still saves
            try:
                trace.wait(timeout=300)
            except subprocess.TimeoutExpired:
                trace.kill()
        if not trace_saved(out / "xctrace.log"):
            warn(f"Instruments did not save the trace: see {out / 'xctrace.log'}")

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
        "trace": str(trace_path) if trace and trace_saved(out / "xctrace.log") else None,
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
            f"| Game memory, physical footprint (mean / peak) | {s['memory_mb_mean']} / {s['memory_mb_peak']} MB |",
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
