"""Upload / erratum / void / late-arrival service with concurrency control."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import field_lock
from ..errors import ConflictError, NotFoundError, StaleVersionError
from ..models import Field, Reading, ReviewConclusion
from ..schemas import UploadBatch
from .common import bump_version, get_version, log_event


def _get_field(session: Session, field_id: int) -> Field:
    field = session.get(Field, field_id)
    if field is None:
        raise NotFoundError(f"田块 {field_id} 不存在")
    return field


def _current_active(session: Session, field_id: int, reading_no: str) -> Reading | None:
    stmt = select(Reading).where(
        Reading.field_id == field_id,
        Reading.reading_no == reading_no,
        Reading.is_active.is_(True),
    )
    return session.execute(stmt).scalar_one_or_none()


def invalidate_affected_conclusions(
    session: Session,
    field_id: int,
    affected_reading_nos: set[str],
    reason: str,
) -> list[int]:
    """Mark effective conclusions invalidated by a data/adjacency change.

    Old rows are preserved (append-only history) but lose ``is_effective``,
    so they no longer show up as valid conclusions while remaining replayable.
    """
    if not affected_reading_nos:
        return []
    stmt = select(ReviewConclusion).where(
        ReviewConclusion.field_id == field_id,
        ReviewConclusion.is_effective.is_(True),
    )
    invalidated: list[int] = []
    for c in session.execute(stmt).scalars().all():
        if (
            c.reading_no_a in affected_reading_nos
            or c.reading_no_b in affected_reading_nos
        ):
            c.is_effective = False
            c.invalidate_reason = reason
            invalidated.append(c.id)
    return invalidated


def apply_upload(
    session: Session, field_id: int, batch: UploadBatch
) -> tuple[int, int, list[Reading], list[int]]:
    """Apply one batch inside the caller's transaction.

    The advisory/row lock serializes writers on this field; the expected
    version then rejects any stale submission without touching prior data.
    """
    _get_field(session, field_id)
    field_lock(session, field_id)

    version_before = get_version(session, field_id)
    if batch.expected_version is not None and batch.expected_version != version_before:
        raise StaleVersionError(batch.expected_version, version_before)

    applied: list[Reading] = []
    affected: set[str] = set()

    for item in batch.readings:
        existing = _current_active(session, field_id, item.reading_no)

        if item.change_type == "upload":
            if existing is not None:
                raise ConflictError(
                    f"读数号 {item.reading_no} 已存在（当前修订号 {existing.revision}），"
                    "勘误请使用 correction，作废请使用 void"
                )
            row = Reading(
                field_id=field_id,
                reading_no=item.reading_no,
                revision=1,
                humidity=item.humidity,
                sampled_at=item.sampled_at,
                is_late=False,
                change_type="upload",
                note=item.note,
            )
            session.add(row)

        elif item.change_type == "late":
            if existing is not None:
                raise ConflictError(
                    f"读数号 {item.reading_no} 已存在，迟到读数不能重复上传"
                )
            row = Reading(
                field_id=field_id,
                reading_no=item.reading_no,
                revision=1,
                humidity=item.humidity,
                sampled_at=item.sampled_at,
                is_late=True,
                change_type="late",
                note=item.note,
            )
            session.add(row)

        elif item.change_type == "correction":
            if existing is None:
                raise NotFoundError(
                    f"读数号 {item.reading_no} 不存在或已失效，无法勘误"
                )
            old_rev = existing.revision
            existing.is_active = False
            row = Reading(
                field_id=field_id,
                reading_no=item.reading_no,
                revision=old_rev + 1,
                humidity=item.humidity,
                sampled_at=item.sampled_at,
                is_late=existing.is_late,
                change_type="correction",
                supersedes_id=existing.id,
                note=item.note,
            )
            session.add(row)

        else:  # void
            if existing is None:
                raise NotFoundError(
                    f"读数号 {item.reading_no} 不存在或已失效，无法作废"
                )
            existing.is_active = False
            existing.is_void = True
            row = existing  # void keeps one row, marked inactive+void

        session.flush()
        applied.append(row)
        affected.add(item.reading_no)

    # Any value/adjacency change can invalidate prior conclusions touching
    # the involved reading numbers. History rows stay in the table. Flush so
    # the partial unique index observes the rows as ineffective.
    invalidated = invalidate_affected_conclusions(
        session, field_id, affected, reason="superseded_by_upload"
    )
    session.flush()

    version_after = bump_version(session, field_id)

    log_event(
        session,
        field_id,
        event_type="upload_batch",
        version=version_after,
        payload={
            "version_before": version_before,
            "version_after": version_after,
            "items": [i.model_dump(mode="json") for i in batch.readings],
            "invalidated_conclusions": invalidated,
        },
    )
    return version_before, version_after, applied, invalidated


def list_readings_history(session: Session, field_id: int) -> list[Reading]:
    _get_field(session, field_id)
    stmt = (
        select(Reading)
        .where(Reading.field_id == field_id)
        .order_by(Reading.received_at.asc(), Reading.id.asc())
    )
    return list(session.execute(stmt).scalars().all())
