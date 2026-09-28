"""时间窗口统计：功率曲线与累计能耗，缺数据不当作零。"""
from __future__ import annotations

from datetime import datetime

from app.models import TelemetryPoint


def bucket_power(points: list[TelemetryPoint], start: datetime, end: datetime, bucket_seconds: int) -> list[dict]:
    """按固定窗口聚合功率；空窗返回 None 而不是 0。"""
    raise NotImplementedError


def cumulative_energy_wh(points: list[TelemetryPoint], start: datetime, end: datetime) -> float | None:
    """梯形积分估算能耗（Wh）；数据不足返回 None。"""
    raise NotImplementedError
