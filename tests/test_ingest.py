"""上报流水线：幂等、重复、迟到/超前、冲突、权限、单位换算与功率推导。"""
from __future__ import annotations

from datetime import timedelta

from conftest import T0, log_case

from power_budget.models import RejectReason


def _payload(dev="fixed", ts=T0, value=600.0, unit="W", metric="power", idem="k"):
    return {
        "idempotency_key": idem,
        "device_id": dev,
        "ts": ts.isoformat(),
        "readings": [{"metric": metric, "unit": unit, "value": value}],
    }


def test_first_accept_then_idempotent_replay(make_service, logger):
    svc, clock = make_service(late_grace_seconds=900, future_tolerance_seconds=5)
    payload = _payload(idem="i1")
    r1 = svc.ingest(payload, "admin-key")
    log_case(logger, "首次上报", payload, r1)
    assert r1.accepted and not r1.duplicate and r1.sample.power_w == 600.0

    r2 = svc.ingest(payload, "admin-key")
    log_case(logger, "同键重放", payload, r2)
    assert r2.duplicate and r2.accepted
    assert r2.sample is r1.sample and r2.decision is r1.decision
    # 存储里只有一条样本
    assert len(svc.store.samples_between("fixed", T0, T0 + timedelta(hours=1))) == 1


def test_same_ts_same_value_new_key_is_dedup_not_counted(make_service, logger):
    svc, clock = make_service()
    r1 = svc.ingest(_payload(idem="a"), "admin-key")
    r2 = svc.ingest(_payload(idem="b"), "admin-key")  # 不同幂等键、同刻同值
    log_case(logger, "同刻同值新键", _payload(idem="b"), r2)
    assert r2.duplicate and r2.sample is r1.sample
    assert len(svc.store.samples_between("fixed", T0, T0 + timedelta(hours=1))) == 1


def test_same_ts_conflicting_value_rejected(make_service, logger):
    svc, clock = make_service()
    svc.ingest(_payload(value=600, idem="a"), "admin-key")
    conflict = _payload(value=601, idem="c")
    r = svc.ingest(conflict, "admin-key")
    log_case(logger, "同刻冲突", conflict, r)
    assert not r.accepted and r.reason is RejectReason.CONFLICTING_SAMPLE
    # 冲突不得新增样本
    assert len(svc.store.samples_between("fixed", T0, T0 + timedelta(hours=1))) == 1


def test_out_of_order_samples_are_ordered_and_stable(make_service, logger):
    # 宽限 10 分钟，允许明显的乱序迟到
    svc, clock = make_service(late_grace_seconds=600, future_tolerance_seconds=5)
    # 时钟在 00:05，先收到测量时刻 00:05 的样本
    clock.set(T0 + timedelta(seconds=300))
    p_late = _payload(ts=T0 + timedelta(seconds=300), value=300, idem="late")
    assert svc.ingest(p_late, "admin-key").accepted
    # 随后收到测量时刻 00:01 的迟到样本（age=240s < 600s），乱序但归属 00:01
    p_mid = _payload(ts=T0 + timedelta(seconds=60), value=100, idem="mid")
    r = svc.ingest(p_mid, "admin-key")
    log_case(logger, "乱序迟到", p_mid, r)
    assert r.accepted
    arr = svc.store.samples_between("fixed", T0, T0 + timedelta(hours=1))
    assert [s.ts for s in arr] == sorted(s.ts for s in arr)
    assert [s.power_w for s in arr] == [100.0, 300.0]
    # 重复查询结果稳定
    q1 = [s.power_w for s in svc.store.samples_between("fixed", T0, T0 + timedelta(hours=1))]
    q2 = [s.power_w for s in svc.store.samples_between("fixed", T0, T0 + timedelta(hours=1))]
    assert q1 == q2


def test_late_beyond_grace_rejected(make_service, logger):
    svc, clock = make_service(late_grace_seconds=900)
    payload = _payload(ts=T0 - timedelta(seconds=901), idem="old")
    r = svc.ingest(payload, "admin-key")
    log_case(logger, "超宽限迟到", payload, r)
    assert not r.accepted and r.reason is RejectReason.STALE_LATE
    assert svc.store.all_samples_snapshot()["fixed"] == []


def test_late_within_grace_accepted(make_service, logger):
    svc, clock = make_service(late_grace_seconds=900)
    payload = _payload(ts=T0 - timedelta(seconds=899), idem="late-ok")
    r = svc.ingest(payload, "admin-key")
    log_case(logger, "宽限内迟到", payload, r)
    assert r.accepted


