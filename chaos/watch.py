"""Live monitor: probe a URL once a second and print one line per probe.

Run it in a second terminal while an experiment runs, to watch the app break and
recover in real time. Read-only: it only sends GET requests.

Usage:
    python -m chaos.watch                 # the hotel /hotels endpoint the experiments probe
    python -m chaos.watch http://127.0.0.1:8081/      # any other URL (e.g. the dummy target)
    python -m chaos.watch --interval 0.5 --timeout 3

Stop with Ctrl+C; it prints a short summary.
"""

import argparse
import sys
import time
from datetime import datetime

from chaos.controller import probe

HOTEL_URL = "http://127.0.0.1:5000/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=38.0235&lon=-122.095"
BAR_MS_PER_CHAR = 50   # one bar character per 50 ms
BAR_MAX = 40


def format_line(sample, now=None):
    now = now or datetime.now()
    status = sample["status"] if sample["status"] is not None else "---"
    bar = "#" * min(BAR_MAX, int(sample["latency_ms"] // BAR_MS_PER_CHAR))
    word = "OK  " if sample["ok"] else "FAIL"
    return f"{now:%H:%M:%S}  {word}  status={status:<3}  {sample['latency_ms']:>7.1f} ms  {bar}"


def summarize(samples):
    if not samples:
        return "no probes"
    fails = sum(not s["ok"] for s in samples)
    worst = max(s["latency_ms"] for s in samples)
    return f"{len(samples)} probes, {fails} failed, slowest {worst:.0f} ms"


def watch(url, interval=1.0, timeout=2.0, count=None, probe_fn=probe, sleep=time.sleep, out=print):
    """Probe `url` until Ctrl+C (or `count` probes); return the samples."""
    samples = []
    out(f"watching {url}  (Ctrl+C to stop)")
    try:
        while count is None or len(samples) < count:
            s = probe_fn(url, timeout=timeout)
            samples.append(s)
            out(format_line(s))
            if count is None or len(samples) < count:
                sleep(interval)
    except KeyboardInterrupt:
        pass
    out(summarize(samples))
    return samples


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m chaos.watch", description=__doc__.splitlines()[0])
    ap.add_argument("url", nargs="?", default=HOTEL_URL)
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between probes (default 1)")
    ap.add_argument("--timeout", type=float, default=2.0, help="probe timeout in seconds (default 2)")
    args = ap.parse_args(argv)
    watch(args.url, interval=args.interval, timeout=args.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
