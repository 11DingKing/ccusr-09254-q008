"""补录工作队列的业务编排：规则版本、入队、租约认领与统计。

所有排序都基于持久化字段与当前时刻实时计算，服务不保存任何内存队列状态，
因此重启后用同一时刻重建队列会得到相同顺序。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from . import queue_repository as repo
from .core.clock import to_utc
from .core.workqueue import (
    ItemState,
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
from .models import QueueItem, QueueRule


class QueueServiceError(Exception):
    pass


class RuleNotFoundError(QueueServiceError):
    pass


class RuleConflictError(QueueServiceError):
    pass


class NoActiveRuleError(QueueServiceError):
    pass


class ItemNotFoundError(QueueServiceError):
    pass


class ItemExistsError(QueueServiceError):
    pass


class LeaseConflictError(QueueServiceError):
    pass


class QueueEmptyError(QueueServiceError):
    pass


def _loaded(value: datetime | None) -> datetime | None:
    """SQLite 不保留时区，读取后统一补回 UTC。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_entry(row: QueueItem) -> QueueEntry:
    return QueueEntry(
        request_id=row.request_id,
        student_id=row.student_id,
        state=ItemState(row.state),
        graduating=row.graduating,
        materials_complete=row.materials_complete,
        wait_anchor=_loaded(row.wait_anchor),
        lease_owner=row.lease_owner,
        lease_token=row.lease_token,
        lease_expires_at=_loaded(row.lease_expires_at),
        return_count=row.return_count,
    )


def _to_rule(row: QueueRule) -> ScoringRule:
    return ScoringRule(
        rule_id=row.rule_id,
        version=row.version,
        graduating_weight=row.graduating_weight,
        materials_weight=row.materials_weight,
        aging_weight_per_hour=row.aging_weight_per_hour,
        aging_cap=row.aging_cap,
    )


def _rule_dict(row: QueueRule, *, active: bool) -> dict[str, Any]:
    return {
        "rule_id": row.rule_id,
        "version": row.version,
        "graduating_weight": row.graduating_weight,
        "materials_weight": row.materials_weight,
        "aging_weight_per_hour": row.aging_weight_per_hour,
        "aging_cap": row.aging_cap,
        "active": active,
        "created_at": _loaded(row.created_at),
    }


def _active_rule_row(db: Session) -> QueueRule | None:
    config = repo.get_config(db)
    if config is None:
        return None
    return repo.get_rule(db, config.rule_id, config.rule_version)


def _active_rule(db: Session) -> ScoringRule | None:
    row = _active_rule_row(db)
    return _to_rule(row) if row is not None else None


def _require_rule(db: Session) -> ScoringRule:
    rule = _active_rule(db)
    if rule is None:
        raise NoActiveRuleError("尚未启用任何评分规则，请先创建规则版本")
    return rule


def _item_dict(row: QueueItem, rule: ScoringRule | None, now: datetime) -> dict[str, Any]:
    entry = _to_entry(row)
    return {
        "request_id": row.request_id,
        "student_id": row.student_id,
        "state": row.state,
        "graduating": row.graduating,
        "materials_complete": row.materials_complete,
        "note": row.note,
        "enqueued_at": _loaded(row.enqueued_at),
        "wait_anchor": _loaded(row.wait_anchor),
        "wait_seconds": wait_seconds(entry, now),
        "score": score_entry(rule, entry, now) if rule is not None else None,
        "lease_owner": row.lease_owner,
        "lease_expires_at": _loaded(row.lease_expires_at),
        "return_count": row.return_count,
        "last_return_reason": row.last_return_reason,
        "completed_at": _loaded(row.completed_at),
        "updated_at": _loaded(row.updated_at),
    }


