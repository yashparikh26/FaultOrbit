# DeathStarBench Hotel Reservation dependency findings

Inspected 10 October 2026.

## Scope and evidence

Repository: delimitrou/DeathStarBench, directory `hotelReservation/`.
Inspected master at commit `6ecb09706140f8730b5385c08f1386c654c3c526`.
This report verifies source and configuration; containers were not launched, and no end-to-end runtime success is claimed.

Primary evidence: [Compose](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/docker-compose.yml), [Dockerfile](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/Dockerfile), [Go module](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/go.mod), [configuration](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/config.json), [Makefile](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/Makefile), service entrypoints, database initializers, and server implementations.

## Deployment inventory

Compose defines **24 services**: 10 application services, 8 MongoDB instances, 4 Memcached instances, Consul, and Jaeger.

| Application | Port | MongoDB instance | Database / collections | Memcached instance |
|---|---:|---|---|---|
| frontend | 5000 | None | — | None |
| search | 8082 | None | — | None |
| geo | 8083 | mongodb-geo | geo-db / geo | None |
| rate | 8084 | mongodb-rate | rate-db / inventory | memcached-rate |
| profile | 8081 | mongodb-profile | profile-db / hotels | memcached-profile |
| recommendation | 8085 | mongodb-recommendation | recommendation-db / recommendation | None |
| reservation | 8087 | mongodb-reservation | reservation-db / reservation, number | memcached-reserve |
| user | 8086 | mongodb-user | user-db / user | None |
| review | 8088 | mongodb-review | review-db / reviews | memcached-review |
| attractions | 8089 | mongodb-attractions | attractions-db / hotels, restaurants, museums, cinemas | None |

MongoDB endpoints use port 27017; Memcached endpoints use 11211. Only frontend's application port is published to the host (5000:5000).

All MongoDB containers use `mongo:5.0`, with separate named volumes mounted at `/data/db`: geo, profile, rate, recommendation, reservation, user, review, attractions.
Memcached uses `memcached:latest` (review uses the untagged `memcached` image).
Consul uses `hashicorp/consul:latest`; Jaeger uses `jaegertracing/all-in-one:latest`.

There is no Redis, SQL database, message broker, or separate API-gateway service in this Compose deployment.

## Application call graph

| Caller | Direct downstream application services |
|---|---|
| frontend | search, profile, recommendation, reservation, user, review, attractions |
| search | geo, rate |
| All other application services | None |

| Frontend endpoint | Calls in order |
|---|---|
| /hotels | search.Nearby → reservation.CheckAvailability → profile.GetProfiles |
| Inside search.Nearby | geo.Nearby → rate.GetRates |
| /recommendations | recommendation.GetRecommendations → profile.GetProfiles |
| /user | user.CheckUser |
| /reservation | user.CheckUser → reservation.MakeReservation |
| /review | user.CheckUser → review.GetReviews |
| /restaurants | user.CheckUser → attractions.NearbyRest |
| /museums | user.CheckUser → attractions.NearbyMus |
| /cinema | user.CheckUser → attractions.NearbyCinema |
| / | Embedded static frontend files |

These are implemented calls, not a claim that every path works in the supplied deployment. Review and attractions have a discovery defect described below.

Search does not call reservation or profile: frontend makes those calls. Recommendation evaluates locally loaded data rather than calling geo, rate, or review. Attractions uses its own geographical indexes rather than calling geo.

The reservation/review/attractions frontend handlers continue to the next RPC when CheckUser returns Correct=false; they change a message but do not return at that point.

Evidence: [frontend/server.go](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/frontend/server.go), [search/server.go](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/search/server.go).

## Backing-store behavior

- Geo loads MongoDB coordinates into an in-process geographical index.
- Recommendation and user load MongoDB data into in-process maps.
- Rate, profile, and review use Memcached and query MongoDB for missing data.
- Reservation persists reservations and room capacities in MongoDB, and caches counts/capacities in Memcached. This identifies dependencies, not a correctness guarantee for its cache implementation.
- Attractions builds geographical indexes from its own database and queries the hotels collection during requests. No active Memcached client is created.
- Attractions reads a cinemas collection, but its database initializer seeds only hotels, restaurants, and museums.

The application database initializers insert benchmark seed data on each startup without an existing-data guard. Restarts against persistent volumes can append duplicate seed records.

## Consul registration and discovery

The configured Consul address is `consul:8500`. Each of the nine backend services registers as `srv-<service>`, using a generated UUID, IP address, and configured port. Missing IP values are resolved to a local non-loopback IPv4 address. Frontend creates a Consul client but does not register itself. Registration does not attach health checks.

Normal frontend calls and search's downstream calls use targets such as:

```text
consul://consul:8500/srv-search
consul://consul:8500/srv-geo
consul://consul:8500/srv-rate
```

