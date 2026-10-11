# Demo runbook

Goal: show the app running, one safe failure injected with visible impact, and the safety stop working. About 5 minutes.

All commands run from the repo root in PowerShell (`python3` on macOS/Linux).

## Pre-flight

```powershell
python sandbox/hotel/prepare.py
cd sandbox/hotel/.runtime; docker compose up -d --build; cd ../../..
python sandbox/hotel/smoke.py          # all 7 checks must PASS
```

Open these tabs: http://localhost:5000, http://localhost:16686 (Jaeger), and `docs/dependency-map.mmd` on GitHub (it renders the diagram).

## 1. The app is running (~1 min)

```powershell
docker ps --filter name=hotel-sandbox --format "{{.Names}}  {{.Status}}"
```

Say: 24 containers (10 Go gRPC services, 8 MongoDB, 4 memcached, Consul, Jaeger), with pinned images and restart turned off so Docker can't hide failures. Show the dependency map: `/hotels` goes frontend -> search -> geo, rate -> reservation -> profile.

## 2. Hard dependency: stop `geo` (~1 min)

```powershell
python -m chaos.controller experiments/hotel_geo_stop.json
```

Expected:
- 5 `[baseline] ok status=200` lines
- `[fault] FAIL status=500` lines
- `SAFETY STOP: 5 consecutive failed probes`
- `recovered: True in ~1s`

Say: the controller only touches allowlisted containers. It checked the app was healthy before injecting, and the safety rules stopped the fault after 5 failures rather than waiting 20 s. Recovery is verified, not assumed.

## 3. The finding: stop the rate cache (~2 min)

```powershell
python -m chaos.controller experiments/hotel_memcached_rate_stop.json
```

Expected: a safety stop, then `[recovery] FAIL status=500` for 30 s and `recovered: False`.

```powershell
docker ps -a --filter name=hotel-sandbox-rate-1 --format "{{.Names}}  {{.Status}}"   # Exited
docker logs hotel-sandbox-rate-1 2>&1 | Select-String "panic:" | Select-Object -First 1
```

Say: a cache outage should only slow things down. Instead `rate` calls `log.Panic` on any memcached error (`services/rate/server.go:114`), so the whole service died. It stays down even after the cache returns. The same pattern exists in profile, reservation and review. The controller caught this because it verifies recovery.

Recover by hand:

```powershell
cd sandbox/hotel/.runtime; docker compose start rate; cd ../../..
```

## 4. Evidence (~30 s)

```powershell
python -m chaos.hooks list
```

Say: every run is saved with its config, the samples, the stop reason and the recovery outcome. These records are what the AI agents read: one plans experiments, one ranks findings. The controller, not the AI, enforces safety.

## If something goes wrong

- Smoke fails: `cd sandbox/hotel/.runtime; docker compose ps --all; docker compose logs --tail=50`
- A run is `refused: baseline already unhealthy`: that's the safety check working. Fix the app (usually `docker compose start rate`), then rerun.
- Kill switch: Ctrl+C during a run. The fault is still restored.
- **Don't pipe controller output** into `Select-Object -First`, `head`, etc., and don't close the terminal mid-run: that force-kills the controller before it can restore. If it happens, run `python -m chaos.controller --recover` (the next experiment also does this automatically), then the smoke test.
- Fallback without the hotel stack: `docker compose -f sandbox/dummy/docker-compose.yml up -d` then `python -m chaos.controller experiments/dummy_web_stop.json`
