"""本地可复现的演示数据：站点/回路/设备/密钥与一段确定性遥测。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from .clock import FakeClock
from .models import ApiKey, Circuit, Device, Metric, Site, Unit
from .service import PowerService
from .storage import Store

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def build_catalog() -> tuple[list[Site], list[Circuit], list[Device], list[ApiKey]]:
    """站点 A：上限 1000W、恢复阈值 95%；1 个不可中断负载 + 3 个可中断负载。

    密钥：
    - admin-key：全局；
    - site-a-key：仅站点 A；
    - dev-fixed-key：仅设备 fixed（演示越权拒绝）。
    """
    sites = [Site(id="site-a", name="A 号站点", limit_w=1000.0, restore_ratio=0.95)]
    circuits = [Circuit(id="main", site_id="site-a", name="主回路", limit_w=1000.0)]
    devices = [
        Device(id="fixed", site_id="site-a", circuit_id="main",
               name="不可中断基础负载", interruptible=False, rated_power_w=600.0),
        Device(id="load-1", site_id="site-a", circuit_id="main",
               name="可中断负载 P1", interruptible=True, shed_priority=10,
               rated_power_w=450.0),
        Device(id="load-2", site_id="site-a", circuit_id="main",
               name="可中断负载 P2", interruptible=True, shed_priority=20,
               rated_power_w=250.0),
        Device(id="load-3", site_id="site-a", circuit_id="main",
               name="可中断负载 P3", interruptible=True, shed_priority=30,
               rated_power_w=200.0),
    ]
    api_keys = [
        ApiKey(key="admin-key", label="全局管理员", site_id=None),
        ApiKey(key="site-a-key", label="站点 A 管理员", site_id="site-a"),
        ApiKey(key="dev-fixed-key", label="仅 fixed 设备", site_id="site-a",
               device_ids=frozenset({"fixed"})),
    ]
    return sites, circuits, devices, api_keys


def _payload(when: datetime, device: str, power_w: float,
             idem: str, unit: Unit) -> dict:
    return {
        "idempotency_key": idem,
        "device_id": device,
        "ts": when.isoformat(),
        "readings": [{"metric": Metric.POWER.value, "unit": unit.value,
                      "value": power_w}],
    }


def build_service(start: datetime | None = None, *, with_telemetry: bool = True) -> PowerService:
    """灌入确定性遥测后的服务；FakeClock 保证每次运行一致。

    每 60s 一个“采集帧”，通过 ingest_frame() 原子提交（齐帧只判一次）：

    - 00:00：600+450(kW)+250+200=1500W > 1000 -> 切 load-1(剩1050)、load-2(剩800)；
    - 00:01..00:04：被切的 load-1/load-2 下线（不耗电、不上报，缺数据不补零），
      在线 fixed=600、load-3=200；恢复试算 load-2 额定 250 => 1050>=950 保持；
    - 00:05：load-3 降到 100：恢复 load-2 试算 600+250+100=950（不严格<950）保持；
    - 00:06：load-3 降到 50：恢复 load-2 试算 900<950 -> 恢复 load-2；
      load-1 试算 +450=1350 保持；
    - 00:07：load-2 重新上线 250，fixed=200，load-3=50：恢复 load-1
      试算 200+450+250+50=950（不严格<950）保持；
    - 00:08：fixed=150：恢复 load-1 试算 900<950 -> 全部恢复。
    """
    start = start or _T0
    sites, circuits, devices, api_keys = build_catalog()
    clock = FakeClock(start)
    store = Store(sites=sites, circuits=circuits, devices=devices, api_keys=api_keys)
    svc = PowerService(store, clock=clock, late_grace_seconds=1800.0,
                       sample_max_age_s=300.0, stats_gap_seconds=300.0)
    if not with_telemetry:
        return svc

    def frame(minute: int, values: dict[str, tuple[float, Unit]]) -> None:
        when = start + timedelta(minutes=minute)
        clock.set(when)
        payloads = [
            _payload(when, dev, val, f"seed-m{minute:02d}-{dev}", unit)
            for dev, (val, unit) in values.items()
        ]
        svc.ingest_frame(payloads, "admin-key")

    # 00:00：1500W -> 切 load-1、load-2
    frame(0, {
        "fixed": (600.0, Unit.WATT),
        "load-1": (0.45, Unit.KILOWATT),   # 450W，验证 kW 换算
        "load-2": (250.0, Unit.WATT),
        "load-3": (200.0, Unit.WATT),
    })
    # 00:01..00:04：在线 800W，保持降载
    for minute in range(1, 5):
        frame(minute, {"fixed": (600.0, Unit.WATT), "load-3": (200.0, Unit.WATT)})
    # 00:05：load-3=100，临界 950 不恢复
    frame(5, {"fixed": (600.0, Unit.WATT), "load-3": (100.0, Unit.WATT)})
    # 00:06：load-3=50 -> 恢复 load-2
    frame(6, {"fixed": (600.0, Unit.WATT), "load-3": (50.0, Unit.WATT)})
    # 00:07：load-2 重新上线，fixed=200；load-1 临界仍保持
    frame(7, {"fixed": (200.0, Unit.WATT), "load-2": (250.0, Unit.WATT),
              "load-3": (50.0, Unit.WATT)})
    # 00:08：fixed=150 -> 恢复 load-1，全部恢复
    frame(8, {"fixed": (150.0, Unit.WATT), "load-2": (250.0, Unit.WATT),
              "load-3": (50.0, Unit.WATT)})

    clock.set(start + timedelta(minutes=8))
    return svc