The imported grpc-consul-resolver resolves these targets. The dialer's WithBalancer option configures gRPC round_robin; it does not itself query Consul. Database/cache clients connect to explicit config.json hostnames through container networking, not through Consul.

**Discovery defect:** frontend's review and attractions clients dial bare `srv-review` and `srv-attractions`, without the Consul scheme or a port. The dialer does not rewrite those targets. Compose provides no corresponding aliases. These two paths are therefore not correctly wired for discovery in the supplied configuration.

Evidence: [registry.go](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/registry/registry.go), [dialer.go](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/dialer/dialer.go), [frontend/server.go](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/frontend/server.go).

## Environment variables

Compose lists application variables without values, forwarding them from the host when available.

| Variable | Default when absent | Code behavior | Compose forwarding |
|---|---|---|---|
| TLS | Disabled | true/1 enables; false/0 disables; recognized cipher-suite names also enable | All 10 applications |
| GC | 100 | Sets Go debug.SetGCPercent | All 10 |
| JAEGER_SAMPLE_RATIO | 0.01 | Probabilistic trace sampling; values above 1 capped at 1 | All 10 |
| LOG_LEVEL | info | Supports error, warning, debug, info, trace in implemented upper/lowercase forms; explicit empty string selects error | All 10 |
| MEMC_TIMEOUT | 2 seconds | Timeout of Memcached client; helper also sets MaxIdleConns=512 | Only review and attractions |

Rate, profile, reservation, and review call the Memcached helper, but Compose forwards MEMC_TIMEOUT only to review and attractions. Attractions has no active cache client, so forwarding it there has no effect.

Additional observations:

- Jaeger initialization calls FromEnv(), allowing Jaeger's own variables to override initial settings if supplied inside the container.
- Registry code reads DSB_GRPC_NETWORK when choosing among multiple local IP addresses. Compose does not forward it. Comments/logs refer to DSB_HOTELRESERV_GRPC_NETWORK, but that is not the executable lookup name.
- Memcached containers declare MEMCACHED_CACHE_SIZE=128 and MEMCACHED_THREADS=2. There is no repository-supplied command or wrapper translating these to daemon flags, so the declarations alone do not verify effective daemon settings.

Evidence: [tune/setting.go](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/tune/setting.go), [tls/options.go](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/tls/options.go), [tracing/tracer.go](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/tracing/tracer.go), [Compose](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/docker-compose.yml).

## config.json

All values in the checked-in file are strings:

```json
{
  "consulAddress": "consul:8500",
  "jaegerAddress": "jaeger:6831",
  "FrontendPort": "5000",
  "GeoPort": "8083",
  "GeoMongoAddress": "mongodb-geo:27017",
  "ProfilePort": "8081",
  "ProfileMongoAddress": "mongodb-profile:27017",
  "ProfileMemcAddress": "memcached-profile:11211",
  "ReviewPort": "8088",
  "ReviewMongoAddress": "mongodb-review:27017",
  "ReviewMemcAddress": "memcached-review:11211",
  "AttractionsPort": "8089",
  "AttractionsMongoAddress": "mongodb-attractions:27017",
  "RatePort": "8084",
  "RateMongoAddress": "mongodb-rate:27017",
  "RateMemcAddress": "memcached-rate:11211",
  "RecommendPort": "8085",
  "RecommendMongoAddress": "mongodb-recommendation:27017",
  "ReservePort": "8087",
  "ReserveMongoAddress": "mongodb-reservation:27017",
  "ReserveMemcAddress": "memcached-reserve:11211",
  "SearchPort": "8082",
  "UserPort": "8086",
  "UserMongoAddress": "mongodb-user:27017",
  "KnativeDomainName": ""
}
```

The entrypoints also read optional service IP keys, such as GeoIP, ProfileIP, RecommendIP, and ReserveIP. These are absent from the checked-in configuration; backend registration therefore selects a local IP. KnativeDomainName is empty for this configuration.

**Config-path mismatch:** Docker sets WORKDIR=/workspace and copies config.json there. Entrypoints open relative "config.json", so a locally built image reads /workspace/config.json. Eight applications mount Compose server_config at /config.json, a different path; review and attractions have no config mount. Editing the host config without rebuilding does not change the file read by these binaries. Mounting at /workspace/config.json consistently, or changing the code's path consistently, would address this mismatch.

## Build and run requirements

The Dockerfile uses golang:1.21; go.mod declares Go 1.18. It copies cmd/, services/, dialer/, registry/, tls/, tracing/, tune/, vendor/, go.mod, go.sum, and config.json. It builds all binaries with:

```bash
CGO_ENABLED=0 GOOS=linux GO111MODULE=on go install -ldflags="-s -w" -mod=vendor ./cmd/...
```

