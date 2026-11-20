# Urja Meter API

A clean, documented, authenticated REST API in front of the legacy **Urja Meter Ops** portal.
A background job mirrors the portal into SQLite; consumers talk only to this API.

- How the portal works (my investigation): **[PROTOCOL.md](PROTOCOL.md)**
- OpenAPI 3 spec: **[openapi.json](openapi.json)** (also live at `/docs` and `/redoc` when running)
- Reflection answers: **[REFLECTION.md](REFLECTION.md)**

## What I built
```
urja_api/
  portal.py     portal client: login, signed bulk export, 401 re-login, 5xx/network backoff, 429 handling, self-throttle
  normalize.py  messy portal payloads -> clean rows (UTC timestamps, floats, hierarchy repair) + data-quality report
  sync.py       idempotent upsert into SQLite (CLI: python -m urja_api.sync); overlap-safe
  db.py         schema (PKs, FKs, indexes)
  auth.py       API keys (SHA-256 hashed), roles reader/admin, per-key token-bucket rate limit
  api.py        FastAPI app, ETag freshness, error format, 2-hourly scheduler
static/         single-page explorer: map of meters coloured by status, per-meter consumption chart, data-quality summary
tests/          19 tests (portal client against a fake portal, normalisation, API/auth/RBAC/ETag/rate-limit)
```
Schema: `hierarchy_nodes(id PK, level, code, parent_id FK->self)`, `transformers(code PK, node_id FK)`,
`meters(meter_id PK, dt_node_id FK)`, `readings(meter_id FK, ts) PK`, `sync_runs`, `data_quality_issues`, `api_keys`.

## Run it
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env && set -a && . ./.env && set +a      # portal creds + API keys
python -m urja_api.sync            # first full sync (~5 min: portal allows ~120 req/min). Add --no-readings for a 5s inventory-only sync
python -m urja_api                 # http://localhost:8000  (UI at /, docs at /docs)
pytest                             # tests (no network needed)
python scripts/export_openapi.py   # regenerate openapi.json   (PYTHONPATH=. if needed)
```
Docker: `docker build -t urja-api . && docker run -p 8000:8000 --env-file .env -v $PWD/data:/data -e DB_PATH=/data/urja.db urja-api`.
The server also syncs on start if the DB is empty and then every `SYNC_INTERVAL_HOURS` (+-5% jitter). Admins can `POST /v1/sync`.

### Sample requests
```bash
curl -H 'X-API-Key: dev-reader-key' 'localhost:8000/v1/meters?install_status=Faulty&limit=2'
curl -H 'X-API-Key: dev-reader-key' 'localhost:8000/v1/meters/J100000'                       # includes full zone->...->dt path
curl -H 'X-API-Key: dev-reader-key' 'localhost:8000/v1/meters/J100000/readings?from=2026-06-25T00:00:00Z&limit=3'
curl -H 'X-API-Key: dev-reader-key' 'localhost:8000/v1/meters/nearby?lat=26.94&lon=75.83&radius_km=1'
curl -H 'X-API-Key: dev-reader-key' 'localhost:8000/v1/meters?node=circle:C-01'              # everything under a hierarchy node
curl -H 'X-API-Key: dev-reader-key' 'localhost:8000/v1/hierarchy?depth=3'
curl -H 'X-API-Key: dev-reader-key' 'localhost:8000/v1/data-quality'
curl -X POST -H 'X-API-Key: dev-admin-key' localhost:8000/v1/sync
```
Errors are always `{"error":{"code","message"}}`. Lists are `{items,total,limit,offset}`.

## Assumptions
- Portal timestamps (`dd/mm/yyyy HH:MM`, no zone) are **IST (UTC+05:30)**: Jaipur utility. API returns UTC ISO-8601.
- `kwh`/`kvah` are cumulative register values, so they're exposed as-is (the UI diffs them).
- The portal export is the source of truth: meters that disappear from it are deleted locally on the next sync.
- Hierarchy: every meter's own path is untrustworthy; the majority parent per node wins (ties: first seen).
- The default bootstrap API keys are for local dev only; set real ones via env.

## Design decisions & trade-offs
- **Mirror to SQLite instead of proxying live.** The portal is slow, session-based, rate-limited (429 at ~120 req/min) and expires sessions hourly. A mirror gives fast reads, filtering the portal can't do (attributes, subtree, nearby) and isolates consumers from outages. Cost: data is up to `SYNC_INTERVAL_HOURS` stale, which is surfaced via `synced_at`, `/v1/sync/status`, `/health`.
- **SQL over Elasticsearch/NoSQL.** The data is relational (tree + meters + time series), the filters are structured, and a recursive CTE walks the hierarchy. ES only pays off for fuzzy full-text at much larger scale; document stores would force me to re-implement joins.
- **Use the signed bulk export** instead of crawling search+geo (1 call vs ~420); readings still need one call per meter.
- **Freshness / caching:** no server-side cache (SQLite is already sub-millisecond at this size). Instead every `/v1` GET carries an `ETag` derived from the last successful sync id, so clients revalidate with `If-None-Match` and get `304`; the ETag changes the moment a new sync lands, so there is nothing to purge. `Cache-Control: private, max-age=60` bounds staleness for browsers.
- **Robustness:** proactive re-login before the 1h cookie expiry plus reactive re-login on 401, exponential backoff with jitter on 5xx/network, `429` wait-and-retry, self-throttle at 100 req/min, per-meter failure isolation (reported in data-quality, doesn't abort the sync), per-meter transactions, overlap lock, failed syncs recorded and old data kept.
- **Auth:** API keys stored hashed, two roles. Simple on purpose.
- **Rate limit:** in-memory token bucket per key (single process). Fine for one instance; multi-instance needs Redis.
- Nearby search = bounding box on an index + haversine. Fine up to ~100k meters; beyond that use SQLite R*Tree/PostGIS.

## Intentionally skipped
GraphQL/gRPC, Redis, CDN, IP rotation/evasion (I have a legitimate login and respect the portal's limit - rotating IPs would be the wrong thing to do), IP banning, sharding, JWT/OAuth, user management endpoints, WebSocket push, DB migrations. They solve problems this 403-meter / 2-hour-freshness system doesn't have. Details below.

## Future scope (ordered by value)
1. Incremental readings sync (only fetch meters whose data changed) and a retention policy; derived interval consumption + anomaly flags (zero-usage, voltage out of band).
2. Postgres (+PostGIS, partitioned `readings`, TimescaleDB if needed) once writes/concurrency or data volume exceed SQLite (roughly >10-50M reading rows or multiple writer processes). Add pgbouncer for connection pooling.
3. Redis for shared rate limiting and a response cache invalidated by the sync version key; Redis pub/sub -> SSE/WebSocket to push "dataset updated" to open dashboards (answers "user sees old price/value": the client gets an event after each sync and refetches; polling `ETag` is the fallback).
4. Real RBAC (per-dataset/per-zone permissions, key management endpoints, key rotation/expiry, audit log); abuse detection (ban IPs with scraping-like access patterns).
5. Scale-out: stateless API containers behind nginx/Traefik (round-robin first, least-connections when request cost varies), single sync worker.
6. GraphQL for flexible client queries and gRPC for service-to-service, only once there is a consumer that needs them.
7. An LLM/MCP layer exposing the same RBAC-scoped endpoints as tools (a natural paid tier). At scale: per-key quotas and metering, read-only DB role, query timeouts, cached tool results.
8. CDN only if static assets/photos appear; they don't in this portal.

## Reflection
See [REFLECTION.md](REFLECTION.md).
