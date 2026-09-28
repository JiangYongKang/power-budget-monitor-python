"""本地存储：设备、预算配置、遥测点（幂等去重、迟到归属）。"""
from __future__ import annotations

import threading
from datetime import datetime

from app.models import BudgetConfig, Device, TelemetryPoint


class Store:
    """线程安全的内存存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._budgets: dict[tuple[str, str], BudgetConfig] = {}
        self._points: dict[str, list[TelemetryPoint]] = {}
        self._seen_report_ids: set[tuple[str, str]] = set()

    def add_device(self, device: Device) -> None:
        raise NotImplementedError

    def get_device(self, device_id: str) -> Device | None:
        raise NotImplementedError

    def set_budget(self, config: BudgetConfig) -> None:
        raise NotImplementedError

    def get_budget(self, scope: str, scope_id: str) -> BudgetConfig | None:
        raise NotImplementedError

    def insert_point(self, point: TelemetryPoint) -> bool:
        """幂等插入；重复 report_id 返回 False。"""
        raise NotImplementedError

    def points_in_window(self, device_ids: list[str], start: datetime, end: datetime) -> list[TelemetryPoint]:
        raise NotImplementedError