Compose selects a binary using entrypoint, such as frontend or search. Eight applications use deathstarbench/hotel-reservation:latest; review and attractions use hotel_reserv_review_single_node and hotel_reserv_attractions_single_node. All build from the same directory.

| Purpose | Main dependency |
|---|---|
| RPC / serialization | gRPC v1.56.3; Protobuf v1.31.0 |
| MongoDB client | mongo-driver v1.12.2 |
| Memcached client | bradfitz/gomemcache |
| Discovery | Consul API v1.26.1; grpc-consul-resolver v1.5.3 |
| Geography | hailocab/go-geoindex |
| Tracing | OpenTracing; Jaeger client v2.30.0 |
| Logging | Zerolog v1.31.0 |

Dependencies are recorded in go.mod/go.sum and vendored. No Maven or npm build step is used for these service binaries.

The Makefile's run target performs:

```bash
cd hotelReservation
docker-compose build
docker-compose up --remove-orphans
```

Docker and Compose are required on the host; the image provides the Go build environment. These commands describe the repository workflow and were not executed during this inspection.

Jaeger receives application traces at jaeger:6831 and publishes its UI on 16686. Compose also publishes Consul ports 8300, 8400, 8500 and host UDP 8600 mapped to container UDP 53, exactly as written.

## Host CPU architecture

The prebuilt `deathstarbench/hotel-reservation` image is linux/amd64 only (see the README). It runs natively on Intel/AMD hosts and under amd64 emulation (Rosetta or Colima) on Apple Silicon. The source Dockerfile sets GOOS=linux but does not hard-code GOARCH. Record whether a run uses native arm64 or emulated amd64; an emulator's architecture is not the physical chip.

To check a host:

- **macOS:** Apple menu → About This Mac → **Chip** or **Processor**. An Apple M-series chip means Apple Silicon / arm64; an Intel processor means amd64. A terminal running under Rosetta can report x86_64 on Apple Silicon.
- **Linux:** Architecture and Model name from `lscpu`; x86_64 maps to amd64, aarch64 to arm64.
- **Windows:** processor and System type from Settings → System → About.
- **Docker:** `docker info --format '{{.Architecture}}'` reports the daemon's architecture; a remote Docker context describes the daemon host, not the laptop.

Tested hosts: Windows 11 on Intel (amd64, native) and macOS on Apple M2 and M5 (amd64 emulation).

## Runtime caveats and suggested corrections

| Finding | Consequence | Suggested correction |
|---|---|---|
| Config mounted at /config.json but read at /workspace/config.json | Host config changes are not consumed by the locally built image without rebuilding | Align the mount/read path across all ten applications |
| Bare review/attractions RPC targets | Those frontend paths do not use the configured Consul resolver | Use the same Consul target construction as other frontend clients |
| TLS certificate files are neither copied nor mounted | Enabling TLS on the supplied build requires additional files; TLS initialization can fail | Supply x509/ca_cert.pem, server_cert.pem, and server_key.pem at the expected paths |
| MEMC_TIMEOUT forwarding is incomplete | Host timeout overrides miss rate, profile, and reservation | Forward it to all four actual cache clients |
| No Compose health checks/readiness gates | Startup ordering does not establish application readiness | Add appropriate checks/readiness handling |
| Startup seeding has no existing-data guard | Persistent volumes may accumulate duplicate benchmark records | Make seeding idempotent or explicitly control benchmark resets |
| cinemas is read but not seeded | Fresh benchmark data does not populate that attraction category | Add cinema seed data if that endpoint is required |

Frontend and search declare only Consul in depends_on despite downstream RPC dependencies. All services use restart: always. These settings do not establish successful end-to-end operation.

## Per-service source references

- **frontend:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/frontend/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/frontend/server.go).
- **search:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/search/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/search/server.go).
- **geo:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/geo/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/geo/server.go), [database initializer](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/geo/db.go).
- **rate:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/rate/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/rate/server.go), [database initializer](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/rate/db.go).
- **profile:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/profile/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/profile/server.go), [database initializer](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/profile/db.go).
- **recommendation:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/recommendation/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/recommendation/server.go), [database initializer](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/recommendation/db.go).
- **reservation:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/reservation/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/reservation/server.go), [database initializer](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/reservation/db.go).
- **user:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/user/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/user/server.go), [database initializer](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/user/db.go).
- **review:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/review/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/review/server.go), [database initializer](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/review/db.go).
- **attractions:** [entrypoint](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/attractions/main.go), [server](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/services/attractions/server.go), [database initializer](https://github.com/delimitrou/DeathStarBench/blob/6ecb09706140f8730b5385c08f1386c654c3c526/hotelReservation/cmd/attractions/db.go).
