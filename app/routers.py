"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import queue_services, services
from .core.workqueue import Clock, SystemClock
from .db import get_db
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    QueueClaimIn,
    QueueClaimOut,
    QueueCompleteIn,
    QueueEnqueueIn,
    QueueItemOut,
    QueuePreviewOut,
    QueueRenewIn,
    QueueReturnIn,
    QueueRuleIn,
    QueueRuleOut,
    QueueStatsOut,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


def get_clock() -> Clock:
    """队列领域的时钟来源，测试可替换为可推进的手动时钟。"""
    return SystemClock()


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---- 补录工作队列 ----


@router.post(
    "/queue/rules",
    response_model=QueueRuleOut,
    status_code=status.HTTP_201_CREATED,
    tags=["queue"],
)
def create_queue_rule(
    body: QueueRuleIn,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.create_rule(
        db,
        rule_id=body.rule_id,
        graduating_weight=body.graduating_weight,
        materials_weight=body.materials_weight,
        aging_weight_per_hour=body.aging_weight_per_hour,
        aging_cap=body.aging_cap,
        now=clock.now(),
    )


@router.get("/queue/rules", response_model=list[QueueRuleOut], tags=["queue"])
def list_queue_rules(db: Session = Depends(get_db)) -> Any:
    return queue_services.list_rules(db)


@router.post(
    "/queue/rules/{rule_id}/versions/{version}/activate",
    response_model=QueueRuleOut,
    tags=["queue"],
)
def activate_queue_rule(
    rule_id: str,
    version: int,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.activate_rule(
        db, rule_id=rule_id, version=version, now=clock.now()
    )


@router.post(
    "/queue/items",
    response_model=QueueItemOut,
    status_code=status.HTTP_201_CREATED,
    tags=["queue"],
)
def enqueue_item(
    body: QueueEnqueueIn,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.enqueue(
        db,
        request_id=body.request_id,
        student_id=body.student_id,
        graduating=body.graduating,
        materials_complete=body.materials_complete,
        note=body.note,
        now=clock.now(),
    )


@router.get("/queue/items", response_model=QueuePreviewOut, tags=["queue"])
def preview_queue(
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.preview(db, now=clock.now())


@router.post("/queue/claim", response_model=QueueClaimOut, tags=["queue"])
def claim_item(
    body: QueueClaimIn,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.claim(
        db,
        worker_id=body.worker_id,
        lease_seconds=body.lease_seconds,
        now=clock.now(),
    )


@router.post(
    "/queue/items/{request_id}/renew",
    response_model=QueueItemOut,
    tags=["queue"],
)
def renew_item(
    request_id: str,
    body: QueueRenewIn,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.renew(
        db,
        request_id=request_id,
        token=body.lease_token,
        lease_seconds=body.lease_seconds,
        now=clock.now(),
    )


@router.post(
    "/queue/items/{request_id}/return",
    response_model=QueueItemOut,
    tags=["queue"],
)
def return_item(
    request_id: str,
    body: QueueReturnIn,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.return_item(
        db,
        request_id=request_id,
        token=body.lease_token,
        reason=body.reason,
        now=clock.now(),
    )


@router.post(
    "/queue/items/{request_id}/complete",
    response_model=QueueItemOut,
    tags=["queue"],
)
def complete_item(
    request_id: str,
    body: QueueCompleteIn,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.complete(
        db, request_id=request_id, token=body.lease_token, now=clock.now()
    )


@router.get("/queue/stats", response_model=QueueStatsOut, tags=["queue"])
def queue_stats(
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
) -> Any:
    return queue_services.stats(db, now=clock.now())
