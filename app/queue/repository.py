"""补录队列表的持久化操作。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import QueueItem, QueueRule


def get_rule(db: Session, rule_version: int) -> QueueRule | None:
    return db.get(QueueRule, rule_version)


def get_active_rule(db: Session) -> QueueRule | None:
    stmt = (
        select(QueueRule)
        .where(QueueRule.is_active.is_(True))
        .order_by(QueueRule.rule_version.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def insert_rule(
    db: Session,
    *,
    graduating_weight: float,
    materials_weight: float,
    aging_per_hour: float,
    now: datetime,
) -> QueueRule:
    """发布新版本并停用旧版本，在同一事务内完成。"""
    db.execute(update(QueueRule).where(QueueRule.is_active.is_(True)).values(is_active=False))
    rule = QueueRule(
        graduating_weight=graduating_weight,
        materials_weight=materials_weight,
        aging_per_hour=aging_per_hour,
        is_active=True,
        created_at=now,
    )
    db.add(rule)
    db.flush()
    return rule


def insert_active_rule(
    db: Session,
    *,
    graduating_weight: float,
    materials_weight: float,
    aging_per_hour: float,
    now: datetime,
) -> QueueRule:
    """仅插入一条激活规则（不停用旧规则）；并发首启时由部分唯一索引拒绝重复。"""
    rule = QueueRule(
        graduating_weight=graduating_weight,
        materials_weight=materials_weight,
        aging_per_hour=aging_per_hour,
        is_active=True,
        created_at=now,
    )
    db.add(rule)
    db.flush()
    return rule


def insert_item(
    db: Session,
    *,
    student_id: str,
    request_type: str,
    is_graduating: bool,
    materials_ready: bool,
    enqueued_at: datetime,
    rule_version_at_enqueue: int,
    payload: dict[str, Any],
) -> QueueItem:
    item = QueueItem(
        student_id=student_id,
        request_type=request_type,
        is_graduating=is_graduating,
        materials_ready=materials_ready,
        status="waiting",
        enqueued_at=enqueued_at,
        # 等待时间从首次入队起累计；退回补证不重置该字段。
        effective_since=enqueued_at,
        rule_version_at_enqueue=rule_version_at_enqueue,
        payload=payload,
    )
    db.add(item)
    db.flush()
    return item


def get_item(db: Session, item_id: int) -> QueueItem | None:
    return db.get(QueueItem, item_id)


def list_claimable(db: Session, now: datetime) -> list[QueueItem]:
    """等待中的条目，以及租约已到期可回收的条目。"""
    stmt = select(QueueItem).where(
        (QueueItem.status == "waiting")
        | (
            (QueueItem.status == "leased")
            & (QueueItem.lease_expires_at.is_not(None))
            & (QueueItem.lease_expires_at <= now)
        )
    )
    return list(db.execute(stmt).scalars().all())


def list_by_status(db: Session, status: str | None) -> list[QueueItem]:
    stmt = select(QueueItem)
    if status is not None:
        stmt = stmt.where(QueueItem.status == status)
    return list(db.execute(stmt).scalars().all())


def try_acquire(
    db: Session,
    *,
    item_id: int,
    worker_id: str,
    lease_token: str,
    lease_expires_at: datetime,
    now: datetime,
) -> bool:
    """条件 UPDATE 抢占条目：仅当条目等待中或其租约已到期时成功。

    并发工作线程在 SQLite 写锁上串行化，rowcount 唯一决定赢家，
    因此同一条目不会被重复处理。
    """
    stmt = (
        update(QueueItem)
        .where(
            QueueItem.id == item_id,
            (QueueItem.status == "waiting")
            | (
                (QueueItem.status == "leased")
                & (QueueItem.lease_expires_at.is_not(None))
                & (QueueItem.lease_expires_at <= now)
            ),
        )
        .values(
            status="leased",
            lease_owner=worker_id,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            attempts=QueueItem.attempts + 1,
            last_return_reason=None,
        )
    )
    rowcount = db.execute(stmt).rowcount
    return rowcount == 1


def renew_lease(
    db: Session,
    *,
    item_id: int,
    lease_token: str,
    lease_expires_at: datetime,
    now: datetime,
) -> bool:
    """只有持有有效（未过期）租约的 token 才能续租。"""
    stmt = (
        update(QueueItem)
        .where(
            QueueItem.id == item_id,
            QueueItem.status == "leased",
            QueueItem.lease_token == lease_token,
            QueueItem.lease_expires_at.is_not(None),
            QueueItem.lease_expires_at > now,
        )
        .values(lease_expires_at=lease_expires_at)
    )
    return db.execute(stmt).rowcount == 1


def return_item(db: Session, *, item_id: int, lease_token: str, reason: str | None) -> bool:
    """退回补证：回到等待队列，effective_since 保持不变（等待时间不重置）。"""
    stmt = (
        update(QueueItem)
        .where(
            QueueItem.id == item_id,
            QueueItem.status == "leased",
            QueueItem.lease_token == lease_token,
        )
        .values(
            status="waiting",
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            last_return_reason=reason,
        )
    )
    return db.execute(stmt).rowcount == 1


def complete_item(db: Session, *, item_id: int, lease_token: str, now: datetime) -> bool:
    stmt = (
        update(QueueItem)
        .where(
            QueueItem.id == item_id,
            QueueItem.status == "leased",
            QueueItem.lease_token == lease_token,
        )
        .values(
            status="completed",
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            completed_at=now,
        )
    )
    return db.execute(stmt).rowcount == 1
