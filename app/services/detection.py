"""Humidity jump detection — the core QC algorithm.

Rules (from the field-QC requirement):

* Only *currently valid* readings participate: ``is_active = true`` and not
  void. Superseded revisions and voided readings never appear.
* Readings are ordered by sampling time. Two readings are eligible as a pair
  only when they are *adjacent* in that ordering.
* When several valid readings share one sampling time, nothing is paired
  arbitrarily: that sampling time is reported as ``pending_check``.
* A pair is returned when the absolute humidity difference exceeds the
  threshold, together with the effective conclusion (if any) recorded for the
  pair identity.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Reading, ReviewConclusion
from ..schemas import ReviewPair


def _pair_key(a_no: str, b_no: str) -> tuple[str, str]:
    return tuple(sorted((a_no, b_no)))  # type: ignore[return-value]


def load_active_readings(session: Session, field_id: int) -> list[Reading]:
    stmt = (
        select(Reading)
        .where(
            Reading.field_id == field_id,
            Reading.is_active.is_(True),
            Reading.is_void.is_(False),
        )
        .order_by(Reading.sampled_at.asc(), Reading.reading_no.asc())
    )
    return list(session.execute(stmt).scalars().all())


def load_effective_conclusions(
    session: Session, field_id: int
) -> dict[tuple[str, str], ReviewConclusion]:
    stmt = select(ReviewConclusion).where(
        ReviewConclusion.field_id == field_id,
        ReviewConclusion.is_effective.is_(True),
    )
    out: dict[tuple[str, str], ReviewConclusion] = {}
    for c in session.execute(stmt).scalars().all():
        out[_pair_key(c.reading_no_a, c.reading_no_b)] = c
    return out


def detect_pairs(
    session: Session, field_id: int, threshold: Decimal
) -> tuple[list[ReviewPair], list]:
    readings = load_active_readings(session, field_id)
    conclusions = load_effective_conclusions(session, field_id)

    # Group readings by sampling time. Times holding more than one valid
    # reading are ambiguous -> pending_check, never auto-paired.
    by_time: dict = defaultdict(list)
    for r in readings:
        by_time[r.sampled_at].append(r)

    pending_times = sorted(t for t, group in by_time.items() if len(group) > 1)
    pending_set = set(pending_times)

    # Ordered sequence of unambiguous readings (one per sampling time).
    sequence = [r for r in readings if r.sampled_at not in pending_set]

    pairs: list[ReviewPair] = []
    for a, b in zip(sequence, sequence[1:]):
        diff = abs(Decimal(b.humidity) - Decimal(a.humidity))
        if diff <= threshold:
            continue
        key = _pair_key(a.reading_no, b.reading_no)
        conclusion = conclusions.get(key)
        pairs.append(
            ReviewPair(
                reading_no_a=a.reading_no,
                revision_a=a.revision,
                reading_no_b=b.reading_no,
                revision_b=b.revision,
                sampled_at_a=a.sampled_at,
                sampled_at_b=b.sampled_at,
                humidity_a=Decimal(a.humidity),
                humidity_b=Decimal(b.humidity),
                humidity_diff=diff,
                conclusion_status="exceeds_threshold",
                decision=conclusion.decision if conclusion else None,
                conclusion_id=conclusion.id if conclusion else None,
                decided_by=conclusion.reviewer if conclusion else None,
                decided_at=conclusion.created_at if conclusion else None,
                decided_revision_a=conclusion.revision_a if conclusion else None,
                decided_revision_b=conclusion.revision_b if conclusion else None,
            )
        )

    # Ambiguous sampling times: expose every involved reading as a
    # pending_check marker, without inventing pairs or differences.
    for t in pending_times:
        for r in by_time[t]:
            pairs.append(
                ReviewPair(
                    reading_no_a=r.reading_no,
                    revision_a=r.revision,
                    reading_no_b="",
                    revision_b=0,
                    sampled_at_a=r.sampled_at,
                    sampled_at_b=r.sampled_at,
                    humidity_a=Decimal(r.humidity),
                    humidity_b=Decimal("0"),
                    humidity_diff=Decimal("0"),
                    conclusion_status="pending_check",
                    decision=None,
                    conclusion_id=None,
                    decided_by=None,
                    decided_at=None,
                    decided_revision_a=None,
                    decided_revision_b=None,
                )
            )

    return pairs, pending_times
