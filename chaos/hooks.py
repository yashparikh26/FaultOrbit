"""Safety check and results store used by chaos/controller.py.

The controller calls exactly two functions here; their signatures are fixed:

    should_stop(sample, baseline, policy) -> str | None
    save_result(record) -> str

Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path

RESULTS_DIR = Path(
    os.environ.get("CHAOS_RESULTS_DIR")
    or Path(__file__).resolve().parent.parent / "results"
)
SCHEMA_VERSION = 1

# Defaults used when a key is missing from policy.json.
DEFAULT_POLICY = {
    "window_size": 10,            # rolling window of recent probes during the fault
    "min_samples": 3,             # don't judge rates/percentiles on fewer probes than this
    "max_error_rate": 0.5,        # stop if > 50% of probes in the window failed
    "max_consecutive_failures": 5,  # stop at once on a run of failures (total outage)
    "max_latency_ms": 2000,       # absolute ceiling for window p95 latency
    "latency_multiplier": 5.0,    # ...or this many times the baseline p95, whichever is lower
    "latency_floor_ms": 200,      # but never a limit below this (tiny baselines would be too strict)
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def percentile(values, pct):
    """Nearest-rank percentile; None for an empty list."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    rank = max(1, math.ceil(pct / 100.0 * len(vals)))
    return vals[rank - 1]


def _is_ok(sample):
    # A malformed sample counts as a failure: when unsure, lean towards stopping.
    return isinstance(sample, dict) and sample.get("ok") is True


def _latency(sample):
    if not isinstance(sample, dict):
        return None
    v = sample.get("latency_ms")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
        return None
    return float(v)


def summarize(samples):
    """Counts, error rate and latency percentiles for a list of probe samples."""
    samples = list(samples or [])
    n = len(samples)
    ok_lat = [_latency(s) for s in samples if _is_ok(s)]
    ok_lat = [v for v in ok_lat if v is not None]
    failures = sum(1 for s in samples if not _is_ok(s))
    return {
        "count": n,
        "failures": failures,
        "error_rate": round(failures / n, 4) if n else None,
        "p50_ms": percentile(ok_lat, 50),
        "p95_ms": percentile(ok_lat, 95),
        "max_ms": max(ok_lat) if ok_lat else None,
    }


def effective_policy(policy):
    """policy.json merged over the defaults, with numeric values validated."""
    merged = dict(DEFAULT_POLICY)
    for key, value in (policy or {}).items():
        if key in DEFAULT_POLICY and value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"policy {key!r} must be a number, got {value!r}")
            merged[key] = value
    if merged["window_size"] < 1 or merged["min_samples"] < 1:
        raise ValueError("window_size and min_samples must be >= 1")
    if not 0 <= merged["max_error_rate"] <= 1:
        raise ValueError("max_error_rate must be between 0 and 1")
    return merged


def latency_limit_ms(baseline, policy):
    """Latency threshold for this experiment, derived from the baseline."""
    p = effective_policy(policy)
    base_p95 = summarize(baseline)["p95_ms"]
    limit = p["max_latency_ms"]
    if base_p95 is not None:
        relative = max(base_p95 * p["latency_multiplier"], p["latency_floor_ms"])
        limit = min(limit, relative)
    return float(limit)


def check_baseline(baseline, policy):
    """Optional pre-flight check: return a reason if the app is already unhealthy.

    The controller calls this after the baseline and refuses the run on a reason.
    """
    p = effective_policy(policy)
    s = summarize(baseline)
    if s["count"] < p["min_samples"]:
        return f"baseline too short ({s['count']} samples < {p['min_samples']})"
    if s["error_rate"] > p["max_error_rate"]:
        return (f"baseline already unhealthy: error rate {s['error_rate']:.0%} "
                f"> {p['max_error_rate']:.0%}")
    return None


# --------------------------------------------------------------------------- #
# Safety check
# --------------------------------------------------------------------------- #

class _RunState:
    """Rolling window for one experiment. Keyed by the baseline list it belongs to."""

    def __init__(self, key, window_size):
        self.key = key
        self.window = deque(maxlen=int(window_size))
        self.consecutive_failures = 0
        self.last_t = None


_state: _RunState | None = None


def _baseline_key(baseline):
    # The controller passes the same baseline list for every probe in one experiment.
    # id() alone could be reused after garbage collection, so add cheap identity hints.
    first_t = baseline[0].get("t") if baseline and isinstance(baseline[0], dict) else None
    return (id(baseline), len(baseline or []), first_t)


def reset_state():
    """Forget the rolling window (tests, or a controller starting a new run)."""
    global _state
    _state = None


