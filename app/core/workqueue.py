"""补录工作队列的领域核心：版本化评分规则、老化排序与租约原语。

排序键完全由持久化字段（wait_anchor、属性、request_id）与当前时刻决定，
因此服务重启后按同一时刻重建队列，顺序必然一致。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

SECONDS_PER_HOUR = 3600.0


class ItemState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"


class QueueError(ValueError):
    """封装队列领域的状态与参数约束。"""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise QueueError("时间必须包含时区")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ScoringRule:
    """可版本化的评分规则：基础权重叠加随等待时间增长的老化因子。

    老化因子保证普通请求不会无限等待：等待越久加分越多（可设上限）。
    """

    rule_id: str
    version: int
    graduating_weight: float = 100.0
    materials_weight: float = 40.0
    aging_weight_per_hour: float = 5.0
    aging_cap: float | None = None

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise QueueError("规则标识不能为空")
        if self.version < 1:
            raise QueueError("规则版本必须大于零")
        weights = {
            "graduating_weight": self.graduating_weight,
            "materials_weight": self.materials_weight,
            "aging_weight_per_hour": self.aging_weight_per_hour,
        }
        if self.aging_cap is not None:
            weights["aging_cap"] = self.aging_cap
        for name, value in weights.items():
            if value < 0:
                raise QueueError(f"评分权重不能为负：{name}")

    def score(
        self,
        *,
        graduating: bool,
        materials_complete: bool,
        wait_seconds: float,
    ) -> float:
        base = 0.0
        if graduating:
            base += self.graduating_weight
        if materials_complete:
            base += self.materials_weight
        aging = self.aging_weight_per_hour * (max(wait_seconds, 0.0) / SECONDS_PER_HOUR)
        if self.aging_cap is not None:
            aging = min(aging, self.aging_cap)
        return base + aging


@dataclass(frozen=True)
class QueueEntry:
    """队列条目的不可变快照，排序与租约判断只依赖持久化字段。"""

    request_id: str
    student_id: str
    state: ItemState
    graduating: bool
    materials_complete: bool
    wait_anchor: datetime
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    return_count: int = 0


def wait_seconds(entry: QueueEntry, now: datetime) -> float:
    """等待时长从 wait_anchor 起算，退回补证不清零。"""
    instant = _utc(now)
    anchor = _utc(entry.wait_anchor)
    return max((instant - anchor).total_seconds(), 0.0)


def lease_active(entry: QueueEntry, now: datetime) -> bool:
    if entry.state is not ItemState.LEASED or entry.lease_expires_at is None:
        return False
    return _utc(entry.lease_expires_at) > _utc(now)


def is_claimable(entry: QueueEntry, now: datetime) -> bool:
    """待处理条目可直接认领；租约过期的条目可被回收再认领。"""
    if entry.state is ItemState.PENDING:
        return True
    if entry.state is ItemState.LEASED and entry.lease_expires_at is not None:
        return _utc(entry.lease_expires_at) <= _utc(now)
    return False


def score_entry(rule: ScoringRule, entry: QueueEntry, now: datetime) -> float:
    return rule.score(
        graduating=entry.graduating,
        materials_complete=entry.materials_complete,
        wait_seconds=wait_seconds(entry, now),
    )


def sort_key(rule: ScoringRule, entry: QueueEntry, now: datetime) -> tuple[float, datetime, str]:
    """全序排序键：分数降序，等待起点升序，请求标识升序兜底。"""
    return (-score_entry(rule, entry, now), _utc(entry.wait_anchor), entry.request_id)


def order_entries(
    rule: ScoringRule, entries: Iterable[QueueEntry], now: datetime
) -> list[QueueEntry]:
    return sorted(entries, key=lambda e: sort_key(rule, e, now))


@dataclass(frozen=True)
class Lease:
    owner: str
    token: str
    expires_at: datetime


def issue_lease(owner: str, now: datetime, ttl: timedelta) -> Lease:
    instant = _utc(now)
    holder = owner.strip()
    if not holder:
        raise QueueError("认领人不能为空")
    if ttl <= timedelta(0):
        raise QueueError("租约时长必须大于零")
    return Lease(owner=holder, token=uuid4().hex, expires_at=instant + ttl)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """测试用时钟，只能显式向前推进。"""

    def __init__(self, start: datetime) -> None:
        self._current = _utc(start)

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise QueueError("时钟不能回拨")
        self._current = self._current + delta
        return self._current
