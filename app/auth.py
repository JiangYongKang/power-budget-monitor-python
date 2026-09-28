"""权限边界：校验上报 token 与设备的归属关系。

约定：每台设备注册时分配独立 token，上报必须携带匹配 token；
设备不存在或 token 不匹配都无权上报（上层映射为不同拒绝原因）。
"""
from __future__ import annotations

import hmac

from app.models import Device


def can_report(device: Device | None, token: str) -> bool:
    if device is None or not token:
        return False
    return hmac.compare_digest(device.owner_token, token)