def create_rule(
    db: Session,
    *,
    rule_id: str,
    graduating_weight: float,
    materials_weight: float,
    aging_weight_per_hour: float,
    aging_cap: float | None,
    now: datetime,
) -> dict[str, Any]:
    """创建规则的新版本；首个规则版本自动启用。"""
    instant = to_utc(now)
    rule_id = rule_id.strip()
    # 借助领域对象校验权重与标识
    ScoringRule(
        rule_id=rule_id,
        version=1,
        graduating_weight=graduating_weight,
        materials_weight=materials_weight,
        aging_weight_per_hour=aging_weight_per_hour,
        aging_cap=aging_cap,
    )
    for _ in range(3):
        version = (repo.max_rule_version(db, rule_id) or 0) + 1
        db.rollback()  # 释放读事务，避免 SQLite 锁升级冲突
        inserted = repo.insert_rule(
            db,
            rule_id=rule_id,
            version=version,
            graduating_weight=graduating_weight,
            materials_weight=materials_weight,
            aging_weight_per_hour=aging_weight_per_hour,
            aging_cap=aging_cap,
            created_at=instant,
        )
        if inserted:
            if repo.get_config(db) is None:
                repo.set_config(db, rule_id=rule_id, rule_version=version, now=instant)
            db.commit()
            row = repo.get_rule(db, rule_id, version)
            assert row is not None
            config = repo.get_config(db)
            active = (
                config is not None
                and config.rule_id == rule_id
                and config.rule_version == version
            )
            return _rule_dict(row, active=active)
        db.rollback()
    raise RuleConflictError(f"规则 '{rule_id}' 版本创建冲突，请重试")


def activate_rule(db: Session, *, rule_id: str, version: int, now: datetime) -> dict[str, Any]:
    """切换当前启用的规则版本，后续排序立即按新版本计算。"""
    instant = to_utc(now)
    row = repo.get_rule(db, rule_id, version)
    db.rollback()
    if row is None:
        raise RuleNotFoundError(f"规则 '{rule_id}' 的版本 {version} 不存在")
    repo.set_config(db, rule_id=rule_id, rule_version=version, now=instant)
    db.commit()
    return _rule_dict(row, active=True)


def list_rules(db: Session) -> list[dict[str, Any]]:
    config = repo.get_config(db)
    rows = repo.list_rules(db)
    return [
        _rule_dict(
            row,
            active=(
                config is not None
                and config.rule_id == row.rule_id
                and config.rule_version == row.version
            ),
        )
        for row in rows
    ]


def enqueue(
    db: Session,
    *,
    request_id: str,
    student_id: str,
    graduating: bool,
    materials_complete: bool,
    note: str,
    now: datetime,
) -> dict[str, Any]:
    instant = to_utc(now)
    rule = _require_rule(db)
    request_id = request_id.strip()
    student_id = student_id.strip()
    if not request_id or not student_id:
        raise QueueError("请求标识与学号不能为空")
    db.rollback()  # 释放读事务，避免 SQLite 锁升级冲突
    inserted = repo.insert_item(
        db,
        request_id=request_id,
        student_id=student_id,
        graduating=graduating,
        materials_complete=materials_complete,
        note=note,
        now=instant,
    )
    if not inserted:
        db.rollback()
        raise ItemExistsError(f"补录请求 '{request_id}' 已存在")
    db.commit()
    row = repo.get_item(db, request_id)
    assert row is not None
    return _item_dict(row, rule, instant)


