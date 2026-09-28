"""FastAPI 路由与应用工厂。服务可注入，便于测试隔离。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from .models import BudgetDecision, EnergyResult, IngestResult, WindowAlign
from .service import PowerService


# ----------------------------------------------------------- 序列化辅助
def _ts(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _decision(d: BudgetDecision | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "site_id": d.site_id,
        "evaluated_at": _ts(d.evaluated_at),
        "scope": d.scope,
        "limit_w": d.limit_w,
        "restore_threshold_w": d.restore_threshold_w,
        "measured_power_w": d.measured_power_w,
        "over_budget": d.over_budget,
        "shed_triggered": d.shed_triggered,
        "newly_shed": list(d.newly_shed),
        "already_shed": list(d.already_shed),
        "restored": list(d.restored),
        "final_shed": list(d.final_shed),
        "basis": d.basis,
    }


def _ingest_body(r: IngestResult) -> dict[str, Any]:
    return {
        "idempotency_key": r.idempotency_key,
        "device_id": r.device_id,
        "accepted": r.accepted,
        "duplicate": r.duplicate,
        "reason": r.reason.value if r.reason else None,
        "detail": r.detail,
        "ts": _ts(r.ts),
        "ingest_ts": _ts(r.ingest_ts),
        "sample": None
        if r.sample is None
        else {
            "device_id": r.sample.device_id,
            "ts": _ts(r.sample.ts),
            "power_w": r.sample.power_w,
            "voltage_v": r.sample.voltage_v,
            "current_a": r.sample.current_a,
        },
        "decision": _decision(r.decision),
    }


def _energy_body(e: EnergyResult) -> dict[str, Any]:
    return {
        "window_start": _ts(e.window_start),
        "window_end": _ts(e.window_end),
        "energy_wh": e.energy_wh,
        "covered_seconds": e.covered_seconds,
        "gap_seconds": e.gap_seconds,
        "sample_count": e.sample_count,
        "complete": e.complete,
    }


def create_app(service: PowerService) -> FastAPI:
    """创建 FastAPI 应用并挂载全部路由（本地无外部依赖）。"""
    app = FastAPI(
        title="本地用电监测与功率限额服务",
        version="0.1.0",
        description="遥测上报（幂等/单位/迟到/权限校验）、分级降载与滞回恢复、"
        "时间窗口功率曲线与累计能耗。",
    )
    app.state.service = service

    @app.get("/")
    def root() -> dict:
        return {
            "service": "power-budget-monitor",
            "sites": list(service.store.sites),
            "devices": list(service.store.devices),
        }

    @app.post("/api/v1/telemetry")
    async def ingest_telemetry(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> JSONResponse:
        """设备遥测上报。

        - 200：接受（duplicate=false）或幂等重放/同刻相同样本（duplicate=true）；
        - 422：校验失败（reason 区分具体原因）；
        - 401：API Key 缺失/未知/越权；
        - 404：设备不存在。
        """
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=422,
                content={"accepted": False, "reason": "bad_type",
                         "detail": "请求体必须是 JSON 对象"},
            )
        result = service.ingest(payload if isinstance(payload, dict) else {},
                                x_api_key)
        status = 200 if result.accepted else _http_status(result.reason.value)
        return JSONResponse(status_code=status, content=_ingest_body(result))

    @app.post("/api/v1/telemetry/frame")
    async def ingest_frame(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> JSONResponse:
        """原子提交同一测量时刻的一批设备上报（一个采集周期成批到达）。

        全部条目在一把锁内入库后只做一次预算判定，返回逐条结果
        （每条都带 accepted/duplicate/reason 与同一 decision）。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=422,
                                content={"results": [], "reason": "bad_type",
                                         "detail": "请求体必须是 JSON 对象"})
        reports = body.get("reports") if isinstance(body, dict) else None
        if not isinstance(reports, list) or not reports:
            return JSONResponse(status_code=422,
                                content={"results": [], "reason": "missing_field",
                                         "detail": "reports 必须是非空数组"})
        results = service.ingest_frame(reports, x_api_key)
        any_rejected = any(not r.accepted for r in results)
        status = 207 if any_rejected else 200
        return JSONResponse(status_code=status,
                            content={"results": [_ingest_body(r) for r in results]})

    @app.get("/api/v1/sites/{site_id}/curve")
    def site_curve(
        site_id: str,
        start: datetime = Query(..., description="窗口起点（含），ISO8601"),
        end: datetime = Query(..., description="窗口终点（不含），ISO8601"),
        align: WindowAlign = Query(WindowAlign.MINUTE),
    ) -> JSONResponse:
        """站点功率曲线；空桶不出现在 points 中，empty_buckets 列出缺数据桶。"""
        try:
            points = service.site_power_curve(site_id, start, end, align)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        from .stats import bucket_grid

        present = {p.bucket_start for p in points}
        grid = bucket_grid(start, end, align)
        empty = [
            {"bucket_start": a.isoformat(), "bucket_end": b.isoformat()}
            for a, b in grid if a not in present
        ]
        return JSONResponse(content={
            "site_id": site_id,
            "align": align.value,
            "points": [
                {
                    "bucket_start": _ts(p.bucket_start),
                    "bucket_end": _ts(p.bucket_end),
                    "average_w": p.average_w,
                    "max_w": p.max_w,
                    "sample_count": p.sample_count,
                }
                for p in points
            ],
            "empty_buckets": empty,
        })

    @app.get("/api/v1/devices/{device_id}/energy")
    def device_energy_ep(device_id: str, start: datetime, end: datetime) -> JSONResponse:
        try:
            e = service.device_energy(device_id, start, end)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        return JSONResponse(content=_energy_body(e))

    @app.get("/api/v1/sites/{site_id}/energy")
    def site_energy_ep(site_id: str, start: datetime, end: datetime) -> JSONResponse:
        try:
            e = service.site_energy(site_id, start, end)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        return JSONResponse(content=_energy_body(e))

    @app.get("/api/v1/sites/{site_id}/budget")
    def site_budget(site_id: str) -> JSONResponse:
        if site_id not in service.store.sites:
            return JSONResponse(status_code=404, content={"detail": f"未知站点: {site_id}"})
        state = service.store.get_shed_state(site_id)
        return JSONResponse(content={
            "site_id": site_id,
            "shed_device_ids": list(state.shed_device_ids),
            "updated_at": _ts(state.updated_at),
            "last_decision": _decision(service.store.last_decision(site_id)),
        })

    @app.get("/api/v1/_debug/snapshot")
    def debug_snapshot() -> JSONResponse:
        snap = service.snapshot()
        samples = {
            dev: [
                {"ts": _ts(s.ts), "power_w": s.power_w, "voltage_v": s.voltage_v,
                 "current_a": s.current_a, "ingest_ts": _ts(s.ingest_ts)}
                for s in arr
            ]
            for dev, arr in snap["samples"].items()
        }
        return JSONResponse(content={
            "samples": samples,
            "shed": {k: v.shed_device_ids for k, v in snap["shed"].items()},
            "decisions": {k: _decision(v) for k, v in snap["decisions"].items()},
        })

    return app


def _http_status(reason: str) -> int:
    """拒绝原因 -> HTTP 状态码。"""
    if reason == "unauthorized":
        return 401
    if reason == "unknown_device":
        return 404
    return 422
