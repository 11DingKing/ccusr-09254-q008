"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class QueueRuleIn(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=128)
    graduating_weight: float = Field(100.0, ge=0)
    materials_weight: float = Field(40.0, ge=0)
    aging_weight_per_hour: float = Field(5.0, ge=0)
    aging_cap: float | None = Field(None, ge=0)


class QueueRuleOut(BaseModel):
    rule_id: str
    version: int
    graduating_weight: float
    materials_weight: float
    aging_weight_per_hour: float
    aging_cap: float | None
    active: bool
    created_at: datetime


class QueueEnqueueIn(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    graduating: bool = False
    materials_complete: bool = False
    note: str = Field("", max_length=512)


class QueueItemOut(BaseModel):
    request_id: str
    student_id: str
    state: str
    graduating: bool
    materials_complete: bool
    note: str
    enqueued_at: datetime
    wait_anchor: datetime
    wait_seconds: float
    score: float | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    return_count: int
    last_return_reason: str | None
    completed_at: datetime | None
    updated_at: datetime


class QueueClaimIn(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=128)
    lease_seconds: int = Field(300, gt=0, le=86400)


class QueueClaimOut(BaseModel):
    item: QueueItemOut
    lease_token: str
    lease_expires_at: datetime


class QueueRenewIn(BaseModel):
    lease_token: str = Field(..., min_length=1, max_length=64)
    lease_seconds: int = Field(300, gt=0, le=86400)


class QueueReturnIn(BaseModel):
    lease_token: str = Field(..., min_length=1, max_length=64)
    reason: str = Field("", max_length=512)


class QueueCompleteIn(BaseModel):
    lease_token: str = Field(..., min_length=1, max_length=64)


class QueuePreviewOut(BaseModel):
    now: datetime
    items: list[QueueItemOut]


class QueueStatsOut(BaseModel):
    generated_at: datetime
    active_rule: QueueRuleOut | None
    pending: int
    leased: int
    completed: int
    claimable: int
    expired_leases: int
    total_returns: int
    oldest_wait_seconds: float | None
    average_wait_seconds: float | None
