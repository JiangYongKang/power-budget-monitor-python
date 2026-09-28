"""上报校验：字段、单位、量程、时间，返回可区分的拒绝原因。"""
from __future__ import annotations

from app.models import RejectReason, TelemetryReport


def validate_report(report: TelemetryReport) -> RejectReason | None:
    """返回 None 表示通过，否则返回拒绝原因。"""
    raise NotImplementedError
