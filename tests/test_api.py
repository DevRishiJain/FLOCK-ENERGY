import pytest
from fastapi.testclient import TestClient

from urja_api.api import create_app
from urja_api.sync import run_sync

RDR, ADM = {"X-API-Key": "rdr"}, {"X-API-Key": "adm"}


@pytest.fixture
def client(settings, portal):
    with TestClient(create_app(settings)) as c:
        assert run_sync(settings, portal)["status"] == "ok"
        yield c


def test_auth_required(client):
    assert client.get("/v1/meters").status_code == 401
    assert client.get("/v1/meters", headers={"X-API-Key": "bad"}).json()["error"]["code"] == "unauthorized"
    assert client.get("/health").status_code == 200


def test_rbac_admin_only_sync(client):
    assert client.post("/v1/sync", headers=RDR).status_code == 403


def test_meters_filter_and_detail(client):
    d = client.get("/v1/meters?build=v2", headers=RDR).json()
    assert d["total"] == 1 and d["items"][0]["meter_id"] == "J100002"
    m = client.get("/v1/meters/J100002", headers=RDR).json()
    assert [h["level"] for h in m["hierarchy"]][0] == "zone" and m["hierarchy"][-1]["code"] == "DT-001"
    assert client.get("/v1/meters/NOPE", headers=RDR).status_code == 404


def test_readings_range(client):
    r = client.get("/v1/meters/J100001/readings?from=2026-06-23T18:30:00Z", headers=RDR).json()
    assert r["total"] == 1 and r["items"][0]["kwh"] == 11.0


def test_hierarchy_and_node_filter(client):
    t = client.get("/v1/hierarchy", headers=RDR).json()["items"]
    assert t[0]["meter_count"] == 2
    assert client.get("/v1/meters?node=circle:C-01", headers=RDR).json()["total"] == 2


def test_nearby(client):
    r = client.get("/v1/meters/nearby?lat=26.901&lon=75.8&radius_km=0.5", headers=RDR).json()
    assert r["items"][0]["meter_id"] == "J100001"


def test_etag_304(client):
    r = client.get("/v1/meters", headers=RDR)
    assert client.get("/v1/meters", headers={**RDR, "If-None-Match": r.headers["etag"]}).status_code == 304


def test_rate_limit(settings, portal):
    settings.rate_limit_per_min = 2
    with TestClient(create_app(settings)) as c:
        codes = [c.get("/v1/transformers", headers=RDR).status_code for _ in range(4)]
        assert codes[:2] == [200, 200] and 429 in codes
