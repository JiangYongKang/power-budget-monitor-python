"""预算判定与分级降载（含滞回），并发下结论一致。

规则：
- 当前功率 = 范围内各设备最新一条遥测功率之和；已被降载的设备按 0 计。
- 超限（total > limit_w）：按 shed_priority 升序（数值小者优先）依次降载
  可中断负载，直到预计总功率 <= limit_w；CRITICAL 负载永不降载。
- 恢复（total < restore_w）：按降载的逆序逐个恢复，前提是恢复后预计
  总功率仍 <= limit_w（不会立即再次超限）；restore_w 必须 < limit_w，
  中间为滞回死区，在死区内保持现状，避免阈值附近反复开关。
- 并发：同一 scope 的判定在锁内串行执行，降载集合的变更与事件记录
  是原子的，不会重复降载同一负载，也不会击穿预算。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.models import LoadClass, LoadState, ShedEvent
from app.store import Store


class BudgetController:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._shed: dict[tuple[str, str], list[str]] = {}  # scope -> 已降载设备（按降载顺序）
        self._events: list[ShedEvent] = []

    # ---- 内部 ----

    def _current_power(self, scope: str, scope_id: str) -> tuple[float, dict[str, float]]:
        """返回 (当前总功率, 各设备最新功率)。已降载设备按 0 计入总量。"""
        shed_set = set(self._shed.get((scope, scope_id), []))
        per_device: dict[str, float] = {}
        total = 0.0
        for dev in self._store.devices_in_scope(scope, scope_id):
            pt = self._store.latest_point(dev.device_id)
            power = pt.power_w if pt else 0.0
            per_device[dev.device_id] = power
            if dev.device_id not in shed_set:
                total += power
        return total, per_device

    # ---- 对外 ----

    def evaluate(self, scope: str, scope_id: str, at: datetime | None = None) -> list[ShedEvent]:
        """根据当前功率与预算，返回本次需要执行的降载/恢复事件。"""
        at = at or datetime.now(timezone.utc)
        with self._lock:
            budget = self._store.get_budget(scope, scope_id)
            if budget is None:
                return []
            key = (scope, scope_id)
            shed_list = self._shed.setdefault(key, [])
            total, per_device = self._current_power(scope, scope_id)
            events: list[ShedEvent] = []

            if total > budget.limit_w:
                # 分级降载：优先级数值小者先降
                candidates = sorted(
                    (d for d in self._store.devices_in_scope(scope, scope_id)
                     if d.load_class == LoadClass.INTERRUPTIBLE and d.device_id not in shed_list),
                    key=lambda d: (d.shed_priority, d.device_id),
                )
                for dev in candidates:
                    if total <= budget.limit_w:
                        break
                    shed_list.append(dev.device_id)
                    total -= per_device.get(dev.device_id, 0.0)
                    events.append(ShedEvent(dev.device_id, "shed", scope, scope_id, at,
                                            f"total>{budget.limit_w}W, priority={dev.shed_priority}"))
            elif total < budget.restore_w and shed_list:
                # 滞回恢复：按降载逆序，恢复后预计不超限才恢复
                for device_id in list(reversed(shed_list)):
                    restored_power = per_device.get(device_id, 0.0)
                    if total + restored_power > budget.limit_w:
                        break
                    shed_list.remove(device_id)
                    total += restored_power
                    events.append(ShedEvent(device_id, "restore", scope, scope_id, at,
                                            f"total<{budget.restore_w}W, hysteresis restore"))
            # 滞回死区 [restore_w, limit_w]：不动作
            self._events.extend(events)
            return events

    def load_state(self, device_id: str) -> str:
        with self._lock:
            for shed_list in self._shed.values():
                if device_id in shed_list:
                    return LoadState.SHED.value
            return LoadState.ON.value

    def events(self) -> list[ShedEvent]:
        with self._lock:
            return list(self._events)