def claim(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """按当前评分规则认领队首条目，CAS 保证并发下不重复认领。"""
    instant = to_utc(now)
    rule = _require_rule(db)
    lease = issue_lease(worker_id, instant, timedelta(seconds=lease_seconds))
    for _ in range(10):
        rows = repo.list_claimable(db, instant)
        entries = [_to_entry(r) for r in rows]
        db.rollback()  # 释放读事务，认领更新独立成写事务
        if not entries:
            raise QueueEmptyError("当前没有可认领的补录请求")
        for entry in order_entries(rule, entries, instant):
            if repo.cas_claim(
                db,
                request_id=entry.request_id,
                now=instant,
                owner=lease.owner,
                token=lease.token,
                expires_at=lease.expires_at,
            ):
                db.commit()
                row = repo.get_item(db, entry.request_id)
                assert row is not None
                return {
                    "item": _item_dict(row, rule, instant),
                    "lease_token": lease.token,
                    "lease_expires_at": lease.expires_at,
                }
            db.rollback()
        # 本轮候选全部被其他工作者抢走，重新读取后再试
    raise QueueEmptyError("当前没有可认领的补录请求")


def preview(db: Session, *, now: datetime) -> dict[str, Any]:
    """按当前规则给出可认领队列的顺序，即重启重建后的认领顺序。"""
    instant = to_utc(now)
    rule = _require_rule(db)
    rows = repo.list_claimable(db, instant)
    by_id = {row.request_id: row for row in rows}
    entries = [_to_entry(row) for row in rows]
    ordered = order_entries(rule, entries, instant)
    return {
        "now": instant,
        "items": [_item_dict(by_id[e.request_id], rule, instant) for e in ordered],
    }


def _require_lease_seconds(lease_seconds: int) -> None:
    if lease_seconds <= 0:
        raise QueueError("租约时长必须大于零")


def _lease_conflict(row: QueueItem, token: str, now: datetime) -> LeaseConflictError:
    if row.state != ItemState.LEASED.value:
        return LeaseConflictError(f"条目 '{row.request_id}' 未处于租用状态")
    if row.lease_token != token:
        return LeaseConflictError("租约令牌不匹配")
    return LeaseConflictError("租约已过期，条目可能已被回收")


def renew(
    db: Session,
    *,
    request_id: str,
    token: str,
    lease_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    instant = to_utc(now)
    _require_lease_seconds(lease_seconds)
    new_expires_at = instant + timedelta(seconds=lease_seconds)
    if repo.cas_renew(
        db, request_id=request_id, token=token, now=instant, new_expires_at=new_expires_at
    ):
        db.commit()
        row = repo.get_item(db, request_id)
        assert row is not None
        return _item_dict(row, _active_rule(db), instant)
    db.rollback()
    row = repo.get_item(db, request_id)
    if row is None:
        raise ItemNotFoundError(f"补录请求 '{request_id}' 不存在")
    raise _lease_conflict(row, token, instant)


def return_item(
    db: Session,
    *,
    request_id: str,
    token: str,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    """退回补证：条目回到待处理，wait_anchor 保持不变以保留原等待时间。"""
    instant = to_utc(now)
    if repo.cas_return(db, request_id=request_id, token=token, reason=reason, now=instant):
        db.commit()
        row = repo.get_item(db, request_id)
        assert row is not None
        return _item_dict(row, _active_rule(db), instant)
    db.rollback()
    row = repo.get_item(db, request_id)
    if row is None:
        raise ItemNotFoundError(f"补录请求 '{request_id}' 不存在")
    raise _lease_conflict(row, token, instant)


def complete(
    db: Session,
    *,
    request_id: str,
    token: str,
    now: datetime,
) -> dict[str, Any]:
    instant = to_utc(now)
    if repo.cas_complete(db, request_id=request_id, token=token, now=instant):
        db.commit()
        row = repo.get_item(db, request_id)
        assert row is not None
        return _item_dict(row, _active_rule(db), instant)
    db.rollback()
    row = repo.get_item(db, request_id)
    if row is None:
        raise ItemNotFoundError(f"补录请求 '{request_id}' 不存在")
    raise _lease_conflict(row, token, instant)


def stats(db: Session, *, now: datetime) -> dict[str, Any]:
    instant = to_utc(now)
    rows = repo.list_items(db)
    entries = [_to_entry(row) for row in rows]
    claimable = [e for e in entries if is_claimable(e, instant)]
    waits = [wait_seconds(e, instant) for e in claimable]
    rule_row = _active_rule_row(db)
    return {
        "generated_at": instant,
        "active_rule": _rule_dict(rule_row, active=True) if rule_row is not None else None,
        "pending": sum(1 for e in entries if e.state is ItemState.PENDING),
        "leased": sum(1 for e in entries if e.state is ItemState.LEASED),
        "completed": sum(1 for e in entries if e.state is ItemState.COMPLETED),
        "claimable": len(claimable),
        "expired_leases": sum(
            1
            for e in entries
            if e.state is ItemState.LEASED and not lease_active(e, instant)
        ),
        "total_returns": sum(e.return_count for e in entries),
        "oldest_wait_seconds": max(waits) if waits else None,
        "average_wait_seconds": (sum(waits) / len(waits)) if waits else None,
    }
