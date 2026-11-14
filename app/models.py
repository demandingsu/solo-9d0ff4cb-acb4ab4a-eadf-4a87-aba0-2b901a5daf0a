"""ORM models for moisture readings, review conclusions and the audit log."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    DDL,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Field(Base):
    __tablename__ = "fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    readings: Mapped[list["Reading"]] = relationship(back_populates="field")


class Reading(Base):
    """One versioned moisture reading.

    Every upload that changes a reading (erratum / correction) inserts a new
    row with a bumped ``revision`` and ``supersedes_id`` pointing at the row it
    replaces.  Voiding marks the current row inactive without deleting it, so
    the full history — including late arrivals — stays traceable.
    """

    __tablename__ = "readings"
    __table_args__ = (
        # Full-history lookup index; the "one active row per reading number"
        # rule is enforced by a dialect-specific partial index attached below.
        Index("ix_readings_field_no", "field_id", "reading_no"),
        CheckConstraint("humidity >= 0 AND humidity <= 100", name="ck_humidity_range"),
        CheckConstraint("revision >= 1", name="ck_revision_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reading_no: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    humidity: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_void: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False, default="upload")
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("readings.id"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)

    field: Mapped[Field] = relationship(back_populates="readings")


class FieldVersion(Base):
    """Per-field monotonic version counter (optimistic concurrency token)."""

    __tablename__ = "field_versions"

    field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ReviewConclusion(Base):
    """A QC reviewer's decision on one detected reading pair.

    Rows are append-only: a changed value or changed adjacency invalidates the
    old conclusion (``is_effective = false``) and a new decision inserts a new
    row. Old rows are never updated or deleted, preserving the audit trail.
    """

    __tablename__ = "review_conclusions"
    __table_args__ = (
        Index("ix_conclusion_field", "field_id"),
        CheckConstraint(
            "decision IN ('confirmed','rejected')", name="ck_decision_value"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reading_no_a: Mapped[str] = mapped_column(String(64), nullable=False)
    reading_no_b: Mapped[str] = mapped_column(String(64), nullable=False)
    # Snapshot of the revisions/difference the reviewer actually saw.
    revision_a: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_b: Mapped[int] = mapped_column(Integer, nullable=False)
    humidity_diff: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False, default="qc")
    # Version the client based its decision on; stale submissions are rejected.
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_effective: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invalidate_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EventLog(Base):
    """Append-only domain event stream powering history replay."""

    __tablename__ = "event_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    field_id: Mapped[int | None] = mapped_column(
        ForeignKey("fields.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --- Dialect-specific partial unique indexes ---------------------------------
# At most one ACTIVE reading row per (field, reading_no) and at most one
# EFFECTIVE conclusion per pair identity, while superseded/voided/history
# rows are kept untouched for traceability.
_PG_ACTIVE_READING = DDL(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_readings_active_no "
    "ON readings (field_id, reading_no) WHERE is_active"
)
_SQLITE_ACTIVE_READING = DDL(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_readings_active_no "
    "ON readings (field_id, reading_no) WHERE is_active = 1"
)
_PG_EFFECTIVE_CONCLUSION = DDL(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_conclusion_effective_pair "
    "ON review_conclusions (field_id, reading_no_a, reading_no_b) "
    "WHERE is_effective"
)
_SQLITE_EFFECTIVE_CONCLUSION = DDL(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_conclusion_effective_pair "
    "ON review_conclusions (field_id, reading_no_a, reading_no_b) "
    "WHERE is_effective = 1"
)

event.listen(
    Reading.__table__,
    "after_create",
    _PG_ACTIVE_READING.execute_if(dialect="postgresql"),
)
event.listen(
    Reading.__table__,
    "after_create",
    _SQLITE_ACTIVE_READING.execute_if(dialect="sqlite"),
)
event.listen(
    ReviewConclusion.__table__,
    "after_create",
    _PG_EFFECTIVE_CONCLUSION.execute_if(dialect="postgresql"),
)
event.listen(
    ReviewConclusion.__table__,
    "after_create",
    _SQLITE_EFFECTIVE_CONCLUSION.execute_if(dialect="sqlite"),
)
