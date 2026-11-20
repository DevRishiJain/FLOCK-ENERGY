from tests.conftest import meter
from urja_api.normalize import normalize_export, normalize_readings, parse_ts


def test_parse_ts_ist_to_utc():
    assert parse_ts("23/06/2026 23:30") == "2026-06-23T18:00:00Z"


def test_readings_numbers_and_dedupe():
    r = normalize_readings([{"timestamp": "24/06/2026 00:00", "kwh": "1.5", "kvah": "", "voltR": "230"},
                            {"timestamp": "24/06/2026 00:00", "kwh": "2", "kvah": "3", "voltR": "230"},
                            {"timestamp": "garbage", "kwh": "1"}])
    assert len(r) == 1 and r[0]["kwh"] == 2.0


def test_empty_code_repaired_and_reported():
    _, nodes, issues = normalize_export([meter(1, circle={"name": "Circle 6", "code": ""})])
    assert any(n["id"] == "circle:C-06" for n in nodes)
    assert [i["kind"] for i in issues] == ["empty_code_repaired"]


def test_majority_parent_and_conflict_reported():
    ms = [meter(1), meter(2), meter(3, division={"name": "Division 9", "code": "D-09"}, circle={"name": "Circle 3", "code": "C-03"})]
    _, nodes, issues = normalize_export(ms)
    assert {n["id"]: n["parent_id"] for n in nodes}["division:D-01"] == "circle:C-01"
    assert not any(i["kind"] == "multiple_parents" and i["entity"] == "division:D-09" for i in issues)


def test_feeder_code_repair_uses_three_digits():
    _, nodes, _ = normalize_export([meter(1, feeder={"name": "Feeder 5", "code": ""})])
    assert any(n["id"] == "feeder:F-005" for n in nodes)
