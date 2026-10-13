# Hands-on demo: break the app and watch it happen

This walkthrough starts the Hotel Reservation app, injects three faults with FaultOrbit, and shows each failure live in the browser and in a terminal monitor. It takes about 20 minutes the first time (mostly image downloads), 5 minutes after that.

Commands are for **Windows PowerShell** from the repo root. On macOS/Linux use `python3` instead of `python`, and `grep` instead of `Select-String`.

**You need:** Git, Python 3.10+, Docker Desktop running (Linux containers), and ports 5000, 8500 and 16686 free.

## 1. Start the app

First time only (downloads DeathStarBench and the pinned images):

```powershell
python sandbox/hotel/prepare.py
cd sandbox/hotel/.runtime
docker compose pull --ignore-buildable
cd ../../..
```

Every time:

```powershell
cd sandbox/hotel/.runtime; docker compose up -d --build; cd ../../..
python sandbox/hotel/smoke.py          # all 7 checks must PASS
python -m chaos.controller --recover   # should say "nothing to recover"
```

## 2. Open the windows you'll watch

| Window | Open | What it shows |
|---|---|---|
| Browser tab: the app | `http://127.0.0.1:5000/hotels?inDate=2015-04-09&outDate=2015-04-10&lat=38.0235&lon=-122.095` | Hotel results as JSON when healthy; an error or a slow load during a fault. Refresh it during each experiment. The front page is `http://localhost:5000` |
| Browser tab: traces | `http://localhost:16686` → service `frontend` → **Find Traces** | Each request across all services; failed calls show errors, slow ones show long bars |
| Second terminal: live monitor | `python -m chaos.watch` | One line per second: OK/FAIL, status and latency with a bar. Stop with Ctrl+C for a summary |

The monitor probes the same URL as the experiments. For another target, pass it: `python -m chaos.watch http://127.0.0.1:8081/`.

## 3. Run the experiments (first terminal)

Each experiment takes a healthy baseline, injects one fault into one allowlisted container, stops early when a safety rule trips, restores the fault and verifies recovery. Every run is saved to `results/`.

### A. Stop `geo` (a hard dependency)

```powershell
python -m chaos.controller experiments/hotel_geo_stop.json
```

| Where | Expect |
|---|---|
| Controller | 5 `[baseline] ok status=200`, then `[fault] FAIL status=500` ×5, `SAFETY STOP: 5 consecutive failed probes`, `restored`, `recovered: True in ~1s` |
| Monitor | About 5 s of `FAIL`, then back to `OK` |
| Browser | Refresh during the fault: the request errors |

It stopped after 5 failures instead of running the full 20 s: that's the safety controller.

### B. Stop `rate`'s cache (the bug)

```powershell
python -m chaos.controller experiments/hotel_memcached_rate_stop.json
docker ps -a --filter name=hotel-sandbox-rate-1 --format "{{.Names}}  {{.Status}}"
docker logs hotel-sandbox-rate-1 2>&1 | Select-String "panic:" | Select-Object -First 1
```

| Where | Expect |
|---|---|
| Controller | Safety stop, cache restored, then `[recovery] FAIL` for 30 s and `recovered: False` |
| Monitor | **Stays on FAIL** after the cache is back |
| `docker ps` / logs | `rate` is `Exited`; the log shows `panic: ...` |

A cache outage should only make things slower. Instead `rate` calls `log.Panic` on any cache error (`services/rate/server.go:114`) and dies. FaultOrbit catches it because it verifies recovery instead of assuming it.

Recover by hand (the monitor returns to OK):

```powershell
cd sandbox/hotel/.runtime; docker compose start rate; cd ../../..
```

### C. 200 ms network delay on `rate` (best to watch in the browser)

```powershell
python -m chaos.controller experiments/hotel_rate_delay.json
```

| Where | Expect |
|---|---|
| Monitor | Latency jumps from about 30–50 ms to **1,300–1,900 ms**, then timeouts |
| Browser | The page loads visibly slowly |
| Controller | Safety stop, delay removed, latency back to normal at once |

200 ms on one backend becomes 6–10× slower at the edge, because each `/hotels` request goes through `rate` several times with no timeouts on that path.

The first run downloads the Pumba (`gaiaadm/pumba`) and `tc` (`gaiadocker/iproute2`) images. These are amd64-only, and the delay is not observable through Docker Desktop for Mac's port forwarding, so run this experiment on an Intel/AMD machine.

## 4. Look at the results

```powershell
python -m chaos.hooks list             # one row per run: error %, p95, recovery time, stop reason
python -m chaos.hooks show <run_id>    # the full record of one run
```

## 5. Try the kill switch

Start experiment A and press **Ctrl+C** during the fault. The controller prints `KILL SWITCH: Ctrl+C`, then `restored hotel-sandbox-geo-1`, and still checks recovery; the monitor goes back to OK.

## 6. Explore at your own pace (optional)

The safety rules end experiments after a few seconds on purpose. To look around the broken app slowly, stop a container by hand (local sandbox only):

```powershell
docker stop hotel-sandbox-geo-1     # refresh the browser, look at Jaeger
docker start hotel-sandbox-geo-1    # put it back
```

This skips FaultOrbit's safety rules and recovery checks. Use the controller for runs you report as results.

## 7. Shut down

```powershell
cd sandbox/hotel/.runtime; docker compose stop; cd ../../..
```

`docker compose down` also removes the containers but keeps the MongoDB volumes. `docker compose down --volumes` deletes the benchmark data too.

## If something goes wrong

| Problem | Fix |
|---|---|
| Smoke test fails | `cd sandbox/hotel/.runtime; docker compose ps --all; docker compose logs --tail=50` |
| `refused: baseline already unhealthy` | The pre-flight check working. Usually `rate` is still down from experiment B: `docker compose start rate`, then rerun |
| A run was killed mid-fault (terminal closed) | `python -m chaos.controller --recover`, then the smoke test |
| Output cut off / run died early | Don't pipe controller output into `Select-Object -First`, `head` etc.: it kills the run mid-fault |
| Hotel stack won't start | Use the small dummy target: `docker compose -f sandbox/dummy/docker-compose.yml up -d`, `python -m chaos.watch http://127.0.0.1:8081/`, then `python -m chaos.controller experiments/dummy_web_stop.json` |
