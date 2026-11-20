"""Turn the portal's messy payloads into clean rows + a data-quality report."""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

LEVELS = ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]
IST = timezone(timedelta(hours=5, minutes=30))
_PREFIX = {"zone": "Z", "circle": "C", "division": "D", "subdivision": "SD", "substation": "SS", "feeder": "F"}


def parse_ts(s: str) -> str:
    """'23/06/2026 23:30' (dd/mm/yyyy, assumed IST) -> UTC ISO-8601."""
    return datetime.strptime(s, "%d/%m/%Y %H:%M").replace(tzinfo=IST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_readings(raw: list[dict]) -> list[dict]:
    rows = {}
    for r in raw:
        try:
            ts = parse_ts(r["timestamp"])
        except (KeyError, ValueError):
            continue
        rows[ts] = {"ts": ts, "kwh": _num(r.get("kwh")), "kvah": _num(r.get("kvah")), "volt_r": _num(r.get("voltR"))}
    return [rows[k] for k in sorted(rows)]


def _repair_code(level: str, node: dict, issues: list) -> str:
    """Empty codes occur in the export; the name ('Circle 6') still carries the number, so rebuild 'C-06'."""
    code = (node.get("code") or "").strip()
    if code:
        return code
    m = re.search(r"(\d+)\s*$", node.get("name") or "")
    if level in _PREFIX and m:
        return f"{_PREFIX[level]}-{int(m.group(1)):0{3 if level == 'feeder' else 2}d}"
    return ""


def normalize_export(meters: list[dict]):
    """Returns (meter_rows, nodes, issues).

    The per-meter hierarchy paths are not consistent with each other (a node can show up under several parents),
    so the tree is rebuilt by majority vote on each node's parent; disagreements are reported, not hidden.
    """
    issues: list[dict] = []
    meter_rows, parent_votes, names = [], defaultdict(Counter), {}
    for m in meters:
        h, prev = m.get("hierarchy") or {}, None
        path = {}
        for lv in LEVELS:
            node = h.get(lv) or {}
            code = _repair_code(lv, node, issues)
            if not (node.get("code") or "").strip():
                issues.append({"kind": "empty_code_repaired" if code else "empty_code", "entity": m["meterId"], "detail": f"{lv} name={node.get('name')!r} -> {code!r}"})
            path[lv] = code
            if code:
                names.setdefault((lv, code), node.get("name"))
                if prev:
                    parent_votes[(lv, code)][prev] += 1
                prev = code
        if path["dt"] and path["dt"] != m.get("dtCode"):
            issues.append({"kind": "dt_mismatch", "entity": m["meterId"], "detail": f"dtCode={m.get('dtCode')} hierarchy.dt={path['dt']}"})
        geo = m.get("geo") or {}
        meter_rows.append({
            "meter_id": m["meterId"], "serial_no": m.get("serialNo"), "make": m.get("make"),
            "phase_type": m.get("phaseType"), "install_status": m.get("installStatus"),
            "install_type": m.get("installType"), "build": m.get("build"),
            "dt_code": m.get("dtCode") or path["dt"],
            "lat": _num(geo.get("lat")), "lng": _num(geo.get("lng")),
            "path": path,
        })
    nodes = []
    for (lv, code), name in names.items():
        votes = parent_votes.get((lv, code))
        parent = votes.most_common(1)[0][0] if votes else None
        if votes and len(votes) > 1:
            issues.append({"kind": "multiple_parents", "entity": f"{lv}:{code}", "detail": f"parents={dict(votes)} -> chose {parent}"})
        nodes.append({"id": f"{lv}:{code}", "level": lv, "code": code, "name": name, "parent_id": f"{LEVELS[LEVELS.index(lv)-1]}:{parent}" if parent else None})
    return meter_rows, nodes, issues
