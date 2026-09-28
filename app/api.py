"""FastAPI 路由：设备注册、预算配置、遥测上报、降载状态、窗口统计。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from pydantic import BaseModel

from app.auth import can_report
from app.budget import BudgetController
from app.models import (BudgetConfig, Device, LoadClass, RejectReason,
                        TelemetryReport, TelemetryPoint)
from app.stats import bucket_power, cumulative_energy_wh
from app.store import Store
from app.units import current_to_a, power_to_w, voltage_to_v
from app.validation import validate_report


class DeviceIn(BaseModel):
    device_id: str
    site_id: str
    circuit_id: str
    load_class: LoadClass = LoadClass.INTERRUPTIBLE
    shed_priority: int = 0
    owner_token: str


class BudgetIn(BaseModel):
    scope: str  # "site" | "circuit"
    scope_id: str
    limit_w: float
    restore_w: float


class TelemetryIn(BaseModel):
    device_id: str
    timestamp: datetime
    power: float | None = None
    power_unit: str | None = None
    voltage: float | None = None
    voltage_unit: str | None = None
    current: float | None = None
    current_unit: str | None = None
    report_id: str = ""
    token: str = ""


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="power-budget-monitor")
    app.state.store = store or Store()
    app.state.controller = BudgetController(app.state.store)

    @app.post("/devices", status_code=201)
    def register_device(body: DeviceIn) -> dict:
        dev = Device(body.device_id, body.site_id, body.circuit_id,
                     body.load_class, body.shed_priority, body.owner_token)
        app.state.store.add_device(dev)
        return {"device_id": dev.device_id}

    @app.post("/budgets", status_code=201)
    def set_budget(body: BudgetIn) -> dict:
        if body.scope not in ("site", "circuit"):
            return {"error": "invalid_scope"}
        if not body.restore_w < body.limit_w:
            return {"error": "restore_w_must_be_less_than_limit_w"}
        app.state.store.set_budget(BudgetConfig(body.scope, body.scope_id,
                                                body.limit_w, body.restore_w))
        return {"scope": body.scope, "scope_id": body.scope_id}

    @app.post("/telemetry")
    def submit_telemetry(body: TelemetryIn) -> dict:
        store: Store = app.state.store
        report = TelemetryReport(**body.model_dump())

        device = store.get_device(report.device_id)
        if device is None:
            return {"accepted": False, "reason": RejectReason.UNKNOWN_DEVICE.value}
        if not can_report(device, report.token):
            return {"accepted": False, "reason": RejectReason.UNAUTHORIZED.value}
        reason = validate_report(report)
        if reason is not None:
            return {"accepted": False, "reason": reason.value}

        point = TelemetryPoint(
            device_id=report.device_id,
            timestamp=report.timestamp.astimezone(timezone.utc),
            power_w=power_to_w(report.power, report.power_unit),
            voltage_v=(voltage_to_v(report.voltage, report.voltage_unit)
                       if report.voltage is not None else None),
            current_a=(current_to_a(report.current, report.current_unit)
                       if report.current is not None else None),
            report_id=report.report_id,
        )
        if not store.insert_point(point):
            return {"accepted": True, "duplicate": True}

        controller: BudgetController = app.state.controller
        events = (controller.evaluate("site", device.site_id)
                  + controller.evaluate("circuit", device.circuit_id))
        return {"accepted": True, "duplicate": False,
                "events": [e.__dict__ for e in events]}

    @app.get("/load-state/{device_id}")
    def load_state(device_id: str) -> dict:
        return {"device_id": device_id,
                "state": app.state.controller.load_state(device_id)}

    @app.get("/events")
    def list_events() -> dict:
        return {"events": [e.__dict__ for e in app.state.controller.events()]}

    def _resolve_scope(store: Store, scope: str, scope_id: str) -> list[str]:
        return [d.device_id for d in store.devices_in_scope(scope, scope_id)]

    @app.get("/stats/power")
    def stats_power(scope: str, scope_id: str, start: datetime, end: datetime,
                    bucket_seconds: int = 3600) -> dict:
        store: Store = app.state.store
        device_ids = _resolve_scope(store, scope, scope_id)
        pts = store.points_in_window(device_ids, start, end)
        return {"buckets": bucket_power(pts, start, end, bucket_seconds)}

    @app.get("/stats/energy")
    def stats_energy(scope: str, scope_id: str, start: datetime, end: datetime,
                     max_gap_seconds: int = 3600) -> dict:
        store: Store = app.state.store
        device_ids = _resolve_scope(store, scope, scope_id)
        pts = store.points_in_window(device_ids, start, end)
        return {"energy_wh": cumulative_energy_wh(pts, start, end, max_gap_seconds)}

    return app
