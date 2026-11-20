"""Pull data from the portal into SQLite. Idempotent: safe to re-run at any time."""
import logging
import threading
from datetime import datetime, timezone

from . import db
from .config import Settings
from .normalize import LEVELS, normalize_export, normalize_readings
from .portal import PortalAuthError, PortalClient, PortalError, PortalNotFound

log = logging.getLogger(__name__)
_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_sync(settings: Settings, portal: PortalClient | None = None, include_readings: bool = True) -> dict:
    if not _lock.acquire(blocking=False):
        return {"status": "skipped", "error": "sync already running"}
    own = portal is None
    portal = portal or PortalClient(settings)
    c = db.connect(settings.db_path)
    run_id = c.execute("INSERT INTO sync_runs(started_at,status) VALUES (?, 'running')", (now(),)).lastrowid
    c.commit()
    try:
        n_meters, issues = _sync_inventory(c, portal)
        n_read = _sync_readings(c, portal, issues) if include_readings else 0
        with db.tx(c):
            c.execute("DELETE FROM data_quality_issues")
            c.executemany("INSERT INTO data_quality_issues(sync_run_id,kind,entity,detail) VALUES (?,?,?,?)",
                          [(run_id, i["kind"], i["entity"], i["detail"]) for i in issues])
            c.execute("UPDATE sync_runs SET status='ok', finished_at=?, meters=?, readings=? WHERE id=?", (now(), n_meters, n_read, run_id))
        log.info("sync ok: %d meters, %d readings, %d issues", n_meters, n_read, len(issues))
        return {"status": "ok", "run_id": run_id, "meters": n_meters, "readings": n_read, "issues": len(issues)}
    except Exception as e:
        log.exception("sync failed")
        c.execute("UPDATE sync_runs SET status='failed', finished_at=?, error=? WHERE id=?", (now(), str(e)[:500], run_id))
        c.commit()
        return {"status": "failed", "run_id": run_id, "error": str(e)}
    finally:
        c.close()
        if own:
            portal.close()
        _lock.release()


def _sync_inventory(c, portal: PortalClient):
    meters, nodes, issues = normalize_export(portal.export())
    dts = portal.dts()
    nodes_by_id = {n["id"]: n for n in nodes}
    for d in dts:
        nid = f"dt:{d['code']}"
        feeder = f"feeder:{d['feederCode']}"
        n = nodes_by_id.get(nid)
        if n is None:
            nodes_by_id[nid] = {"id": nid, "level": "dt", "code": d["code"], "name": d["name"], "parent_id": feeder if feeder in nodes_by_id else None}
            issues.append({"kind": "dt_without_meters", "entity": d["code"], "detail": "listed in /portal/dts, absent from export"})
            continue
        if n["name"] != d["name"]:
            issues.append({"kind": "dt_name_mismatch", "entity": d["code"], "detail": f"export={n['name']!r} dts={d['name']!r} -> using dts"})
            n["name"] = d["name"]
        if n["parent_id"] != feeder:
            issues.append({"kind": "dt_feeder_mismatch", "entity": d["code"], "detail": f"hierarchy={n['parent_id']} dts={feeder}"})
    ts = now()
    with db.tx(c):
        for n in sorted(nodes_by_id.values(), key=lambda n: LEVELS.index(n["level"])):
            c.execute("""INSERT INTO hierarchy_nodes(id,level,code,name,parent_id) VALUES (:id,:level,:code,:name,:parent_id)
                         ON CONFLICT(id) DO UPDATE SET name=excluded.name, parent_id=excluded.parent_id""", n)
        c.executemany("""INSERT INTO transformers(code,node_id,name,feeder_code,capacity_kva) VALUES (?,?,?,?,?)
                         ON CONFLICT(code) DO UPDATE SET name=excluded.name, feeder_code=excluded.feeder_code, capacity_kva=excluded.capacity_kva""",
                      [(d["code"], f"dt:{d['code']}", d["name"], d["feederCode"], d["capacityKva"]) for d in dts])
        for m in meters:
            m["dt_node_id"] = f"dt:{m['dt_code']}"
            m["synced_at"] = ts
            c.execute("""INSERT INTO meters(meter_id,serial_no,make,phase_type,install_status,install_type,build,dt_node_id,lat,lng,synced_at)
                         VALUES (:meter_id,:serial_no,:make,:phase_type,:install_status,:install_type,:build,:dt_node_id,:lat,:lng,:synced_at)
                         ON CONFLICT(meter_id) DO UPDATE SET serial_no=excluded.serial_no, make=excluded.make, phase_type=excluded.phase_type,
                           install_status=excluded.install_status, install_type=excluded.install_type, build=excluded.build,
                           dt_node_id=excluded.dt_node_id, lat=excluded.lat, lng=excluded.lng, synced_at=excluded.synced_at""", m)
        ids = [m["meter_id"] for m in meters]
        c.execute(f"DELETE FROM meters WHERE meter_id NOT IN ({','.join('?'*len(ids))})", ids)
    return len(meters), issues


def _sync_readings(c, portal: PortalClient, issues: list) -> int:
    total = 0
    for (mid,) in c.execute("SELECT meter_id FROM meters ORDER BY meter_id").fetchall():
        try:
            rows = normalize_readings(portal.energy(mid))
        except PortalAuthError:
            raise
        except (PortalNotFound, PortalError) as e:
            issues.append({"kind": "readings_fetch_failed", "entity": mid, "detail": str(e)[:200]})
            continue
        with db.tx(c):
            c.executemany("""INSERT INTO readings(meter_id,ts,kwh,kvah,volt_r) VALUES (?,?,?,?,?)
                             ON CONFLICT(meter_id,ts) DO UPDATE SET kwh=excluded.kwh, kvah=excluded.kvah, volt_r=excluded.volt_r""",
                          [(mid, r["ts"], r["kwh"], r["kvah"], r["volt_r"]) for r in rows])
        total += len(rows)
    return total


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Sync the Urja portal into the local DB")
    ap.add_argument("--no-readings", action="store_true", help="inventory only (fast)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = Settings()
    db.init(s.db_path)
    print(json.dumps(run_sync(s, include_readings=not args.no_readings)))
