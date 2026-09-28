"""时间窗口统计：功率曲线与累计能耗。口径显式、结果对重复/乱序数据稳定。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from .models import CurvePoint, EnergyResult, Sample, WindowAlign

DEFAULT_GAP_SECONDS: float = 300.0

_ALIGN_DELTAS: dict[WindowAlign, timedelta] = {
    WindowAlign.RAW: timedelta(0),
    WindowAlign.MINUTE: timedelta(minutes=1),
    WindowAlign.FIVE_MIN: timedelta(minutes=5),
    WindowAlign.HOUR: timedelta(hours=1),
}


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def dedup_sorted(samples: list[Sample]) -> list[Sample]:
    """按测量时刻去重并升序排列。存储层已保证同测量时刻只有一条，
    这里再做一次防御性去重，使重复/乱序输入绝不改变结论。
    """
    by_ts: dict[datetime, Sample] = {}
    for s in samples:
        by_ts.setdefault(s.ts, s)
    return [by_ts[k] for k in sorted(by_ts)]


def _power(s: Sample) -> float | None:
    if s.power_w is not None:
        return s.power_w
    if s.voltage_v is not None and s.current_a is not None:
        return s.voltage_v * s.current_a
    return None


def floor_to_bucket(ts: datetime, align: WindowAlign) -> datetime:
    """向下取整到桶边界（按 UTC 的分/5分/时边界；RAW 不使用）。"""
    ts = _as_utc(ts)
    if align is WindowAlign.MINUTE:
        return ts.replace(second=0, microsecond=0)
    if align is WindowAlign.FIVE_MIN:
        m = (ts.minute // 5) * 5
        return ts.replace(minute=m, second=0, microsecond=0)
    if align is WindowAlign.HOUR:
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts


def bucket_grid(start: datetime, end: datetime, align: WindowAlign) -> list[tuple[datetime, datetime]]:
    """生成 [start, end) 对齐后的空桶网格，用于与曲线对照识别缺数据桶。"""
    start, end = _as_utc(start), _as_utc(end)
    delta = _ALIGN_DELTAS[align]
    if delta == timedelta(0) or end <= start:
        return []
    cur = floor_to_bucket(start, align)
    grid: list[tuple[datetime, datetime]] = []
    while cur < end:
        nxt = cur + delta
        if nxt > start:
            grid.append((max(cur, start), min(nxt, end)))
        cur = nxt
    return grid


def power_curve(
    samples: list[Sample],
    start: datetime,
    end: datetime,
    align: WindowAlign = WindowAlign.MINUTE,
) -> list[CurvePoint]:
    """返回 [start, end) 内功率曲线。

    - RAW 口径：每个测量时刻一个点（average=max=该时刻功率）；
    - 桶口径：只对有功率值的样本做桶内平均/最大；
    - 没有样本的桶不出现在结果中（缺数据不静默补零；对照 bucket_grid 可见空桶）；
    - 起点包含、终点排除（[start, end)），跨天按自然 UTC 桶边界对齐。
    """
    start, end = _as_utc(start), _as_utc(end)
    ordered = dedup_sorted(samples)
    in_win = [s for s in ordered if start <= s.ts < end and _power(s) is not None]

    if align is WindowAlign.RAW:
        return [
            CurvePoint(s.ts, s.ts, _power(s), _power(s), 1)
            for s in in_win
        ]

    delta = _ALIGN_DELTAS[align]
    buckets: dict[datetime, list[tuple[datetime, float]]] = {}
    for s in in_win:
        b = floor_to_bucket(s.ts, align)
        buckets.setdefault(b, []).append((s.ts, _power(s)))  # type: ignore[arg-type]

    points: list[CurvePoint] = []
    for b in sorted(buckets):
        vals = [v for _, v in buckets[b]]
        points.append(
            CurvePoint(
                bucket_start=max(b, start),
                bucket_end=min(b + delta, end),
                average_w=sum(vals) / len(vals),
                max_w=max(vals),
                sample_count=len(vals),
            )
        )
    return points


def energy(
    samples: list[Sample],
    start: datetime,
    end: datetime,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
) -> EnergyResult:
    """相邻样本梯形积分计算 [start, end) 累计能耗（Wh）。

    - 先按测量时刻去重排序：重复/乱序不改变结论；
    - 相邻样本间隔 > gap_seconds 视为缺口，缺口区间不积分（不按零累计）；
    - 积分在窗口端点处截断：首段从前一外部样本线性插值到 start，
      末段从最后样本积分到 end（该段间隔仍受缺口规则约束）；
    - energy_wh 仅覆盖 covered_seconds；complete=False 表示窗口内有缺口，
      调用方不应将其当作全窗口真实总量。
    """
    start, end = _as_utc(start), _as_utc(end)
    total_window = max(0.0, (end - start).total_seconds())

    series: list[tuple[datetime, float]] = []
    for s in dedup_sorted(samples):
        p = _power(s)
        if p is not None:
            series.append((s.ts, p))
    series.sort(key=lambda x: x[0])

    energy_wh = 0.0
    covered = 0.0
    gap = 0.0

    if total_window > 0:
        if not series:
            gap = total_window
        else:
            # 首点之前 / 末点之后无依据 -> 全部记为缺口（clamp 到窗口内，
            # 首点早于/末点晚于窗口时对应余量为 0）。
            first_ts, last_ts = series[0][0], series[-1][0]
            gap += max(0.0, (min(first_ts, end) - start).total_seconds()) if first_ts > start else 0.0
            gap += max(0.0, (end - max(last_ts, start)).total_seconds()) if last_ts < end else 0.0

            for (t0, p0), (t1, p1) in zip(series, series[1:]):
                seg_start = max(t0, start)
                seg_end = min(t1, end)
                if seg_end <= seg_start:
                    continue
                ds = (seg_end - seg_start).total_seconds()
                dt_full = (t1 - t0).total_seconds()
                if dt_full > gap_seconds:
                    gap += ds
                    continue

                def _interp(t, _p0=p0, _p1=p1, _t0=t0, _dt=dt_full) -> float:
                    return _p0 + (_p1 - _p0) * (t - _t0).total_seconds() / _dt

                pa, pb = _interp(seg_start), _interp(seg_end)
                energy_wh += (pa + pb) / 2.0 * ds / 3600.0
                covered += ds

    gap = max(0.0, min(gap, total_window))
    covered = max(0.0, min(covered, total_window - gap))
    return EnergyResult(
        window_start=start,
        window_end=end,
        energy_wh=energy_wh,
        covered_seconds=covered,
        gap_seconds=gap,
        sample_count=len(series),
        complete=(gap <= 0.0 and total_window > 0 and len(series) >= 2),
    )
