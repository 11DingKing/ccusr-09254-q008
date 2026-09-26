"""补录公平队列的业务编排：入队、认领、续租、退回、完成与统计。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import QueueRule
from . import repository as repo
from .clock import clock
from .scoring import (
    DEFAULT_AGING_PER_HOUR,
    DEFAULT_GRADUATING_WEIGHT,
    DEFAULT_MATERIALS_WEIGHT,
    Rule,
    rank,
)

DEFAULT_LEASE_SECONDS = 300


class QueueError(ValueError):
    """队列领域约束错误。"""


class ItemNotFoundError(QueueError):
    pass


class LeaseInvalidError(QueueError):
    pass


def _now():
    return clock.now()


def _rule_from_model(model) -> Rule:
    return Rule(
        rule_version=model.rule_version,
        graduating_weight=model.graduating_weight,
        materials_weight=model.materials_weight,
        aging_per_hour=model.aging_per_hour,
    )


def ensure_default_rule(db: Session) -> Rule:
    active = repo.get_active_rule(db)
    if active is not None:
        return _rule_from_model(active)
    try:
        model = repo.insert_active_rule(
            db,
            graduating_weight=DEFAULT_GRADUATING_WEIGHT,
            materials_weight=DEFAULT_MATERIALS_WEIGHT,
            aging_per_hour=DEFAULT_AGING_PER_HOUR,
            now=_now(),
        )
        db.commit()
        return _rule_from_model(model)
    except IntegrityError:
        # 并发首次入队：另一个事务已发布激活规则，部分唯一索引拒绝本次插入。
        db.rollback()
        active = repo.get_active_rule(db)
        assert active is not None
        return _rule_from_model(active)


def get_active_rule(db: Session) -> Rule:
    active = repo.get_active_rule(db)
    if active is None:
        return ensure_default_rule(db)
    return _rule_from_model(active)


def publish_rule(
    db: Session,
    *,
    graduating_weight: float,
    materials_weight: float,
    aging_per_hour: float,
) -> dict[str, Any]:
    """发布新版本规则；旧版本立即停用但保留历史，实现规则可版本化。"""
    if graduating_weight < 0 or materials_weight < 0 or aging_per_hour < 0:
        raise QueueError("权重与老化因子不能为负")
    model = repo.insert_rule(
        db,
        graduating_weight=float(graduating_weight),
        materials_weight=float(materials_weight),
        aging_per_hour=float(aging_per_hour),
        now=_now(),
    )
    db.commit()
    return rule_to_dict(model)


def list_rules(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(select(QueueRule).order_by(QueueRule.rule_version)).scalars().all()
    return [rule_to_dict(r) for r in rows]


def rule_to_dict(model) -> dict[str, Any]:
    return {
        "rule_version": model.rule_version,
        "graduating_weight": model.graduating_weight,
        "materials_weight": model.materials_weight,
        "aging_per_hour": model.aging_per_hour,
        "is_active": bool(model.is_active),
        "created_at": model.created_at,
    }


def enqueue(
    db: Session,
    *,
    student_id: str,
    request_type: str = "backfill",
    is_graduating: bool = False,
    materials_ready: bool = False,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rule = get_active_rule(db)
    now = _now()
    item = repo.insert_item(
        db,
        student_id=student_id,
        request_type=request_type,
        is_graduating=is_graduating,
        materials_ready=materials_ready,
        enqueued_at=now,
        rule_version_at_enqueue=rule.rule_version,
        payload=payload or {},
    )
    db.commit()
    return item_to_dict(item, rule=rule, now=now)


def claim(db: Session, *, worker_id: str, lease_seconds: int | None = None) -> dict[str, Any] | None:
    """认领优先级最高的可处理条目。

    候选包含等待中的条目和租约已到期被回收的条目；评分完全由持久化
    字段与当前规则推导，因此进程重启后按同一时钟会重建出相同顺序。
    每个候选用条件 UPDATE 抢占，rowcount 保证并发下只有一个赢家。
    """
    if not worker_id or not worker_id.strip():
        raise QueueError("worker_id 不能为空")
    lease_seconds = lease_seconds or DEFAULT_LEASE_SECONDS
    if lease_seconds <= 0:
        raise QueueError("租约时长必须大于零")

    rule = get_active_rule(db)
    now = _now()
    candidates = repo.list_claimable(db, now)
    ordered = rank(candidates, rule, now)
    candidate_ids = [candidate.id for _, candidate in ordered]
    # 结束读事务以释放快照：SQLite WAL 下持有快照的连接直接升级写操作
    # 会与并发提交者冲突（busy_timeout 无法重试快照冲突）。
    db.rollback()

    for candidate_id in candidate_ids:
        token = uuid.uuid4().hex
        expires = now + timedelta(seconds=lease_seconds)
        if repo.try_acquire(
            db,
            item_id=candidate_id,
            worker_id=worker_id,
            lease_token=token,
            lease_expires_at=expires,
            now=now,
        ):
            db.commit()
            item = repo.get_item(db, candidate_id)
            assert item is not None
            return item_to_dict(item, rule=rule, now=now, lease_token=token)
        db.rollback()
    return None


def _require_leased_item(db: Session, item_id: int) -> Any:
    item = repo.get_item(db, item_id)
    if item is None:
        raise ItemNotFoundError(f"队列条目 {item_id} 不存在")
    if item.status != "leased":
        raise LeaseInvalidError(f"队列条目 {item_id} 当前未被认领（状态 {item.status}）")
    # 释放读快照后再执行条件 UPDATE，避免 SQLite WAL 下的快照冲突；
    # 状态与租约归属仍由 UPDATE 的 WHERE 子句原子保证。
    db.rollback()
    return item


def renew(
    db: Session, *, item_id: int, lease_token: str, lease_seconds: int | None = None
) -> dict[str, Any]:
    lease_seconds = lease_seconds or DEFAULT_LEASE_SECONDS
    if lease_seconds <= 0:
        raise QueueError("租约时长必须大于零")
    item = _require_leased_item(db, item_id)
    now = _now()
    if not repo.renew_lease(
        db,
        item_id=item_id,
        lease_token=lease_token,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        now=now,
    ):
        raise LeaseInvalidError("租约令牌无效或租约已到期，无法续租")
    db.commit()
    item = repo.get_item(db, item_id)
    assert item is not None
    return item_to_dict(item, rule=get_active_rule(db), now=now)


def return_item(
    db: Session, *, item_id: int, lease_token: str, reason: str | None = None
) -> dict[str, Any]:
    """退回补证：条目回到等待队列，effective_since 保留首次入队时间。"""
    item = _require_leased_item(db, item_id)
    if not repo.return_item(db, item_id=item_id, lease_token=lease_token, reason=reason):
        raise LeaseInvalidError("租约令牌无效，无法退回")
    db.commit()
    item = repo.get_item(db, item_id)
    assert item is not None
    return item_to_dict(item, rule=get_active_rule(db), now=_now())


def complete_item(
    db: Session, *, item_id: int, lease_token: str
) -> dict[str, Any]:
    _require_leased_item(db, item_id)
    now = _now()
    if not repo.complete_item(db, item_id=item_id, lease_token=lease_token, now=now):
        raise LeaseInvalidError("租约令牌无效，无法完成")
    db.commit()
    item = repo.get_item(db, item_id)
    assert item is not None
    return item_to_dict(item, rule=get_active_rule(db), now=now)


def peek(db: Session, *, status: str | None = None) -> list[dict[str, Any]]:
    rule = get_active_rule(db)
    now = _now()
    if status is None:
        rows = repo.list_claimable(db, now)
    else:
        rows = repo.list_by_status(db, status)
    ordered = rank(rows, rule, now)
    return [item_to_dict(item, rule=rule, now=now) for _, item in ordered]


def stats(db: Session) -> dict[str, Any]:
    rule = get_active_rule(db)
    now = _now()
    rows = repo.list_by_status(db, None)
    waiting = [r for r in rows if r.status == "waiting"]
    leased = [r for r in rows if r.status == "leased"]
    expired = [
        r
        for r in leased
        if r.lease_expires_at is not None and r.lease_expires_at <= now
    ]
    completed = [r for r in rows if r.status == "completed"]
    ranked = rank(waiting + expired, rule, now)
    next_item = ranked[0][1] if ranked else None
    return {
        "rule_version": rule.rule_version,
        "generated_at": now,
        "waiting": len(waiting),
        "leased": len(leased),
        "expired_leases": len(expired),
        "completed": len(completed),
        "total": len(rows),
        "next_item_id": next_item.id if next_item is not None else None,
    }


def item_to_dict(item, *, rule: Rule, now, lease_token: str | None = None) -> dict[str, Any]:
    score = rule.score(
        is_graduating=item.is_graduating,
        materials_ready=item.materials_ready,
        effective_since=item.effective_since,
        now=now,
    )
    waited = rule.waited_hours(item.effective_since, now)
    result: dict[str, Any] = {
        "id": item.id,
        "student_id": item.student_id,
        "request_type": item.request_type,
        "is_graduating": bool(item.is_graduating),
        "materials_ready": bool(item.materials_ready),
        "status": item.status,
        "enqueued_at": item.enqueued_at,
        "effective_since": item.effective_since,
        "waited_hours": round(waited, 6),
        "score": round(score, 6),
        "rule_version": rule.rule_version,
        "rule_version_at_enqueue": item.rule_version_at_enqueue,
        "attempts": item.attempts,
        "last_return_reason": item.last_return_reason,
        "lease_owner": item.lease_owner,
        "lease_expires_at": item.lease_expires_at,
        "completed_at": item.completed_at,
        "payload": dict(item.payload),
    }
    if lease_token is not None:
        result["lease_token"] = lease_token
    return result
