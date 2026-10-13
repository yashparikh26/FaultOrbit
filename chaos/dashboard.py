"""FaultOrbit dashboard: a read-only web view of experiment runs.

    python -m chaos.dashboard              # http://127.0.0.1:8765
    python -m chaos.dashboard --port 9000 --open

Standard library only. The server listens on 127.0.0.1 and only reads: it lists
saved runs, returns one run's record, and sends single GET probes for the live
view. Starting and stopping experiments stays in the controller, so the
dashboard can't change the sandbox or bypass the safety policy.

Endpoints (all JSON except /):
    GET /                    the single-page dashboard (chaos/web/index.html)
    GET /api/runs            every saved run (index.jsonl), newest first
    GET /api/runs/<run_id>   one full run record, plus its latency limit
    GET /api/live[?url=...]  one probe now (localhost URLs only) + active fault
    GET /api/status          active fault (if any) and the results folder
"""

import argparse
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from chaos import hooks
from chaos.controller import marker_path, probe
from chaos.watch import HOTEL_URL

STATIC_DIR = Path(__file__).resolve().parent / "web"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def active_fault():
    """The controller's marker while a fault is injected, else None."""
    try:
        return json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def runs_payload():
    rows = hooks.list_results()
    rows.reverse()  # newest first
    return {"runs": rows, "active_fault": active_fault()}


def run_payload(run_id):
    """Full record for a run listed in the index, or None if unknown."""
    known = {r.get("run_id") for r in hooks.list_results()}
    if run_id not in known:
        return None
    try:
        record = hooks.load_result(run_id)
    except (OSError, ValueError):
        return None
    try:
        limit = hooks.latency_limit_ms(record.get("baseline") or [], record.get("policy") or {})
    except (ValueError, TypeError):
        limit = None
    record["latency_limit_ms"] = limit
    return record


def is_local_url(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname in LOCAL_HOSTS


def live_payload(url=None, probe_fn=probe):
    url = url or HOTEL_URL
    if not is_local_url(url):
        raise ValueError("live probes are limited to localhost URLs")
    return {"url": url, "sample": probe_fn(url, timeout=2.0), "active_fault": active_fault()}


class Handler(BaseHTTPRequestHandler):
    server_version = "FaultOrbitDashboard/1.0"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, status, body, content_type):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, default=str), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/":
                html = (STATIC_DIR / "index.html").read_bytes()
                return self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
            if path == "/api/runs":
                return self._json(HTTPStatus.OK, runs_payload())
            if path.startswith("/api/runs/"):
                record = run_payload(unquote(path[len("/api/runs/"):]))
                if record is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                return self._json(HTTPStatus.OK, record)
            if path == "/api/live":
                url = (parse_qs(parsed.query).get("url") or [None])[0]
                try:
                    return self._json(HTTPStatus.OK, live_payload(url))
                except ValueError as e:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            if path == "/api/status":
                return self._json(HTTPStatus.OK, {"active_fault": active_fault(),
                                                  "results_dir": str(hooks.RESULTS_DIR)})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as e:  # never crash the server on one bad request
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})


def make_server(host="127.0.0.1", port=8765):
    return ThreadingHTTPServer((host, port), Handler)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m chaos.dashboard", description="FaultOrbit dashboard")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    args = ap.parse_args(argv)
    server = make_server("127.0.0.1", args.port)
    url = f"http://127.0.0.1:{args.port}"
    print(f"FaultOrbit dashboard on {url}  (reading {hooks.RESULTS_DIR}; Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
