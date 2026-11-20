import json

import httpx
import pytest

from urja_api.config import Settings
from urja_api.portal import PortalClient


def meter(i, dt="DT-001", build="legacy", **h):
    hier = {
        "zone": {"name": "Jaipur Zone 1", "code": "Z-01"}, "circle": {"name": "Circle 1", "code": "C-01"},
        "division": {"name": "Division 1", "code": "D-01"}, "subdivision": {"name": "Subdivision 1", "code": "SD-01"},
        "substation": {"name": "Substation 1", "code": "SS-01"}, "feeder": {"name": "Feeder 1", "code": "F-001"},
        "dt": {"name": "Malviya Nagar DT 1", "code": dt},
    }
    hier.update(h)
    return {"meterId": f"J{100000+i}", "serialNo": f"SE{i}", "make": "HPL", "phaseType": "single", "installStatus": "Installed",
            "installType": "Whole Current", "build": build, "dtCode": dt, "hierarchy": hier, "geo": {"lat": 26.9 + i * 0.001, "lng": 75.8}}


class FakePortal:
    """Minimal stand-in for the portal; counts logins and can expire the session on demand."""

    def __init__(self, meters):
        self.meters, self.logins, self.valid, self.fail_next = meters, 0, False, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/login":
            ok = b"password=test-pass" in req.content and req.headers.get("origin")
            if ok:
                self.logins += 1
                self.valid = True
                return httpx.Response(200, json={"type": "redirect", "status": 303, "location": "/meters"})
            return httpx.Response(401, json={"type": "failure"})
        if not self.valid:
            return httpx.Response(401, json={"error": "unauthorized"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), text="boom")
        if p == "/portal/keys":
            return httpx.Response(200, json={"data": {"signingSecret": "sek"}})
        if p == "/portal/export":
            from urja_api.portal import sign
            ts, sig = req.headers.get("x-timestamp"), req.headers.get("x-signature")
            if not sig or sig != sign("sek", "GET", "/portal/export", req.url.query.decode(), ts):
                return httpx.Response(401, json={"error": "signature_invalid"})
            return httpx.Response(200, json={"data": self.meters, "total": len(self.meters)})
        if p == "/portal/dts":
            dts = [{"code": "DT-001", "name": "Malviya Nagar DT 1", "feederCode": "F-001", "capacityKva": 100}]
            return httpx.Response(200, json={"data": dts, "total": 1, "page": 1, "pageSize": 20})
        if p.endswith("/energy"):
            return httpx.Response(200, json={"data": [
                {"timestamp": "23/06/2026 23:30", "kwh": "10.5", "kvah": "11", "voltR": "230"},
                {"timestamp": "24/06/2026 00:00", "kwh": "11", "kvah": "11.6", "voltR": "231"}]})
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def settings(tmp_path):
    return Settings(portal_password="test-pass",db_path=str(tmp_path / "t.db"), request_delay_s=0, admin_api_key="adm", reader_api_key="rdr", sync_interval_hours=0, rate_limit_per_min=1000)


@pytest.fixture
def fake():
    return FakePortal([meter(1), meter(2, build="v2", circle={"name": "Circle 6", "code": ""})])


@pytest.fixture
def portal(settings, fake):
    return PortalClient(settings, transport=httpx.MockTransport(fake), sleep=lambda s: None)
