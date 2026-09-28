"""并发上报：预算结论一致、不被击穿、同一负载不重复降载。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from conftest import T0


def _payload(when, dev, power_w, idem, unit="W"):
    return {
        "idempotency_key": idem, "device_id": dev, "ts": when.isoformat(),
        "readings": [{"metric": "power", "unit": unit, "value": power_w}],
    }


def test_concurrent_single_reports_final_state_consistent(make_service, logger):
    """同一帧 4 设备在多线程同时单设备上报：每设备仅一条样本，齐帧结论稳定。"""
    svc, clock = make_service(sample_max_age_s=300)
    when = T0
    clock.set(when)
    base = [
        _payload(when, "fixed", 600, "c-fixed"),
        _payload(when, "load-1", 450, "c-l1"),
        _payload(when, "load-2", 250, "c-l2"),
        _payload(when, "load-3", 200, "c-l3"),
    ]
    barrier = threading.Barrier(len(base))

    def send(i):
        barrier.wait()
        return svc.ingest(base[i], "admin-key")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, [2, 0, 3, 1]))
    assert all(r.accepted for r in results)
    for dev in ("fixed", "load-1", "load-2", "load-3"):
        assert len(svc.store.samples_between(dev, when, when + timedelta(seconds=1))) == 1

    # 用齐帧数据重估，结论必须稳定唯一
    from power_budget.budget import BudgetConfig, evaluate
    d = evaluate(BudgetConfig(
        site=svc.store.sites["site-a"], circuits=svc.store.circuits_of_site("site-a"),
        devices=svc.store.devices_of_site("site-a"),
        latest=svc.store.samples_at(when),
        shed=svc.store.get_shed_state("site-a"), evaluated_at=when))
    logger.info("[并发-收敛] final=%s measured=%s basis=%s",
                d.final_shed, d.measured_power_w, d.basis)
    assert set(d.final_shed) == {"load-1", "load-2"}
    assert d.measured_power_w == 800.0


def test_concurrent_repeated_frame_no_double_count_no_breach(make_service, logger):
    """同一重载帧被多线程高并发重复提交（含相同/不同幂等键）：

    - 样本绝不重复计数（每设备每时刻仅 1 条）；
    - 每次预算判定降后功率 <= limit，预算不被击穿；
    - 同一负载在一次判定中不会被重复降载。
    """
    svc, clock = make_service(sample_max_age_s=300)
    when = T0
    clock.set(when)

    devices = [("fixed", 600), ("load-1", 450), ("load-2", 250), ("load-3", 200)]

    def build_frame(tag):
        return [_payload(when, d, p, f"{tag}-{d}") for d, p in devices]

    barrier = threading.Barrier(16)

    def worker(tag):
        barrier.wait()
        # 首个 tag 用规范键（真正写入），其余用各自键（应全部走“同刻相同样本”幂等）
        return svc.ingest_frame(build_frame(tag), "admin-key")

    tags = [f"t{i:02d}" for i in range(16)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        batches = list(pool.map(worker, tags))

    # 1) 不重复计数
    for dev, _ in devices:
        arr = svc.store.samples_between(dev, when, when + timedelta(seconds=1))
        assert len(arr) == 1, (dev, len(arr))

    # 2) 每个被接受的决策都不击穿、且不出现重复降载
    for batch in batches:
        for r in batch:
            d = r.decision
            if d is None:
                continue
            assert d.measured_power_w <= 1000.0 + 1e-9, d.basis
            assert len(set(d.newly_shed)) == len(d.newly_shed)
            assert not (set(d.newly_shed) & set(d.restored))

    final = svc.store.get_shed_state("site-a").shed_device_ids
    logger.info("[并发-重复帧] 最终降载=%s", final)
    assert final == ["load-1", "load-2"]


def test_concurrent_distinct_keys_same_payload_stays_idempotent(make_service):
    """同内容用不同幂等键并发：只有第一条写入，其余同刻相同样本不计数。"""
    svc, clock = make_service()
    when = T0
    clock.set(when)
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        return svc.ingest(_payload(when, "fixed", 600, f"key-{i}"), "admin-key")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    accepted_new = [r for r in results if r.accepted and not r.duplicate]
    dups = [r for r in results if r.accepted and r.duplicate]
    assert len(accepted_new) == 1
    assert len(dups) == 7
    assert len(svc.store.samples_between("fixed", when, when + timedelta(seconds=1))) == 1
