"""补录工作队列领域核心的纯逻辑测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.workqueue import (
    ItemState,
    ManualClock,
    QueueEntry,
    QueueError,
    ScoringRule,
    is_claimable,
    issue_lease,
    lease_active,
    order_entries,
    score_entry,
    wait_seconds,
)

T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
RULE = ScoringRule(
    rule_id="makeup",
    version=1,
    graduating_weight=100.0,
    materials_weight=40.0,
    aging_weight_per_hour=5.0,
)


def _entry(
    request_id: str,
    *,
    state: ItemState = ItemState.PENDING,
    graduating: bool = False,
    materials_complete: bool = False,
    wait_anchor: datetime = T0,
    lease_expires_at: datetime | None = None,
) -> QueueEntry:
    return QueueEntry(
        request_id=request_id,
        student_id=f"stu-{request_id}",
        state=state,
        graduating=graduating,
        materials_complete=materials_complete,
        wait_anchor=wait_anchor,
        lease_owner="w1" if state is ItemState.LEASED else None,
        lease_token="tok" if state is ItemState.LEASED else None,
        lease_expires_at=lease_expires_at,
    )


def test_graduating_and_materials_raise_score():
    ordinary = score_entry(RULE, _entry("A"), T0)
    graduating = score_entry(RULE, _entry("B", graduating=True), T0)
    complete = score_entry(RULE, _entry("C", materials_complete=True), T0)
    both = score_entry(
        RULE, _entry("D", graduating=True, materials_complete=True), T0
    )
    assert ordinary == 0.0
    assert graduating == 100.0
    assert complete == 40.0
    assert both == 140.0


def test_aging_factor_rewards_waiting():
    entry = _entry("A")
    assert score_entry(RULE, entry, T0) == 0.0
    later = T0 + timedelta(hours=10)
    assert score_entry(RULE, entry, later) == pytest.approx(50.0)


def test_aging_lets_old_ordinary_request_overtake_fresh_priority():
    # 普通请求等待足够久后，老化加分应超过新到的优先请求，避免无限等待。
    old_ordinary = _entry("OLD", wait_anchor=T0)
    fresh_priority = _entry(
        "NEW",
        graduating=True,
        materials_complete=True,
        wait_anchor=T0 + timedelta(hours=30),
    )
    now = T0 + timedelta(hours=30)
    ordered = order_entries(RULE, [fresh_priority, old_ordinary], now)
    assert [e.request_id for e in ordered] == ["OLD", "NEW"]


def test_aging_cap_limits_bonus():
    capped = ScoringRule(
        rule_id="makeup", version=2, aging_weight_per_hour=5.0, aging_cap=20.0
    )
    entry = _entry("A")
    now = T0 + timedelta(hours=100)
    assert capped.score(graduating=False, materials_complete=False, wait_seconds=100 * 3600) == 20.0
    assert score_entry(capped, entry, now) == 20.0


def test_negative_weights_rejected():
    with pytest.raises(QueueError):
        ScoringRule(rule_id="makeup", version=1, graduating_weight=-1.0)
    with pytest.raises(QueueError):
        ScoringRule(rule_id="makeup", version=1, aging_cap=-0.5)
    with pytest.raises(QueueError):
        ScoringRule(rule_id=" ", version=1)


def test_order_is_total_and_deterministic_across_rebuild():
    # 相同分数时先比等待起点，再比请求标识；重建后顺序完全一致。
    entries = [
        _entry("R-03", wait_anchor=T0 + timedelta(hours=1)),
        _entry("R-01", wait_anchor=T0 + timedelta(hours=1)),
        _entry("R-02", wait_anchor=T0),
    ]
    first = [e.request_id for e in order_entries(RULE, entries, T0)]
    # 模拟重启：从持久化字段重建全新的条目对象再排序
    rebuilt = [
        QueueEntry(
            request_id=e.request_id,
            student_id=e.student_id,
            state=e.state,
            graduating=e.graduating,
            materials_complete=e.materials_complete,
            wait_anchor=e.wait_anchor,
        )
        for e in entries
    ]
    second = [e.request_id for e in order_entries(RULE, rebuilt, T0)]
    assert first == ["R-02", "R-01", "R-03"]
    assert second == first


def test_wait_seconds_never_negative():
    entry = _entry("A", wait_anchor=T0 + timedelta(hours=1))
    assert wait_seconds(entry, T0) == 0.0


def test_claimable_and_lease_states():
    now = T0
    assert is_claimable(_entry("P"), now)
    active_lease = _entry(
        "L", state=ItemState.LEASED, lease_expires_at=T0 + timedelta(minutes=5)
    )
    assert not is_claimable(active_lease, now)
    assert lease_active(active_lease, now)
    expired_lease = _entry(
        "X", state=ItemState.LEASED, lease_expires_at=T0 - timedelta(seconds=1)
    )
    assert is_claimable(expired_lease, now)
    assert not lease_active(expired_lease, now)
    assert not is_claimable(_entry("C", state=ItemState.COMPLETED), now)


def test_issue_lease_validates_inputs():
    lease = issue_lease("worker-1", T0, timedelta(seconds=60))
    assert lease.expires_at == T0 + timedelta(seconds=60)
    assert lease.token
    other = issue_lease("worker-1", T0, timedelta(seconds=60))
    assert other.token != lease.token
    with pytest.raises(QueueError):
        issue_lease("  ", T0, timedelta(seconds=60))
    with pytest.raises(QueueError):
        issue_lease("worker-1", T0, timedelta(0))


def test_naive_datetime_rejected():
    with pytest.raises(QueueError):
        wait_seconds(_entry("A"), datetime(2026, 6, 1, 8, 0))


def test_manual_clock_only_moves_forward():
    clock = ManualClock(T0)
    assert clock.now() == T0
    clock.advance(timedelta(hours=2))
    assert clock.now() == T0 + timedelta(hours=2)
    with pytest.raises(QueueError):
        clock.advance(timedelta(seconds=-1))
