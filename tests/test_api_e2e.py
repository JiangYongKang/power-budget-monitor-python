"""端到端：只有校验+鉴权通过的上报才能影响预算与统计。"""
from __future__ import annotations

from tests.conftest import TOKEN, log_case


def _register(client):
    r = client.post("/devices", json={
        "device_id": "ac", "site_id": "s1", "circuit_id": "c1",
        "load_class": "interruptible", "shed_priority": 1, "owner_token": TOKEN})
    assert r.status_code == 201
    client.post("/budgets", json={"scope": "site", "scope_id": "s1",
                                  "limit_w": 1000, "restore_w": 600})


def test_rejected_reports_never_affect_budget_or_stats(client):
    _register(client)
    t = "2026-09-28T12:00:00Z"
    outcomes = []

    # 合法但不超限
    r1 = client.post("/telemetry", json={"device_id": "ac", "timestamp": t,
                     "power": 500, "power_unit": "W", "report_id": "ok1", "token": TOKEN})
    outcomes.append(("valid", r1.json()))
    # 越权
    r2 = client.post("/telemetry", json={"device_id": "ac", "timestamp": t,
                     "power": 900, "power_unit": "W", "report_id": "noauth", "token": "x"})
    outcomes.append(("unauthorized", r2.json()))
    # 非法单位
    r3 = client.post("/telemetry", json={"device_id": "ac", "timestamp": t,
                     "power": 900, "power_unit": "hp", "report_id": "badunit", "token": TOKEN})
    outcomes.append(("bad_unit", r3.json()))
    # 越界
    r4 = client.post("/telemetry", json={"device_id": "ac", "timestamp": t,
                     "power": 99_000, "power_unit": "kW", "report_id": "oor", "token": TOKEN})
    outcomes.append(("out_of_range", r4.json()))

    log_case("混合上报（合法+3 类拒绝）", [o[0] for o in outcomes],
             "拒绝的上报不得写入或触发降载",
             {"outcomes": outcomes,
              "load_state": client.get("/load-state/ac").json()})
    assert r2.json()["reason"] == "unauthorized"
    assert r3.json()["reason"] == "invalid_unit"
    assert r4.json()["reason"] == "out_of_range"
    assert client.get("/load-state/ac").json()["state"] == "on"  # 非法上报未推高超限

    stats = client.get("/stats/power", params={
        "scope": "site", "scope_id": "s1",
        "start": "2026-09-28T00:00:00Z", "end": "2026-09-29T00:00:00Z",
        "bucket_seconds": 86400}).json()
    assert stats["buckets"][0]["avg_power_w"] == 500.0
    assert stats["buckets"][0]["samples"] == 1  # 只有 1 条合法数据


def test_http_shed_then_restore_flow(client):
    _register(client)
    # 超限 → 降载
    r = client.post("/telemetry", json={"device_id": "ac",
                 "timestamp": "2026-09-28T12:00:00Z", "power": 1.5,
                 "power_unit": "kW", "report_id": "r1", "token": TOKEN}).json()
    log_case("HTTP 上报 1.5kW", "limit=1000W", "超限降载 ac", r)
    assert r["events"][0]["action"] == "shed"
    assert client.get("/load-state/ac").json()["state"] == "shed"
    # 重复 report_id：幂等
    dup = client.post("/telemetry", json={"device_id": "ac",
                   "timestamp": "2026-09-28T12:00:00Z", "power": 1.5,
                   "power_unit": "kW", "report_id": "r1", "token": TOKEN}).json()
    assert dup == {"accepted": True, "duplicate": True}
    # 低功率上报：活动功率 0 < 600，且设备最新读数 0，恢复后预计不超限 → 恢复
    r3 = client.post("/telemetry", json={"device_id": "ac",
              "timestamp": "2026-09-28T13:00:00Z", "power": 0,
              "power_unit": "W", "report_id": "r2", "token": TOKEN}).json()
    state = client.get("/load-state/ac").json()["state"]
    log_case("降载设备上报 0W", {"restore": 600, "limit": 1000},
             "活动功率<600 且最新读数 0，恢复后预计不超限",
             {"resp": r3, "state": state})
    assert state == "on"
    assert r3["events"][0]["action"] == "restore"
