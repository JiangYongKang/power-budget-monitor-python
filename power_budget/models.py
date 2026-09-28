"""领域模型：枚举、数据类与跨模块共享的数据结构（仅类型与静态数据）。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime

# ---------------------------------------------------------------- 枚举与单位

class Metric(str, enum.Enum):
    """可上报的遥测量。"""

    POWER = "power"
    VOLTAGE = "voltage"
    CURRENT = "current"


class Unit(str, enum.Enum):
    """允许使用的上报单位。内部统一换算为 SI：W / V / A。"""

    WATT = "W"
    KILOWATT = "kW"
    VOLT = "V"
    MILLIVOLT = "mV"
    AMPERE = "A"
    MILLIAMPERE = "mA"


# 每种指标允许的单位集合（用于区分 “未知单位” 与 “单位/指标不匹配”）。
METRIC_UNITS: dict[Metric, frozenset[Unit]] = {
    Metric.POWER: frozenset({Unit.WATT, Unit.KILOWATT}),
    Metric.VOLTAGE: frozenset({Unit.VOLT, Unit.MILLIVOLT}),
    Metric.CURRENT: frozenset({Unit.AMPERE, Unit.MILLIAMPERE}),
}

# 单位 -> SI 换算系数：si_value = raw_value * factor。
UNIT_TO_SI: dict[Unit, float] = {
    Unit.WATT: 1.0,
    Unit.KILOWATT: 1000.0,
    Unit.VOLT: 1.0,
    Unit.MILLIVOLT: 0.001,
    Unit.AMPERE: 1.0,
    Unit.MILLIAMPERE: 0.001,
}

SI_UNIT: dict[Metric, Unit] = {
    Metric.POWER: Unit.WATT,
    Metric.VOLTAGE: Unit.VOLT,
    Metric.CURRENT: Unit.AMPERE,
}


class RejectReason(str, enum.Enum):
    """上报被拒绝的可区分原因（重复上报不是拒绝，见 ingest 结果）。"""

    MISSING_FIELD = "missing_field"            # 必填字段缺失
    BAD_TYPE = "bad_type"                      # 字段类型错误（如 value 非数字）
    NON_FINITE = "non_finite"                  # NaN / Inf
    UNKNOWN_METRIC = "unknown_metric"          # 无法识别的指标名
    UNKNOWN_UNIT = "unknown_unit"              # 无法识别的单位
    UNIT_METRIC_MISMATCH = "unit_metric_mismatch"  # 单位与指标不匹配
    VALUE_OUT_OF_RANGE = "value_out_of_range"  # 数值明显越界
    BAD_TIMESTAMP = "bad_timestamp"            # 时间戳无法解析
    UNKNOWN_DEVICE = "unknown_device"          # 设备不存在
    UNAUTHORIZED = "unauthorized"              # API Key 无该设备权限
    STALE_LATE = "stale_late"                  # 迟到数据超过宽限窗口
    CONFLICTING_SAMPLE = "conflicting_sample"  # 同设备同测量时刻不同值


class WindowAlign(str, enum.Enum):
    """功率曲线的时间窗口分桶口径。"""

    RAW = "raw"      # 不上采样，按原始样本返回
    MINUTE = "1m"
    FIVE_MIN = "5m"
    HOUR = "1h"


# ----------------------------------------------------------- 目录（配置数据）

@dataclass(frozen=True)
class Site:
    id: str
    name: str
    # 站点级总功率上限（W）；缺省 None 表示不做站点级限额。
    limit_w: float | None = None
    # 降载恢复滞回阈值占上限的比例（如 0.95 表示回落到 95% 以下才恢复）。
    restore_ratio: float = 0.95


@dataclass(frozen=True)
class Circuit:
    id: str
    site_id: str
    name: str
    limit_w: float
    restore_ratio: float = 0.95


@dataclass(frozen=True)
class Device:
    id: str
    site_id: str
    circuit_id: str
    name: str
    # 是否为可中断负载（只有可中断负载才会参与分级降载）。
    interruptible: bool = False
    # 降载优先级：数字越小越先被关闭。
    shed_priority: int = 100
    # 额定功率（W），用于在设备被降载后估算其“应当贡献”的功率。
    rated_power_w: float | None = None
    # 被降载后该设备保留的基线功率（W），0 表示完全关闭。
    baseline_when_shed_w: float = 0.0


@dataclass(frozen=True)
class ApiKey:
    key: str
    label: str
    # site_id 为 None 表示全局管理员；device_ids 为空表示该站点管理员。
    site_id: str | None
    device_ids: frozenset[str] = frozenset()


# --------------------------------------------------------------- 遥测样本

@dataclass(frozen=True)
class Reading:
    """通过校验后的单条读数，已换算为 SI 单位。"""

    metric: Metric
    unit: Unit
    raw_value: float
    si_value: float


@dataclass(frozen=True)
class Sample:
    """一台设备在某测量时刻的一条完整遥测（测量时刻 ts，入库时刻 ingest_ts）。"""

    device_id: str
    ts: datetime
    power_w: float | None
    voltage_v: float | None
    current_a: float | None
    ingest_ts: datetime


@dataclass(frozen=True)
class IngestResult:
    """一次上报的处理结果。accepted=False 时 reason 必填；duplicate=True 表示幂等重放。"""

    idempotency_key: str
    device_id: str | None
    accepted: bool
    duplicate: bool
    reason: RejectReason | None
    detail: str
    ts: datetime | None
    ingest_ts: datetime
    sample: Sample | None = None
    # 本次入库触发的预算判定（接受且为首次写入时存在）。
    decision: BudgetDecision | None = None


# ------------------------------------------------------------- 预算与降载

@dataclass
class ShedState:
    """一个站点当前被降载（关闭）的设备集合，持久在 storage 中以实现滞回。"""

    site_id: str
    shed_device_ids: list[str] = field(default_factory=list)
    updated_at: datetime | None = None


@dataclass(frozen=True)
class BudgetDecision:
    """一次预算判定的完整结论。"""

    site_id: str
    evaluated_at: datetime
    scope: str                       # "site" 或 "circuit:<id>"
    limit_w: float
    restore_threshold_w: float
    measured_power_w: float          # 判定（含降载/恢复）后的最终总功率
    over_budget: bool                # 判定后是否仍超限（候选耗尽仍压不下来时为 True）
    shed_triggered: bool             # 本次是否因超限触发了降载（newly_shed 非空）
    newly_shed: tuple[str, ...]
    already_shed: tuple[str, ...]
    restored: tuple[str, ...]
    final_shed: tuple[str, ...]
    basis: str                       # 判定依据的人类可读说明


# ----------------------------------------------------------------- 统计

@dataclass(frozen=True)
class CurvePoint:
    """功率曲线上的一个时间桶。average_w 为 None 表示该桶无数据（不静默当 0）。"""

    bucket_start: datetime
    bucket_end: datetime
    average_w: float | None
    max_w: float | None
    sample_count: int


@dataclass(frozen=True)
class EnergyResult:
    """窗口累计能耗结果（Wh），并显式报告覆盖/缺口时长，避免缺数据被当零。"""

    window_start: datetime
    window_end: datetime
    energy_wh: float
    covered_seconds: float
    gap_seconds: float
    sample_count: int
    complete: bool
