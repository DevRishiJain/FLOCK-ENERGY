import asyncio
import hashlib
import logging
import math
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, db
from .config import Settings
from .sync import run_sync

log = logging.getLogger("urja_api")
STATIC = Path(__file__).resolve().parent.parent / "static"
LEVELS = ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]

DESCRIPTION = """
Clean read API over the legacy **Urja Meter Ops** portal. Data is synced into a local SQLite index
(see `/v1/sync/status` for freshness) so consumers never touch the portal.

Auth: send `X-API-Key`. Roles: `reader` (all GETs) and `admin` (also `POST /v1/sync`).
Responses carry an `ETag` that changes only when a sync completes; send `If-None-Match` to get `304`.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init(s.db_path)
        auth.seed_keys(s)
        tasks = []
        if s.sync_interval_hours > 0:
            tasks.append(asyncio.create_task(_scheduler(s)))
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="Urja Meter API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
    app.state.settings = s
    app.state.limiter = auth.RateLimiter(s.rate_limit_per_min)
    _conns = {}

    def get_db() -> sqlite3.Connection:
        import threading
        tid = threading.get_ident()
        if tid not in _conns:
            _conns[tid] = db.connect(s.db_path)
        return _conns[tid]

    app.state.db = get_db

    def D() -> sqlite3.Connection:
        return get_db()

    # ---- errors: one consistent shape
    def err(status, code, msg, headers=None):
        return JSONResponse({"error": {"code": code, "message": msg}}, status_code=status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_, e):
        return err(e.status_code, {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "rate_limited"}.get(e.status_code, "error"), str(e.detail), getattr(e, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _val(_, e):
        return err(422, "validation_error", "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))

    # ---- freshness: ETag = sync version + URL; invalidated automatically when a new sync lands
    @app.middleware("http")
    async def etag(request: Request, call_next):
        if request.method != "GET" or not request.url.path.startswith("/v1/") or request.url.path == "/v1/sync/status":
            return await call_next(request)
        row = await asyncio.to_thread(lambda: get_db().execute("SELECT MAX(id) v FROM sync_runs WHERE status='ok'").fetchone())
        tag = 'W/"%s"' % hashlib.sha1(f"{row['v']}|{request.url.path}|{request.url.query}".encode()).hexdigest()[:20]
        if request.headers.get("if-none-match") == tag and request.headers.get("x-api-key"):
            return Response(status_code=304, headers={"ETag": tag})
        resp = await call_next(request)
        if resp.status_code == 200:
            resp.headers["ETag"] = tag
            resp.headers["Cache-Control"] = "private, max-age=60"
        return resp

    @app.get("/health", tags=["system"], summary="Liveness + data freshness (no auth)")
    def health():
        r = D().execute("SELECT id, finished_at FROM sync_runs WHERE status='ok' ORDER BY id DESC LIMIT 1").fetchone()
        return {"status": "ok", "last_sync_at": r["finished_at"] if r else None}

    # ---- meters
    MCOLS = "m.meter_id, m.serial_no, m.make, m.phase_type, m.install_status, m.install_type, m.build, n.code AS dt_code, m.lat, m.lng, m.synced_at"
    MFROM = "FROM meters m JOIN hierarchy_nodes n ON n.id = m.dt_node_id"

    def meter_dict(r):
        d = dict(r)
        d["location"] = {"lat": d.pop("lat"), "lng": d.pop("lng")}
        return d

    @app.get("/v1/meters", tags=["meters"], summary="List/filter meters", dependencies=[Depends(auth.reader)])
    def list_meters(q: str | None = Query(None, description="substring of meter id or serial"),
                    make: str | None = None, install_status: str | None = None, phase_type: str | None = None,
                    install_type: str | None = None, build: str | None = None, dt_code: str | None = None,
                    node: str | None = Query(None, description="hierarchy node id (e.g. `circle:C-01`); returns all meters beneath it"),
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        where, args = [], []
        for col, v in (("m.make", make), ("m.install_status", install_status), ("m.phase_type", phase_type), ("m.install_type", install_type), ("m.build", build), ("n.code", dt_code)):
            if v:
                where.append(f"{col} = ?")
                args.append(v)
        if q:
            where.append("(m.meter_id LIKE ? OR m.serial_no LIKE ?)")
            args += [f"%{q}%"] * 2
        pre = ""
        if node:
            pre = "WITH RECURSIVE sub(id) AS (SELECT id FROM hierarchy_nodes WHERE id=? UNION ALL SELECT h.id FROM hierarchy_nodes h JOIN sub ON h.parent_id=sub.id) "
            where.append("m.dt_node_id IN (SELECT id FROM sub)")
            args = [node] + args
        w = (" WHERE " + " AND ".join(where)) if where else ""
        total = D().execute(f"{pre}SELECT COUNT(*) {MFROM}{w}", args).fetchone()[0]
        rows = D().execute(f"{pre}SELECT {MCOLS} {MFROM}{w} ORDER BY m.meter_id LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return {"items": [meter_dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    @app.get("/v1/meters/nearby", tags=["meters"], summary="Meters within a radius of a point", dependencies=[Depends(auth.reader)])
    def nearby(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
               radius_km: float = Query(2, gt=0, le=200), limit: int = Query(50, ge=1, le=500)):
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
        rows = D().execute(f"SELECT {MCOLS} {MFROM} WHERE m.lat BETWEEN ? AND ? AND m.lng BETWEEN ? AND ?", (lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
        out = []
        for r in rows:
            d = meter_dict(r)
            d["distance_km"] = round(_haversine(lat, lon, d["location"]["lat"], d["location"]["lng"]), 3)
            if d["distance_km"] <= radius_km:
                out.append(d)
        out.sort(key=lambda x: x["distance_km"])
        return {"items": out[:limit], "total": len(out)}

    @app.get("/v1/meters/{meter_id}", tags=["meters"], summary="Meter detail incl. full hierarchy path", dependencies=[Depends(auth.reader)])
    def get_meter(meter_id: str):
        r = D().execute(f"SELECT {MCOLS}, m.dt_node_id {MFROM} WHERE m.meter_id=?", (meter_id,)).fetchone()
        if not r:
            raise HTTPException(404, f"Meter {meter_id} not found")
        d = meter_dict(r)
        d["hierarchy"] = _ancestors(D(), d.pop("dt_node_id"))
        return d

    @app.get("/v1/meters/{meter_id}/readings", tags=["meters"], summary="Consumption readings (cumulative kWh/kVAh registers, UTC)", dependencies=[Depends(auth.reader)])
    def readings(meter_id: str, from_: str | None = Query(None, alias="from", description="ISO-8601 UTC, inclusive"),
                 to: str | None = Query(None, description="ISO-8601 UTC, inclusive"),
                 limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0)):
        if not D().execute("SELECT 1 FROM meters WHERE meter_id=?", (meter_id,)).fetchone():
            raise HTTPException(404, f"Meter {meter_id} not found")
        w, a = ["meter_id=?"], [meter_id]
        if from_:
            w.append("ts >= ?"); a.append(from_)
        if to:
            w.append("ts <= ?"); a.append(to)
        cond = " AND ".join(w)
        total = D().execute(f"SELECT COUNT(*) FROM readings WHERE {cond}", a).fetchone()[0]
        rows = D().execute(f"SELECT ts, kwh, kvah, volt_r FROM readings WHERE {cond} ORDER BY ts LIMIT ? OFFSET ?", a + [limit, offset]).fetchall()
        return {"meter_id": meter_id, "items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    # ---- hierarchy
    @app.get("/v1/hierarchy", tags=["hierarchy"], summary="Network tree (zone -> ... -> transformer) with meter counts", dependencies=[Depends(auth.reader)])
    def tree(depth: int = Query(7, ge=1, le=7, description="levels to include (1 = zones only)")):
        rows = D().execute("""SELECT h.id, h.level, h.code, h.name, h.parent_id,
                              (SELECT COUNT(*) FROM meters m WHERE m.dt_node_id=h.id) AS own FROM hierarchy_nodes h ORDER BY h.code""").fetchall()
        nodes = {r["id"]: {"id": r["id"], "level": r["level"], "code": r["code"], "name": r["name"], "meter_count": r["own"], "children": []} for r in rows}
        roots = []
        for r in rows:
            (nodes[r["parent_id"]]["children"] if r["parent_id"] in nodes else roots).append(nodes[r["id"]])

        def roll(n):
            n["meter_count"] += sum(roll(c) for c in n["children"])
            return n["meter_count"]
        for r_ in roots:
            roll(r_)

        def cut(n, d):
            if d >= depth:
                n["children"] = []
            for c in n["children"]:
                cut(c, d + 1)
        for r_ in roots:
            cut(r_, 1)
        return {"items": roots}

    @app.get("/v1/hierarchy/{level}/{code}", tags=["hierarchy"], summary="One node: ancestors, direct children, meter count", dependencies=[Depends(auth.reader)])
    def node(level: str, code: str):
        n = D().execute("SELECT * FROM hierarchy_nodes WHERE level=? AND code=?", (level, code)).fetchone()
        if not n:
            raise HTTPException(404, f"Node {level}:{code} not found")
        kids = D().execute("""SELECT h.id, h.level, h.code, h.name, (SELECT COUNT(*) FROM meters m WHERE m.dt_node_id=h.id) own
                              FROM hierarchy_nodes h WHERE h.parent_id=? ORDER BY h.code""", (n["id"],)).fetchall()
        cnt = D().execute("""WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL SELECT h.id FROM hierarchy_nodes h JOIN sub ON h.parent_id=sub.id)
                             SELECT COUNT(*) FROM meters WHERE dt_node_id IN (SELECT id FROM sub)""", (n["id"],)).fetchone()[0]
        return {"id": n["id"], "level": n["level"], "code": n["code"], "name": n["name"], "meter_count": cnt,
                "ancestors": _ancestors(D(), n["id"])[:-1], "children": [dict(k) for k in kids]}

    @app.get("/v1/transformers", tags=["hierarchy"], summary="Distribution transformers", dependencies=[Depends(auth.reader)])
    def transformers():
        rows = D().execute("""SELECT t.code, t.name, t.feeder_code, t.capacity_kva, (SELECT COUNT(*) FROM meters m WHERE m.dt_node_id=t.node_id) AS meter_count
                              FROM transformers t ORDER BY t.code""").fetchall()
        return {"items": [dict(r) for r in rows]}

    # ---- ops
    @app.get("/v1/data-quality", tags=["system"], summary="Inconsistencies found in the portal data at last sync", dependencies=[Depends(auth.reader)])
    def dq(kind: str | None = None, limit: int = Query(200, ge=1, le=2000)):
        summary = [dict(r) for r in D().execute("SELECT kind, COUNT(*) AS count FROM data_quality_issues GROUP BY kind ORDER BY count DESC")]
        rows = D().execute("SELECT kind, entity, detail FROM data_quality_issues WHERE (? IS NULL OR kind=?) ORDER BY id LIMIT ?", (kind, kind, limit)).fetchall()
        return {"summary": summary, "items": [dict(r) for r in rows]}

    @app.get("/v1/sync/status", tags=["system"], summary="Sync history and data freshness", dependencies=[Depends(auth.reader)])
    def sync_status():
        rows = D().execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 5").fetchall()
        return {"items": [dict(r) for r in rows]}

    @app.post("/v1/sync", tags=["system"], status_code=202, summary="Trigger a sync now (admin)", dependencies=[Depends(auth.admin)])
    def trigger_sync(bg: BackgroundTasks, readings: bool = True):
        bg.add_task(run_sync, s, None, readings)
        return {"status": "accepted"}

    if STATIC.exists():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app


def _haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def _ancestors(c, node_id):
    rows = c.execute("""WITH RECURSIVE up(id, level, code, name, parent_id) AS (
                          SELECT id, level, code, name, parent_id FROM hierarchy_nodes WHERE id=?
                          UNION ALL SELECT h.id, h.level, h.code, h.name, h.parent_id FROM hierarchy_nodes h JOIN up ON h.id=up.parent_id)
                        SELECT level, code, name FROM up""", (node_id,)).fetchall()
    return [dict(r) for r in reversed(rows)]


async def _scheduler(s: Settings):
    """Hourly-ish refresh. Jittered so the portal never sees a metronome."""
    import random
    await asyncio.sleep(5)
    if s.sync_on_start and db.connect(s.db_path).execute("SELECT COUNT(*) FROM meters").fetchone()[0] == 0:
        await asyncio.to_thread(run_sync, s)
    while True:
        await asyncio.sleep(s.sync_interval_hours * 3600 * random.uniform(0.95, 1.05))
        await asyncio.to_thread(run_sync, s)


app = create_app()