def should_stop(sample, baseline, policy):
    """Safety check. Called after every probe while a fault is active.

    sample:   {"t": float, "ok": bool, "status": int|None, "latency_ms": float}
    baseline: list of samples taken before the fault
    policy:   contents of policy.json (threshold keys: see DEFAULT_POLICY)

    Returns a short reason string to stop early, or None to continue.
    Only decides; the controller does the restore, hard timeout and kill switch.
    Fail-safe: if the check itself errors, it asks to stop rather than carry on blind.
    """
    global _state
    try:
        p = effective_policy(policy)
        key = _baseline_key(baseline)
        t = sample.get("t") if isinstance(sample, dict) else None

        new_run = (
            _state is None
            or _state.key != key
            or _state.window.maxlen != int(p["window_size"])
            or (t is not None and _state.last_t is not None and t < _state.last_t)
        )
        if new_run:
            _state = _RunState(key, p["window_size"])
        st = _state

        st.window.append(sample)
        st.last_t = t if t is not None else st.last_t
        st.consecutive_failures = 0 if _is_ok(sample) else st.consecutive_failures + 1

        # 1. A run of failures: the service is down, no need to wait for the window.
        if st.consecutive_failures >= p["max_consecutive_failures"]:
            return (f"{st.consecutive_failures} consecutive failed probes "
                    f"(limit {p['max_consecutive_failures']})")

        if len(st.window) < p["min_samples"]:
            return None

        # 2. Error rate, judged only on a full window so one short blip can't stop a run.
        win = summarize(st.window)
        if len(st.window) >= st.window.maxlen and win["error_rate"] > p["max_error_rate"]:
            return (f"error rate {win['error_rate']:.0%} > {p['max_error_rate']:.0%} "
                    f"over last {win['count']} probes")

        # 3. Latency: p95 over the window above the limit, and at least min_samples
        #    slow probes (on a small window p95 is just the max, so one spike isn't enough).
        if win["p95_ms"] is not None:
            limit = latency_limit_ms(baseline, p)
            slow = sum(1 for x in st.window
                       if _is_ok(x) and (_latency(x) or 0) > limit)
            if win["p95_ms"] > limit and slow >= p["min_samples"]:
                base = summarize(baseline)["p95_ms"]
                base_txt = f", baseline p95 {base:.0f}ms" if base is not None else ""
                return (f"p95 latency {win['p95_ms']:.0f}ms > {limit:.0f}ms "
                        f"over last {win['count']} probes{base_txt}")
        return None
    except Exception as exc:  # never let a bug in the safety check keep a fault running
        return f"safety check error, stopping to be safe: {exc!r}"


# --------------------------------------------------------------------------- #
# Results store
# --------------------------------------------------------------------------- #

def _slug(name):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "experiment")).strip("-.")
    return (slug or "experiment")[:60]


def _atomic_write(path, text):
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # readers never see a half-written file


def save_result(record):
    """Results store. Persist one experiment record, return where it went.

    Writes results/<timestamp>-<name>-<id>.json (the full record plus a computed
    summary) and appends one line to results/index.jsonl for quick listing.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    experiment = record.get("experiment") or {}
    name = experiment.get("name") if isinstance(experiment, dict) else str(experiment)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(name)}-{uuid.uuid4().hex[:6]}"
    path = RESULTS_DIR / f"{run_id}.json"

    summary = {
        "baseline": summarize(record.get("baseline")),
        "during": summarize(record.get("during")),
        "recovery": summarize(record.get("recovery")),
    }
    out = dict(record)  # don't mutate the controller's dict
    out["run_id"] = run_id
    out["schema_version"] = SCHEMA_VERSION
    out["saved_at"] = time.time()
    out["summary"] = summary

    _atomic_write(path, json.dumps(out, indent=2, default=str))

    index_line = {
        "run_id": run_id,
        "file": path.name,
        "experiment": name,
        "target": experiment.get("target") if isinstance(experiment, dict) else None,
        "fault_started": record.get("fault_started"),
        "fault_ended": record.get("fault_ended"),
        "stop_reason": record.get("stop_reason"),
        "recovered": record.get("recovered"),
        "recovery_s": record.get("recovery_s"),
        "during_error_rate": summary["during"]["error_rate"],
        "during_p95_ms": summary["during"]["p95_ms"],
        "baseline_p95_ms": summary["baseline"]["p95_ms"],
    }
    with open(RESULTS_DIR / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(index_line, default=str) + "\n")
    return str(path)


def list_results():
    """All index entries, oldest first (skips any corrupt line)."""
    index = RESULTS_DIR / "index.jsonl"
    if not index.exists():
        return []
    rows = []
    for line in index.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_result(run_id):
    """Full record for a run_id (or a file name)."""
    name = run_id if run_id.endswith(".json") else f"{run_id}.json"
    return json.loads((RESULTS_DIR / Path(name).name).read_text(encoding="utf-8"))


def _fmt(v, spec=""):
    return "-" if v is None else format(v, spec)


def _main(argv):
    if len(argv) >= 2 and argv[0] == "show":
        print(json.dumps(load_result(argv[1]), indent=2))
        return 0
    rows = list_results()
    if not rows:
        print(f"no results in {RESULTS_DIR}")
        return 0
    print(f"{'run_id':<48} {'err%':>5} {'p95ms':>7} {'rec_s':>6}  stop_reason")
    for r in rows:
        err = r.get("during_error_rate")
        print(f"{r['run_id']:<48} {_fmt(None if err is None else err * 100, '.0f'):>5} "
              f"{_fmt(r.get('during_p95_ms'), '.0f'):>7} {_fmt(r.get('recovery_s'), '.1f'):>6}  "
              f"{r.get('stop_reason') or ('recovered' if r.get('recovered') else 'NOT RECOVERED')}")
    return 0


if __name__ == "__main__":  # python -m chaos.hooks [list | show <run_id>]
    import sys
    raise SystemExit(_main(sys.argv[1:]))