def test_future_timestamp_beyond_tolerance_rejected(make_service, logger):
    svc, clock = make_service(future_tolerance_seconds=5)
    payload = _payload(ts=T0 + timedelta(seconds=60), idem="future")
    r = svc.ingest(payload, "admin-key")
    log_case(logger, "超前数据", payload, r)
    assert not r.accepted and r.reason is RejectReason.STALE_LATE


def test_rejected_report_is_persisted_and_replays_as_rejected(make_service, logger):
    svc, clock = make_service()
    bad = _payload(value=-1, idem="bad")
    r1 = svc.ingest(bad, "admin-key")
    assert not r1.accepted and r1.reason is RejectReason.VALUE_OUT_OF_RANGE
    # 用相同幂等键重试：仍拒绝（重放），不会变成成功
    r2 = svc.ingest(bad, "admin-key")
    log_case(logger, "拒绝重放", bad, r2)
    assert not r2.accepted and r2.duplicate and r2.reason is RejectReason.VALUE_OUT_OF_RANGE


def test_permissions(make_service, logger):
    svc, clock = make_service()
    # 设备白名单 key 越权写其他设备
    p = _payload(dev="load-1", idem="cross")
    r = svc.ingest(p, "dev-fixed-key")
    log_case(logger, "越权", p, r)
    assert not r.accepted and r.reason is RejectReason.UNAUTHORIZED
    # 缺 key
    r = svc.ingest(_payload(idem="nokey"), None)
    assert r.reason is RejectReason.UNAUTHORIZED
    # 未知 key
    r = svc.ingest(_payload(idem="badkey"), "nope")
    assert r.reason is RejectReason.UNAUTHORIZED
    # 站点 key 可写本站点
    r = svc.ingest(_payload(dev="load-3", value=10, idem="siteok"), "site-a-key")
    assert r.accepted
    # 全局 key 可写任意设备
    r = svc.ingest(_payload(dev="load-1", value=10, idem="adm"), "admin-key")
    assert r.accepted


def test_unknown_device(make_service, logger):
    svc, clock = make_service()
    payload = _payload(dev="ghost", idem="g")
    r = svc.ingest(payload, "admin-key")
    log_case(logger, "未知设备", payload, r)
    assert not r.accepted and r.reason is RejectReason.UNKNOWN_DEVICE


def test_missing_idempotency_key_not_persisted(make_service):
    svc, clock = make_service()
    payload = {
        "device_id": "fixed",
        "ts": T0.isoformat(),
        "readings": [{"metric": "power", "unit": "W", "value": 1}],
    }
    r = svc.ingest(payload, "admin-key")
    assert not r.accepted and r.reason is RejectReason.MISSING_FIELD
    assert r.idempotency_key == ""


def test_readings_shape_and_power_derivation(make_service, logger):
    svc, clock = make_service()
    base = {"idempotency_key": "vi", "device_id": "fixed", "ts": T0.isoformat()}
    # 只有电压没有电流/功率 -> 缺字段
    r = svc.ingest({**base, "readings": [{"metric": "voltage", "unit": "V", "value": 10}]},
                   "admin-key")
    assert r.reason is RejectReason.MISSING_FIELD
    # 空 readings
    r = svc.ingest({**base, "idempotency_key": "empty", "readings": []}, "admin-key")
    assert r.reason is RejectReason.MISSING_FIELD
    # V*I 推导功率
    vi = {**base, "idempotency_key": "vi2",
          "readings": [{"metric": "voltage", "unit": "V", "value": 10},
                       {"metric": "current", "unit": "A", "value": 12}]}
    r = svc.ingest(vi, "admin-key")
    log_case(logger, "V*I推导", vi, r)
    assert r.accepted and r.sample.power_w is None
    assert r.sample.voltage_v == 10 and r.sample.current_a == 12
    # 同一指标重复出现 -> 拒绝
    dup_metric = {**base, "idempotency_key": "dupm",
                  "readings": [{"metric": "power", "unit": "W", "value": 1},
                               {"metric": "power", "unit": "kW", "value": 2}]}
    r = svc.ingest(dup_metric, "admin-key")
    assert not r.accepted and r.reason is RejectReason.MISSING_FIELD


def test_unit_conversion_on_ingest(make_service, logger):
    svc, clock = make_service()
    payload = _payload(value=0.45, unit="kW", idem="kw")
    r = svc.ingest(payload, "admin-key")
    log_case(logger, "kW换算", payload, r)
    assert r.accepted and r.sample.power_w == 450.0
