"""骨架冒烟测试：应用可构造、根路由可用。"""
from fastapi.testclient import TestClient

from power_budget.api import create_app
from power_budget.seed import build_service


def test_app_constructs_and_root() -> None:
    app = create_app(build_service(with_telemetry=False))
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert "site-a" in body["sites"]
