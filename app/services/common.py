"""Shared service helpers: version counters and event logging."""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy.orm import Session

from ..models import EventLog, FieldVersion


def get_version(session: Session, field_id: int) -> int:
    row = session.get(FieldVersion, field_id)
    if row is None:
        row = FieldVersion(field_id=field_id, version=1)
        session.add(row)
        session.flush()
    return row.version


def bump_version(session: Session, field_id: int) -> int:
    row = session.get(FieldVersion, field_id)
    if row is None:
        row = FieldVersion(field_id=field_id, version=1)
        session.add(row)
        session.flush()
    row.version += 1
    session.flush()
    return row.version


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def log_event(
    session: Session,
    field_id: int | None,
    event_type: str,
    version: int,
    payload: dict,
) -> None:
    session.add(
        EventLog(
            field_id=field_id,
            event_type=event_type,
            version=version,
            payload=json.dumps(payload, ensure_ascii=False, default=_json_default),
        )
    )


def parse_event(row: EventLog) -> dict:
    return json.loads(row.payload)
