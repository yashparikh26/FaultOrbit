"""Tests for chaos/watch.py (no network)."""

import unittest
from datetime import datetime

from chaos import watch


def sample(ok=True, status=200, latency=30.0):
    return {"t": 0, "ok": ok, "status": status, "latency_ms": latency}


class WatchTests(unittest.TestCase):
    def test_format_ok_and_fail(self):
        t = datetime(2026, 1, 1, 12, 0, 5)
        self.assertEqual(watch.format_line(sample(latency=120), t),
                         "12:00:05  OK    status=200    120.0 ms  ##")
        line = watch.format_line(sample(ok=False, status=None, latency=2000), t)
        self.assertIn("FAIL  status=---", line)
        self.assertTrue(line.endswith("#" * watch.BAR_MAX))  # bar is capped

    def test_runs_count_probes_and_summarizes(self):
        seq = iter([sample(), sample(ok=False, status=500, latency=900), sample()])
        printed, slept = [], []
        samples = watch.watch("http://x", count=3, probe_fn=lambda url, timeout: next(seq),
                              sleep=slept.append, out=printed.append)
        self.assertEqual(len(samples), 3)
        self.assertEqual(len(slept), 2)              # no sleep after the last probe
        self.assertEqual(printed[-1], "3 probes, 1 failed, slowest 900 ms")

    def test_ctrl_c_stops_cleanly(self):
        calls = []

        def probe(url, timeout):
            calls.append(url)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return sample()

        printed = []
        samples = watch.watch("http://x", probe_fn=probe, sleep=lambda s: None, out=printed.append)
        self.assertEqual(len(samples), 1)
        self.assertEqual(printed[-1], "1 probes, 0 failed, slowest 30 ms")

    def test_default_url_matches_hotel_experiments(self):
        import json
        from pathlib import Path
        exp = json.loads(Path("experiments/hotel_geo_stop.json").read_text())
        self.assertEqual(watch.HOTEL_URL, exp["probe_url"])


if __name__ == "__main__":
    unittest.main()
