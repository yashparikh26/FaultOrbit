"""Tests for chaos/hooks.py: safety check and results store."""

import json
import tempfile
import unittest
from pathlib import Path

from chaos import hooks


def s(t, ok=True, latency=20.0, status=200):
    return {"t": t, "ok": ok, "status": status if ok else None, "latency_ms": latency}


BASELINE = [s(i, latency=20 + (i % 5)) for i in range(20)]  # p95 ~24ms
POLICY = {"window_size": 10, "min_samples": 3, "max_error_rate": 0.5,
          "max_consecutive_failures": 5, "max_latency_ms": 2000,
          "latency_multiplier": 5.0, "latency_floor_ms": 200,
          "duration_s": 30}  # unrelated controller keys must be ignored


def run(samples, baseline=BASELINE, policy=POLICY):
    """Feed samples in order; return (index, reason) of the first stop, or (None, None)."""
    for i, smp in enumerate(samples):
        reason = hooks.should_stop(smp, baseline, policy)
        if reason:
            return i, reason
    return None, None


class ShouldStopTests(unittest.TestCase):
    def setUp(self):
        hooks.reset_state()

    def test_healthy_run_never_stops(self):
        self.assertEqual(run([s(100 + i, latency=30) for i in range(50)]), (None, None))

    def test_mild_slowdown_below_limit_continues(self):
        # 5 x 24ms = 120ms, floor 200ms -> limit 200ms; 150ms must not stop
        self.assertEqual(run([s(100 + i, latency=150) for i in range(30)]), (None, None))

    def test_total_outage_stops_on_consecutive_failures(self):
        idx, reason = run([s(100 + i, ok=False, latency=1000) for i in range(20)])
        self.assertIsNotNone(reason)
        self.assertIn("consecutive", reason)
        self.assertLessEqual(idx, 4)

    def test_intermittent_errors_stop_on_error_rate(self):
        # fail, fail, ok repeating: never 5 in a row, but 67% error rate
        samples = [s(100 + i, ok=(i % 3 == 2)) for i in range(30)]
        idx, reason = run(samples)
        self.assertIn("error rate", reason)

    def test_error_rate_at_limit_does_not_stop(self):
        samples = [s(100 + i, ok=(i % 2 == 0)) for i in range(30)]  # exactly 50%
        self.assertEqual(run(samples), (None, None))

    def test_latency_spike_stops(self):
        idx, reason = run([s(100 + i, latency=900) for i in range(20)])
        self.assertIn("p95 latency", reason)
        self.assertIn("200ms", reason)  # limit came from the floor
        self.assertEqual(idx, 2)        # stops once min_samples reached

    def test_absolute_latency_cap_applies_to_slow_baseline(self):
        slow_base = [s(i, latency=1000) for i in range(20)]  # 5x = 5000 > cap 2000
        idx, reason = run([s(100 + i, latency=2500) for i in range(10)], baseline=slow_base)
        self.assertIn("2000ms", reason)

    def test_single_slow_probe_does_not_stop(self):
        samples = [s(100 + i, latency=30) for i in range(10)]
        samples[5] = s(105, latency=5000)
        self.assertEqual(run(samples), (None, None))

    def test_window_forgets_old_failures(self):
        samples = ([s(100 + i, ok=False) for i in range(4)]
                   + [s(200 + i) for i in range(40)])
        self.assertEqual(run(samples), (None, None))

    def test_state_resets_between_experiments(self):
        run([s(100 + i, ok=(i % 3 == 2)) for i in range(4)])  # dirty window
        new_baseline = [s(i) for i in range(20)]
        self.assertEqual(run([s(500 + i) for i in range(10)], baseline=new_baseline),
                         (None, None))

    def test_time_going_backwards_resets_state(self):
        run([s(100 + i, ok=False) for i in range(4)])
        # same baseline object, but a new run starting at t=1
        self.assertIsNone(hooks.should_stop(s(1), BASELINE, POLICY))

    def test_malformed_sample_counts_as_failure(self):
        bad = [{"t": 100 + i} for i in range(5)]
        idx, reason = run(bad)
        self.assertIn("consecutive", reason)

    def test_bad_policy_fails_safe(self):
        reason = hooks.should_stop(s(1), BASELINE, {"max_error_rate": "lots"})
        self.assertIn("safety check error", reason)

    def test_missing_policy_keys_use_defaults(self):
        idx, reason = run([s(100 + i, ok=False) for i in range(10)], policy={})
        self.assertIn("consecutive", reason)

    def test_empty_baseline_uses_absolute_cap(self):
        self.assertEqual(hooks.latency_limit_ms([], POLICY), 2000.0)

    def test_check_baseline(self):
        self.assertIsNone(hooks.check_baseline(BASELINE, POLICY))
        bad = [s(i, ok=False) for i in range(10)]
        self.assertIn("unhealthy", hooks.check_baseline(bad, POLICY))


class SaveResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = hooks.RESULTS_DIR
        hooks.RESULTS_DIR = Path(self.tmp.name) / "results"

    def tearDown(self):
        hooks.RESULTS_DIR = self._orig
        self.tmp.cleanup()

    def record(self, name="kill-geo"):
        return {
            "experiment": {"name": name, "target": "hotel_reserv_geo", "fault": "stop"},
            "policy": POLICY,
            "baseline": BASELINE,
            "during": [s(100 + i, ok=i > 3, latency=40) for i in range(10)],
            "recovery": [s(200 + i) for i in range(5)],
            "stop_reason": "error rate 60% > 50% over last 5 probes",
            "recovered": True,
            "recovery_s": 3.2,
            "fault_started": 100.0,
            "fault_ended": 110.0,
        }

    def test_saves_full_record_with_summary(self):
        rec = self.record()
        path = Path(hooks.save_result(rec))
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        for key in rec:
            self.assertEqual(data[key], rec[key])
        self.assertEqual(data["summary"]["during"]["failures"], 4)
        self.assertEqual(data["summary"]["during"]["error_rate"], 0.4)
        self.assertEqual(data["schema_version"], hooks.SCHEMA_VERSION)
        self.assertNotIn("summary", rec)  # caller's dict untouched

    def test_index_and_listing(self):
        hooks.save_result(self.record("a"))
        hooks.save_result(self.record("b"))
        rows = hooks.list_results()
        self.assertEqual([r["experiment"] for r in rows], ["a", "b"])
        self.assertEqual(rows[0]["recovery_s"], 3.2)
        self.assertEqual(hooks.load_result(rows[1]["run_id"])["experiment"]["name"], "b")

    def test_same_second_runs_do_not_overwrite(self):
        p1 = hooks.save_result(self.record())
        p2 = hooks.save_result(self.record())
        self.assertNotEqual(p1, p2)

    def test_unsafe_name_stays_inside_results_dir(self):
        path = Path(hooks.save_result(self.record("../../etc/passwd")))
        self.assertEqual(path.parent, hooks.RESULTS_DIR)

    def test_non_json_values_are_stringified(self):
        rec = self.record()
        rec["extra"] = Path("/x")
        data = json.loads(Path(hooks.save_result(rec)).read_text())
        self.assertEqual(data["extra"], str(Path("/x")))

    def test_no_temp_files_left(self):
        hooks.save_result(self.record())
        self.assertEqual(list(hooks.RESULTS_DIR.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
