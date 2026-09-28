"""预算分级降载、滞回恢复、临界条件与并发一致性。"""
from __future__ import annotations

import threading
from datetime import timedelta

from app.models import BudgetConfig, LoadClass
from tests.conftest import T0, log_case, make_device, make_point

LIMIT = 1000.0
RESTORE = 700.0


def _setup_site(store, budgets=True):
    store.add_device(make_device("server", priority=0, load_class=LoadClass.CRITICAL))
    store.add_device(make_device("ac", priority=1))
    store.add_device(make_device("heater", priority=2))
    store.add_device(make_device("fan", priority=3))
    if budgets:
        store.set_budget(BudgetConfig("site", "s1", LIMIT, RESTORE))


def test_priority_shed_and_critical_protected(store, controller):
    _setup_site(store)
    readings = {"server": 400.0, "ac": 350.0, "heater": 300.0, "fan": 200.0}
    for i, (did, w) in enumerate(readings.items()):
        store.insert_point(make_point(did, T0, w, f"r{did}"))
    events = controller.evaluate("site", "s1", at=T0)
    actions = [(e.device_id, e.action) for e in events]
    log_case("总量1250超1000", readings,
             "按优先级 1 降 ac(350) 后剩 900≤1000 即停",
             {"events": actions,
              "states": {d: controller.load_state(d) for d in readings}})
    assert actions == [("ac", "shed")]
    assert controller.load_state("server") == "on"  # 关键负载永不降载
    assert controller.load_state("heater") == "on"  # 降到不超限即停
    assert controller.load_state("fan") == "on"
    # 预算未被击穿：当前有效功率 = server+heater+fan = 900 ≤ 1000
    total, _ = controller._current_power("site", "s1")
    assert total == 900.0


def test_exact_boundary_does_not_shed(store, controller):
    _setup_site(store)
    readings = {"server": 700.0, "ac": 300.0, "heater": 0.0, "fan": 0.0}
    for did, w in readings.items():
        store.insert_point(make_point(did, T0, w, f"r{did}"))
    events = controller.evaluate("site", "s1", at=T0)
    log_case("总量恰好=1000", readings, "判定为 total > limit，等于不触发", events)
    assert events == []


def test_hysteresis_dead_zone_holds(store, controller):
    _setup_site(store)
    store.insert_point(make_point("server", T0, 800.0, "rs"))
    store.insert_point(make_point("ac", T0, 300.0, "ra"))
    controller.evaluate("site", "s1", at=T0)  # 1100 → 降 ac，剩 800
    assert controller.load_state("ac") == "shed"
    # 800 落在死区 [700, 1000]：不恢复也不再降
    events = controller.evaluate("site", "s1", at=T0 + timedelta(minutes=1))
    log_case("死区判定", {"active_power": 800.0}, "700≤800≤1000 维持现状", events)
    assert events == [] and controller.load_state("ac") == "shed"


def test_restore_below_restore_threshold(store, controller):
    _setup_site(store)
    store.insert_point(make_point("server", T0, 800.0, "rs1"))
    store.insert_point(make_point("ac", T0, 300.0, "ra1"))
    controller.evaluate("site", "s1", at=T0)
    # server 降到 200：活动功率 200 < 700，恢复 ac 后 500 ≤ 1000 → 恢复
    store.insert_point(make_point("server", T0 + timedelta(minutes=2), 200.0, "rs2"))
    events = controller.evaluate("site", "s1", at=T0 + timedelta(minutes=2))
    log_case("滞回恢复", {"server": 200.0, "ac_last": 300.0},
             "活动功率<700 且恢复后≤1000", [(e.device_id, e.action) for e in events])
    assert [(e.device_id, e.action) for e in events] == [("ac", "restore")]
    assert controller.load_state("ac") == "on"
    # 再跑一次不能重复恢复
    assert controller.evaluate("site", "s1", at=T0 + timedelta(minutes=3)) == []


def test_concurrent_reports_budget_consistent(store, controller):
    """多线程同时上报并触发判定：结论必须一致、不重复降载、不击穿预算。"""
    _setup_site(store)
    devices = ["server", "ac", "heater", "fan"]
    powers = {"server": 500.0, "ac": 400.0, "heater": 300.0, "fan": 200.0}
    barrier = threading.Barrier(len(devices))
    errors: list[Exception] = []

    def worker(did: str):
        try:
            barrier.wait()
            store.insert_point(make_point(did, T0, powers[did], f"r-{did}"))
            controller.evaluate("site", "s1", at=T0)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(d,)) for d in devices]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    total, per_device = controller._current_power("site", "s1")
    shed = [d for d in devices if controller.load_state(d) == "shed"]
    log_case("4 设备并发上报", powers,
             "同 scope 判定在锁内串行；降载直到 total≤1000",
             {"total": total, "per_device": per_device, "shed": shed})
    assert controller.load_state("server") == "on"
    assert total <= LIMIT, f"预算被击穿: {total}"
    # 同一负载不会被重复降载：事件中每台设备的 shed 次数恰为 0 或 1
    shed_counts = {d: 0 for d in devices}
    for e in controller.events():
        if e.action == "shed":
            shed_counts[e.device_id] += 1
    assert all(v <= 1 for v in shed_counts.values())
    # 全部事件里 shed/restore 配对后，状态与降载集合一致
    expected_total = sum(w for d, w in powers.items() if d not in shed)
    assert total == expected_total


def test_concurrent_duplicate_report_id(store, controller):
    _setup_site(store, budgets=False)
    point = make_point("ac", T0, 400.0, "same-id")
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        ok = store.insert_point(point)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pts = store.points_in_window(["ac"], T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    log_case("8 线程同 report_id 并发", "相同点 x8",
             "幂等集合在锁内检查并写入", {"inserted": sum(results), "points": len(pts)})
    assert sum(results) == 1 and len(pts) == 1
