"""核心数据模型与枚举定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RejectReason(str, Enum):
    """上报被拒绝的可区分原因。"""

    MISSING_FIELD = "missing_field"
    INVALID_UNIT = "invalid_unit"
    OUT_OF_RANGE = "out_of_range"
    INVALID_TIMESTAMP = "invalid_timestamp"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN_DEVICE = "unknown_device"


class LoadClass(str, Enum):
    """负载类型：可中断负载参与分级降载。"""

    CRITICAL = "critical"        # 关键负载，永不降载
    INTERRUPTIBLE = "interruptible"  # 可中断负载，按优先级降载


class LoadState(str, Enum):
    ON = "on"
    SHED = "shed"  # 已被降载（逻辑关闭）


@dataclass(frozen=True)
class Device:
    device_id: str
    site_id: str
    circuit_id: str
    load_class: LoadClass
    shed_priority: int = 0  # 数值越小越先被降载；CRITICAL 忽略
    owner_token: str = ""   # 上报鉴权 token


@dataclass(frozen=True)
class BudgetConfig:
    """站点或回路级别的功率上限与滞回配置。"""

    scope: str            # "site" 或 "circuit"
    scope_id: str
    limit_w: float        # 超限阈值（瓦）
    restore_w: float      # 恢复阈值（瓦），必须 < limit_w，形成滞回带


@dataclass(frozen=True)
class TelemetryReport:
    """一条遥测上报（原始单位，由 units 模块归一化）。"""

    device_id: str
    timestamp: datetime
    power: float | None
    power_unit: str | None
    voltage: float | None = None
    voltage_unit: str | None = None
    current: float | None = None
    current_unit: str | None = None
    report_id: str = ""     # 幂等键：同一设备同一 report_id 只计一次
    token: str = ""


@dataclass
class TelemetryPoint:
    """归一化后的遥测点（统一为 W / V / A，UTC 时间）。"""

    device_id: str
    timestamp: datetime
    power_w: float
    voltage_v: float | None
    current_a: float | None
    report_id: str


@dataclass
class ShedEvent:
    """一次降载/恢复事件。"""

    device_id: str
    action: str  # "shed" 或 "restore"
    scope: str
    scope_id: str
    at: datetime
    reason: str
