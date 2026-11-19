"""`./port night`: the permuter left running with nobody watching, on the Mac or on the Windows PC, over a list
of recovered functions that are written but differ.

For each function of the list (biggest first): the targeted rules (permute.targeted) in turn, then random
rewrites (permute.mutate) of the best candidates so far, until the function's budget is spent; then the next
function. When the list is done it starts again over what is left, with new seeds, until the time is up.

- A win is a candidate in which the function matches, every relocation reaches what the shipped code's does
  (decomp.relocation_targets: matching masks those bytes), and every function of the blob that matched
  before still matches. Each is saved in the results folder (wins/<function>.json: the old and the new
  definition, the rule chain, the scores, the time) with a diff beside it. Nothing reaches the recovered code
  from here: `night apply` re-checks each win with a real match first.
- Resumable: results/state.json holds what was done and the best candidate of each function; started again
  with the same command it goes on. A file named STOP in the results folder makes it finish the compiles in
  flight and exit.
- Polite: the lowest priority (nice 19 on the Mac, below normal on Windows); before each compile it waits
  while the load average is over --max-load, or (on the PC) while the game, a build or other heavy work runs.
  It never touches another process, and writes only in the staged project's worker folders and the results.
- Reports: results/report.tsv, one line per function, and results/perf.txt every few minutes (compiles per
  hour, seconds per compile, wins).
"""

import concurrent.futures
import csv
import difflib
import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, decomp, msvc, permute
from .util import Fail

MODULE = "FactoryGameRecomp"
PERF_EVERY = 300          # seconds between perf.txt rewrites
LOAD_POLL = 30            # seconds between load checks while waiting
KEEP = 5                  # best candidates kept per function, parents of the random rewrites
WINDOWS = os.name == "nt"
# On the PC: work that must have the machine (read only: never stopped or touched). Extend with --busy.
BUSY_PROCESSES = ("FactoryGameSteam-Win64-Shipping.exe", "FactoryGame-Win64-Shipping.exe", "FactoryGameSteam.exe",
                  "UnrealBuildTool.exe", "UnrealEditor.exe", "UnrealEditor-Cmd.exe", "ShaderCompileWorker.exe",
                  "MSBuild.exe")


def safe(name):
    return re.sub(r"\W+", "_", name).strip("_")[:120] or "function"


def sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------------------------- the list

@dataclass
class Row:
    blob: str
    file: str
    function: str
    bytes: int


def read_list(path, order="big"):
    """The functions of a tab-separated list with blob, file, function and bytes columns (the leftovers and
    careful lists qualify); engine-drift rows (a `list` column saying so) left out; biggest first, or smallest
    with order="small"; each function once."""
    rows, seen = [], set()
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        need = {"blob", "file", "function", "bytes"}
        if not need <= set(reader.fieldnames or ()):
            raise Fail(f"{path}: needs the columns {', '.join(sorted(need))} (has {', '.join(reader.fieldnames or ())})")
        for r in reader:
            if "drift" in (r.get("list") or "") or not r["function"] or r["function"] in seen:
                continue
            seen.add(r["function"])
            rows.append(Row(r["blob"].strip(), r["file"].strip(), r["function"].strip(), int(r["bytes"] or 0)))
    rows.sort(key=lambda r: r.bytes, reverse=order != "small")
    return rows


# ---------------------------------------------------------------------------------------------- the machine

