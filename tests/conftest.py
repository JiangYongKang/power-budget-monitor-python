"""pytest 共享夹具与测试日志辅助。

每个关键用例都会通过 ``log_case`` 打印：
  输入数据 -> 判定依据（decision.basis / 拒绝 reason）-> 最终状态（降载集合等），
方便定位问题属于“拒绝 / 降载 / 统计口径”中的哪一类。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timezone

import pytest

from power_budget.clock import FakeClock
from power_budget.models import ApiKey, Circuit, Device, Site
from power_budget.service import PowerService
from power_budget.storage import Store

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


@pytest.fixture
def logger() -> logging.Logger:
    logging.basicConfig(level=logging.INFO)
    return logging.getLogger("power_budget.test")


@pytest.fixture
def catalog():
    """一个站点（1000W/0.95 滞回）+ 主回路 + 固定负载与三个可中断负载。"""
    sites = [Site(id="site-a", name="A", limit_w=1000.0, restore_ratio=0.95)]
    circuits = [Circuit(id="main", site_id="site-a", name="主回路", limit_w=1000.0)]
    devices = [
        Device(id="fixed", site_id="site-a", circuit_id="main", name="固定负载",
               interruptible=False, rated_power_w=600.0),
        Device(id="load-1", site_id="site-a", circuit_id="main", name="P1",
               interruptible=True, shed_priority=10, rated_power_w=450.0),
        Device(id="load-2", site_id="site-a", circuit_id="main", name="P2",
               interruptible=True, shed_priority=20, rated_power_w=250.0),
        Device(id="load-3", site_id="site-a", circuit_id="main", name="P3",
               interruptible=True, shed_priority=30, rated_power_w=200.0),
    ]
    keys = [
        ApiKey(key="admin-key", label="全局", site_id=None),
        ApiKey(key="site-a-key", label="站点A", site_id="site-a"),
        ApiKey(key="dev-fixed-key", label="仅fixed", site_id="site-a",
               device_ids=frozenset({"fixed"})),
    ]
    return sites, circuits, devices, keys


@pytest.fixture
def make_service(catalog, logger):
    """构造一个使用 FakeClock 的服务工厂。"""
    sites, circuits, devices, keys = catalog

    def _make(start: datetime | None = T0, **kwargs):
        clock = FakeClock(start)
        store = Store(sites=sites, circuits=circuits, devices=devices, api_keys=keys)
        return PowerService(store, clock=clock, logger=logger, **kwargs), clock

    return _make


def log_case(logger: logging.Logger, title: str, payload: object, result, *, note: str = "") -> None:
    """统一打印 输入 -> 判定依据 -> 最终状态。"""
    state = None
    decision = getattr(result, "decision", None)
    if decision is not None:
        state = f"final_shed={list(decision.final_shed)} measured={decision.measured_power_w:g}W"
    logger.info(
        "[%s] INPUT=%s NOTE=%s -> accepted=%s duplicate=%s reason=%s | basis=%s | state=%s",
        title, payload, note,
        getattr(result, "accepted", None),
        getattr(result, "duplicate", None),
        getattr(getattr(result, "reason", None), "value", None),
        decision.basis if decision else "-",
        state or "-",
    )
