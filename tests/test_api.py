"""HTTP 层集成：状态码、拒绝码可区分、批量帧接口、曲线与预算查询。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import T0
from fastapi.testclient import TestClient

from power_budget.api import create_app


@pytest.fixture
def client(make_service):
    svc, clock = make_service(sample_max_age_s=300, late_grace_seconds=3600)
    return TestClient(create_app(svc)), svc, clock


def _telemetry(dev, power, idem, ts=None, unit="W"):
    return {
        "idempotency_key": idem, "device_id": dev,
        "ts": (ts or T0).isoformat(),
        "readings": [{"metric": "power", "unit": unit, "value": power}],
    }


def _iso(minute):
    return (T0 + timedelta(minutes=minute)).isoformat()


def test_root_and_unknown_site(client):
    c, _, _ = client
    assert c.get("/").status_code == 200
    assert c.get("/api/v1/sites/nope/budget").status_code == 404
    assert c.get("/api/v1/devices/nope/energy",
                 params={"start": _iso(0), "end": _iso(1)}).status_code == 404


def test_telemetry_status_codes_are_distinguishable(client):
    c, _, clock = client
    clock.set(T0)
    h = {"X-API-Key": "admin-key"}
    assert c.post("/api/v1/telemetry", headers=h,
                  json=_telemetry("fixed", 600, "ok")).status_code == 200
    r = c.post("/api/v1/telemetry", headers=h, json=_telemetry("fixed", 600, "ok"))
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert c.post("/api/v1/telemetry", json=_telemetry("fixed", 1, "nokey")).status_code == 401
    r = c.post("/api/v1/telemetry", headers={"X-API-Key": "dev-fixed-key"},
               json=_telemetry("load-1", 1, "cross"))
    assert r.status_code == 401 and r.json()["reason"] == "unauthorized"
    assert c.post("/api/v1/telemetry", headers=h,
                  json=_telemetry("ghost", 1, "nd")).status_code == 404
    r = c.post("/api/v1/telemetry", headers=h,
               json=_telemetry("fixed", 1, "badunit", unit="Pa"))
    assert r.status_code == 422 and r.json()["reason"] == "unknown_unit"
    bad_ts = _telemetry("fixed", 1, "badts"); bad_ts["ts"] = "not-time"
    assert c.post("/api/v1/telemetry", headers=h, json=bad_ts).json()["reason"] == "bad_timestamp"
    r = c.post("/api/v1/telemetry",
               headers={"X-API-Key": "admin-key", "Content-Type": "application/json"},
               content=b"xxx")
    assert r.status_code == 422 and r.json()["reason"] == "bad_type"


def test_frame_endpoint_sheds_and_budget_query(client):
    c, _, clock = client
    clock.set(T0)
    resp = c.post("/api/v1/telemetry/frame", headers={"X-API-Key": "admin-key"}, json={
        "reports": [
            _telemetry("fixed", 600, "f0"),
            _telemetry("load-1", 0.45, "l1", unit="kW"),
            _telemetry("load-2", 250, "l2"),
            _telemetry("load-3", 200, "l3"),
        ],
    })
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert all(x["accepted"] for x in results)
    # 同一 decision 写回每条结果
    decisions = {tuple(x["decision"]["final_shed"]) for x in results if x["decision"]}
    assert decisions == {("load-1", "load-2")}

    r = c.get("/api/v1/sites/site-a/budget").json()
    assert r["shed_device_ids"] == ["load-1", "load-2"]
    assert r["last_decision"]["measured_power_w"] == 800.0
    assert r["last_decision"]["basis"]  # 判定依据非空，可用于定位


def test_frame_endpoint_partial_rejection_is_207(client):
    c, _, clock = client
    clock.set(T0)
    resp = c.post("/api/v1/telemetry/frame", headers={"X-API-Key": "admin-key"}, json={
        "reports": [
            _telemetry("fixed", 600, "good"),
            _telemetry("load-1", -1, "badrange"),          # 越界拒绝
            _telemetry("ghost", 1, "badddev"),             # 未知设备
        ],
    })
    assert resp.status_code == 207
    results = resp.json()["results"]
    by_key = {x["idempotency_key"]: x for x in results}
    assert by_key["good"]["accepted"]
    assert by_key["badrange"]["reason"] == "value_out_of_range"
    assert by_key["badddev"]["reason"] == "unknown_device"


def test_curve_and_energy_empty_gap(client):
    c, svc, clock = client
    clock.set(T0)
    svc.ingest(_telemetry("fixed", 100, "only"), "admin-key")
    r = c.get("/api/v1/devices/fixed/energy",
              params={"start": _iso(0), "end": _iso(10)})
    j = r.json()
    assert j["complete"] is False and j["gap_seconds"] > 0  # 缺数据不静默当零
    curve = c.get("/api/v1/sites/site-a/curve",
                  params={"start": _iso(0), "end": _iso(3), "align": "1m"}).json()
    assert len(curve["points"]) == 1 and len(curve["empty_buckets"]) == 2


def test_frame_endpoint_requires_reports_array(client):
    c, _, _ = client
    r = c.post("/api/v1/telemetry/frame", headers={"X-API-Key": "admin-key"}, json={})
    assert r.status_code == 422 and r.json()["reason"] == "missing_field"