class Machine:
    """What the runner asks of the machine it runs on: its load, whether something else needs it, how to be
    polite. Read only."""

    def __init__(self, busy=()):
        self.cores = os.cpu_count() or 4
        self.busy_names = {n.lower() for n in BUSY_PROCESSES + tuple(busy)}

    @property
    def name(self):
        if WINDOWS:
            return f"Windows {platform.release()}, {self.cores} cores, native cl.exe"
        if sys.platform == "darwin":
            return f"Mac (macOS {platform.mac_ver()[0]}), {self.cores} cores, cl.exe under Wine"
        return f"{platform.system()} {platform.release()}, {self.cores} cores"

    def default_jobs(self):
        return max(1, self.cores // 2)

    def default_max_load(self):
        return max(1.0, self.cores - 2.0)

    def load(self):
        """How many cores are busy now, its own compiles included: the processors' use (100 % = all the cores). On
        the Mac from top's idle figure (the load average lags a minute and counts waiting threads), falling back to
        the load average when top gives nothing."""
        if not WINDOWS:
            try:
                out = subprocess.run(["top", "-l", "2", "-n", "0", "-s", "1"], capture_output=True, text=True,
                                     timeout=30).stdout
                idle = re.findall(r"CPU usage:.*?([\d.]+)% idle", out)
                return (100 - float(idle[-1])) / 100 * self.cores
            except (OSError, ValueError, IndexError, subprocess.SubprocessError):
                return os.getloadavg()[0]
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average"],
                                 capture_output=True, text=True, timeout=60).stdout.strip()
            return float(out.replace(",", ".")) / 100 * self.cores
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0.0

    def busy(self):
        """The name of a running program that needs the machine (the game, a build), or None. Windows only."""
        if not WINDOWS:
            return None
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            name = line.split('","')[0].strip('"').lower()
            if name in self.busy_names:
                return name
        return None

    def be_polite(self):
        """The lowest priority for this process and the compilers it starts (they inherit it)."""
        if WINDOWS:
            try:
                import ctypes
                k = ctypes.windll.kernel32
                k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS
            except (OSError, AttributeError):
                pass
            return
        try:
            os.nice(19 - os.nice(0))
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------- compiling

@dataclass
class Result:
    state: str          # matched, equivalent, differs, missing, failed (did not compile)
    score: int = None
    seconds: float = 0.0
    matched: set = field(default_factory=set)   # with full: the blob's matched functions
    targets: list = None                        # with full: relocations reaching elsewhere than shipped
    scores: dict = field(default_factory=dict)  # with full: shipped name -> (state, score), every function
    full: bool = False                          # the whole blob was matched (every matching candidate is)


class Compiler:
    """The real compile and match: msvc.compile_one on a staged project (Wine on the Mac, cl.exe on the PC),
    decomp.match against the shipped module. A worker's candidate stands in for the staged file through an
    include folder searched first (msvc.command_for's include_first)."""

    def __init__(self, project, module=MODULE):
        self.project, self.module = Path(project), module
        self.lock = threading.Lock()
        self._shipped = None
        self._pch = set()

    @property
    def shipped(self):
        if self._shipped is None:
            self._shipped = decomp.Module(msvc.shipped_dll(self.module))
        return self._shipped

    def blob_of(self, file):
        return msvc.blob_name(file, self.project, self.module)

    def compile(self, blob, out_dir, function=None, include_first=None, full=False):
        """Compile the blob and score `function`; when it matches (or with full), match the whole blob from the
        same object: every function's state, and the function's relocation targets."""
        started = time.monotonic()
        pc_pch = msvc.pch_of(self.module, blob)
        if pc_pch and pc_pch not in self._pch:
            with self.lock:
                msvc.build_pch(pc_pch)
                self._pch.add(pc_pch)
        c = msvc.compile_one(self.project, self.module, blob, out_dir, include_first=include_first)
        took = time.monotonic() - started
        if not c.ok:
            return Result("failed", seconds=took)
        with self.lock:   # the shipped module's pairing state is per call
            if not full:
                rows = [r for r in decomp.match(self.shipped, c.obj, only=function) if r.state != "missing"]
                if not rows:
                    return Result("missing", seconds=took)
                if rows[0].state != "matched":
                    return Result(rows[0].state, rows[0].score, took)
            # the whole blob, from the same object: what matches, and the function's relocations
            rows = decomp.match(self.shipped, c.obj)
            res = Result("missing", seconds=took, full=True)
            res.matched = {r.name for r in rows if r.state == "matched"}
            res.scores = {(r.shipped or r.name): (r.state, r.score) for r in rows if r.state != "missing"}
            mine = [r for r in rows if function and function in (r.name, r.shipped)]
            if mine:
                res.state, res.score = mine[0].state, mine[0].score
                if res.state == "matched":
                    res.targets = decomp.relocation_targets(self.shipped, c.obj, function) or []
            return res


