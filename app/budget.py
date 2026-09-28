"""预算判定与分级降载（含滞回），并发下结论一致。"""
from __future__ import annotations

from app.models import ShedEvent
from app.store import Store


class BudgetController:
    def __init__(self, store: Store) -> None:
        self._store = store

    def evaluate(self, scope: str, scope_id: str) -> list[ShedEvent]:
        """根据当前功率与预算，返回本次需要执行的降载/恢复事件。"""
        raise NotImplementedError

    def load_state(self, device_id: str) -> str:
        raise NotImplementedError
