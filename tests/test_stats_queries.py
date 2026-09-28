"""时间窗口功率曲线与累计能耗：口径明确、结果稳定、缺数据不静默当零。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from conftest import T0

from power_budget.models import WindowAlign


def _add(svc, dev, minute, power, second=0):
    ts = T0 + timedelta(minutes=minute, seconds=second)
    svc.clock.set(ts)
    r = svc.ingest({
        "idempotency_key": f"{dev}-{minute}-{second}",
        "device_id": dev, "ts": ts.isoformat(),
        "readings": [{"metric": "power", "unit": "W", "value": power}],
    }, "admin-key")
    assert r.accepted, (r.reason, r.detail)
    return ts


def test_device_energy_constant_power(make_service):
    svc, _ = make_service(late_grace_seconds=7200, stats_gap_seconds=3601)
    for m in range(7):  # 0..6 每分钟 100W
        _add(svc, "fixed", m, 100.0)
    # 窗口 [0,6min)，t=6 样本用于末端插值；100W*6min=10Wh，全段覆盖
    e = svc.device_energy("fixed", T0, T0 + timedelta(minutes=6))
    assert e.complete and e.gap_seconds == 0.0, e
    assert abs(e.energy_wh - 10.0) < 1e-9


def test_energy_gap_is_explicit_not_zero(make_service):
    """两个样本间隔超过缺口阈值：缺口段不按零积分，complete=False。"""
    svc, _ = make_service(late_grace_seconds=7200, stats_gap_seconds=300)
    _add(svc, "fixed", 0, 100.0)
    _add(svc, "fixed", 10, 100.0)   # 间隔 600s > 300s
    e = svc.device_energy("fixed", T0, T0 + timedelta(minutes=10))
    assert not e.complete
    assert e.covered_seconds == 0.0 and e.gap_seconds == 600.0
    assert e.energy_wh == 0.0


def test_energy_empty_window_not_zero(make_service):
    """无任何样本的窗口：能耗 0 但整窗标记缺口，不能解读为“真实零能耗”。"""
    svc, _ = make_service()
    e = svc.device_energy("fixed", T0, T0 + timedelta(hours=1))
    assert e.energy_wh == 0.0 and e.gap_seconds == 3600.0 and not e.complete


def test_repeated_and_out_of_order_do_not_change_result(make_service):
    """重复/乱序上报后查询结论必须稳定一致。"""
    svc, _ = make_service(late_grace_seconds=7200, stats_gap_seconds=3601)
    _add(svc, "fixed", 6, 100.0)   # 乱序：先收最晚
    _add(svc, "fixed", 0, 0.0)
    _add(svc, "fixed", 3, 50.0)
    # 同刻相同样本（不同幂等键）-> 不重复计数
    ts_dup = T0 + timedelta(minutes=3)
    svc.clock.set(ts_dup)
    r = svc.ingest({
        "idempotency_key": "dup", "device_id": "fixed", "ts": ts_dup.isoformat(),
        "readings": [{"metric": "power", "unit": "W", "value": 50.0}],
    }, "admin-key")
    assert r.duplicate
    e1 = svc.device_energy("fixed", T0, T0 + timedelta(minutes=6))
    e2 = svc.device_energy("fixed", T0, T0 + timedelta(minutes=6))
    assert e1.energy_wh == e2.energy_wh
    # 0->50->100 over 6min（t=6 样本插值覆盖末端）：
    # (0+50)/2*3min + (50+100)/2*3min = 25*0.05h + 75*0.05h = 5 Wh
    assert abs(e1.energy_wh - 5.0) < 1e-9, e1
    assert e1.sample_count == 3


def test_curve_missing_buckets_are_absent_and_listed(make_service):
    """功率曲线缺数据桶不补零；empty_buckets 显式列出。"""
    from fastapi.testclient import TestClient

    from power_budget.api import create_app

    svc, _ = make_service(late_grace_seconds=7200)
    _add(svc, "fixed", 0, 10.0)
    _add(svc, "fixed", 0, 20.0, second=30)  # 同分钟第二样本
    _add(svc, "fixed", 2, 40.0)
    client = TestClient(create_app(svc))
    resp = client.get("/api/v1/sites/site-a/curve", params={
        "start": T0.isoformat(), "end": (T0 + timedelta(minutes=3)).isoformat(),
        "align": "1m",
    })
    j = resp.json()
    present = [p["bucket_start"][14:16] for p in j["points"]]
    assert present == ["00", "02"]
    # 第 0 桶平均=(10+20)/2=15, max=20
    assert j["points"][0]["average_w"] == 15.0
    assert j["points"][0]["max_w"] == 20.0
    assert len(j["empty_buckets"]) == 1  # 第 1 分钟无数据


def test_curve_raw_align(make_service):
    svc, _ = make_service(late_grace_seconds=7200)
    _add(svc, "fixed", 0, 10.0)
    _add(svc, "fixed", 0, 20.0, second=30)
    pts = svc.site_power_curve("site-a", T0, T0 + timedelta(minutes=1), WindowAlign.RAW)
    assert [p.average_w for p in pts] == [10.0, 20.0]


def test_cross_day_window_bucketing(make_service):
    """跨 UTC 自然日边界的 5 分钟桶对齐正确。"""
    from power_budget.stats import bucket_grid
    start = datetime(2026, 1, 1, 23, 58, tzinfo=UTC)
    end = datetime(2026, 1, 2, 0, 3, tzinfo=UTC)
    grid = bucket_grid(start, end, WindowAlign.FIVE_MIN)
    assert grid[0][0] == start
    assert grid[-1][1] == end
    assert any(a == datetime(2026, 1, 2, 0, 0, tzinfo=UTC) for a, _ in grid)


def test_site_energy_sums_per_device_and_flags_gap(make_service):
    """站点能耗=逐设备积分之和；任一设备有缺口则 complete=False。"""
    svc, _ = make_service(late_grace_seconds=7200, stats_gap_seconds=3601)
    for m in range(7):
        _add(svc, "fixed", m, 100.0)        # 完整：窗口内 10Wh
    _add(svc, "load-3", 0, 50.0)
    _add(svc, "load-3", 6, 50.0)            # 末端外部样本插值 -> 50W*6min=5Wh
    e = svc.site_energy("site-a", T0, T0 + timedelta(minutes=6))
    assert e.complete, e
    assert abs(e.energy_wh - 15.0) < 1e-9, e  # 10 + 5 Wh
    assert e.sample_count >= 9


def test_window_half_open_endpoint(make_service):
    """窗口为 [start, end)：恰在 end 的样本不作为“窗口内点”，但可用于端点插值。"""
    svc, _ = make_service(late_grace_seconds=7200, stats_gap_seconds=3601)
    _add(svc, "fixed", 0, 100.0)
    _add(svc, "fixed", 5, 200.0)
    e = svc.device_energy("fixed", T0, T0 + timedelta(minutes=5))
    # t=5 外部样本插值覆盖整段 [0,5)：功率从 100 线性到 200 -> 平均 150W*1/12h
    assert e.sample_count == 2  # 含一个外部邻接样本
    assert abs(e.energy_wh - 12.5) < 1e-9, e  # 150W * 5min
    assert e.gap_seconds == 0.0
    # 曲线口径下 t=5 的点不出现在 [0,5) 窗口
    pts = svc.site_power_curve("site-a", T0, T0 + timedelta(minutes=5), WindowAlign.RAW)
    assert all(p.bucket_start < T0 + timedelta(minutes=5) for p in pts)
