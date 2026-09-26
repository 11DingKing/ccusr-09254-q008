"""补录公平队列的 HTTP 接口：规则版本、入队、认领、续租、退回、完成、查看与统计。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import (
    QueueClaimIn,
    QueueEnqueueIn,
    QueueItemOut,
    QueueLeaseIn,
    QueueReturnIn,
    QueueRuleIn,
    QueueRuleOut,
    QueueStatsOut,
)
from ..queue import service as queue_service

router = APIRouter(prefix="/api/queue")


@router.post(
    "/rules",
    response_model=QueueRuleOut,
    status_code=status.HTTP_201_CREATED,
)
def publish_rule(body: QueueRuleIn, db: Session = Depends(get_db)) -> Any:
    try:
        return queue_service.publish_rule(
            db,
            graduating_weight=body.graduating_weight,
            materials_weight=body.materials_weight,
            aging_per_hour=body.aging_per_hour,
        )
    except queue_service.QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rules", response_model=list[QueueRuleOut])
def list_rules(db: Session = Depends(get_db)) -> Any:
    return queue_service.list_rules(db)


@router.post(
    "/items",
    response_model=QueueItemOut,
    status_code=status.HTTP_201_CREATED,
)
def enqueue(body: QueueEnqueueIn, db: Session = Depends(get_db)) -> Any:
    return queue_service.enqueue(
        db,
        student_id=body.student_id,
        request_type=body.request_type,
        is_graduating=body.is_graduating,
        materials_ready=body.materials_ready,
        payload=body.payload,
    )


@router.get("/items", response_model=list[QueueItemOut])
def list_items(status: str | None = None, db: Session = Depends(get_db)) -> Any:
    if status is not None and status not in {"waiting", "leased", "completed"}:
        raise HTTPException(status_code=400, detail="非法状态过滤")
    return queue_service.peek(db, status=status)


@router.post("/claim", response_model=QueueItemOut)
def claim(body: QueueClaimIn, db: Session = Depends(get_db)) -> Any:
    """认领当前评分最高的可处理条目；队列为空时返回 204。"""
    try:
        item = queue_service.claim(
            db, worker_id=body.worker_id, lease_seconds=body.lease_seconds
        )
    except queue_service.QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return item


@router.post("/items/{item_id}/renew", response_model=QueueItemOut)
def renew_item(item_id: int, body: QueueLeaseIn, db: Session = Depends(get_db)) -> Any:
    try:
        return queue_service.renew(
            db,
            item_id=item_id,
            lease_token=body.lease_token,
            lease_seconds=body.lease_seconds,
        )
    except queue_service.ItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except queue_service.LeaseInvalidError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except queue_service.QueueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/items/{item_id}/return", response_model=QueueItemOut)
def return_item(item_id: int, body: QueueReturnIn, db: Session = Depends(get_db)) -> Any:
    """退回补证：等待时间从首次入队起连续计算，不重新排队。"""
    try:
        return queue_service.return_item(
            db,
            item_id=item_id,
            lease_token=body.lease_token,
            reason=body.reason,
        )
    except queue_service.ItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except queue_service.LeaseInvalidError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/items/{item_id}/complete", response_model=QueueItemOut)
def complete_item(item_id: int, body: QueueLeaseIn, db: Session = Depends(get_db)) -> Any:
    try:
        return queue_service.complete_item(
            db, item_id=item_id, lease_token=body.lease_token
        )
    except queue_service.ItemNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except queue_service.LeaseInvalidError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/stats", response_model=QueueStatsOut)
def get_stats(db: Session = Depends(get_db)) -> Any:
    return queue_service.stats(db)
