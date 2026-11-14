"""Field management and history replay service."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ConflictError, NotFoundError
from ..models import EventLog, Field
from .common import get_version, log_event, parse_event


def create_field(session: Session, code: str, name: str) -> Field:
    existing = session.execute(select(Field).where(Field.code == code)).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"田块编码 {code} 已存在")
    field = Field(code=code, name=name)
    session.add(field)
    session.flush()
    get_version(session, field.id)  # initialize version row at 1
    log_event(
        session,
        field.id,
        event_type="field_created",
        version=1,
        payload={"code": code, "name": name},
    )
    return field


def list_fields(session: Session) -> list[Field]:
    return list(session.execute(select(Field).order_by(Field.id)).scalars().all())


def get_field_or_404(session: Session, field_id: int) -> Field:
    field = session.get(Field, field_id)
    if field is None:
        raise NotFoundError(f"田块 {field_id} 不存在")
    return field


def replay_events(
    session: Session,
    field_id: int | None = None,
    after_seq: int = 0,
    limit: int = 500,
) -> list[dict]:
    """Replay the append-only event stream (history playback)."""
    stmt = select(EventLog).where(EventLog.seq > after_seq)
    if field_id is not None:
        stmt = stmt.where(EventLog.field_id == field_id)
    stmt = stmt.order_by(EventLog.seq.asc()).limit(limit)
    rows = session.execute(stmt).scalars().all()
    return [
        {
            "seq": r.seq,
            "field_id": r.field_id,
            "event_type": r.event_type,
            "version": r.version,
            "payload": parse_event(r),
            "created_at": r.created_at,
        }
        for r in rows
    ]
