"""入参校验与单位换算：缺失字段、类型、单位、量纲匹配、越界等可区分拒绝原因。

校验顺序固定（文档中同样说明），便于根据拒绝码定位：
读数内部：metric -> unit -> value(存在/类型/有限) -> 量纲匹配 -> 数值范围。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timezone

from .models import METRIC_UNITS, UNIT_TO_SI, Metric, Reading, RejectReason, Unit


@dataclass(frozen=True)
class ValidationError(Exception):
    """校验失败；reason 为可区分拒绝码，field 指出问题字段。"""

    reason: RejectReason
    detail: str
    field: str | None = None

    def __str__(self) -> str:
        return f"{self.reason.value}: {self.detail}"


# 各指标在 SI 单位下的明显越界范围（闭区间；超出即拒绝，属于“明显”不合理值）。
SI_RANGES: dict[Metric, tuple[float, float]] = {
    Metric.POWER: (0.0, 10_000_000.0),     # 0 ~ 10 MW
    Metric.VOLTAGE: (0.0, 10_000.0),       # 0 ~ 10 kV
    Metric.CURRENT: (0.0, 100_000.0),      # 0 ~ 100 kA
}


def parse_metric(value: object) -> Metric:
    """解析指标名，未知 -> ValidationError(UNKNOWN_METRIC)。"""
    if not isinstance(value, str):
        raise ValidationError(RejectReason.UNKNOWN_METRIC, "metric 必须是字符串", "metric")
    try:
        return Metric(value)
    except ValueError:
        raise ValidationError(
            RejectReason.UNKNOWN_METRIC, f"未知指标: {value!r}", "metric"
        ) from None


def parse_unit(value: object) -> Unit:
    """解析单位字符串，未知 -> ValidationError(UNKNOWN_UNIT)。"""
    if not isinstance(value, str):
        raise ValidationError(RejectReason.UNKNOWN_UNIT, "unit 必须是字符串", "unit")
    try:
        return Unit(value)
    except ValueError:
        raise ValidationError(
            RejectReason.UNKNOWN_UNIT, f"未知单位: {value!r}", "unit"
        ) from None


def parse_timestamp(value: object) -> datetime:
    """解析 ISO8601 字符串或 epoch 秒数值；非法 -> ValidationError(BAD_TIMESTAMP)。

    - 数值按 Unix epoch 秒解释；bool 不算数值；
    - 字符串按 ISO8601 解析，末尾 Z 视为 UTC；
    - naive 时间一律视为 UTC；返回 aware UTC datetime。
    """
    if isinstance(value, bool):
        raise ValidationError(RejectReason.BAD_TIMESTAMP, "时间戳不能是布尔值", "ts")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValidationError(RejectReason.BAD_TIMESTAMP, "时间戳非有限数", "ts")
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValidationError(RejectReason.BAD_TIMESTAMP, "时间戳为空", "ts")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            raise ValidationError(
                RejectReason.BAD_TIMESTAMP, f"无法解析时间戳: {value!r}", "ts"
            ) from None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    raise ValidationError(RejectReason.BAD_TIMESTAMP, "时间戳必须是字符串或数值", "ts")


def require_str(payload: dict, key: str) -> str:
    """取必填字符串字段；缺失/空 -> MISSING_FIELD，类型错 -> BAD_TYPE。"""
    if key not in payload:
        raise ValidationError(RejectReason.MISSING_FIELD, f"缺少必填字段: {key}", key)
    value = payload[key]
    if not isinstance(value, str):
        raise ValidationError(RejectReason.BAD_TYPE, f"字段 {key} 必须是字符串", key)
    if not value.strip():
        raise ValidationError(RejectReason.MISSING_FIELD, f"字段 {key} 不能为空", key)
    return value


def to_si(metric: Metric, unit: Unit, raw_value: float) -> float:
    """按单位换算为 SI 值：si = raw * factor。"""
    return raw_value * UNIT_TO_SI[unit]


def parse_reading(raw: object) -> Reading:
    """校验并换算单条读数 {metric, unit, value} 为 SI Reading。

    拒绝码区分：BAD_TYPE / NON_FINITE / UNKNOWN_METRIC / UNKNOWN_UNIT /
    UNIT_METRIC_MISMATCH / VALUE_OUT_OF_RANGE / MISSING_FIELD。
    """
    if not isinstance(raw, dict):
        raise ValidationError(RejectReason.BAD_TYPE, "读数必须是对象", None)

    if "metric" not in raw:
        raise ValidationError(RejectReason.MISSING_FIELD, "读数缺少 metric", "metric")
    metric = parse_metric(raw["metric"])

    if "unit" not in raw:
        raise ValidationError(RejectReason.MISSING_FIELD, "读数缺少 unit", "unit")
    unit = parse_unit(raw["unit"])

    if "value" not in raw:
        raise ValidationError(RejectReason.MISSING_FIELD, "读数缺少 value", "value")
    raw_value = raw["value"]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ValidationError(RejectReason.BAD_TYPE, "value 必须是数值", "value")
    raw_value = float(raw_value)
    if not math.isfinite(raw_value):
        raise ValidationError(RejectReason.NON_FINITE, "value 不能是 NaN/Inf", "value")

    if unit not in METRIC_UNITS[metric]:
        raise ValidationError(
            RejectReason.UNIT_METRIC_MISMATCH,
            f"单位 {unit.value} 不适用于指标 {metric.value}",
            "unit",
        )

    si_value = to_si(metric, unit, raw_value)
    low, high = SI_RANGES[metric]
    if si_value < low or si_value > high:
        raise ValidationError(
            RejectReason.VALUE_OUT_OF_RANGE,
            f"{metric.value}={si_value:g} 超出允许范围 [{low:g}, {high:g}] (SI)",
            "value",
        )

    return Reading(
        metric=metric,
        unit=unit,
        raw_value=raw_value,
        si_value=si_value,
    )
