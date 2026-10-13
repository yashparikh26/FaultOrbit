# FaultOrbit

**Safe, AI-assisted chaos engineering for microservices.** FaultOrbit injects controlled failures into a sandboxed cloud-native app, measures the impact, stops automatically when safety thresholds are breached, verifies recovery, and records every run for analysis.

The target is [DeathStarBench Hotel Reservation](https://github.com/delimitrou/DeathStarBench/tree/master/hotelReservation): 24 containers (10 Go gRPC services, 8 MongoDB, 4 memcached, Consul, Jaeger) running locally on Docker Compose.

## Questions it answers

- What happens when a service or dependency fails?
- How much do latency, errors and health degrade?
- Does the app recover, and how fast?
- When should an experiment be stopped?
- Which weaknesses should be fixed first?

## Architecture

```text
Sample app (Docker Compose)
        │
Dependency map ──► Experiment controller ──► Fault injection (Docker, Pumba)
                              │
                     Health probes
                              │
                     Safety controller (auto-stop, kill switch, timeout)
                              │
                     Results store ──► AI agents (planner, analyst)
```

| Component | Where |
|---|---|
| Sample app sandbox | `sandbox/hotel/` (DeathStarBench Hotel Reservation, 24 containers) |
| Dependency map | `docs/dependency-map.json` (+ `.mmd` diagram) |
| Experiment controller + health probes | `chaos/controller.py` |
| Fault types | `chaos/faults.py`: container stop (Docker CLI), network delay ([Pumba](https://github.com/alexei-led/pumba)) |
| Safety rules + results store | `chaos/hooks.py`, thresholds in `chaos/policy.json` |
| AI planner / analyst | In progress (see [Roadmap](#roadmap)) |

**The controller enforces safety, not the AI.** AI output is a recommendation, never a command.

## Safety model

- Runs only in a local sandbox; never against production or shared infrastructure.
- Allowlisted containers and fault types only, in `chaos/policy.json`. Experiments can't change the policy.
- Pre-flight: refuses to inject if the baseline shows the app is already unhealthy.
- Three auto-stop rules after every probe: consecutive failures, error rate over a rolling window, and p95 latency against a baseline-relative limit. Fail-safe on bad data or internal errors.
- Hard timeout, Ctrl+C kill switch, and the fault is always restored. Recovery is verified, not assumed.

Details: [docs/SAFETY.md](docs/SAFETY.md).

## Findings

| Experiment | Result |
|---|---|
| Stop `memcached-rate` | `/hotels` fails, and **the `rate` service crashes**: it calls `log.Panic` on any cache error (`services/rate/server.go:114`). It doesn't recover until restarted by hand. The same pattern exists in profile, reservation and review |
| Stop `geo` | `/hotels` fails (a hard dependency) and recovers about 1 s after `geo` returns |
| 200 ms network delay on `rate` | `/hotels` goes from 30–50 ms to **1.3–1.9 s, then times out (>2 s)**: one slow backend amplifies 6–10× at the edge. The safety stop fires; it recovers instantly once the delay is removed |

These are defects in upstream DeathStarBench, surfaced automatically by the controller.

## Roadmap

- [x] Sandbox, dependency map, wrk2 baseline
- [x] Experiment controller with container-stop and network-delay faults
- [x] Safety controller and results store
- [ ] Analyst agent: reads saved runs, ranks weaknesses by severity, suggests fixes, computes a resilience score
- [ ] Planner agent: proposes the next experiment from the dependency map and past runs, validated against the policy
- [ ] Closed loop with human approval: plan → approve → run → analyse
- [ ] Fix `rate`'s cache crash and show the before/after

## Docs

- [Dependency findings](docs/hotel-reservation-dependencies.md): services, call graph, config, known defects
- [Dependency map](docs/dependency-map.json): machine-readable, used by the controller and the agents ([diagram](docs/dependency-map.mmd))
- [Baseline](docs/baseline.md): wrk2 steady-state latency
- [Safety controller and results store](docs/SAFETY.md)
- [Hands-on demo](docs/demo.md): run the app, inject faults and watch them live, step by step

## Setup

### Hotel Reservation sandbox

Requires Git, Python 3.10+, Docker Engine/Desktop with a running **Linux** daemon,
and Docker Compose v2.29.7 or later. On Windows, Docker Desktop with Linux
containers and PowerShell works directly; a WSL2 shell is not required.
Reserve localhost ports 5000, 8500, and 16686.

The eight core application services pull the immutable
`deathstarbench/hotel-reservation` image. It is **linux/amd64**, verified from its
registry image configuration. Apple Silicon hosts therefore need amd64
emulation enabled in Docker Desktop or a Colima VM; Intel/AMD hosts run it
natively. Review and attractions build from the pinned
upstream source using the host's default build platform. MongoDB, Memcached,
Consul, Jaeger, and the Go builder have arm64 and amd64 manifests.

From the repository root, use `python` on Windows (PowerShell) and `python3`
on Linux/macOS. The commands below use the Windows spelling; substitute
`python3` on Linux/macOS:

```powershell
docker info
docker compose version
python sandbox/hotel/prepare.py
cd sandbox/hotel/.runtime
docker compose pull --ignore-buildable
docker compose up -d --build
docker compose ps --all
python ../smoke.py
```

The first preparation clones DeathStarBench at commit
`6ecb09706140f8730b5385c08f1386c654c3c526`. It reads the **existing upstream
`hotelReservation/docker-compose.yml`** through Compose and derives a local
`compose.yaml`; it does not edit tracked upstream source files or invent services.
Generated files live under the gitignored `sandbox/hotel/.runtime/`.
Preparation stops if an existing upstream checkout has a different revision or
tracked modifications.

For Windows checkout compatibility, preparation enables repository-local
`core.longpaths=true` immediately after clone, then selects only
`hotelReservation` with `git sparse-checkout set --cone --sparse-index`
before checkout. This avoids invalid `*` filenames in `daprApps_v1/` and
supports long paths under `vendor/` and `helm-chart/`.
If an earlier preparation failed during checkout, move just
`sandbox/hotel/.runtime/DeathStarBench` to a backup location and rerun preparation.
Keep any local edits in that backup; there is no need to delete Docker volumes.

After preparation, plain `docker compose up -d --build` in that directory starts
the defined stack (only review and attractions have build definitions). Source
builds use the pinned Go base in a generated `Dockerfile.sandbox`. All registry
images, including MongoDB and the Go builder, use the immutable digests recorded
in [images.lock.json](sandbox/hotel/images.lock.json). The lock records the
human-readable version used when resolving each digest:

| Image | Version resolved |
|---|---|
| deathstarbench/hotel-reservation | latest resolved to digest on 10 October 2026; amd64 only |
| hashicorp/consul | 1.17.3 |
| jaegertracing/all-in-one | 1.54.0 (legacy UDP agent supported) |
| memcached | 1.6.24 |
| mongo | 5.0.26 |
| golang | 1.21.13 |

Runtime configuration uses digests, **not floating latest tags**. Re-running
preparation does not refresh the lock. Any image upgrade requires an explicit
lock change and another smoke run. The pulled prebuilt application's source
revision is not established by this lock; source-built review/attractions use
the commit above.

### Readiness and verification

- Frontend: [http://localhost:5000](http://localhost:5000)
- Consul: [http://localhost:8500](http://localhost:8500)
- Jaeger UI: [http://localhost:16686](http://localhost:16686)

Ports bind only to 127.0.0.1. Database/cache ports stay internal. The generator
mounts the upstream config at `/workspace/config.json`, matching the verified
prebuilt image working directory and source Dockerfile. TLS is disabled for this
local sandbox; trace sampling is set to 1 for verification. Consul and MongoDB
health checks gate dependent service startup. Memcached runs with explicit
`-m 128 -t 2` flags. HTTP readiness is checked by the smoke script.

The smoke script checks all 24 containers are running, verifies actual Docker
restart policies, tests valid/invalid login, requires non-empty search and
recommendation results, creates **one real benchmark reservation**, verifies its
MongoDB record, and checks Jaeger UI plus a reported frontend tracing service.
It saves timestamped pass/fail evidence to `results/hotel-smoke-*.json` even if a
check fails. Reservation requests are not automatically retried.

This is a small API smoke run, not the fixed-rate wrk2 baseline in [docs/baseline.md](docs/baseline.md).
It uses seeded benchmark dates (2015-04-09 to 2015-04-10) and may consume one
room per successful invocation. It checks container state for review and
attractions, not their frontend RPC routes.

### Restart policy for chaos experiments

**Decision: disable automatic restart on every service in this sandbox.**
The generated model sets `restart: "no"` and removes upstream
`deploy.restart_policy.condition: any`. Docker must not undo a killed-container
fault before our safety controller observes it. A crashed startup stays visible;
inspect logs and fix the cause rather than relying on a restart loop.

The controller remains responsible for restoring its allowlisted target and
verifying recovery. This setup does not add Hotel containers to the controller's
allowlist automatically. Identify the exact container in this Compose project
before configuring an experiment:

```bash
docker compose ps --all
docker compose ps -q frontend
docker compose logs --tail=100 frontend search reservation
```

For manual recovery of a stopped container, use `docker compose start SERVICE`
inside this sandbox only. Starting an application again may rerun upstream
seed insertion. Do not assume the benchmark data is unchanged after a restart.

### Shutdown, data, and troubleshooting

```bash
# Stop and remove this project's containers/network, retaining MongoDB volumes:
docker compose down
# Start it again:
docker compose up -d --build
```

MongoDB data is persistent in project-specific `hotel-sandbox_*` named volumes.
Upstream initializers are not idempotent; repeated starts may duplicate seed
records. For a deliberately fresh benchmark, `docker compose down --volumes`
deletes this sandbox's database contents. Use that command only when you intend
to discard the local benchmark data; never use a global Docker prune.

On startup failure, inspect `docker compose ps --all` and
`docker compose logs --tail=100`. On an Apple Silicon execution-format failure,
check amd64 emulation in the Docker VM. On frontend smoke failures, keep the
saved evidence and logs; do not equate running containers or HTTP 200 with a
working booking flow. See the known source/configuration caveats in
[dependency findings](docs/hotel-reservation-dependencies.md), including the
review/attractions discovery mismatch. This deployment does not silently patch
the pulled application binaries.

### Validation status

The configuration was generated from the pinned upstream Compose file and
validated with standalone Docker Compose **v2.29.7**. Run the focused tests with
`python` on Windows or `python3` on Linux/macOS:

```powershell
python -m unittest discover -s tests -p 'test_hotel_setup.py' -v
```

**Verified end to end:** all 7 smoke checks pass on Windows 11 / Intel (amd64,
Docker Desktop) from a fresh clone.
The stack also runs on Apple Silicon (M2, M5) with amd64 emulation.

## Running experiments

The controller injects one fault into one **allowlisted** container, probes the app once a
second, stops early when a safety rule trips, restores the fault, and verifies recovery.

```powershell
# Quick start without the hotel stack (tiny nginx target):
docker compose -f sandbox/dummy/docker-compose.yml up -d
python -m chaos.controller experiments/dummy_web_stop.json

# Real app (stack from Setup above must be running):
python -m chaos.controller experiments/hotel_geo_stop.json
python -m chaos.controller experiments/hotel_memcached_rate_stop.json

# Watch it live in a second terminal (one line per second: OK/FAIL, latency):
python -m chaos.watch

# Past runs:
python -m chaos.hooks list
python -m chaos.hooks show <run_id>
```

New here? Follow the [hands-on demo](docs/demo.md): it walks through all three experiments with what to expect in the terminal, the browser and Jaeger.

- **Experiments** (`experiments/*.json`): target, fault, duration, probe URL, baseline samples.
- **Policy** (`chaos/policy.json`): allowed targets and faults, max duration, hard timeout, and
  safety thresholds. Experiments can't change it. See [docs/SAFETY.md](docs/SAFETY.md).
- **Before injecting**, the controller refuses if the baseline shows the app is already unhealthy.
- **Kill switch:** Ctrl+C. The fault is always restored.
- **If the controller is force-killed** mid-run (terminal closed), the fault stays in place.
  `python -m chaos.controller --recover` restores it; the next experiment also does this automatically.
- **Results** are saved to `results/` (gitignored): config, every probe sample, stop reason and
  recovery time, with a computed summary.

Tests:

```powershell
python -m unittest discover -s tests -t .
```

## License

MIT. See [LICENSE](LICENSE).

