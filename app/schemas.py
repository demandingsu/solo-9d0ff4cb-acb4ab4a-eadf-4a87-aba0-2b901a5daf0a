"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import HUMIDITY_MAX, HUMIDITY_MIN


# ---------- field ----------

class FieldCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, examples=["F-01"])
    name: str = Field(min_length=1, max_length=255, examples=["北区一号田"])


class FieldOut(BaseModel):
    id: int
    code: str
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- upload ----------

class ReadingUpload(BaseModel):
    reading_no: str = Field(min_length=1, max_length=64, examples=["R1001"])
    humidity: Decimal = Field(description="湿度百分比，0~100，最多两位小数")
    sampled_at: datetime
    # upload | correction | void | late
    change_type: Literal["upload", "correction", "void", "late"] = "upload"
    note: str | None = Field(default=None, max_length=512)

    @field_validator("humidity")
    @classmethod
    def _check_range(cls, v: Decimal) -> Decimal:
        if not (Decimal(str(HUMIDITY_MIN)) <= v <= Decimal(str(HUMIDITY_MAX))):
            raise ValueError("humidity 必须在 0~100 之间")
        return v.quantize(Decimal("0.01"))


class UploadBatch(BaseModel):
    """One batch is applied transactionally and bumps the field version once."""

    expected_version: int | None = Field(
        default=None,
        description="乐观锁：上传前看到的田块版本号；并发冲突时返回 409",
    )
    readings: list[ReadingUpload] = Field(min_length=1)


class ReadingOut(BaseModel):
    id: int
    reading_no: str
    revision: int
    humidity: Decimal
    sampled_at: datetime
    received_at: datetime
    is_active: bool
    is_void: bool
    is_late: bool
    change_type: str
    note: str | None = None
    supersedes_id: int | None = None

    model_config = {"from_attributes": True}


class UploadResult(BaseModel):
    field_id: int
    version_before: int
    version_after: int
    applied: list[ReadingOut]
    # Conclusions that stopped being effective because of this mutation.
    invalidated_conclusions: list[int]


# ---------- review (new QC API) ----------

class ReviewPair(BaseModel):
    """One adjacent-in-sampling-time pair of currently valid readings."""

    reading_no_a: str
    revision_a: int
    reading_no_b: str
    revision_b: int
    sampled_at_a: datetime
    sampled_at_b: datetime
    humidity_a: Decimal
    humidity_b: Decimal
    humidity_diff: Decimal = Field(description="|h_b - h_a|，始终为非负数")
    # 超阈值 / 待核（同一采样时刻存在多条有效读数，不任意配对）
    conclusion_status: Literal["exceeds_threshold", "pending_check"]
    # Effective review conclusion on this pair identity, if any.
    decision: Literal["confirmed", "rejected"] | None = None
    conclusion_id: int | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decided_revision_a: int | None = None
    decided_revision_b: int | None = None


class ReviewQueryResult(BaseModel):
    field_id: int
    field_code: str
    threshold: Decimal
    # Snapshot token: the exact field version this answer was computed from.
    snapshot_version: int
    pairs: list[ReviewPair]
    # Pending-check markers for sampling times holding >1 valid reading.
    pending_times: list[datetime]


class ReviewDecisionIn(BaseModel):
    reading_no_a: str = Field(min_length=1, max_length=64)
    reading_no_b: str = Field(min_length=1, max_length=64)
    decision: Literal["confirmed", "rejected"]
    reason: str | None = Field(default=None, max_length=512)
    reviewer: str = Field(default="qc", min_length=1, max_length=128)
    # 复核查询时使用的差值阈值，随结论留档，便于追溯。
    threshold: Decimal = Field(default=Decimal("0"), ge=0)
    # Must match the snapshot_version returned by the review query. A stale
    # value means another writer changed the field first -> 409, no rewrite.
    base_version: int = Field(ge=1)


class ReviewDecisionOut(BaseModel):
    conclusion_id: int = Field(validation_alias="id")
    field_id: int
    reading_no_a: str
    reading_no_b: str
    revision_a: int
    revision_b: int
    humidity_diff: Decimal
    threshold: Decimal
    decision: str
    reason: str | None
    reviewer: str
    base_version: int
    is_effective: bool
    invalidate_reason: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class StaleError(BaseModel):
    detail: str
    current_version: int
    submitted_version: int


# ---------- history replay ----------

class EventOut(BaseModel):
    seq: int
    field_id: int | None
    event_type: str
    version: int
    payload: dict
    created_at: datetime

    model_config = {"from_attributes": True}
