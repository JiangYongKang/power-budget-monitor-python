"""FastAPI 路由：遥测上报、预算配置、降载状态、窗口统计。"""
from __future__ import annotations

from fastapi import FastAPI

from app.budget import BudgetController
from app.store import Store


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="power-budget-monitor")
    app.state.store = store or Store()
    app.state.controller = BudgetController(app.state.store)
    return app
