"""Review query (snapshot reads) and reviewer decision service."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import field_lock
from ..errors import ConflictError, NotFoundError, StaleVersionError
from ..models import Field, ReviewConclusion
from ..schemas import ReviewDecisionIn, ReviewQueryResult
from .common import bump_version, get_version, log_event
from .detection import detect_pairs, load_active_readings


def _get_field(session: Session, field_id: int) -> Field:
    field = session.get(Field, field_id)
    if field is None:
        raise NotFoundError(f"田块 {field_id} 不存在")
    return field


def query_review(
    session: Session, field_id: int, threshold: Decimal
) -> ReviewQueryResult:
    """Return pairs AND conclusions from one repeatable-read snapshot.

    The caller opens the transaction via ``snapshot_session`` (REPEATABLE
    READ on Postgres), so concurrent uploads/reviews cannot make the pair
    list and the attached conclusions come from different states.
    """
    field = _get_field(session, field_id)
    if threshold < 0:
        raise ConflictError("差值阈值必须为非负数")

    pairs, pending_times = detect_pairs(session, field_id, threshold)
    snapshot_version = get_version(session, field_id)
    return ReviewQueryResult(
        field_id=field_id,
        field_code=field.code,
        threshold=threshold,
        snapshot_version=snapshot_version,
        pairs=pairs,
        pending_times=pending_times,
    )


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def submit_decision(
    session: Session, field_id: int, body: ReviewDecisionIn
) -> ReviewConclusion:
    """Confirm or reject one detected pair.

    Concurrency guarantees:
    * ``field_lock`` serializes decisions with uploads on the same field.
    * ``base_version`` must equal the current version — a stale submission is
      rejected with 409 and the previous conclusion is never rewritten.
    * The pair must still exist as adjacent valid readings over threshold in
      the current state; otherwise the decision is refused (409).
    * Conclusions are append-only: a re-decision supersedes the old row
      (``is_effective=false``) instead of updating it.
    """
    _get_field(session, field_id)
    field_lock(session, field_id)

    current_version = get_version(session, field_id)
    if body.base_version != current_version:
        raise StaleVersionError(body.base_version, current_version)

    if body.reading_no_a == body.reading_no_b:
        raise ConflictError("读数对必须由两个不同的读数号组成")

    # Re-validate against the *current* snapshot under lock: both readings
    # must still be active and adjacent in sampling-time order.
    active = load_active_readings(session, field_id)
    by_no = {r.reading_no: r for r in active}
    ra = by_no.get(body.reading_no_a)
    rb = by_no.get(body.reading_no_b)
    if ra is None or rb is None:
        raise ConflictError("读数对中的读数已失效或不存在，不能基于旧快照下结论；请重新查询")
    if ra.sampled_at == rb.sampled_at:
        raise ConflictError("同一采样时刻存在多条读数，标记为待核，不能直接配对下结论")

    ordered = sorted(active, key=lambda r: (r.sampled_at, r.reading_no))
    idx = {r.reading_no: i for i, r in enumerate(ordered)}
    ia, ib = idx[body.reading_no_a], idx[body.reading_no_b]
    if abs(ia - ib) != 1:
        raise ConflictError("两个读数当前已不相邻，旧相邻关系已改变；请重新查询")

    diff = abs(Decimal(rb.humidity) - Decimal(ra.humidity))

    # Supersede any prior effective conclusion on this pair identity — the
    # old row is retained, never overwritten. Flush before inserting so the
    # partial unique index sees the old row as ineffective.
    key = _pair_key(body.reading_no_a, body.reading_no_b)
    stmt = select(ReviewConclusion).where(
        ReviewConclusion.field_id == field_id,
        ReviewConclusion.is_effective.is_(True),
    )
    for c in session.execute(stmt).scalars().all():
        if _pair_key(c.reading_no_a, c.reading_no_b) == key:
            c.is_effective = False
            c.invalidate_reason = "superseded_by_new_decision"
    session.flush()

    conclusion = ReviewConclusion(
        field_id=field_id,
        # Canonicalize the stored order so the partial unique index and pair
        # identity are independent of the direction the client submitted.
        reading_no_a=key[0],
        reading_no_b=key[1],
        revision_a=ra.revision if ra.reading_no == key[0] else rb.revision,
        revision_b=rb.revision if rb.reading_no == key[1] else ra.revision,
        humidity_diff=diff,
        threshold=body.threshold,
        decision=body.decision,
        reason=body.reason,
        reviewer=body.reviewer,
        base_version=body.base_version,
        is_effective=True,
    )
    session.add(conclusion)
    session.flush()

    new_version = bump_version(session, field_id)
    log_event(
        session,
        field_id,
        event_type="review_decision",
        version=new_version,
        payload={
            "conclusion_id": conclusion.id,
            "reading_no_a": body.reading_no_a,
            "reading_no_b": body.reading_no_b,
            "revision_a": ra.revision,
            "revision_b": rb.revision,
            "humidity_diff": str(diff),
            "decision": body.decision,
            "reason": body.reason,
            "reviewer": body.reviewer,
            "base_version": body.base_version,
            "version_after": new_version,
        },
    )
    return conclusion
