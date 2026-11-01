import unittest
from unittest import mock

from portkit import bench


class SampleTest(unittest.TestCase):
    def test_ctrl_c_keeps_what_was_sampled(self):
        sleeps = iter([None, KeyboardInterrupt()])

        def sleep(_):
            if isinstance(s := next(sleeps), BaseException):
                raise s

        with mock.patch.object(bench.wine, "processes", lambda: iter([(42, 2048, 50.0, "game.exe")])), \
                mock.patch.object(bench, "sysctl", lambda name: ""), \
                mock.patch.object(bench, "footprint_mb", lambda pid: 3000), \
                mock.patch.object(bench.time, "sleep", sleep):
            rows = bench.sample(42, duration=60)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rss_mb"], 2)
        self.assertEqual(bench.summarise(rows)["memory_mb_peak"], 3000)


if __name__ == "__main__":
    unittest.main()


class FootprintTest(unittest.TestCase):
    def test_own_footprint(self):
        import os
        mb = bench.footprint_mb(os.getpid())
        self.assertGreater(mb, 1)
        self.assertLess(mb, 10_000)

    def test_gone_process(self):
        self.assertIsNone(bench.footprint_mb(999_999))

    def test_summary_reports_footprint(self):
        rows = [{"t": 0, "rss_mb": 300, "footprint_mb": 4200, "cpu_percent": 100.0,
                 "swap_used_mb": 10.0, "free_memory_percent": 30}]
        s = bench.summarise(rows)
        self.assertEqual((s["memory_mb_mean"], s["memory_mb_peak"]), (4200, 4200))


class TraceSavedTest(unittest.TestCase):
    def test_saved_only_when_xctrace_says_so(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "xctrace.log"
            log.write_text("Reached specified time limit, ending recording...\n")
            self.assertFalse(bench.trace_saved(log))
            log.write_text("Recording completed. Saving output file...\nOutput file saved as: run.trace\n")
            self.assertTrue(bench.trace_saved(log))
