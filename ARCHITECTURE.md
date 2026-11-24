# Architecture

## 1. Current system (this submission, single process)

Everything runs in one Python process + one SQLite file. The sync job and the API server share the same binary.

```mermaid
flowchart TB
    subgraph external["External (third party)"]
        PORTAL["Urja Meter Ops portal<br/>(SvelteKit, session cookie,<br/>~120 req/min, HMAC-signed export)"]
    end

    subgraph app["This service (single process)"]
        subgraph sync["Sync path (every 2h, jittered; or admin POST /v1/sync; or CLI)"]
            CLIENT["PortalClient<br/>login + re-login on 401<br/>5xx/429 backoff + jitter<br/>self-throttle 100 req/min"]
            NORM["normalizer<br/>IST->UTC, str->float<br/>empty-code repair, majority-vote tree<br/>-> data_quality_issues"]
            CLIENT --> NORM
        end
        subgraph serve["Serving path"]
            AUTH["auth middleware<br/>hashed API keys, reader/admin roles<br/>per-key token-bucket rate limit"]
            EP["endpoints /v1/*<br/>meters, readings, hierarchy,<br/>nearby, data-quality, sync status"]
            ETAG["ETag middleware<br/>tag = f(last_ok_sync, url) -> 304"]
            UI["static explorer<br/>Leaflet map + chart"]
            AUTH --> EP --> ETAG
        end
        DB[("SQLite (WAL)<br/>hierarchy_nodes -> transformers<br/>-> meters -> readings<br/>sync_runs, api_keys, data_quality")]
    end

    CONSUMER["API consumer<br/>(Postman / curl / service)"] --> AUTH
    BROWSER["Operator's browser"] --> UI
    UI --> AUTH
    PORTAL <--> CLIENT
    NORM --> DB
    EP --> DB
```

Design properties:
- **Pull model**: portal is authoritative and read-only; everything flows portal -> DB -> consumers. Consumers never touch the portal.
- **Freshness**: data is as old as `SYNC_INTERVAL_HOURS` at worst; surfaced via `synced_at`, `/health`, `/v1/sync/status`. ETags change exactly when a sync lands - no cache to purge.
- **Failure behavior**: sync failure keeps old data (availability over freshness); per-meter failures are recorded, not fatal; overlapping syncs blocked by a lock.
- **Single point of everything**: one process, one file DB, in-memory rate limiter - deliberate for this scale (403 meters / 16k readings), and each breaking point is documented below.

## 2. Production version (if this became a real product)

Same data flow, horizontally scaled. What changes and why is in the README "Future scope" - this is that list drawn.

```mermaid
flowchart TB
    subgraph external["External"]
        PORTAL["Urja portal(s)<br/>(still read-only)"]
    end

    subgraph edge["Edge"]
        CDN["CDN<br/>(static UI assets)"]
        LB["Load balancer<br/>nginx/Traefik (least-conn)"]
        WAF["Rate limit / abuse rules<br/>(IP ban lists, anomaly detection)"]
    end

    subgraph cluster["Stateless API tier (N containers, docker/k8s)"]
        A1["api-1"]
        A2["api-2"]
        AN["api-N"]
        AUTH2["auth: hashed keys + JWT<br/>RBAC: per-zone/dataset permissions<br/>key mgmt endpoints + rotation + audit log"]
        RL["rate limit<br/>(Redis token bucket, shared)"]
        APIE["endpoints + ETag/data_version"]
        A1 & A2 & AN --> AUTH2 --> RL --> APIE
    end

    subgraph data["Data tier"]
        PG[("Postgres + PostGIS<br/>partitioned readings (TimescaleDB)<br/>read replicas for analytics")]
        REDIS[("Redis<br/>rate-limit counters, response cache<br/>keyed by data_version; pub/sub")]
        S3[("Object storage<br/>(exports, photos if they appear)")]
    end

    subgraph workers["Ingestion tier (single writer)"]
        CRON["K8s CronJob / Celery beat<br/>hourly, jittered"]
        SYNC2["sync worker (only 1)<br/>portal client + normalizer<br/>incremental readings by max(ts)<br/>row-hash diff -> change_log table"]
        CRON --> SYNC2
    end

    subgraph live["Push path"]
        SSE["SSE/WebSocket gateway<br/>'dataset updated' event per sync<br/>(clients refetch; ETag/304 if nothing changed)"]
    end

    subgraph obs["Observability"]
        PROM["Prometheus + Grafana<br/>sync lag, 4xx/5xx, portal latency"]
        LOGS["Central logs + alerts<br/>(sync failed, dq spike, 429 storm)"]
    end

    subgraph ai["Optional paid tier"]
        LLM["LLM / MCP data agent<br/>tools = same RBAC-scoped endpoints<br/>per-key quota + metering for billing"]
    end

    CONSUMER2["Consumers"] --> WAF --> LB
    BROWSER2["Web users"] --> CDN --> WAF --> LB
    LLM --> LB
    LB --> cluster
    APIE --> PG
    APIE <--> REDIS
    UI2["Web app"] -.-> SSE
    PORTAL <--> SYNC2
    SYNC2 --> PG
    SYNC2 -->|"data_version++"| REDIS
    REDIS -->|"pub/sub"| SSE
    cluster & workers --> PROM & LOGS
```

What each addition buys:
- **Postgres + partitioning** - survives concurrent writers + tens of millions of readings; PostGIS replaces the bounding-box/haversine scan.
- **Redis** - the rate limiter and the data_version counter must be shared once there's more than one API process; pub/sub turns "sync done" into a push event.
- **SSE/WebSocket** - solves "user looking at stale data": after each successful sync the worker bumps `data_version`, subscribers are told to refetch.
- **change_log** - sync diffs rows by hash so only real changes write + can be pushed/audited.
- **Single sync worker** - mirrors are cheap to read, expensive to write; keep one writer, scale readers.
- **WAF/IP rules** - the abuse detection that was consciously skipped: ban IPs with scraping-shaped traffic.
- **LLM tier** - paid natural-language access on top of the same endpoints; RBAC keeps it inside each customer's permissions; metering makes it billable.
