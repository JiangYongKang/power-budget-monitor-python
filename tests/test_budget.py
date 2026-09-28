"""预算限额、分级降载与滞回恢复（均以原子采集帧提交，断言齐帧结论）。"""
from __future__ import annotations

from datetime import timedelta

from conftest import T0


def _payload(when, dev, power_w, idem, unit="W"):
    return {
        "idempotency_key": idem, "device_id": dev, "ts": when.isoformat(),
        "readings": [{"metric": "power", "unit": unit, "value": power_w}],
    }


def _frame(svc, logger, minute, values):
    """原子提交同一测量时刻的一帧，返回该帧的预算判定。"""
    when = T0 + timedelta(minutes=minute)
    svc.clock.set(when)
    payloads = [
        _payload(when, dev, val, f"b{minute:02d}-{dev}", unit)
        for dev, (val, unit) in values.items()
    ]
    results = svc.ingest_frame(payloads, "admin-key")
    # 日志：输入 -> 判定依据 -> 最终状态
    accepted = [r for r in results if r.accepted and not r.duplicate]
    decision = accepted[-1].decision if accepted else None
    logger.info(
        "[预算帧 00:0%d] INPUT=%s -> basis=%s | final_shed=%s measured=%sW",
        minute, {d: v for d, v in values.items()},
        decision.basis if decision else "-",
        list(decision.final_shed) if decision else "-",
        decision.measured_power_w if decision else "-",
    )
    return decision


def test_no_shed_when_at_or_under_limit(make_service, logger):
    svc, _ = make_service(sample_max_age_s=300)
    d = _frame(svc, logger, 0, {"fixed": (600, "W"),
                                "load-1": (200, "W"), "load-2": (200, "W")})  # 1000 整
    assert not d.shed_triggered and d.final_shed == ()
    assert d.measured_power_w == 1000.0


def test_shedding_in_priority_order_and_limit_not_breached(make_service, logger):
    svc, _ = make_service(sample_max_age_s=300)
    # 600+450+250+200=1500 -> 切 load-1(450)=1050 仍超 -> 切 load-2(250)=800
    d = _frame(svc, logger, 0, {"fixed": (600, "W"), "load-1": (450, "W"),
                                "load-2": (250, "W"), "load-3": (200, "W")})
    assert d.newly_shed == ("load-1", "load-2"), d.basis
    assert d.measured_power_w == 800.0
    assert svc.store.get_shed_state("site-a").shed_device_ids == ["load-1", "load-2"]


def test_already_shed_not_shed_again(make_service, logger):
    svc, _ = make_service(sample_max_age_s=300)
    _frame(svc, logger, 0, {"fixed": (600, "W"), "load-1": (450, "W"),
                            "load-2": (250, "W"), "load-3": (200, "W")})
    # 下一帧：被切设备下线，在线 fixed+load-3=800；不重复降载
    d = _frame(svc, logger, 1, {"fixed": (600, "W"), "load-3": (200, "W")})
    assert d.newly_shed == ()
    assert set(d.final_shed) == {"load-1", "load-2"}


def test_hysteresis_holds_then_restores_in_reverse_order(make_service, logger):
    svc, _ = make_service(sample_max_age_s=300)
    _frame(svc, logger, 0, {"fixed": (600, "W"), "load-1": (450, "W"),
                            "load-2": (250, "W"), "load-3": (200, "W")})
    assert svc.store.get_shed_state("site-a").shed_device_ids == ["load-1", "load-2"]

    # 00:05 临界：恢复 load-2 试算 950 不严格 < 950 -> 保持
    d = _frame(svc, logger, 1, {"fixed": (600, "W"), "load-3": (100, "W")})
    assert d.restored == () and set(d.final_shed) == {"load-1", "load-2"}, d.basis

    # 00:06：load-3=50 -> 恢复 load-2（900<950）；load-1 试算 1350 保持
    d = _frame(svc, logger, 2, {"fixed": (600, "W"), "load-3": (50, "W")})
    assert d.restored == ("load-2",) and set(d.final_shed) == {"load-1"}, d.basis

    # 00:07：load-2 已重新上线，fixed=200；恢复 load-1 试算 950 临界 -> 保持
    d = _frame(svc, logger, 3, {"fixed": (200, "W"), "load-2": (250, "W"),
                                "load-3": (50, "W")})
    assert d.restored == () and set(d.final_shed) == {"load-1"}, d.basis

    # 00:08：fixed=150 -> 恢复 load-1 试算 900<950 -> 全恢复
    d = _frame(svc, logger, 4, {"fixed": (150, "W"), "load-2": (250, "W"),
                                "load-3": (50, "W")})
    assert d.restored == ("load-1",) and d.final_shed == (), d.basis


def test_no_chatter_near_threshold(make_service, logger):
    """在线负荷在阈值附近抖动，被切负载不得来回恢复/切除。"""
    svc, _ = make_service(sample_max_age_s=300)
    _frame(svc, logger, 0, {"fixed": (600, "W"), "load-1": (450, "W"),
                            "load-2": (250, "W"), "load-3": (200, "W")})
    for minute, l3 in [(1, 200), (2, 190), (3, 200), (4, 185)]:
        d = _frame(svc, logger, minute, {"fixed": (600, "W"), "load-3": (l3, "W")})
        assert d.restored == (), d.basis
    assert set(svc.store.get_shed_state("site-a").shed_device_ids) == {"load-1", "load-2"}


def test_over_budget_remains_when_no_interruptible_powered(make_service, logger):
    """不可中断固定负载自己就超限：没有可中断的实时负载可切，over_budget 保持 True。"""
    svc, _ = make_service(sample_max_age_s=300)
    d = _frame(svc, logger, 0, {"fixed": (1500, "W")})
    # 可中断负载本帧缺数据（不上报），不被空转降载；超限无法消除
    assert d.over_budget and d.newly_shed == () and d.measured_power_w == 1500, d.basis


def test_kw_unit_uses_si_in_budget(make_service, logger):
    svc, _ = make_service(sample_max_age_s=300)
    d = _frame(svc, logger, 0, {"fixed": (0.6, "kW"), "load-1": (0.9, "kW")})
    # 1500W -> 切 load-1（基线0） -> 600W
    assert d.newly_shed == ("load-1",) and d.measured_power_w == 600.0, d.basis
