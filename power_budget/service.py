"""门面服务：在单把锁内完成 鉴权 -> 校验 -> 幂等 -> 入库 -> 预算判定，
以及统计查询。所有并发上报因此串行化，预算结论一致、不会击穿。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timezone

from .budget import BudgetConfig, evaluate
from .clock import Clock, SystemClock
from .models import (
    BudgetDecision,
    Device,
    EnergyResult,
    IngestResult,
    Metric,
    RejectReason,
    Sample,
    WindowAlign,
)
from .permissions import AuthError, authorize, resolve_api_key
from .stats import energy as _energy
from .stats import power_curve
from .storage import Store
from .validation import ValidationError, parse_reading, parse_timestamp, require_str


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


class PowerService:
    def __init__(
        self,
        store: Store,
        clock: Clock | None = None,
        *,
        late_grace_seconds: float = 900.0,
        future_tolerance_seconds: float = 5.0,
        sample_max_age_s: float = 300.0,
        stats_gap_seconds: float = 300.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self.late_grace_seconds = late_grace_seconds
        self.future_tolerance_seconds = future_tolerance_seconds
        self.sample_max_age_s = sample_max_age_s
        self.stats_gap_seconds = stats_gap_seconds
        self.log = logger or logging.getLogger("power_budget")

    # ============================================================ 上报
    def ingest(self, payload: dict, api_key: str | None, client_id: str = "anon") -> IngestResult:
        """单设备上报：锁内完成全流程并立即做一次预算判定。"""
        now = _as_utc(self.clock.now())
        with self.store.lock:
            return self._ingest_one(payload, api_key, client_id, now, evaluate=True)

    def ingest_frame(self, payloads: list[dict], api_key: str | None,
                     client_id: str = "anon") -> list[IngestResult]:
        """原子提交“同一测量时刻”的一批设备上报（网关一个采集周期成批到达）。

        - 全部条目在同一把锁内依次 鉴权/校验/幂等/入库；
        - 所有新增样本入库后只做一次预算判定，结论写回每条结果 ——
          多设备齐帧时预算结论唯一，不会被帧内中间态击穿或重复降载；
        - 逐条结果可区分：某条被拒绝不影响其他条目，拒绝条目不入库。
        """
        now = _as_utc(self.clock.now())
        results: list[IngestResult] = []
        with self.store.lock:
            frame_site_ts: dict[str, datetime] = {}
            for payload in payloads:
                r = self._ingest_one(payload, api_key, client_id, now, evaluate=False)
                results.append(r)
                if r.accepted and not r.duplicate and r.device_id and r.sample is not None:
                    frame_site_ts[self.store.devices[r.device_id].site_id] = r.sample.ts

            decisions = {
                site_id: self._evaluate_site(site_id, fts)
                for site_id, fts in frame_site_ts.items()
            }
            for idx, r in enumerate(results):
                if r.accepted and not r.duplicate and r.device_id:
                    site_id = self.store.devices[r.device_id].site_id
                    dec = decisions.get(site_id)
                    if dec is None:
                        continue
                    # 用齐帧判定回填，并同步幂等表，保证后续重放结论一致
                    r = IngestResult(
                        idempotency_key=r.idempotency_key, device_id=r.device_id,
                        accepted=True, duplicate=False, reason=None, detail=r.detail,
                        ts=r.ts, ingest_ts=r.ingest_ts, sample=r.sample, decision=dec,
                    )
                    results[idx] = r
                    self.store.put_report(r)
        return results

    # ----------------------------------------------------- 单条处理
    def _ingest_one(self, payload: dict, api_key: str | None, client_id: str,
                    now: datetime, *, evaluate: bool) -> IngestResult:
        if not isinstance(payload, dict):
            payload = {}

        # 0. 幂等键
        try:
            idem = require_str(payload, "idempotency_key")
        except ValidationError as ve:
            return self._reject(None, None, now, ve, payload, client_id)

        existing = self.store.get_report(idem)
        if existing is not None:
            dup = IngestResult(
                idempotency_key=existing.idempotency_key, device_id=existing.device_id,
                accepted=existing.accepted, duplicate=True, reason=existing.reason,
                detail="幂等重放：" + existing.detail, ts=existing.ts,
                ingest_ts=existing.ingest_ts, sample=existing.sample,
                decision=existing.decision,
            )
            self.log.info("ingest duplicate idem=%s -> 原样返回首次结论", idem)
            return dup

        # 1. 鉴权
        try:
            device_id = require_str(payload, "device_id")
            device = self.store.devices.get(device_id)
            if device is None:
                raise ValidationError(
                    RejectReason.UNKNOWN_DEVICE, f"未知设备: {device_id}", "device_id"
                )
            principal = resolve_api_key(self.store.api_keys, api_key)
            authorize(principal, device)
        except ValidationError as ve:
            return self._reject(idem, None, now, ve, payload, client_id)
        except AuthError as ae:
            return self._reject(idem, device_id, now,
                                ValidationError(ae.reason, ae.detail), payload, client_id)

        # 2. 字段/单位/数值
        try:
            ts = parse_timestamp(payload.get("ts"))
            raw_readings = payload.get("readings")
            if not isinstance(raw_readings, list) or not raw_readings:
                raise ValidationError(
                    RejectReason.MISSING_FIELD, "readings 必须是非空数组", "readings"
                )
            power_w = voltage_v = current_a = None
            seen: set[Metric] = set()
            for raw in raw_readings:
                reading = parse_reading(raw)
                if reading.metric in seen:
                    raise ValidationError(
                        RejectReason.MISSING_FIELD,
                        f"同一指标重复出现: {reading.metric.value}", "readings",
                    )
                seen.add(reading.metric)
                if reading.metric is Metric.POWER:
                    power_w = reading.si_value
                elif reading.metric is Metric.VOLTAGE:
                    voltage_v = reading.si_value
                else:
                    current_a = reading.si_value
            if power_w is None and not (voltage_v is not None and current_a is not None):
                raise ValidationError(
                    RejectReason.MISSING_FIELD,
                    "必须提供 power，或同时提供 voltage 与 current 以推导功率", "readings",
                )
        except ValidationError as ve:
            return self._reject(idem, device_id, now, ve, payload, client_id)

        # 3. 迟到 / 超前
        age = (now - ts).total_seconds()
        if age > self.late_grace_seconds:
            return self._reject(
                idem, device_id, now,
                ValidationError(
                    RejectReason.STALE_LATE,
                    f"测量时刻 {ts.isoformat()} 早于当前 {now.isoformat()} 超过 "
                    f"{self.late_grace_seconds:g}s 宽限窗口（age={age:g}s）", "ts",
                ), payload, client_id, ts=ts,
            )
        if age < -self.future_tolerance_seconds:
            return self._reject(
                idem, device_id, now,
                ValidationError(
                    RejectReason.STALE_LATE,
                    f"测量时刻 {ts.isoformat()} 超前当前 {now.isoformat()} 超过 "
                    f"{self.future_tolerance_seconds:g}s（age={age:g}s）", "ts",
                ), payload, client_id, ts=ts,
            )

        # 4. 同设备同测量时刻冲突/幂等
        prior = self.store.find_sample(device_id, ts)
        if prior is not None:
            same = (prior.power_w == power_w and prior.voltage_v == voltage_v
                    and prior.current_a == current_a)
            if not same:
                return self._reject(
                    idem, device_id, now,
                    ValidationError(
                        RejectReason.CONFLICTING_SAMPLE,
                        f"设备 {device_id} 在 {ts.isoformat()} 已有不同样本", "ts",
                    ), payload, client_id, ts=ts,
                )
            result = IngestResult(
                idempotency_key=idem, device_id=device_id, accepted=True, duplicate=True,
                reason=None, detail="同测量时刻相同样本：按首次样本幂等返回，不重复计数",
                ts=ts, ingest_ts=now, sample=prior,
                decision=self.store.last_decision(device.site_id),
            )
            self.store.put_report(result)
            return result

        # 5. 入库 +（可选）判定
        sample = Sample(
            device_id=device_id, ts=ts, power_w=power_w,
            voltage_v=voltage_v, current_a=current_a, ingest_ts=now,
        )
        self.store.add_sample(sample)
        decision = self._evaluate_site(device.site_id, ts) if evaluate else None
        result = IngestResult(
            idempotency_key=idem, device_id=device_id, accepted=True, duplicate=False,
            reason=None, detail="accepted", ts=ts, ingest_ts=now, sample=sample,
            decision=decision,
        )
        self.store.put_report(result)
        self.log.info(
            "ingest accepted idem=%s device=%s ts=%s p=%s V=%s I=%s | %s",
            idem, device_id, ts.isoformat(), power_w, voltage_v, current_a,
            decision.basis if decision else "批量帧：判定延后至整帧",
        )
        return result

    def _reject(self, idem, device_id, now, ve: ValidationError, payload, client_id,
                *, ts=None) -> IngestResult:
        result = IngestResult(
            idempotency_key=idem or "", device_id=device_id, accepted=False,
            duplicate=False, reason=ve.reason, detail=ve.detail,
            ts=ts, ingest_ts=now,
        )
        if idem:
            self.store.put_report(result)  # 拒绝也固化：重试不会“变成功”
        self.log.warning(
            "ingest REJECT idem=%s device=%s client=%s reason=%s detail=%s payload=%s",
            idem, device_id, client_id, ve.reason.value, ve.detail, payload,
        )
        return result

    def _evaluate_site(self, site_id: str, frame_ts: datetime) -> BudgetDecision | None:
        """以“测量时刻 frame_ts 这一帧”的样本做一次预算判定（只认该帧，不串上一帧）。"""
        site = self.store.sites.get(site_id)
        if site is None:
            return None
        cfg = BudgetConfig(
            site=site,
            circuits=self.store.circuits_of_site(site_id),
            devices=self.store.devices_of_site(site_id),
            latest=self.store.samples_at(frame_ts),
            shed=self.store.get_shed_state(site_id),
            evaluated_at=frame_ts,
            sample_max_age_s=self.sample_max_age_s,
        )
        decision = evaluate(cfg)
        from .models import ShedState
        self.store.set_shed_state(
            ShedState(site_id=site_id, shed_device_ids=list(decision.final_shed),
                      updated_at=frame_ts)
        )
        self.store.set_last_decision(decision)
        return decision

    # ============================================================ 查询
    def _device(self, device_id: str) -> Device:
        device = self.store.devices.get(device_id)
        if device is None:
            raise KeyError(f"未知设备: {device_id}")
        return device

    def _site_series(self, site_id: str, start: datetime, end: datetime) -> list[Sample]:
        """站点内各设备样本按测量时刻对齐求和（缺失设备按缺数据，不补零）。"""
        if site_id not in self.store.sites:
            raise KeyError(f"未知站点: {site_id}")
        per_ts: dict[datetime, list[float]] = {}
        for device in self.store.devices_of_site(site_id):
            for s in self.store.samples_between(device.id, start, end):
                p = s.power_w
                if p is None and s.voltage_v is not None and s.current_a is not None:
                    p = s.voltage_v * s.current_a
                if p is None:
                    continue
                per_ts.setdefault(s.ts, []).append(p)
        return [
            Sample(device_id=f"@{site_id}", ts=t, power_w=sum(ps),
                   voltage_v=None, current_a=None, ingest_ts=t)
            for t, ps in sorted(per_ts.items())
        ]

    def site_power_curve(self, site_id, start, end, align=WindowAlign.MINUTE):
        start, end = _as_utc(start), _as_utc(end)
        with self.store.lock:
            merged = self._site_series(site_id, start, end)
        return power_curve(merged, start, end, align)

    def device_energy(self, device_id, start, end) -> EnergyResult:
        start, end = _as_utc(start), _as_utc(end)
        with self.store.lock:
            self._device(device_id)
            samples = self.store.samples_for_integration(device_id, start, end)
        return _energy(samples, start, end, self.stats_gap_seconds)

    def site_energy(self, site_id, start, end) -> EnergyResult:
        """逐设备梯形积分后求和；任一设备窗口内有缺口则 complete=False。"""
        start, end = _as_utc(start), _as_utc(end)
        total_window = max(0.0, (end - start).total_seconds())
        with self.store.lock:
            if site_id not in self.store.sites:
                raise KeyError(f"未知站点: {site_id}")
            per_device = [
                _energy(self.store.samples_for_integration(d.id, start, end),
                        start, end, self.stats_gap_seconds)
                for d in self.store.devices_of_site(site_id)
            ]
        # 只对“窗口内有样本”的设备求和与完整性判定；无样本设备不臆造能耗，
        # 其缺数据情况通过设备维度查询获取（站点维度不把它们静默当零，也不因此
        # 把本来完整的站点误判为不完整）。
        reported = [r for r in per_device if r.sample_count > 0]
        covered = max((r.covered_seconds for r in reported), default=0.0)
        return EnergyResult(
            window_start=start, window_end=end,
            energy_wh=sum(r.energy_wh for r in reported),
            covered_seconds=min(covered, total_window),
            gap_seconds=max(0.0, total_window - covered),
            sample_count=sum(r.sample_count for r in reported),
            complete=bool(reported) and all(r.complete for r in reported),
        )

    def snapshot(self) -> dict:
        with self.store.lock:
            return {
                "samples": self.store.all_samples_snapshot(),
                "shed": {sid: self.store.get_shed_state(sid) for sid in self.store.sites},
                "decisions": {sid: self.store.last_decision(sid) for sid in self.store.sites},
            }
