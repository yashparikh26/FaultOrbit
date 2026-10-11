# Safety controller and results store

Both live in `chaos/hooks.py` and are called by `chaos/controller.py`.

## Safety check: `should_stop(sample, baseline, policy)`

Called after every probe while a fault is active. It only decides whether to stop.
The controller does the restore, the hard timeout and the Ctrl+C kill switch.

A rolling window of the last `window_size` probes is kept per experiment.
The function stops the run when any of these rules trips:

| Rule | Trips when | Why |
|---|---|---|
| Consecutive failures | `max_consecutive_failures` failed probes in a row | Total outage: stop fast, don't wait for the window |
| Error rate | Window is full **and** failures / window > `max_error_rate` | Intermittent errors; a full window stops one short blip from ending a run |
| Latency | Window p95 > limit **and** at least `min_samples` probes over the limit | A single slow probe can't stop a run |

Latency limit = `min(max_latency_ms, max(latency_multiplier × baseline p95, latency_floor_ms))`.
The limit adapts to each app's normal speed and never goes below the floor or above the cap.

**Fail-safe:** a malformed sample counts as a failure. A bad policy value, or any error
inside the check, returns a "stopping to be safe" reason instead of letting a fault run unchecked.

`check_baseline(baseline, policy)` is a pre-flight check. The controller calls it after the
baseline samples: if it returns a reason, no fault is injected and the run is saved as refused.

### Policy keys (add to `chaos/policy.json`; missing keys use these defaults)

```json
{
  "window_size": 10,
  "min_samples": 3,
  "max_error_rate": 0.5,
  "max_consecutive_failures": 5,
  "max_latency_ms": 2000,
  "latency_multiplier": 5.0,
  "latency_floor_ms": 200
}
```

At one probe per second, the defaults tolerate about 5 s of total outage.
For a container-stop experiment, the consecutive-failures rule may stop the
run early. This is expected and accepted behavior. Experiments must not change
or override the safety policy.

## Results store: `save_result(record)`

- Writes `results/<timestamp>-<name>-<id>.json`: the full record plus `run_id`,
  `schema_version`, `saved_at`, and a computed `summary`. The summary gives the count,
  failures, error rate, p50, p95 and max latency for each of baseline, during and recovery.
- Appends one line to `results/index.jsonl` for quick listing.
- Writes atomically (temp file, then rename), so files are never half-written.
  Names are sanitised, so records always stay inside `results/`.
  Two runs in the same second don't overwrite each other.
- Override the folder with the `CHAOS_RESULTS_DIR` environment variable.

```bash
python -m chaos.hooks list              # table of every run
python -m chaos.hooks show <run_id>     # full record
```

## Tests

```bash
python -m unittest tests.test_hooks tests.test_controller
```
