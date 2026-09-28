"""校验与权限：单位异常、字段缺失、越界、越权上报，拒绝原因可区分。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import RejectReason, TelemetryReport
from app.validation import validate_report
from tests.conftest import TOKEN, log_case

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _report(**over) -> TelemetryReport:
    base = dict(device_id="d1", timestamp=NOW, power=1.0, power_unit="kW",
                report_id="r1", token=TOKEN)
    base.update(over)
    return TelemetryReport(**base)


def test_valid_report_passes():
    r = _report()
    result = validate_report(r, now=NOW)
    log_case("合法上报", r, "字段齐全/单位合法/量程内", result)
    assert result is None


def test_missing_field_rejected():
    r = _report(power=None)
    result = validate_report(r, now=NOW)
    log_case("缺功率字段", r, "power 为必填", result)
    assert result == RejectReason.MISSING_FIELD
    r2 = _report(report_id="")
    assert validate_report(r2, now=NOW) == RejectReason.MISSING_FIELD


def test_invalid_unit_rejected():
    r = _report(power_unit="马力")
    result = validate_report(r, now=NOW)
    log_case("非法单位", r, "单位不在白名单", result)
    assert result == RejectReason.INVALID_UNIT


def test_out_of_range_rejected():
    r = _report(power=20_000, power_unit="kW")  # 20MW > 10MW 上限
    result = validate_report(r, now=NOW)
    log_case("功率越界", r, "上限 10MW", result)
    assert result == RejectReason.OUT_OF_RANGE
    assert validate_report(_report(power=-5), now=NOW) == RejectReason.OUT_OF_RANGE


def test_invalid_timestamp_rejected():
    future = _report(timestamp=NOW + timedelta(hours=1))
    result = validate_report(future, now=NOW)
    log_case("未来时间戳", future, "允许偏差 +5min", result)
    assert result == RejectReason.INVALID_TIMESTAMP
    naive = _report(timestamp=datetime(2026, 9, 28, 12, 0))
    assert validate_report(naive, now=NOW) == RejectReason.INVALID_TIMESTAMP


def test_late_report_accepted_and_attributed():
    late = _report(timestamp=NOW - timedelta(days=3))
    result = validate_report(late, now=NOW)
    log_case("迟到 3 天的数据", late, "迟到合法，按自身时间戳归属", result)
    assert result is None


def test_unauthorized_report_rejected(client):
    client.post("/devices", json={"device_id": "d1", "site_id": "s1",
                                  "circuit_id": "c1", "owner_token": TOKEN})
    body = {"device_id": "d1", "timestamp": NOW.isoformat(), "power": 100,
            "power_unit": "W", "report_id": "r1", "token": "wrong"}
    resp = client.post("/telemetry", json=body).json()
    log_case("越权上报", body, "token 与设备不匹配", resp)
    assert resp == {"accepted": False, "reason": "unauthorized"}

    body["device_id"] = "ghost"
    resp = client.post("/telemetry", json=body).json()
    log_case("未知设备上报", body, "设备未注册", resp)
    assert resp == {"accepted": False, "reason": "unknown_device"}
