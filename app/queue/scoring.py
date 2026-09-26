"""补录队列的可版本化评分规则（纯函数，不依赖数据库与时钟）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, Sequence

# 首次启动且未显式建规则时使用的默认版本。
DEFAULT_GRADUATING_WEIGHT = 100.0
DEFAULT_MATERIALS_WEIGHT = 50.0
DEFAULT_AGING_PER_HOUR = 10.0


class ScoreSubject(Protocol):
    id: int
    is_graduating: bool
    materials_ready: bool
    effective_since: datetime


def utc(value: datetime) -> datetime:
    """统一归一化为 UTC，拒绝 naive 时间。"""
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Rule:
    """不可变的评分规则版本。

    score = 毕业权重 * 毕业标志 + 材料权重 * 材料齐全标志
             + 老化因子 * 已等待小时数
    """

    rule_version: int
    graduating_weight: float
    materials_weight: float
    aging_per_hour: float

    def base_score(self, *, is_graduating: bool, materials_ready: bool) -> float:
        score = 0.0
        if is_graduating:
            score += self.graduating_weight
        if materials_ready:
            score += self.materials_weight
        return score

    def waited_hours(self, effective_since: datetime, now: datetime) -> float:
        delta = utc(now) - utc(effective_since)
        hours = delta.total_seconds() / 3600.0
        return max(0.0, hours)

    def score(
        self,
        *,
        is_graduating: bool,
        materials_ready: bool,
        effective_since: datetime,
        now: datetime,
    ) -> float:
        return (
            self.base_score(
                is_graduating=is_graduating, materials_ready=materials_ready
            )
            + self.aging_per_hour
            * self.waited_hours(effective_since, now)
        )


def ranking_key(subject: ScoreSubject, score: float) -> tuple[float, datetime, int]:
    """分数降序、入队生效时间升序（同分先到先得）、id 升序兜底，保证全序确定。"""
    return (-score, utc(subject.effective_since), subject.id)


def rank(
    subjects: Sequence[ScoreSubject], rule: Rule, now: datetime
) -> list[tuple[float, ScoreSubject]]:
    """按当前规则返回 (score, subject) 的确定性优先级序列。"""
    scored = [
        (
            rule.score(
                is_graduating=s.is_graduating,
                materials_ready=s.materials_ready,
                effective_since=s.effective_since,
                now=now,
            ),
            s,
        )
        for s in subjects
    ]
    scored.sort(key=lambda pair: ranking_key(pair[1], pair[0]))
    return scored
