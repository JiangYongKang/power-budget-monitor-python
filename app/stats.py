"""时间窗口统计：功率曲线与累计能耗。

口径约定：
- 窗口为左闭右开 [start, end)，按 UTC 时间戳归属；跨天无需特殊处理。
- 分桶从 start 起按 bucket_seconds 对齐，最后一个桶可能不足一个完整桶长。
- 空桶（桶内无任何样本）的功率为 None，绝不静默当作 0 参与统计。
- 能耗用梯形积分估算；相邻样本间隔超过 max_gap_seconds 的区段视为
  数据缺失，不参与积分（不假设功率为 0 或线性延续）。
- 输入点先去重排序（按 timestamp, report_id），乱序/重复不改变结果。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models import TelemetryPoint

DEFAULT_MAX_GAP_SECONDS = 3600


def _normalize(points: list[TelemetryPoint]) -> list[TelemetryPoint]:
    seen: set[tuple[str, str]] = set()
    out: list[TelemetryPoint] = []
    for p in sorted(points, key=lambda p: (p.timestamp, p.report_id)):
        key = (p.device_id, p.report_id)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def bucket_power(points: list[TelemetryPoint], start: datetime, end: datetime,
                 bucket_seconds: int) -> list[dict]:
    """按固定窗口聚合平均功率；空窗返回 None 而不是 0。"""
    if bucket_seconds <= 0 or end <= start:
        return []
    pts = _normalize(points)
    buckets: list[dict] = []
    cursor = start
    step = timedelta(seconds=bucket_seconds)
    while cursor < end:
        buckets.append({"start": cursor, "sum": 0.0, "samples": 0})
        cursor += step
    for p in pts:
        if start <= p.timestamp < end:
            idx = int((p.timestamp - start).total_seconds() // bucket_seconds)
            buckets[idx]["sum"] += p.power_w
            buckets[idx]["samples"] += 1
    return [
        {
            "start": b["start"],
            "avg_power_w": (b["sum"] / b["samples"]) if b["samples"] else None,
            "samples": b["samples"],
        }
        for b in buckets
    ]


def cumulative_energy_wh(points: list[TelemetryPoint], start: datetime, end: datetime,
                         max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS) -> float | None:
    """梯形积分估算能耗（Wh）；窗口内样本不足 2 个时返回 None。"""
    pts = [p for p in _normalize(points) if start <= p.timestamp < end]
    if len(pts) < 2:
        return None
    energy = 0.0
    for a, b in zip(pts, pts[1:]):
        gap = (b.timestamp - a.timestamp).total_seconds()
        if gap <= 0 or gap > max_gap_seconds:
            continue  # 缺数据区段不计入，而不是当作 0
        energy += (a.power_w + b.power_w) / 2 * gap / 3600.0
    return energy
