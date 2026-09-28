"""可注入的时钟，便于用确定性时间测试迟到/滞回/跨天行为。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


def utcnow() -> datetime:
    """当前 UTC 时间（aware datetime）。"""
    return datetime.now(UTC)


class SystemClock:
    """墙上时钟（生产默认）。"""

    def now(self) -> datetime:
        return utcnow()


class FakeClock:
    """测试用时钟：可手动推进/回拨，默认从固定时刻 2026-01-01T00:00:00Z 开始。"""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = _EPOCH
        self._now = _as_utc(start)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """将时钟向后推进指定秒数（可为负，便于构造迟到场景）。"""
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, ts: datetime) -> None:
        """直接把时钟设置到指定时刻。"""
        self._now = _as_utc(ts)


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)