# ---------------------------------------------------------------------------------------------- state

def _new_function(row):
    return {"blob": row.blob, "file": row.file, "bytes": row.bytes, "result": "pending", "start": None, "best": None,
            "original": None, "best_text": None, "best_chain": [], "parents": [], "tried": [], "seen": [],
            "candidates": 0, "compiles": 0, "failed": 0, "seconds": 0.0, "rounds": 0, "note": ""}


class State:
    """results/state.json, written whole after every batch (to a temporary file, then renamed)."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {"version": 1, "functions": {}, "blobs": {}, "round": 0, "compiles": 0, "compile_seconds": 0.0,
                     "waited": 0.0, "running": 0.0}
        if self.path.is_file():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    @property
    def functions(self):
        return self.data["functions"]

    def function(self, row):
        return self.functions.setdefault(row.function, _new_function(row))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------------------------- the runner

class Stop(Exception):
    pass


class Night:
    def __init__(self, project, rows, results, jobs=None, max_load=None, per_function_minutes=20.0, deadline=None,
                 log=print, compiler=None, machine=None, clock=time.monotonic, sleep=time.sleep, seed=0):
        self.project = Path(project).resolve()
        self.rows = rows
        self.results = Path(results)
        self.results.mkdir(parents=True, exist_ok=True)
        (self.results / "wins").mkdir(exist_ok=True)
        self.machine = machine or Machine()
        self.jobs = jobs or self.machine.default_jobs()
        self.max_load = max_load if max_load is not None else self.machine.default_max_load()
        self.minutes = per_function_minutes
        self.deadline = deadline
        self.log_fn = log
        self.compiler = compiler or Compiler(self.project)
        self.clock, self.sleep = clock, sleep
        self.seed = seed
        self.state = State(self.results / "state.json")
        self.workers = [self.results / "work" / f"w{k}" for k in range(self.jobs)]
        self.started = clock()
        self.last_perf = None
        self.wait_lock = threading.Lock()
        self.inflight = 0
        self.inflight_lock = threading.Lock()
        self.members_cache = {}
        self.headers = None
        self._log_file = open(self.results / "night.log", "a", encoding="utf-8")

    # -------------------------------------------------------------------------------- small things

    def log(self, message):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + message
        self._log_file.write(line + "\n")
        self._log_file.flush()
        self.log_fn(line)

    def stopping(self):
        return (self.results / "STOP").exists()

    def out_of_time(self):
        return self.deadline is not None and self.clock() >= self.deadline

    def wait_for_room(self):
        """Before a compile: wait while one more compile would take the machine over max_load busy cores (its own
        compiles count: with room for two it runs two, with none it runs none, up to its jobs), or while something
        that needs the machine runs."""
        with self.wait_lock:
            said = None
            while not self.stopping():
                why = self.machine.busy()
                load = self.machine.load()
                if why is None and load + 1 <= self.max_load:
                    break
                reason = f"{why} is running" if why else f"busy {load:.1f} of {self.max_load:.1f} cores " \
                    f"({self.inflight} of them its own compiles): no room for one more"
                if reason.split(" ")[0] != (said or "").split(" ")[0]:
                    self.log(f"waiting: {reason}")
                    said = reason
                self.sleep(LOAD_POLL)
                self.state.data["waited"] += LOAD_POLL
            if said:
                self.log("going on")

    def staged_path(self, file):
        return permute.recovered_file(self.project, file)

    def members(self, cls):
        """The class's data members from the first staged header that defines it (for the init-list rules)."""
        if cls not in self.members_cache:
            if self.headers is None:
                self.headers = sorted((self.project / "Source").rglob("*.h"))
            found = None
            pat = re.compile(rf"\b(?:class|struct)\s+(?:\w+\s+)?{re.escape(cls)}\b[^;{{()]*\{{")
            for h in self.headers:
                try:
                    text = h.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if cls in text and pat.search(text):
                    found = permute.class_members(text, cls)
                    break
            self.members_cache[cls] = found
        return self.members_cache[cls]

    # -------------------------------------------------------------------------------- compiles

    def blob_files(self, blob):
        """The staged files a blob includes, for the key of its baseline."""
        path = self.project / "Source" / MODULE / "Private" / "Recovered" / blob
        texts = [path.read_text(encoding="utf-8", errors="replace")] if path.is_file() else []
        for name in re.findall(r'#include "([^"]+\.cpp)"', texts[0] if texts else ""):
            try:
                texts.append(self.staged_path(name).read_text(encoding="utf-8"))
            except Fail:
                pass
        return sha("\n".join(texts))

    def baseline(self, blob):
        """The blob as staged, compiled once: the functions that match and every function's score."""
        key = self.blob_files(blob)
        b = self.state.data["blobs"].get(blob)
        if b and b.get("key") == key:
            return b
        self.wait_for_room()
        self.log(f"blob {blob}: baseline compile")
        res = self.compiler.compile(blob, self.results / "work" / "base", full=True)
        self.count_compile(res.seconds)
        if res.state == "failed":
            raise Fail(f"{blob} does not compile as staged")
        b = {"key": key, "matched": sorted(res.matched), "scores": {k: list(v) for k, v in res.scores.items()}}
        self.state.data["blobs"][blob] = b
        self.state.save()
        return b

    def count_compile(self, seconds):
        self.state.data["compiles"] += 1
        self.state.data["compile_seconds"] += seconds

    def evaluate(self, worker, row, fs, candidate, text, span):
        """Compile one candidate in a worker's folder and score it; a matching candidate is checked whole."""
        if self.stopping():
            return None
        self.wait_for_room()
        src = worker / "src"
        src.mkdir(parents=True, exist_ok=True)
        path = self.staged_path(row.file)
        (src / path.name).write_text(text[:span[0]] + candidate + text[span[1]:], encoding="utf-8")
        with self.inflight_lock:
            self.inflight += 1
        try:
            return self.compiler.compile(row.blob, worker, function=row.function, include_first=src)
        finally:
            with self.inflight_lock:
                self.inflight -= 1

    # -------------------------------------------------------------------------------- one function

    def candidates(self, fs, rng, k, members):
        """Up to k new candidates: the targeted ones not tried yet (from the best so far), else random rewrites
        of the best ones. (text, chain, change) each."""
        seen, tried = set(fs["seen"]), set(fs["tried"])
        out = []
        base = fs["best_text"]
        if not fs.get("targeted_done"):
            for e in permute.targeted(base, members=members):
                if e.change in tried:
                    continue
                text = permute.keep_lines(fs["original"], e.text)
                if sha(text) in seen:
                    tried.add(e.change)
                    fs["tried"].append(e.change)
                    continue
                out.append((text, fs["best_chain"] + [e.rule], e.change))
                seen.add(sha(text))
                if len(out) >= k:
                    return out
            if not out:
                fs["targeted_done"] = True
        idle = 0
        parents = fs["parents"] or [[fs["best"], fs["best_text"], fs["best_chain"]]]
        while len(out) < k and idle < 300:
            parent = parents[min(int(rng.expovariate(1.5)), len(parents) - 1)]
            try:
                d = permute.Definition(parent[1])
            except Fail:
                break
            body = permute.mutate(d.body, rng)
            if body is None:
                idle += 1
                continue
            text = permute.keep_lines(fs["original"], parent[1][:d.body_start] + body)
            if sha(text) in seen:
                idle += 1
                continue
            seen.add(sha(text))
            out.append((text, parent[2] + ["random"], None))
        return out

    def work(self, row):
        fs = self.state.function(row)
        if fs["result"] in ("won", "already", "missing", "error"):
            return
        try:
            text = self.staged_path(row.file).read_text(encoding="utf-8")
            span = permute.find_definition(text, permute.source_function(row.function))
            blob = self.compiler.blob_of(row.file)
        except Fail as e:
            fs["result"], fs["note"] = "error", str(e)
            self.log(f"{row.function}: {e}")
            return
        original = text[span[0]:span[1]]
        if fs["original"] != original:   # first time, or the staged file changed since: start this one afresh
            keep = {k: fs[k] for k in ("candidates", "compiles", "failed", "seconds", "rounds")}
            fs.clear()
            fs.update(_new_function(row), **keep, original=original, best_text=original)
        row.blob = blob
        fs["blob"] = blob
        b = self.baseline(blob)
        start = None
        if row.function in b["scores"]:
            start = tuple(b["scores"][row.function])
        if start is None:
            fs["result"], fs["note"] = "missing", f"not in {blob}'s object"
            self.log(f"{row.function}: not in {blob}'s object")
            return
        if start[0] == "matched":
            fs["result"] = "already"
            self.log(f"{row.function}: matches already")
            return
        if fs["start"] is None:
            fs["start"] = fs["best"] = start[1] if start[1] is not None else 10 ** 6
        matched_before = set(b["matched"])
        members = None
        try:
            d = permute.Definition(original)
            if d.constructor:
                members = self.members(d.cls)
        except Fail:
            pass
        budget_end = self.clock() + 60 * self.minutes   # from here: the blob's baseline is not the function's time
        if self.deadline is not None:
            budget_end = min(budget_end, self.deadline)
        rng = random.Random(f"{self.seed}:{row.function}:{fs['rounds']}")
        fs["rounds"] += 1
        began = self.clock()
        self.log(f"{row.function} ({row.bytes} bytes, blob {blob}): score {fs['best']}, round {fs['rounds']}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.jobs) as pool:
            while self.clock() < budget_end and not self.stopping() and not self.out_of_time():
                batch = self.candidates(fs, rng, self.jobs, members)
                if not batch:
                    fs["note"] = fs["note"] or "no new candidate"
                    break
                futures = [pool.submit(self.evaluate, self.workers[i], row, fs, c[0], text, span)
                           for i, c in enumerate(batch)]
                won = False
                for (cand, chain, change), fut in zip(batch, futures):
                    res = fut.result()
                    if res is None:
                        continue   # STOP: not compiled
                    fs["candidates"] += 1
                    fs["compiles"] += 1
                    fs["seen"].append(sha(cand))
                    if change:
                        fs["tried"].append(change)
                    self.count_compile(res.seconds)
                    if res.state == "failed" or res.score is None and res.state != "matched":
                        fs["failed"] += 1
                        continue
                    if res.state == "matched" and not res.full:
                        res.state, res.score = "differs", 1   # the blob not checked: never a win
                    if res.state == "matched" and not won:
                        lost = sorted(matched_before - res.matched)
                        if res.targets:
                            fs["note"] = f"matched, but a relocation reaches elsewhere ({res.targets[0][1]}): {' + '.join(chain)}"
                            self.log(f"  {fs['note']}")
                            continue
                        if lost:
                            fs["note"] = f"matched, but {len(lost)} other functions no longer do ({lost[0]})"
                            self.log(f"  {fs['note']}")
                            continue
                        won = True
                        self.save_win(row, fs, cand, chain, began)
                        continue
                    score = res.score if res.state != "matched" else 0
                    if score < fs["best"]:
                        self.log(f"  {' + '.join(chain)}: {fs['best']} -> {score}")
                        fs["best"], fs["best_text"], fs["best_chain"] = score, cand, chain
                        fs["targeted_done"] = False   # the targeted rules again, from the new best
                    parents = fs["parents"] + [[score, cand, chain]]
                    parents.sort(key=lambda p: p[0])
                    fs["parents"] = parents[:KEEP]
                self.state.save()
                self.perf()
                if won:
                    break
        fs["seconds"] += self.clock() - began
        if fs["result"] != "won":
            fs["result"] = "closer" if fs["best"] < fs["start"] else "no"
        self.state.save()
        self.report()

    def save_win(self, row, fs, text, chain, began):
        fs["result"], fs["best"], fs["best_text"], fs["best_chain"] = "won", 0, text, chain
        took = self.clock() - began + fs["seconds"]
        win = {"function": row.function, "file": row.file, "blob": row.blob, "bytes": row.bytes,
               "source_function": permute.source_function(row.function), "old": fs["original"], "new": text,
               "chain": chain, "start": fs["start"], "seconds": round(took), "candidates": fs["candidates"],
               "found": time.strftime("%Y-%m-%d %H:%M:%S"), "machine": self.machine.name}
        base = self.results / "wins" / safe(row.function)
        base.with_suffix(".json").write_text(json.dumps(win, indent=1), encoding="utf-8")
        diff = difflib.unified_diff(fs["original"].splitlines(True), text.splitlines(True), f"a/{row.file}", f"b/{row.file}")
        base.with_suffix(".diff").write_text("".join(diff), encoding="utf-8")
        self.log(f"  WIN {row.function} ({row.bytes} bytes): {' + '.join(chain)}, {fs['candidates']} candidates")

    # -------------------------------------------------------------------------------- reports

    def report(self):
        cols = ["function", "bytes", "start", "best", "result", "candidates", "compiles", "minutes", "chain", "blob",
                "file", "note"]
        lines = ["\t".join(cols)]
        for row in self.rows:
            fs = self.state.functions.get(row.function)
            if not fs:
                continue
            lines.append("\t".join(str(x) for x in [
                row.function, row.bytes, fs["start"] if fs["start"] is not None else "", fs["best"] if fs["best"] is not None else "",
                fs["result"], fs["candidates"], fs["compiles"], round(fs["seconds"] / 60, 1),
                " + ".join(fs["best_chain"]) if fs["result"] in ("won", "closer") else "", fs["blob"], fs["file"],
                fs["note"].replace("\t", " ").replace("\n", " ")]))
        (self.results / "report.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def perf(self, force=False):
        now = self.clock()
        if not force and self.last_perf is not None and now - self.last_perf < PERF_EVERY:
            return
        self.last_perf = now
        d = self.state.data
        running = d["running"] + (now - self.started)
        fns = [f for f in d["functions"].values() if f["candidates"] or f["result"] != "pending"]
        won = [f for f in fns if f["result"] == "won"]
        closer = [f for f in fns if f["result"] == "closer"]
        hours = running / 3600 or 1e-9
        try:
            load = f"{self.machine.load():.1f}"
        except Exception:   # a report never stops the run
            load = "?"
        text = "\n".join([
            f"updated            {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"machine            {self.machine.name}",
            f"jobs               {self.jobs} (max load {self.max_load:.1f}, load now {load})",
            f"running            {running / 3600:.2f} h (waited for the machine {d['waited'] / 60:.0f} min)",
            f"compiles           {d['compiles']} ({d['compiles'] / hours:.0f} per hour)",
            f"seconds a compile  {d['compile_seconds'] / max(1, d['compiles']):.0f}",
            f"functions tried    {len(fns)} of {len(self.rows)} (round {d['round'] + 1})",
            f"wins               {len(won)} functions, {sum(f['bytes'] for f in won)} bytes "
            f"({len(won) / hours:.2f} functions and {sum(f['bytes'] for f in won) / hours / 1024:.1f} KB per hour)",
            f"closer             {len(closer)} functions, {sum(f['bytes'] for f in closer)} bytes",
        ])
        (self.results / "perf.txt").write_text(text + "\n", encoding="utf-8")

    # -------------------------------------------------------------------------------- the whole run

    def run(self):
        self.machine.be_polite()
        for w in self.workers:   # src/ is searched first: an earlier run's copy would stand in for a staged file
            shutil.rmtree(w / "src", ignore_errors=True)
        if self.stopping():   # left from the last stop: starting again means going on
            (self.results / "STOP").unlink()
            self.log("night: an old STOP file removed")
        self.log(f"night: {len(self.rows)} functions, {self.jobs} jobs, max load {self.max_load:.1f}, "
                 f"{self.minutes:g} min a function, {self.machine.name}")
        round_began = -1
        try:
            while not self.stopping() and not self.out_of_time():
                pending = [r for r in self.rows if self.state.function(r)["result"] not in ("won", "already", "missing", "error")]
                todo = [r for r in pending if self.state.function(r)["rounds"] <= self.state.data["round"]]
                if not pending:
                    self.log("every function is done")
                    break
                if not todo:
                    if self.state.data["compiles"] == round_began:
                        self.log("no rewrite left to try in a whole round: stopping")
                        break
                    round_began = self.state.data["compiles"]
                    self.state.data["round"] += 1
                    self.log(f"round {self.state.data['round'] + 1}: {len(pending)} functions left")
                    continue
                for row in todo:
                    if self.stopping() or self.out_of_time():
                        break
                    try:
                        self.work(row)
                    except Fail as e:
                        fs = self.state.function(row)
                        fs["result"], fs["note"] = "error", str(e)
                        self.log(f"{row.function}: {e}")
                        self.state.save()
        finally:
            self.state.data["running"] += self.clock() - self.started
            self.started = self.clock()
            self.state.save()
            self.report()
            self.perf(force=True)
            why = "STOP file" if self.stopping() else "time is up" if self.out_of_time() else "done"
            self.log(f"night: stopped ({why})")
            self._log_file.close()
        return self.state


def deadline_from(hours=None, until=None, clock=time.monotonic, now=None):
    """The run's end on the clock: after `hours`, or at the next `until` (HH:MM, local time); None for none."""
    if hours is not None:
        return clock() + hours * 3600
    if until:
        h, m = (int(x) for x in until.split(":"))
        t = time.localtime(now)
        seconds = (h * 3600 + m * 60) - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)
        if seconds <= 0:
            seconds += 86400
        return clock() + seconds
    return None


def run(project, list_path, results, order="big", **kw):
    project = Path(project).resolve()
    if project.is_relative_to(config.RECOVERED_REPO.resolve()) or config.inside_repository(project):
        raise Fail(f"{project}: the runner works on a staged copy, never in the recovered-code repository or here")
    if config.inside_repository(results):
        raise Fail(f"{results} is inside this repository; results hold recovered code and stay outside it")
    if "compiler" not in kw and not decomp.objdump():
        raise Fail("objdump (Xcode's) or llvm-objdump (LLVM's, on Windows) not found: the scores need it")
    return Night(project, read_list(list_path, order), results, **kw).run()


# ---------------------------------------------------------------------------------------------- applying the wins

def _git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise Fail(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _dirty(repo):
    """The paths git sees changed or new in the repository."""
    out = set()
    for line in _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        out.add(path)
    return out


def _stage_project(repo, stage):
    script = Path(repo) / "Project" / "stage.sh"
    if not script.is_file():
        raise Fail(f"{script} not found: is {repo} the recovered-code repository?")
    r = subprocess.run(["sh", str(script), str(stage)], capture_output=True, text=True)
    if r.returncode:
        raise Fail(f"staging failed: {r.stderr.strip()[-500:]}")


def _mark(results, marks):
    """report.tsv with an `applied` column saying what became of each win."""
    path = Path(results) / "report.tsv"
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    head = lines[0].split("\t")
    if "applied" not in head:
        head.append("applied")
    k = head.index("applied")
    out = ["\t".join(head)]
    for line in lines[1:]:
        cells = line.split("\t")
        cells += [""] * (len(head) - len(cells))
        if cells[0] in marks:
            cells[k] = marks[cells[0]]
        out.append("\t".join(cells))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def apply(results, repo, stage=None, carry=None, commit=False, push=False, log=print, compiler=None,
          source_dir="Source/FactoryGame/Private", stage_fn=_stage_project):
    """Each win of a results folder applied to the recovered-code repository, checked with a real compile and
    match of its blob (staged into `stage`), kept only if the function matches, its relocations reach what the
    shipped code's do, and nothing that matched before differs; put back otherwise. carry: a shell command run
    in the repository after the patch ({file}, {function}), for a repository whose files are generated from
    others. commit: one commit per kept win (the configured identity checked first); push: to `backup` only."""
    results, repo = Path(results), Path(repo).resolve()
    if commit:
        email = _git(repo, "config", "user.email").strip()
        if email != config.COMMIT_EMAIL:
            raise Fail(f"{repo}: git's user.email is {email or '(none)'}, not {config.COMMIT_EMAIL}: no commit")
    if push and not commit:
        raise Fail("--push needs --commit")
    stage = Path(stage or config.SMART / "msvc-stage" / "night-apply")
    if stage.resolve().is_relative_to(repo):
        raise Fail(f"{stage} is inside {repo}")
    compiler = compiler or Compiler(stage)
    out_dir = config.MSVC_BUILDS / "night-apply"
    wins = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((results / "wins").glob("*.json"))]
    marks, kept, baselines = {}, [], {}
    if not wins:
        log("no wins to apply")
    for win in wins:
        name, path = win["function"], repo / source_dir / win["file"]
        rel = str(path.relative_to(repo))
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        if win["old"] not in text:
            marks[name] = "already in" if win["new"] in text else "stale: the function changed since"
            log(f"{name}: {marks[name]}")
            continue
        before = _dirty(repo)
        if commit and rel in before:
            marks[name] = "skipped: uncommitted changes in the file"
            log(f"{name}: {marks[name]}")
            continue
        blob = win["blob"]
        if blob not in baselines:
            stage_fn(repo, stage)
            res = compiler.compile(blob, out_dir / "base", full=True)
            baselines[blob] = res.matched
        path.write_text(text.replace(win["old"], win["new"], 1), encoding="utf-8")
        why = None
        if carry:
            cmd = carry.replace("{file}", rel).replace("{function}", name)
            r = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True)
            if r.returncode:
                why = f"carry failed: {(r.stderr or r.stdout).strip()[-200:]}"
        if why is None:
            stage_fn(repo, stage)
            res = compiler.compile(blob, out_dir, function=name, full=True)
            lost = sorted(baselines[blob] - res.matched)
            if res.state != "matched":
                why = f"{res.state} in the real match (score {res.score})"
            elif res.targets:
                why = f"a relocation reaches elsewhere ({res.targets[0][1]})"
            elif lost:
                why = f"{len(lost)} functions no longer match ({lost[0]})"
        changed = sorted(_dirty(repo) - before)
        if why:
            path.write_text(text, encoding="utf-8")
            others = [p for p in changed if p != rel]
            if others:
                _git(repo, "checkout", "--", *[p for p in others if (repo / p).exists()], check=False)
            marks[name] = f"rejected: {why}"
            log(f"{name}: {marks[name]}")
            continue
        baselines[blob] = res.matched
        kept.append(name)
        marks[name] = "applied"
        log(f"{name}: applied ({' + '.join(win['chain'])})")
        if commit:
            _git(repo, "add", "--", *changed)
            _git(repo, "commit", "-q", "-m", f"Night: {name} matches ({' + '.join(win['chain'])})", "--", *changed)
    _mark(results, marks)
    if push and kept:
        if _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() != "main":
            raise Fail("--push: not on main")
        _git(repo, "pull", "-q", "--rebase", "backup", "main")
        _git(repo, "push", "-q", "backup", "main")
        log("pushed to backup")
    return marks


# ---------------------------------------------------------------------------------------------- the PC's results

FETCHED = ("state.json", "report.tsv", "perf.txt", "night.log")


def fetch(host, remote, local, runner=subprocess.run):
    """A results folder from another machine (the PC) over scp, without its worker folders: the state, the
    reports, the log and the wins, for `night apply` here."""
    local = Path(local)
    local.mkdir(parents=True, exist_ok=True)
    remote = remote.replace("\\", "/").rstrip("/")
    r = runner(["scp", "-q", *[f"{host}:{remote}/{name}" for name in FETCHED], str(local)], capture_output=True, text=True)
    if r.returncode:
        raise Fail(f"scp from {host}:{remote} failed: {r.stderr.strip()}")
    r = runner(["scp", "-q", "-r", f"{host}:{remote}/wins", str(local)], capture_output=True, text=True)
    if r.returncode and "No such file" not in r.stderr:
        raise Fail(f"scp of {host}:{remote}/wins failed: {r.stderr.strip()}")
    return local
