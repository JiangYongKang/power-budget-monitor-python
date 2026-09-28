"""单位归一化：把上报的功率/电压/电流换算为 W / V / A。"""
from __future__ import annotations


class UnknownUnitError(ValueError):
    """单位无法识别时抛出。"""


def power_to_w(value: float, unit: str) -> float:
    raise NotImplementedError


def voltage_to_v(value: float, unit: str) -> float:
    raise NotImplementedError


def current_to_a(value: float, unit: str) -> float:
    raise NotImplementedError
