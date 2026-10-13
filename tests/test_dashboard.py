"""Tests for chaos/dashboard.py: API helpers and the HTTP server (no Docker, no outside network)."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from chaos import dashboard, hooks


def s(t, ok=True, latency=20.0):
    return {"t": t, "ok": ok, "status": 200 if ok else 500, "latency_ms": latency}


POLICY = {"window_size": 10, "min_samples": 3, "max_error_rate": 0.5, "max_consecutive_failures": 5,
          "max_latency_ms": 2000, "latency_multiplier": 5.0, "latency_floor_ms": 200}


def record(name="geo-stop", recovered=True):
    return {"experiment": {"name": name, "target": "hotel-sandbox-geo-1", "fault": "container_stop"},
            "policy": POLICY, "baseline": [s(i) for i in range(5)],
            "during": [s(10 + i, ok=False) for i in range(5)], "recovery": [s(20)],
            "stop_reason": "safety: 5 consecutive failed probes (limit 5)",
            "recovered": recovered, "recovery_s": 1.0 if recovered else None,
            "fault_started": 9.5, "fault_ended": 15.0}


class TempResults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = hooks.RESULTS_DIR
        hooks.RESULTS_DIR = Path(self.tmp.name)

    def tearDown(self):
        hooks.RESULTS_DIR = self._orig
        self.tmp.cleanup()


class PayloadTests(TempResults):
    def test_runs_newest_first(self):
        hooks.save_result(record("a"))
        hooks.save_result(record("b"))
        payload = dashboard.runs_payload()
        self.assertEqual([r["experiment"] for r in payload["runs"]], ["b", "a"])
        self.assertIsNone(payload["active_fault"])

    def test_run_payload_adds_latency_limit(self):
        run_id = Path(hooks.save_result(record())).stem
        rec = dashboard.run_payload(run_id)
        self.assertEqual(rec["experiment"]["name"], "geo-stop")
        self.assertEqual(rec["latency_limit_ms"], 200.0)  # 5 x 20 ms = 100, raised to the 200 ms floor

    def test_unknown_or_unsafe_run_id(self):
        hooks.save_result(record())
        self.assertIsNone(dashboard.run_payload("nope"))
        self.assertIsNone(dashboard.run_payload("../../etc/passwd"))

    def test_active_fault_reads_marker(self):
        dashboard.marker_path().write_text(json.dumps({"target": "t", "fault": "container_stop"}))
        self.assertEqual(dashboard.active_fault()["target"], "t")

    def test_live_only_localhost(self):
        self.assertTrue(dashboard.is_local_url("http://127.0.0.1:5000/hotels"))
        self.assertTrue(dashboard.is_local_url("http://localhost:8081/"))
        for bad in ["http://example.com/", "file:///etc/passwd", "http://10.0.0.5/", "not a url"]:
            self.assertFalse(dashboard.is_local_url(bad), bad)
        with self.assertRaises(ValueError):
            dashboard.live_payload("http://example.com/")
        got = dashboard.live_payload("http://127.0.0.1:1/", probe_fn=lambda url, timeout: s(1))
        self.assertTrue(got["sample"]["ok"])

    def test_live_defaults_to_hotel_url(self):
        seen = []
        dashboard.live_payload(probe_fn=lambda url, timeout: seen.append(url) or s(1))
        self.assertEqual(seen, [dashboard.HOTEL_URL])


class ServerTests(TempResults):
    def setUp(self):
        super().setUp()
        self.server = dashboard.make_server("127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # talk to localhost directly
        try:
            with opener.open(self.base + path, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_serves_page(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"FaultOrbit", body)

    def test_runs_and_run_endpoints(self):
        run_id = Path(hooks.save_result(record())).stem
        status, ctype, body = self.get("/api/runs")
        self.assertEqual((status, ctype.split(";")[0]), (200, "application/json"))
        self.assertEqual(json.loads(body)["runs"][0]["run_id"], run_id)
        status, _, body = self.get(f"/api/runs/{run_id}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["latency_limit_ms"], 200.0)
        self.assertEqual(self.get("/api/runs/missing")[0], 404)

    def test_live_rejects_remote_url(self):
        status, _, body = self.get("/api/live?url=http://example.com/")
        self.assertEqual(status, 400)
        self.assertIn("localhost", json.loads(body)["error"])

    def test_live_probe(self):
        fake = lambda url=None: {"url": url, "sample": s(1, latency=12.0), "active_fault": None}
        with mock.patch.object(dashboard, "live_payload", fake):
            status, _, body = self.get("/api/live")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["sample"]["latency_ms"], 12.0)

    def test_status_and_unknown_path(self):
        status, _, body = self.get("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("results_dir", json.loads(body))
        self.assertEqual(self.get("/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
