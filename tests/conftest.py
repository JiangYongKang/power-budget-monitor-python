"""测试公共装置：日志中打印输入数据、判定依据和最终状态。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from app.api import create_app
from app.budget import BudgetController
from app.models import BudgetConfig, Device, LoadClass, TelemetryPoint
from app.store import Store

logger = logging.getLogger("power_budget_tests")
logger.setLevel(logging.INFO)

T0 = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
TOKEN = "secret-token"


@pytest.fixture()
def store() -> Store:
    return Store()


@pytest.fixture()
def controller(store: Store) -> BudgetController:
    return BudgetController(store)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(create_app())


def make_device(device_id: str, site: str = "s1", circuit: str = "c1",
                load_class: LoadClass = LoadClass.INTERRUPTIBLE,
                priority: int = 0, token: str = TOKEN) -> Device:
    return Device(device_id, site, circuit, load_class, priority, token)


def make_point(device_id: str, ts: datetime, power_w: float, report_id: str) -> TelemetryPoint:
    return TelemetryPoint(device_id, ts, power_w, None, None, report_id)


def log_case(title: str, inputs, basis, final) -> None:
    """统一打印：输入数据 / 判定依据 / 最终状态，便于定位问题类别。"""
    logger.info("\n=== %s ===\n输入数据: %s\n判定依据: %s\n最终状态: %s",
                title, inputs, basis, final)
