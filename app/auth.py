"""权限边界：校验上报 token 与设备的归属关系。"""
from __future__ import annotations

from app.models import Device


def can_report(device: Device | None, token: str) -> bool:
    raise NotImplementedError
