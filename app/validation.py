"""上报校验：字段、单位、量程、时间，返回可区分的拒绝原因。

量程约定（归一化后）：
- 功率   0 .. 10 MW
- 电压   0 .. 100 kV
- 电流   0 .. 100 kA
时间约定：
- 必须带时区；允许迟到（按自身时间戳归属），但不允许早于 2000-01-01，
  也不允许超过当前时间 +5 分钟（明显异常的“未来数据”）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import RejectReason, TelemetryReport
from app.units import UnknownUnitError, current_to_a, power_to_w, voltage_to_v

MAX_POWER_W = 10_000_000.0
MAX_VOLTAGE_V = 100_000.0
MAX_CURRENT_A = 100_000.0
MIN_TIMESTAMP = datetime(2000, 1, 1, tzinfo=timezone.utc)
MAX_FUTURE_SKEW = timedelta(minutes=5)


def _check_range(value: float, lo: float, hi: float) -> bool:
    return lo <= value <= hi


def validate_report(report: TelemetryReport, now: datetime | None = None) -> RejectReason | None:
    """返回 None 表示通过，否则返回拒绝原因。"""
    now = now or datetime.now(timezone.utc)

    # 1. 必填字段
    if not report.device_id:
        return RejectReason.MISSING_FIELD
    if report.power is None or report.power_unit is None:
        return RejectReason.MISSING_FIELD
    if not report.report_id:
        return RejectReason.MISSING_FIELD
    if report.timestamp is None:
        return RejectReason.MISSING_FIELD

    # 2. 时间合法性
    ts = report.timestamp
    if ts.tzinfo is None:
        return RejectReason.INVALID_TIMESTAMP
    ts_utc = ts.astimezone(timezone.utc)
    if ts_utc < MIN_TIMESTAMP or ts_utc > now + MAX_FUTURE_SKEW:
        return RejectReason.INVALID_TIMESTAMP

    # 3. 单位与量程（功率必填，电压/电流可选）
    try:
        power_w = power_to_w(report.power, report.power_unit)
        voltage_v = voltage_to_v(report.voltage, report.voltage_unit) if report.voltage is not None else None
        current_a = current_to_a(report.current, report.current_unit) if report.current is not None else None
    except UnknownUnitError:
        return RejectReason.INVALID_UNIT
    except (TypeError, ValueError):
        return RejectReason.MISSING_FIELD

    if not _check_range(power_w, 0.0, MAX_POWER_W):
        return RejectReason.OUT_OF_RANGE
    if voltage_v is not None and not _check_range(voltage_v, 0.0, MAX_VOLTAGE_V):
        return RejectReason.OUT_OF_RANGE
    if current_a is not None and not _check_range(current_a, 0.0, MAX_CURRENT_A):
        return RejectReason.OUT_OF_RANGE

    return None
