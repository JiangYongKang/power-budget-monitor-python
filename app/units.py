"""单位归一化：把上报的功率/电压/电流换算为 W / V / A。

约定：
- 单位字符串大小写不敏感，允许前后空白。
- 无法识别的单位抛出 UnknownUnitError，由上层映射为 RejectReason.INVALID_UNIT。
"""
from __future__ import annotations


class UnknownUnitError(ValueError):
    """单位无法识别时抛出。"""


_POWER_FACTORS = {
    "w": 1.0,
    "kw": 1_000.0,
    "mw": 1_000_000.0,  # 兆瓦
    "wh": 1.0,          # 兼容部分电表以 Wh/h 形式上报功率
}

_VOLTAGE_FACTORS = {
    "v": 1.0,
    "kv": 1_000.0,
    "mv": 0.001,  # 毫伏
}

_CURRENT_FACTORS = {
    "a": 1.0,
    "ma": 0.001,
    "ka": 1_000.0,
}


def _convert(value: float, unit: str, factors: dict[str, float], kind: str) -> float:
    key = unit.strip().lower()
    if key not in factors:
        raise UnknownUnitError(f"未知的{kind}单位: {unit!r}")
    return value * factors[key]


def power_to_w(value: float, unit: str) -> float:
    return _convert(value, unit, _POWER_FACTORS, "功率")


def voltage_to_v(value: float, unit: str) -> float:
    return _convert(value, unit, _VOLTAGE_FACTORS, "电压")


def current_to_a(value: float, unit: str) -> float:
    return _convert(value, unit, _CURRENT_FACTORS, "电流")
