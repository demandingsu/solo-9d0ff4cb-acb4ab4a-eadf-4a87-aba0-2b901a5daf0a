"""HTTP API: fields, upload, review (new), history replay."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Query

from .db import session_scope, snapshot_session
from .models import ReviewConclusion
from .schemas import (
    EventOut,
    FieldCreate,
    FieldOut,
    ReadingOut,
    ReviewDecisionIn,
    ReviewDecisionOut,
    ReviewQueryResult,
    UploadBatch,
    UploadResult,
)
from .services import history, review, upload
from sqlalchemy import select

router = APIRouter()


# ---------- fields ----------

@router.post("/fields", response_model=FieldOut, tags=["fields"])
def create_field(body: FieldCreate):
    with session_scope() as s:
        f = history.create_field(s, body.code, body.name)
        return FieldOut.model_validate(f)


@router.get("/fields", response_model=list[FieldOut], tags=["fields"])
def list_fields():
    with session_scope() as s:
        return [FieldOut.model_validate(f) for f in history.list_fields(s)]


# ---------- upload / erratum / void / late ----------

@router.post(
    "/fields/{field_id}/readings/batch",
    response_model=UploadResult,
    tags=["upload"],
    responses={409: {"description": "过期版本（乐观锁冲突）或业务冲突"}},
)
def upload_batch(field_id: int, body: UploadBatch):
    with session_scope() as s:
        before, after, applied, invalidated = upload.apply_upload(s, field_id, body)
        return UploadResult(
            field_id=field_id,
            version_before=before,
            version_after=after,
            applied=[ReadingOut.model_validate(r) for r in applied],
            invalidated_conclusions=invalidated,
        )


@router.get(
    "/fields/{field_id}/readings",
    response_model=list[ReadingOut],
    tags=["upload"],
)
def reading_history(field_id: int):
    """Full version history of a field's readings (including voided/superseded)."""
    with session_scope() as s:
        rows = upload.list_readings_history(s, field_id)
        return [ReadingOut.model_validate(r) for r in rows]


# ---------- review (new QC API) ----------

@router.get(
    "/fields/{field_id}/review",
    response_model=ReviewQueryResult,
    tags=["review"],
    summary="按田块和差值阈值查询湿度突跳读数对（同一快照）",
)
def get_review(
    field_id: int,
    threshold: Decimal = Query(..., ge=0, description="湿度差值阈值，超过即判为突跳"),
):
    # Snapshot isolation: pair list and conclusions always come from one
    # consistent database state, even while another instance uploads/reviews.
    with snapshot_session() as s:
        result = review.query_review(s, field_id, threshold)
        # Materialize ORM values before the session closes.
        return result.model_copy(deep=True)


@router.post(
    "/fields/{field_id}/review/decisions",
    response_model=ReviewDecisionOut,
    tags=["review"],
    summary="质控员确认或驳回一个突跳读数对",
    responses={
        409: {
            "description": "过期提交（base_version 不匹配）或读数对已失效/不相邻",
        }
    },
)
def decide(field_id: int, body: ReviewDecisionIn):
    with session_scope() as s:
        c = review.submit_decision(s, field_id, body)
        return ReviewDecisionOut.model_validate(c)


@router.get(
    "/fields/{field_id}/review/conclusions",
    response_model=list[ReviewDecisionOut],
    tags=["review"],
    summary="结论追溯：含已失效的历史结论",
)
def list_conclusions(field_id: int, include_inactive: bool = True):
    with snapshot_session() as s:
        history.get_field_or_404(s, field_id)
        q = select(ReviewConclusion).where(ReviewConclusion.field_id == field_id)
        if not include_inactive:
            q = q.where(ReviewConclusion.is_effective.is_(True))
        q = q.order_by(ReviewConclusion.id.asc())
        rows = s.execute(q).scalars().all()
        return [ReviewDecisionOut.model_validate(r) for r in rows]


# ---------- history replay ----------

@router.get(
    "/events",
    response_model=list[EventOut],
    tags=["history"],
    summary="历史回放：按事件序号顺序重放",
)
def replay(
    field_id: int | None = None,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
):
    with snapshot_session() as s:
        raw = history.replay_events(s, field_id, after_seq, limit)
        out: list[EventOut] = []
        for item in raw:
            out.append(
                EventOut(
                    seq=item["seq"],
                    field_id=item["field_id"],
                    event_type=item["event_type"],
                    version=item["version"],
                    payload=item["payload"],
                    created_at=item["created_at"],
                )
            )
        return out
