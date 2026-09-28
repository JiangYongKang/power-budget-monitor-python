"""本地存储：设备、预算配置、遥测点（幂等去重、迟到归属）。

约定：
- 幂等键为 (device_id, report_id)，重复上报直接丢弃，不影响统计。
- 迟到/乱序数据按自身时间戳插入有序序列，归属到其真实时间窗，
  不按到达顺序归属。
"""
from __future__ import annotations

import bisect
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
        with self._lock:
            self._devices[device.device_id] = device
            self._points.setdefault(device.device_id, [])

    def get_device(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(device_id)

    def devices_in_scope(self, scope: str, scope_id: str) -> list[Device]:
        with self._lock:
            if scope == "site":
                return [d for d in self._devices.values() if d.site_id == scope_id]
            return [d for d in self._devices.values() if d.circuit_id == scope_id]

    def set_budget(self, config: BudgetConfig) -> None:
        with self._lock:
            self._budgets[(config.scope, config.scope_id)] = config

    def get_budget(self, scope: str, scope_id: str) -> BudgetConfig | None:
        with self._lock:
            return self._budgets.get((scope, scope_id))

    def insert_point(self, point: TelemetryPoint) -> bool:
        """幂等插入；重复 report_id 返回 False。按时间戳有序插入。"""
        key = (point.device_id, point.report_id)
        with self._lock:
            if key in self._seen_report_ids:
                return False
            self._seen_report_ids.add(key)
            series = self._points.setdefault(point.device_id, [])
            # 相同时间戳按到达顺序排在后面：迟到/同时刻数据里以后到者为“最新”。
            idx = bisect.bisect_right([p.timestamp for p in series], point.timestamp)
            series.insert(idx, point)
            return True

    def points_in_window(self, device_ids: list[str], start: datetime, end: datetime) -> list[TelemetryPoint]:
        """返回 [start, end) 内所有设备的遥测点，按时间戳排序。"""
        with self._lock:
            out: list[TelemetryPoint] = []
            for did in device_ids:
                for p in self._points.get(did, []):
                    if start <= p.timestamp < end:
                        out.append(p)
            out.sort(key=lambda p: p.timestamp)
            return out

    def latest_point(self, device_id: str, before: datetime | None = None) -> TelemetryPoint | None:
        with self._lock:
            series = self._points.get(device_id, [])
            for p in reversed(series):
                if before is None or p.timestamp <= before:
                    return p
            return None
