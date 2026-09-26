"""补录工作队列的持久化操作。

认领、续租、退回、完成都采用比较并交换（CAS）的条件更新，
依靠数据库原子性防止并发下的重复处理。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import QueueConfig, QueueItem, QueueRule


def insert_rule(
    db: Session,
    *,
    rule_id: str,
    version: int,
    graduating_weight: float,
    materials_weight: float,
    aging_weight_per_hour: float,
    aging_cap: float | None,
    created_at: datetime,
) -> bool:
    stmt = (
        sqlite_insert(QueueRule)
        .values(
            rule_id=rule_id,
            version=version,
            graduating_weight=graduating_weight,
            materials_weight=materials_weight,
            aging_weight_per_hour=aging_weight_per_hour,
            aging_cap=aging_cap,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["rule_id", "version"])
        .returning(QueueRule.rule_id)
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def get_rule(db: Session, rule_id: str, version: int) -> QueueRule | None:
    return db.get(QueueRule, (rule_id, version))


def max_rule_version(db: Session, rule_id: str) -> int | None:
    stmt = select(func.max(QueueRule.version)).where(QueueRule.rule_id == rule_id)
    return db.execute(stmt).scalar_one_or_none()


def list_rules(db: Session) -> list[QueueRule]:
    stmt = select(QueueRule).order_by(QueueRule.rule_id, QueueRule.version)
    return list(db.execute(stmt).scalars().all())


def get_config(db: Session) -> QueueConfig | None:
    return db.get(QueueConfig, 1)


def set_config(db: Session, *, rule_id: str, rule_version: int, now: datetime) -> None:
    stmt = sqlite_insert(QueueConfig).values(
        id=1, rule_id=rule_id, rule_version=rule_version, updated_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={"rule_id": rule_id, "rule_version": rule_version, "updated_at": now},
    )
    db.execute(stmt)


def insert_item(
    db: Session,
    *,
    request_id: str,
    student_id: str,
    graduating: bool,
    materials_complete: bool,
    note: str,
    now: datetime,
) -> bool:
    stmt = (
        sqlite_insert(QueueItem)
        .values(
            request_id=request_id,
            student_id=student_id,
            state="pending",
            graduating=graduating,
            materials_complete=materials_complete,
            note=note,
            enqueued_at=now,
            wait_anchor=now,
            return_count=0,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["request_id"])
        .returning(QueueItem.request_id)
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def get_item(db: Session, request_id: str) -> QueueItem | None:
    return db.get(QueueItem, request_id)


def list_items(db: Session) -> list[QueueItem]:
    stmt = select(QueueItem).order_by(QueueItem.request_id)
    return list(db.execute(stmt).scalars().all())


def list_claimable(db: Session, now: datetime) -> list[QueueItem]:
    """待处理条目与租约已过期的条目构成当前可认领集合。"""
    stmt = select(QueueItem).where(
        or_(
            QueueItem.state == "pending",
            and_(
                QueueItem.state == "leased",
                QueueItem.lease_expires_at <= now,
            ),
        )
    )
    return list(db.execute(stmt).scalars().all())


def cas_claim(
    db: Session,
    *,
    request_id: str,
    now: datetime,
    owner: str,
    token: str,
    expires_at: datetime,
) -> bool:
    """仅当条目仍可认领时建立租约；并发下只有一个认领者成功。"""
    stmt = (
        update(QueueItem)
        .where(QueueItem.request_id == request_id)
        .where(
            or_(
                QueueItem.state == "pending",
                and_(
                    QueueItem.state == "leased",
                    QueueItem.lease_expires_at <= now,
                ),
            )
        )
        .values(
            state="leased",
            lease_owner=owner,
            lease_token=token,
            lease_expires_at=expires_at,
            updated_at=now,
        )
    )
    return db.execute(stmt).rowcount == 1


def cas_renew(
    db: Session,
    *,
    request_id: str,
    token: str,
    now: datetime,
    new_expires_at: datetime,
) -> bool:
    """持有效租约令牌才能续租。"""
    stmt = (
        update(QueueItem)
        .where(QueueItem.request_id == request_id)
        .where(QueueItem.state == "leased")
        .where(QueueItem.lease_token == token)
        .where(QueueItem.lease_expires_at > now)
        .values(lease_expires_at=new_expires_at, updated_at=now)
    )
    return db.execute(stmt).rowcount == 1


def cas_return(
    db: Session,
    *,
    request_id: str,
    token: str,
    reason: str,
    now: datetime,
) -> bool:
    """退回补证：回到待处理并清空租约，但不动 wait_anchor。"""
    stmt = (
        update(QueueItem)
        .where(QueueItem.request_id == request_id)
        .where(QueueItem.state == "leased")
        .where(QueueItem.lease_token == token)
        .where(QueueItem.lease_expires_at > now)
        .values(
            state="pending",
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            return_count=QueueItem.return_count + 1,
            last_return_reason=reason,
            updated_at=now,
        )
    )
    return db.execute(stmt).rowcount == 1


def cas_complete(
    db: Session,
    *,
    request_id: str,
    token: str,
    now: datetime,
) -> bool:
    stmt = (
        update(QueueItem)
        .where(QueueItem.request_id == request_id)
        .where(QueueItem.state == "leased")
        .where(QueueItem.lease_token == token)
        .where(QueueItem.lease_expires_at > now)
        .values(
            state="completed",
            lease_token=None,
            lease_expires_at=None,
            completed_at=now,
            updated_at=now,
        )
    )
    return db.execute(stmt).rowcount == 1
