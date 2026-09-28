"""权限边界：API Key 认证与设备级授权。"""
from __future__ import annotations

from .models import ApiKey, Device, RejectReason


class AuthError(Exception):
    """认证/授权失败。"""

    def __init__(self, reason: RejectReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def resolve_api_key(keys: dict[str, ApiKey], key: str | None) -> ApiKey:
    """解析 API Key；缺失/未知 -> AuthError(UNAUTHORIZED)。"""
    if key is None or not key.strip():
        raise AuthError(RejectReason.UNAUTHORIZED, "缺少 API Key（X-API-Key 头）")
    api_key = keys.get(key.strip())
    if api_key is None:
        raise AuthError(RejectReason.UNAUTHORIZED, "未知 API Key")
    return api_key


def authorize(api_key: ApiKey, device: Device) -> None:
    """判定该 Key 是否可向该设备上报，否则 AuthError(UNAUTHORIZED)。

    - 全局 Key（site_id is None）可写所有设备；
    - 站点 Key（device_ids 为空）可写所属站点的所有设备；
    - 设备 Key 只能写 device_ids 白名单内的设备。
    设备白名单 Key 即使写同站点其他设备也拒绝（最小权限）。
    """
    if api_key.site_id is None:
        return
    if not api_key.device_ids:
        if api_key.site_id == device.site_id:
            return
        raise AuthError(
            RejectReason.UNAUTHORIZED,
            f"站点 Key 无权访问其他站点 {device.site_id} 的设备 {device.id}",
        )
    if device.id in api_key.device_ids:
        return
    raise AuthError(
        RejectReason.UNAUTHORIZED,
        f"设备 Key 无权向设备 {device.id} 上报（白名单外）",
    )
