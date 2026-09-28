"""功率限额与分级降载：超限依次关闭低优先级可中断负载，恢复需滞回。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import BudgetDecision, Circuit, Device, Sample, ShedState, Site


@dataclass(frozen=True)
class BudgetConfig:
    """一次预算判定所需的静态/动态上下文。"""

    site: Site
    circuits: list[Circuit]
    devices: list[Device]
    # 当前上报“测量时刻”这一帧的完整设备样本；未齐帧/无本帧数据的设备不在其中。
    latest: dict[str, Sample]
    shed: ShedState
    evaluated_at: datetime
    sample_max_age_s: float = 300.0
    shed_power_basis: str = "zero"     # 已降载设备计值："zero" | "rated"
    # 恢复试验时被恢复设备的计值："rated" 保守按额定（默认）|"zero"。
    restore_power_basis: str = "rated"


@dataclass(frozen=True)
class _Contrib:
    device: Device
    power_w: float
    present: bool           # 本帧是否有功率数据
    source: str             # power | v*i | shed | restore-estimate | missing


def _sample_power(sample: Sample | None) -> tuple[float, bool, str]:
    if sample is not None:
        if sample.power_w is not None:
            return sample.power_w, True, "power"
        if sample.voltage_v is not None and sample.current_a is not None:
            return sample.voltage_v * sample.current_a, True, "v*i"
    return 0.0, False, "missing"


def contribution(device: Device, sample: Sample | None, is_shed: bool, cfg: BudgetConfig) -> _Contrib:
    """单个设备对总功率的有效贡献。

    - 已降载设备按切除基线计（默认 0），不采用任何旧功率样本；
    - 未降载设备只认当前帧样本，无本帧数据=缺数据（计 0 且显式标记）。
    """
    if is_shed:
        if cfg.shed_power_basis == "rated" and device.rated_power_w is not None:
            return _Contrib(device, device.rated_power_w, True, "shed")
        return _Contrib(device, device.baseline_when_shed_w, True, "shed")
    power, present, source = _sample_power(sample)
    return _Contrib(device, power, present, source)


def _candidate_order(devices: list[Device]) -> list[Device]:
    return sorted((d for d in devices if d.interruptible),
                  key=lambda d: (d.shed_priority, d.id))


def _total(devices: list[Device], shed_set: set[str], cfg: BudgetConfig,
           trial_restore: frozenset[str] = frozenset()) -> tuple[float, list[_Contrib]]:
    contribs: list[_Contrib] = []
    for d in devices:
        if d.id in trial_restore:
            if cfg.restore_power_basis == "rated" and d.rated_power_w is not None:
                contribs.append(_Contrib(d, d.rated_power_w, True, "restore-estimate"))
            else:
                contribs.append(_Contrib(d, d.baseline_when_shed_w, True, "restore-estimate"))
        else:
            contribs.append(contribution(d, cfg.latest.get(d.id), d.id in shed_set, cfg))
    return sum(c.power_w for c in contribs), contribs


def _frame_target_shed(cfg, candidates, frame_total, limit):
    """据当前帧功率求“满足 <= limit 的最小目标降载集”（只切有本帧功率者）。"""
    target: set[str] = set()
    steps: list[str] = []
    total = frame_total
    for dev in candidates:
        if total <= limit:
            break
        _, present, _ = _sample_power(cfg.latest.get(dev.id))
        if not present:
            continue
        target.add(dev.id)
        total, _ = _total(cfg.devices, target, cfg)
        steps.append(f"目标降载 {dev.id}(prio={dev.shed_priority})：降后 {total:g}W")
    return target, steps, total


def evaluate(cfg: BudgetConfig) -> BudgetDecision:
    """基于“当前上报测量时刻这一帧”的完整数据执行预算判定（在存储锁内调用）。

    状态机（对并发/齐帧上报结论一致，同一负载不会被重复降载）：
    1. 计算当前帧总功率 frame_total（缺数据设备计 0 并在依据中显式列出）；
    2. 求满足 <= limit 的最小目标降载集 target（按优先级/id 稳定排序切除）；
    3. 与上一降载状态比较做迁移，迁移遵循滞回：
       - frame_total > limit：本帧只降载不恢复；
       - frame_total <= limit：被切负载仅在“恢复试算严格 < limit*ratio”时恢复，
         等于阈值不恢复（临界不抖动），按降载逆序逐级试算。
    """
    limit = cfg.site.limit_w if cfg.site.limit_w is not None else float("inf")
    restore_threshold = limit * cfg.site.restore_ratio
    candidates = _candidate_order(cfg.devices)

    previous = set(cfg.shed.shed_device_ids)
    already_shed = tuple(cfg.shed.shed_device_ids)
    basis: list[str] = [
        f"site={cfg.site.id} limit={limit:g}W 恢复需严格<{restore_threshold:g}W "
        f"(ratio={cfg.site.restore_ratio})"
    ]

    frame_total, contribs = _total(cfg.devices, set(), cfg)
    missing = sorted(c.device.id for c in contribs if not c.present)
    if missing:
        basis.append("本帧缺数据不计入: " + ",".join(missing))

    target, target_steps, post_target = _frame_target_shed(
        cfg, candidates, frame_total, limit
    )

    final: set[str] = set(previous)
    newly_shed: list[str] = []
    restored: list[str] = []

    if frame_total > limit:
        # 路径 A：当前帧超限 -> 只降载不恢复
        basis.append(f"当前帧 {frame_total:g}W > {limit:g}W，超限只降载不恢复")
        for dev in candidates:
            if dev.id in target and dev.id not in final:
                final.add(dev.id)
                newly_shed.append(dev.id)
        basis.extend(target_steps)
    else:
        # 路径 B：当前帧未超限 -> 滞回恢复（逆序，严格低于阈值）
        for dev in list(reversed(candidates)):
            if dev.id not in previous:
                continue
            if dev.id in target:
                basis.append(f"保持降载 {dev.id}：当前帧仍需其切除以满足限额")
                continue
            trial_total, _ = _total(cfg.devices, final, cfg,
                                    trial_restore=frozenset({dev.id}))
            if trial_total < restore_threshold:
                final.remove(dev.id)
                restored.append(dev.id)
                basis.append(f"恢复 {dev.id}：试算 {trial_total:g}W < {restore_threshold:g}W")
            else:
                basis.append(f"保持降载 {dev.id}：恢复试算 {trial_total:g}W >= {restore_threshold:g}W")
        # 理论上 frame_total<=limit 时 target 为空；防御性补齐
        for dev in candidates:
            if dev.id in target and dev.id not in final:
                final.add(dev.id)
                newly_shed.append(dev.id)

    total, _ = _total(cfg.devices, final, cfg)
    basis.append(f"最终降载集合={[d.id for d in candidates if d.id in final]} 计入功率={total:g}W")

    final_shed = [d.id for d in candidates if d.id in final]
    return BudgetDecision(
        site_id=cfg.site.id,
        evaluated_at=cfg.evaluated_at,
        scope="site",
        limit_w=limit,
        restore_threshold_w=restore_threshold,
        measured_power_w=total,
        over_budget=total > limit,
        shed_triggered=bool(newly_shed),
        newly_shed=tuple(newly_shed),
        already_shed=already_shed,
        restored=tuple(restored),
        final_shed=tuple(final_shed),
        basis="；".join(basis),
    )
