"""幂等去重、迟到/乱序归属、重复查询稳定性、缺数据不当零。"""
from __future__ import annotations

from datetime import timedelta

from app.stats import bucket_power, cumulative_energy_wh
from tests.conftest import T0, log_case, make_device, make_point


def test_duplicate_report_id_counted_once(store):
    store.add_device(make_device("d1"))
    p1 = make_point("d1", T0, 100.0, "r1")
    first = store.insert_point(p1)
    dup = store.insert_point(make_point("d1", T0 + timedelta(minutes=1), 999.0, "r1"))
    pts = store.points_in_window(["d1"], T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    log_case("重复 report_id", {"first": first, "dup_insert": dup},
             "(device_id, report_id) 唯一约束", pts)
    assert first is True and dup is False
    assert [p.power_w for p in pts] == [100.0]


def test_late_and_out_of_order_data_attributed_by_timestamp(store):
    store.add_device(make_device("d1"))
    store.insert_point(make_point("d1", T0 + timedelta(minutes=20), 300.0, "late-arrival-1"))
    store.insert_point(make_point("d1", T0, 100.0, "late-arrival-2"))
    pts = store.points_in_window(["d1"], T0, T0 + timedelta(hours=1))
    log_case("迟到+乱序到达", [(p.report_id, p.timestamp) for p in pts],
             "按时间戳而非到达顺序排序归属", pts)
    assert [p.power_w for p in pts] == [100.0, 300.0]
    assert store.latest_point("d1").power_w == 300.0


def test_repeated_queries_are_stable(store):
    store.add_device(make_device("d1"))
    for i in range(3):
        store.insert_point(make_point("d1", T0 + timedelta(minutes=10 * i),
                                      float(100 * (i + 1)), f"r{i}"))
    w = (T0, T0 + timedelta(hours=1))
    pts = store.points_in_window(["d1"], *w)
    b1 = bucket_power(pts, *w, bucket_seconds=600)
    b2 = bucket_power(list(reversed(pts)), *w, bucket_seconds=600)
    e1 = cumulative_energy_wh(pts, *w)
    e2 = cumulative_energy_wh(list(reversed(pts)), *w)
    log_case("重复查询", "乱序输入两次", "归一化排序+去重后计算",
             {"buckets_equal": b1 == b2, "energy_equal": e1 == e2, "energy": e1})
    assert b1 == b2 and e1 == e2


def test_empty_bucket_is_none_not_zero(store):
    store.add_device(make_device("d1"))
    store.insert_point(make_point("d1", T0, 100.0, "r1"))
    pts = store.points_in_window(["d1"], T0, T0 + timedelta(hours=3))
    buckets = bucket_power(pts, T0, T0 + timedelta(hours=3), 3600)
    log_case("空窗统计", [b["samples"] for b in buckets],
             "空桶 avg_power_w=None，不当 0", buckets)
    assert [b["avg_power_w"] for b in buckets] == [100.0, None, None]


def test_energy_missing_gap_not_counted_as_zero():
    pts = [
        make_point("d1", T0, 100.0, "a"),
        make_point("d1", T0 + timedelta(minutes=10), 100.0, "b"),
        make_point("d1", T0 + timedelta(hours=48), 100.0, "c"),
    ]
    e_full = cumulative_energy_wh(pts, T0, T0 + timedelta(hours=49))
    e_pair = cumulative_energy_wh(pts[:2], T0, T0 + timedelta(hours=49))
    log_case("48h 缺口的能耗", [(p.report_id) for p in pts],
             "缺口 > max_gap 区段不积分", {"e_full": e_full, "e_pair": e_pair})
    assert e_full == e_pair  # 缺口没有被静默当 0 拉长积分
    assert cumulative_energy_wh(pts[:1], T0, T0 + timedelta(hours=1)) is None
