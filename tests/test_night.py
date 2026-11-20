"""`./port night` with the compiler replaced by a fake that scores the candidate's text: the loop, resume, the
STOP file, the load wait, the win check. Made-up functions only."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from portkit import night

SOURCE = """#include "Foo.h"

void UFoo::Tick( float dt )
{
	for( int32 i = 0; i < mCount; ++i )
	{
		Step( i );
	}

	const int32 n = Count( mA, mB );
	Use( n, mA + mB );
	if( mReady && n == 1 )
	{
		Go( (float)dt, mA * mB );
	}
	mDone = true;
}

void UFoo::Stuck()
{
	Wait( 1, 2 );
}

AFoo::AFoo()
{
	Go();
}
"""

LIST = "blob\tfile\tfunction\tbytes\tlist\n" \
       "1\tFGFoo.cpp\tUFoo::Tick\t300\tclose\n" \
       "1\tFGFoo.cpp\tUFoo::Stuck\t100\tclose\n" \
       "1\tFGFoo.cpp\tAFoo::AFoo\t900\tengine drift\n"


class Clock:
    def __init__(self):
        self.t = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        return self.t

    def advance(self, s):
        with self.lock:
            self.t += s


class FakeCompiler:
    """Scores UFoo::Tick 0 once its counter is uint64 (as a careful pass found), else 5; UFoo::Stuck never
    better than 3. Matched-before: Other::Fine. `lose` names a function a winning candidate breaks."""

    def __init__(self, project, clock, lose=None, wrong_target=False, on_compile=None, win=True):
        self.project, self.clock, self.win = project, clock, win
        self.lose, self.wrong_target, self.on_compile = lose, wrong_target, on_compile
        self.texts, self.lock = [], threading.Lock()

    def blob_of(self, file):
        return "Module.FactoryGame.1.cpp"

    def compile(self, blob, out_dir, function=None, include_first=None, full=False):
        path = (include_first / "FGFoo.cpp") if include_first else \
            self.project / "Source" / "FactoryGameRecomp-Recovered" / "FGFoo.cpp"
        text = path.read_text()
        with self.lock:
            self.texts.append((function, text))
        self.clock.advance(40)
        if self.on_compile:
            self.on_compile(len(self.texts))
        tick = 0 if "uint64 i" in text and self.win else 5
        scores = {"UFoo::Tick": ("matched" if tick == 0 else "differs", tick), "UFoo::Stuck": ("differs", 3),
                  "Other::Fine": ("matched", 0)}
        if function == "UFoo::Tick" and tick == 0 or full:
            matched = {n for n, (s, _) in scores.items() if s == "matched"}
            if tick == 0 and self.lose:
                matched.discard(self.lose)
            r = night.Result(scores[function][0] if function else "missing", scores[function][1] if function else None,
                             40, matched=matched, scores=scores, full=True)
            if r.state == "matched":
                r.targets = [(2, "?Other", "Wrong")] if self.wrong_target else []
            return r
        return night.Result(*scores[function], 40)


class Machine:
    def __init__(self, loads=()):
        self.loads = list(loads)
        self.name = "test machine"

    def default_jobs(self):
        return 2

    def default_max_load(self):
        return 4.0

    def load(self):
        return self.loads.pop(0) if self.loads else 0.5

    def busy(self):
        return None

    def be_polite(self):
        pass


class NightTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.project = self.dir / "Project"
        f = self.project / "Source" / "FactoryGameRecomp-Recovered" / "FGFoo.cpp"
        f.parent.mkdir(parents=True)
        f.write_text(SOURCE)
        (self.dir / "list.tsv").write_text(LIST)
        self.results = self.dir / "results"
        self.clock = Clock()
        self.logs = []

    def night(self, compiler=None, minutes=4.0, hours=None, machine=None, **kw):
        compiler = compiler or FakeCompiler(self.project, self.clock)
        deadline = None if hours is None else self.clock() + hours * 3600
        n = night.Night(self.project, night.read_list(self.dir / "list.tsv"), self.results, jobs=2,
                        per_function_minutes=minutes, deadline=deadline, log=self.logs.append, compiler=compiler,
                        machine=machine or Machine(), clock=self.clock, sleep=lambda s: self.clock.advance(s), **kw)
        return n, compiler

    def test_the_list_biggest_first_without_engine_drift(self):
        rows = night.read_list(self.dir / "list.tsv")
        self.assertEqual([r.function for r in rows], ["UFoo::Tick", "UFoo::Stuck"])
        self.assertEqual([r.function for r in night.read_list(self.dir / "list.tsv", "small")], ["UFoo::Stuck", "UFoo::Tick"])

    def test_the_list_s_minutes_and_skip_columns(self):
        # A-1: a row's own budget per round, and rows left out with the reason logged once at start
        (self.dir / "list.tsv").write_text(
            "blob\tfile\tfunction\tbytes\tcareful\tminutes\tskip\n"
            "1\tFGFoo.cpp\tUFoo::Tick\t300\t\t60\t\n"
            "1\tFGFoo.cpp\tUFoo::Stuck\t100\t\t\t\n"
            "1\tFGFoo.cpp\tAFoo::AFoo\t900\t\t\tengine drift: the base class grew\n")
        rows = night.read_list(self.dir / "list.tsv")
        by = {r.function: r for r in rows}
        self.assertEqual(by["UFoo::Tick"].minutes, 60.0)
        self.assertIsNone(by["UFoo::Stuck"].minutes)
        self.assertEqual(by["AFoo::AFoo"].skip, "engine drift: the base class grew")
        n, compiler = self.night(compiler=FakeCompiler(self.project, self.clock, win=False), minutes=4.0, hours=1.5)
        state = n.run()
        self.assertNotIn("AFoo::AFoo", state.functions)
        self.assertFalse(any(f == "AFoo::AFoo" for f, _ in compiler.texts))
        skipped = [line for line in self.logs if "AFoo::AFoo" in line]
        self.assertEqual(len(skipped), 1)
        self.assertIn("skipped: engine drift: the base class grew", skipped[0])
        # one round each: Tick ran up to its 60 minutes, Stuck up to the default 4 (both may end early when out of
        # candidates, never later)
        tick, stuck = state.functions["UFoo::Tick"], state.functions["UFoo::Stuck"]
        self.assertGreater(tick["seconds"] / tick["rounds"], 4 * 60 + 40)
        self.assertLessEqual(stuck["seconds"] / stuck["rounds"], 4 * 60 + 2 * 40)
        self.assertIn("of 2 (round", (self.results / "perf.txt").read_text())

    def test_a_list_without_the_new_columns_still_reads(self):
        rows = night.read_list(self.dir / "list.tsv")
        self.assertTrue(all(r.minutes is None and not r.skip for r in rows))

    def test_targeted_rules_find_the_win_and_it_is_saved_with_its_chain(self):
        n, compiler = self.night(hours=0.5)
        state = n.run()
        tick = state.functions["UFoo::Tick"]
        self.assertEqual(tick["result"], "won")
        win = json.loads((self.results / "wins" / "UFoo_Tick.json").read_text())
        self.assertEqual(win["chain"], ["counter-type"])
        self.assertIn("for( uint64 i = 0;", win["new"])
        self.assertIn("for( int32 i = 0;", win["old"])
        self.assertEqual(win["new"].count("\n"), win["old"].count("\n"))
        self.assertIn("+\tfor( uint64 i", (self.results / "wins" / "UFoo_Tick.diff").read_text())
        # the targeted candidates come before any random one
        first = [t for f, t in compiler.texts if f == "UFoo::Tick"]
        self.assertTrue(any("uint64 i" in t for t in first[:12]))
        report = (self.results / "report.tsv").read_text().splitlines()
        self.assertEqual(report[0].split("\t")[:9], ["function", "bytes", "start", "best", "result", "candidates",
                                                     "compiles", "minutes", "chain"])
        rows = {line.split("\t")[0]: line.split("\t") for line in report[1:]}
        self.assertEqual(rows["UFoo::Tick"][2:5], ["5", "0", "won"])
        self.assertEqual(rows["UFoo::Tick"][8], "counter-type")
        self.assertEqual(rows["UFoo::Stuck"][4], "no")
        perf = (self.results / "perf.txt").read_text()
        self.assertIn("per hour", perf)
        self.assertRegex(perf, r"wins +1 functions, 300 bytes")
        # the staged file is never rewritten, and the time ran out with UFoo::Stuck still pending more rounds
        self.assertEqual((self.project / "Source" / "FactoryGameRecomp-Recovered" / "FGFoo.cpp").read_text(), SOURCE)
        self.assertGreater(state.functions["UFoo::Stuck"]["rounds"], 1)

    PAIR = "\nvoid UFoo::Pair( int32 a )\n{\n\tWait( a, 2 );\n\tGo( mA + mB );\n}\n"
    PAIR_LIST = "blob\tfile\tfunction\tbytes\n1\tFGFoo.cpp\tUFoo::Pair\t200\n"

    def pair_night(self, **kw):
        """UFoo::Pair matches only with both of two targeted edits (its arguments swapped and its operands
        swapped); either alone leaves the score at 3."""
        f = self.project / "Source" / "FactoryGameRecomp-Recovered" / "FGFoo.cpp"
        f.write_text(SOURCE + self.PAIR)
        (self.dir / "list.tsv").write_text(self.PAIR_LIST)
        test = self

        class Fake(FakeCompiler):
            def compile(self, blob, out_dir, function=None, include_first=None, full=False):
                text = ((include_first / "FGFoo.cpp") if include_first else f).read_text()
                with self.lock:
                    self.texts.append((function, text))
                test.clock.advance(40)
                both = "Wait( 2, a );" in text and "mB + mA" in text
                scores = {"UFoo::Pair": ("matched", 0) if both else ("differs", 3), "Other::Fine": ("matched", 0)}
                if both or full:
                    matched = {n for n, (st, _) in scores.items() if st == "matched"}
                    st, sc = scores[function] if function else ("missing", None)
                    return night.Result(st, sc, 40, matched=matched, scores=scores, full=True, targets=[])
                return night.Result(*scores[function], 40)
        return self.night(compiler=Fake(self.project, self.clock), **kw)

    def test_a_fix_in_two_steps_the_first_a_tie_is_found_from_a_kept_tie(self):
        # A-2 (a, b): today an edit that ties the score is dropped and the targeted rules run from the best only
        n, compiler = self.pair_night(hours=0.5)
        state = n.run()
        pair = state.functions["UFoo::Pair"]
        self.assertEqual(pair["result"], "won")
        self.assertNotIn("random", pair["best_chain"])
        self.assertEqual(sorted(pair["best_chain"]), ["swap-arguments", "swap-operands"])

    def test_pairs_of_targeted_edits_when_the_parents_are_done(self):
        # A-2 (c): with no parent kept, two edits that each tie the score are tried together
        from unittest import mock
        with mock.patch.object(night, "KEEP", 0):
            n, compiler = self.pair_night(hours=0.5)
            state = n.run()
        pair = state.functions["UFoo::Pair"]
        self.assertEqual(pair["result"], "won")
        self.assertNotIn("random", pair["best_chain"])
        self.assertEqual(len(pair["best_chain"]), 2)
        self.assertLessEqual(pair["pairs"], night.PAIRS)

    def test_a_new_rule_set_runs_the_targeted_rules_again_from_every_base(self):
        # the done-marks are per text and rule set (permute.rules_version): new rules reach bases already done
        from unittest import mock
        n, _ = self.pair_night(hours=0.5)
        text = (self.project / "Source" / "FactoryGameRecomp-Recovered" / "FGFoo.cpp").read_text()
        best = text[text.index("void UFoo::Pair"):].rstrip("\n")
        fs = {"best_text": best, "best_chain": [], "best": 3, "original": best, "parents": [], "tried": [],
              "seen": [], "targeted_done": True}
        with mock.patch.object(night.permute, "rules_version", create=True, return_value="old"):
            first = n.candidates(fs, None, 10, None)
            self.assertEqual(len(first), 3)
            fs["seen"] += [night.sha(t) for t, _, _ in first]
            fs["tried"] += [c for _, _, c in first]
            n.candidates(fs, __import__("random").Random(0), 2, None)   # the best is done for "old"
            self.assertEqual(fs["targeted_from"], [night.sha(best) + ":old"])
        with mock.patch.object(night.permute, "rules_version", create=True, return_value="new"):
            n.candidates(fs, __import__("random").Random(0), 2, None)
            self.assertIn(night.sha(best) + ":new", fs["targeted_from"])

    def test_parents_keep_ties_once_each_other_chains_first(self):
        best = ["counter-type"]
        kept = night.keep_parents([[3, "a", best], [3, "b", best + ["random"]], [3, "a", ["x"]], [2, "c", best],
                                   [5, "d", ["y"]]], best)
        self.assertEqual([p[1] for p in kept], ["c", "b", "a", "d"])
        self.assertEqual(len(night.keep_parents([[1, str(i), []] for i in range(9)], [])), night.KEEP)

    def test_resume_goes_on_without_trying_a_candidate_twice(self):
        n, first = self.night(compiler=FakeCompiler(self.project, self.clock, win=False), hours=0.1)   # a few batches
        n.run()
        before = json.loads((self.results / "state.json").read_text())
        self.assertGreater(before["compiles"], 0)
        n2, second = self.night(compiler=FakeCompiler(self.project, self.clock, win=False), hours=0.3)
        n2.run()
        texts = [t for f, t in first.texts + second.texts if f]
        self.assertEqual(len(texts), len(set((f, t) for f, t in first.texts + second.texts if f)))
        after = json.loads((self.results / "state.json").read_text())
        self.assertGreater(after["compiles"], before["compiles"])
        self.assertFalse(any(f is None for f, _ in second.texts))   # the baseline is not compiled again

    def test_a_stop_file_finishes_the_compiles_in_flight_and_exits(self):
        stop = self.results / "STOP"

        def on_compile(k):
            if k == 3:
                stop.parent.mkdir(parents=True, exist_ok=True)
                stop.write_text("")
        compiler = FakeCompiler(self.project, self.clock, on_compile=on_compile)
        n, _ = self.night(compiler=compiler, hours=5)
        n.run()
        self.assertLessEqual(len(compiler.texts), 4)   # the baseline, one batch of two, maybe one more in flight
        self.assertTrue(any("STOP" in line for line in self.logs))
        state = json.loads((self.results / "state.json").read_text())
        self.assertEqual(state["compiles"], len(compiler.texts))
        # started again: the old STOP file goes, and the run goes on
        n2, again = self.night(hours=0.2)
        n2.run()
        self.assertFalse(stop.exists())
        self.assertTrue(again.texts)

    def test_it_waits_while_the_load_is_high(self):
        machine = Machine(loads=[9.0, 9.0, 9.0, 1.0])
        n, compiler = self.night(machine=machine, hours=0.05)
        n.run()
        self.assertTrue(any("waiting: busy 9.0 of 4.0 cores" in line for line in self.logs))
        self.assertTrue(any("going on" in line for line in self.logs))
        self.assertGreaterEqual(json.loads((self.results / "state.json").read_text())["waited"], 90)

    def test_wins_per_hour_count_only_this_machine_s(self):
        # A-3 (a): a state seeded from another machine's: its wins are inherited, not found here
        self.results.mkdir()
        (self.results / "wins").mkdir()
        old = {"result": "won", "bytes": 100, "candidates": 3, "found_by": "the PC", "found_at": "2026-11-19 03:00:00"}
        older = {"result": "won", "bytes": 50, "candidates": 2}   # from before found_by: its win file says where
        (self.results / "wins" / "UFoo_Older.json").write_text(json.dumps({"machine": "the PC"}))
        (self.results / "state.json").write_text(json.dumps({"functions": {
            "UFoo::Old": dict(night._new_function(night.Row("1", "FGFoo.cpp", "UFoo::Old", 100)), **old),
            "UFoo::Older": dict(night._new_function(night.Row("1", "FGFoo.cpp", "UFoo::Older", 50)), **older)}}))
        n, _ = self.night(hours=0.5)
        state = n.run()
        tick = state.functions["UFoo::Tick"]
        self.assertEqual(tick["found_by"], "test machine")
        self.assertRegex(tick["found_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        win = json.loads((self.results / "wins" / "UFoo_Tick.json").read_text())
        self.assertEqual((win["found_by"], win["found_at"]), ("test machine", tick["found_at"]))
        perf = (self.results / "perf.txt").read_text()
        m = __import__("re").search(r"wins +3 functions, 450 bytes \(1 found here: ([\d.]+) functions and [\d.]+ KB "
                                    r"per hour; 2 inherited\)", perf)
        self.assertTrue(m, perf)
        self.assertAlmostEqual(float(m.group(1)), 1 / (state.data["running"] / 3600), places=2)   # 1, not 3, a run

    def test_perf_and_state_are_written_while_it_waits(self):
        # A-3 (b): an hour's wait shows in perf.txt (its time and the minutes waited) and in the state
        n, _ = self.night(machine=Machine(loads=[9.0] * 15 + [1.0]), hours=1)
        n.wait_for_room()
        perf = (self.results / "perf.txt").read_text()
        self.assertRegex(perf, r"waited for the machine [56] min")
        self.assertIn("load now 9.0", perf)
        self.assertGreaterEqual(json.loads((self.results / "state.json").read_text())["waited"], 300)

    def test_it_starts_a_compile_only_while_there_is_room_for_it(self):
        # C-21 (deliberately replaces C-18's test): its own compiles count in the busy cores, so it grows into the
        # room left and shrinks when the machine is used. 2.5 busy of 4: room for one more.
        n, _ = self.night(machine=Machine(loads=[2.5] * 50), hours=0.05)
        n.wait_for_room()
        self.assertFalse(any("waiting" in line for line in self.logs))
        # 3.5 busy of 4 (say two of its compiles and someone's build): no room, it waits until a core frees up
        n2, _ = self.night(machine=Machine(loads=[3.5, 3.5, 2.0]), hours=0.05)
        n2.inflight = 2
        n2.wait_for_room()
        self.assertTrue(any("waiting: busy 3.5 of 4.0 cores (2 of them its own compiles)" in line for line in self.logs))
        self.assertTrue(any("going on" in line for line in self.logs))

    def test_on_a_mac_whose_top_does_not_answer_the_machine_counts_as_full(self):
        # A-4: the Mac rebooted while top timed out and the runner read the load average (17.6 of 8 on 10 cores)
        import subprocess
        from unittest import mock
        mac = night.Machine(cores=10, system="darwin")
        for failure in (subprocess.TimeoutExpired(["top"], 30), OSError("no top")):
            with mock.patch.object(night.subprocess, "run", side_effect=failure), \
                    mock.patch.object(night.os, "getloadavg", return_value=(17.6, 9.0, 5.0)):
                self.assertEqual(mac.load(), 10.0)
                self.assertIn("top", mac.unknown)
        idle = subprocess.CompletedProcess(["top"], 0, "CPU usage: 10.0% user, 10.0% sys, 80.0% idle\n" * 2, "")
        with mock.patch.object(night.subprocess, "run", return_value=idle):
            self.assertAlmostEqual(mac.load(), 2.0)
            self.assertIsNone(mac.unknown)
        # never more busy cores than cores (the load average elsewhere)
        with mock.patch.object(night.os, "getloadavg", return_value=(17.6, 9.0, 5.0)):
            self.assertEqual(night.Machine(cores=8, system="linux").load(), 8.0)
        # the runner waits while the load is unknown, and goes on once top answers
        calls = iter([subprocess.TimeoutExpired(["top"], 30), subprocess.TimeoutExpired(["top"], 30), idle, idle])

        def run(*a, **kw):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch.object(night.subprocess, "run", side_effect=run):
            n, _ = self.night(machine=mac, hours=0.05)
            n.wait_for_room()
        self.assertTrue(any("waiting: the load is unknown (top did not answer" in line for line in self.logs), self.logs)
        self.assertTrue(any("going on" in line for line in self.logs))
        self.assertGreaterEqual(json.loads((self.results / "state.json").read_text())["waited"], 60)

    def test_the_mac_s_defaults_leave_it_room(self):
        mac, pc = night.Machine(cores=10, system="darwin"), night.Machine(cores=8, system="windows")
        self.assertEqual((mac.default_jobs(), mac.default_max_load()), (2, 6.0))
        self.assertEqual((pc.default_jobs(), pc.default_max_load()), (4, 6.0))

    def test_a_match_that_breaks_another_function_is_no_win(self):
        compiler = FakeCompiler(self.project, self.clock, lose="Other::Fine")
        n, _ = self.night(compiler=compiler, hours=0.2)
        state = n.run()
        tick = state.functions["UFoo::Tick"]
        self.assertNotEqual(tick["result"], "won")
        self.assertIn("no longer", tick["note"])
        self.assertFalse((self.results / "wins" / "UFoo_Tick.json").exists())

    def test_a_match_whose_call_reaches_elsewhere_is_no_win(self):
        compiler = FakeCompiler(self.project, self.clock, wrong_target=True)
        n, _ = self.night(compiler=compiler, hours=0.2)
        state = n.run()
        self.assertNotEqual(state.functions["UFoo::Tick"]["result"], "won")
        self.assertIn("relocation", state.functions["UFoo::Tick"]["note"])

    def test_never_in_the_recovered_code_repository(self):
        from portkit import config
        with self.assertRaises(night.Fail):
            night.run(config.ROOT, self.dir / "list.tsv", self.results)

    def test_until_a_time_of_day(self):
        import time
        now = time.mktime((2026, 11, 17, 22, 0, 0, 0, 0, -1))
        self.assertAlmostEqual(night.deadline_from(until="06:30", clock=lambda: 0, now=now), 8.5 * 3600)
        self.assertAlmostEqual(night.deadline_from(until="23:00", clock=lambda: 0, now=now), 3600)
        self.assertEqual(night.deadline_from(hours=2, clock=lambda: 10), 7210)


if __name__ == "__main__":
    unittest.main()


class ApplyTest(unittest.TestCase):
    """night apply on a throwaway git repository; the compiler is a fake that reads the repository's file."""

    def setUp(self):
        import subprocess
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.repo = self.dir / "recovered"
        self.file = self.repo / "Source" / "FactoryGame" / "Private" / "FGFoo.cpp"
        self.file.parent.mkdir(parents=True)
        self.file.write_text(SOURCE)
        self.git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], capture_output=True, text=True, check=True).stdout
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", night.config.COMMIT_EMAIL)
        self.git("add", ".")
        self.git("commit", "-q", "-m", "start")
        self.results = self.dir / "results"
        (self.results / "wins").mkdir(parents=True)
        (self.results / "report.tsv").write_text("function\tbytes\tresult\nUFoo::Tick\t300\twon\n")
        old = SOURCE[SOURCE.index("void UFoo::Tick"):SOURCE.index("void UFoo::Stuck")].rstrip("\n")
        self.win = {"function": "UFoo::Tick", "file": "FGFoo.cpp", "blob": "Module.FactoryGame.1.cpp", "bytes": 300,
                    "old": old, "new": old.replace("int32 i", "uint64 i"), "chain": ["counter-type"]}
        (self.results / "wins" / "UFoo_Tick.json").write_text(json.dumps(self.win))

    def compiler(self, breaks=False):
        test = self

        class Fake:
            def compile(self, blob, out_dir, function=None, include_first=None, full=False):
                text = test.file.read_text()
                ok = "uint64 i" in text and not breaks
                matched = {"Other::Fine"} | ({"UFoo::Tick"} if "uint64 i" in text else set())
                if breaks and "uint64 i" in text:
                    matched.discard("Other::Fine")
                return night.Result("matched" if "uint64 i" in text else "differs", 0 if ok else 5, 1,
                                    matched=matched, targets=[], full=True)
        return Fake()

    def apply(self, **kw):
        return night.apply(self.results, self.repo, stage=self.dir / "stage", compiler=kw.pop("compiler", self.compiler()),
                           stage_fn=lambda repo, stage: None, log=lambda m: None, **kw)

    def test_a_win_is_applied_and_committed_alone(self):
        marks = self.apply(commit=True)
        self.assertEqual(marks, {"UFoo::Tick": "applied"})
        self.assertIn("for( uint64 i", self.file.read_text())
        log = self.git("log", "--format=%s%n%b", "-1")
        self.assertEqual(log.strip(), "Night: UFoo::Tick matches (counter-type)")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("\tapplied", (self.results / "report.tsv").read_text())
        self.assertEqual(self.apply(commit=True), {"UFoo::Tick": "already in"})

    def test_a_win_the_real_match_rejects_is_put_back(self):
        marks = self.apply(compiler=self.compiler(breaks=True), commit=True)
        self.assertTrue(marks["UFoo::Tick"].startswith("rejected: 1 functions no longer match (Other::Fine)"))
        self.assertEqual(self.file.read_text(), SOURCE)
        self.assertEqual(self.git("log", "--format=%s"), "start\n")
        self.assertIn("rejected", (self.results / "report.tsv").read_text())

    def test_a_carry_command_runs_and_its_files_are_committed_too(self):
        self.apply(commit=True, carry="cp {file} copy-of-$(basename {file}).txt")
        self.assertIn("copy-of-FGFoo.cpp.txt", self.git("show", "--name-only", "--format=", "HEAD"))

    def test_another_identity_is_refused(self):
        self.git("config", "user.email", "someone@example.com")
        with self.assertRaises(night.Fail):
            self.apply(commit=True)
        self.assertEqual(self.file.read_text(), SOURCE)

    def test_a_changed_function_is_stale(self):
        self.file.write_text(SOURCE.replace("Step( i );", "Step( i + 1 );"))
        self.assertTrue(self.apply()["UFoo::Tick"].startswith("stale"))


class FetchTest(unittest.TestCase):
    def test_the_reports_and_wins_come_back_without_the_workers(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            night.fetch("windows", "D:\\SatisfactoryRecomp\\night\\results", Path(tmp) / "pc", runner=runner)
        self.assertIn("windows:D:/SatisfactoryRecomp/night/results/report.tsv", calls[0])
        self.assertEqual(calls[1][:3], ["scp", "-q", "-r"])
        self.assertTrue(calls[1][3].endswith("/wins"))
        self.assertFalse(any("work" in part for cmd in calls for part in cmd))
