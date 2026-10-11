# Hotel Reservation steady-state baseline

Captured: 2026-10-10  
Sandbox: the `sandbox/hotel` setup, using its pinned Hotel Reservation images  
Faults: none (normal steady-state run)

## Workload

- Target: `http://frontend:5000` from a container on `hotel-sandbox_default`
- wrk2: 2 threads, 10 connections, 50 requests/second, 60 seconds
- Lua workload: `hotel-reservation/mixed-workload_type_1.lua`
- Lua dependency: LuaSocket installed with `luarocks install luasocket`
- The Linux ARM64 wrk2 container builds its own Linux LuaJIT from the mounted source copy. The source mount is read-only.

`sandbox/hotel/prepare.py` checks out only `hotelReservation/`, so first add the wrk2 source and its LuaJIT submodule (from the repo root):

```bash
git -C sandbox/hotel/.runtime/DeathStarBench sparse-checkout add wrk2
git -C sandbox/hotel/.runtime/DeathStarBench submodule update --init wrk2/deps/luajit
cd sandbox/hotel/.runtime/DeathStarBench/hotelReservation
```

Run the following from that `hotelReservation` directory. The source path `../wrk2` and script path `wrk2/scripts` are relative to it. On Intel/AMD hosts use `--platform linux/amd64`.

```bash
docker run --rm --platform linux/arm64 --network hotel-sandbox_default \
  -v "$PWD/../wrk2:/source:ro" \
  -v "$PWD/wrk2/scripts:/scripts:ro" \
  ubuntu:24.04 bash -lc \
  'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq build-essential libssl-dev zlib1g-dev luarocks ca-certificates >/dev/null && luarocks install luasocket >/dev/null && cp -R /source /tmp/wrk2 && cd /tmp/wrk2 && make clean && make && ./wrk -D exp -t 2 -c 10 -d 60s -R 50 -L -s /scripts/hotel-reservation/mixed-workload_type_1.lua http://frontend:5000'
```

**Windows:** the `submodule update` step fails because other folders in the DeathStarBench repo contain `*` filenames. Instead, clone `https://github.com/LuaJIT/LuaJIT.git` separately, check out the pinned commit `2090842410e0ba6f81fad310a77bf5432488249a`, add `-v <luajit-path>:/luajit:ro` to the `docker run`, and run `cp -R /luajit/. /tmp/wrk2/deps/luajit/` after copying `/source` and before `make` (skip `make clean`). Keep the shell commands in a mounted script file: Windows PowerShell 5.1 strips double quotes from native-command arguments.

## Results

| Machine / host | wrk2 container platform | Requests | Throughput | Average | p50 | p90 | p99 | p99.9 | Errors/timeouts |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| Windows 11, Intel i7-13700HX (amd64 host) | linux/amd64 | 3,001 | 50.01 req/s | Not captured | 4.6 ms | 11.8 ms | 17.4 ms | 22.9 ms | 0 socket errors, 0 non-2xx |
| MacBook Air M5 (arm64 host) | linux/arm64 | 3,001 | 50.00 req/s | 4.95 ms | 3.14 ms | 9.18 ms | 22.78 ms | 171.26 ms | No error/timeout lines in captured output; run completed at target throughput |

On the Mac, the app used the pinned image, which the image lock identifies as linux/amd64. Docker Desktop had Rosetta enabled. The wrk2 generator ran as linux/arm64. The Mac result has a notably higher p99.9 tail than the Windows run; keep both sets of numbers when comparing machines.

## Interpretation limits

These are single steady-state runs, not repeated statistical samples. They record normal performance only; no chaos fault was active. Keep the two machines' measurements separate because their CPU architectures differ. The earlier 5 RPS run is excluded: its LuaSocket dependency was missing, and its connection/timeout settings produced timeouts.
