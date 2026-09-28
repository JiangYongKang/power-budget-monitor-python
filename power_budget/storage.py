"""本地内存存储：幂等表、设备样本（按测量时刻去重/排序）、降载状态。

线程安全由一把可重入锁保证；service 层在该锁内完成
“入库 -> 预算判定 -> 降载状态更新”，保证并发结论一致。
"""
from __future__ import annotations

import threading
from bisect import bisect_left
from datetime import datetime

from .models import (
    ApiKey,
    BudgetDecision,
    Circuit,
    Device,
    IngestResult,
    Sample,
    ShedState,
    Site,
)


class Store:
    def __init__(
        self,
        sites: list[Site] | None = None,
        circuits: list[Circuit] | None = None,
        devices: list[Device] | None = None,
        api_keys: list[ApiKey] | None = None,
    ) -> None:
        self.sites: dict[str, Site] = {s.id: s for s in (sites or [])}
        self.circuits: dict[str, Circuit] = {c.id: c for c in (circuits or [])}
        self.devices: dict[str, Device] = {d.id: d for d in (devices or [])}
        self.api_keys: dict[str, ApiKey] = {k.key: k for k in (api_keys or [])}
        self.site_order: list[str] = [s.id for s in (sites or [])]

        self._samples: dict[str, list[Sample]] = {
            d.id: [] for d in (devices or [])
        }
        self._reports: dict[str, IngestResult] = {}
        self._shed: dict[str, ShedState] = {}
        self._last_decision: dict[str, BudgetDecision] = {}
        self.lock = threading.RLock()

    # --------------------------------------------------------- 目录
    def devices_of_site(self, site_id: str) -> list[Device]:
        return [d for d in self.devices.values() if d.site_id == site_id]

    def circuits_of_site(self, site_id: str) -> list[Circuit]:
        return [c for c in self.circuits.values() if c.site_id == site_id]

    # --------------------------------------------------------- 幂等表
    def get_report(self, idempotency_key: str) -> IngestResult | None:
        return self._reports.get(idempotency_key)

    def put_report(self, result: IngestResult) -> None:
        self._reports[result.idempotency_key] = result

    # --------------------------------------------------------- 样本
    def find_sample(self, device_id: str, ts: datetime) -> Sample | None:
        arr = self._samples.setdefault(device_id, [])
        idx = bisect_left(arr, ts, key=lambda s: s.ts)
        if idx < len(arr) and arr[idx].ts == ts:
            return arr[idx]
        return None

    def add_sample(self, sample: Sample) -> None:
        """写入样本并保持按测量时刻升序；同测量时刻由上层保证不会二次写入。"""
        arr = self._samples.setdefault(sample.device_id, [])
        idx = bisect_left(arr, sample.ts, key=lambda s: s.ts)
        arr.insert(idx, sample)

    def latest_samples(self, now: datetime, max_age_s: float | None) -> dict[str, Sample]:
        """每台设备最新一条样本；测量时刻早于 now-max_age_s 视为缺数据（不返回）。"""
        result: dict[str, Sample] = {}
        for device_id, arr in self._samples.items():
            if not arr:
                continue
            latest = arr[-1]
            if max_age_s is not None:
                age = (now - latest.ts).total_seconds()
                if age > max_age_s:
                    continue
            result[device_id] = latest
        return result

    def samples_at(self, ts: datetime) -> dict[str, Sample]:
        """取测量时刻“恰等于 ts”的每设备样本（同一上报帧的完整输入）。

        用于预算判定：只承认同一测量时刻齐帧的设备功率，未齐帧的设备
        不拿上一帧数据冒充本帧。
        """
        result: dict[str, Sample] = {}
        for device_id, arr in self._samples.items():
            idx = bisect_left(arr, ts, key=lambda s: s.ts)
            if idx < len(arr) and arr[idx].ts == ts:
                result[device_id] = arr[idx]
        return result

    def samples_between(
        self, device_id: str, start: datetime, end: datetime
    ) -> list[Sample]:
        """取 [start, end) 内按测量时刻升序、去重后的样本。"""
        arr = self._samples.get(device_id, [])
        lo = bisect_left(arr, start, key=lambda s: s.ts)
        hi = bisect_left(arr, end, key=lambda s: s.ts)
        return arr[lo:hi]

    def samples_for_integration(
        self, device_id: str, start: datetime, end: datetime
    ) -> list[Sample]:
        """取积分用样本：[start, end) 内样本，外加紧邻窗口前/后的各一个样本。

        外部邻接样本仅用于对窗口端点做线性插值；缺口/覆盖统计仍在 stats 内
        按窗口严格裁剪，不会把窗口外时间计入。
        """
        arr = self._samples.get(device_id, [])
        lo = bisect_left(arr, start, key=lambda s: s.ts)
        hi = bisect_left(arr, end, key=lambda s: s.ts)
        out = list(arr[lo:hi])
        if lo > 0:
            out.insert(0, arr[lo - 1])
        if hi < len(arr):
            out.append(arr[hi])
        return out

    def all_samples_snapshot(self) -> dict[str, list[Sample]]:
        return {dev: list(arr) for dev, arr in self._samples.items()}

    # --------------------------------------------------------- 降载状态
    def get_shed_state(self, site_id: str) -> ShedState:
        return self._shed.get(site_id, ShedState(site_id=site_id))

    def set_shed_state(self, state: ShedState) -> None:
        self._shed[state.site_id] = state

    def last_decision(self, site_id: str) -> BudgetDecision | None:
        return self._last_decision.get(site_id)

    def set_last_decision(self, decision: BudgetDecision) -> None:
        self._last_decision[decision.site_id] = decision
