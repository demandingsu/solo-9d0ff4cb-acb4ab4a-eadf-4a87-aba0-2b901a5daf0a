"""Concurrency tests simulating two instances sharing one database.

The optimistic version protocol (field_versions token) is database agnostic:
on PostgreSQL it is combined with a transaction-scoped advisory lock; these
tests exercise the same stale-submission rejection on SQLite.
"""
from __future__ import annotations

import threading
from decimal import Decimal

from sqlalchemy import select

from app.db import session_scope, snapshot_session
from app.models import ReviewConclusion
from app.schemas import ReadingUpload, ReviewDecisionIn, UploadBatch
from app.services import review, upload
from app.services.common import get_version


def _jump_batch(version=None):
    return UploadBatch(
        expected_version=version,
        readings=[
            ReadingUpload(reading_no="R1", humidity="30.0",
                          sampled_at="2026-11-01T08:00:00Z"),
            ReadingUpload(reading_no="R2", humidity="80.0",
                          sampled_at="2026-11-01T09:00:00Z"),
        ],
    )


def test_concurrent_uploads_one_succeeds_one_stale(field):
    fid = field["id"]
    # Both threads compute against version 1 before racing to commit.
    barrier = threading.Barrier(2)
    results = {}

    def worker(name, reading_no):
        barrier.wait()  # align outside any transaction
        with session_scope() as s:
            try:
                upload.apply_upload(
                    s, fid,
                    UploadBatch(
                        expected_version=1,
                        readings=[
                            ReadingUpload(
                                reading_no=reading_no, humidity="33.0",
                                sampled_at="2026-11-01T10:00:00Z",
                            )
                        ],
                    ),
                )
                results[name] = "ok"
            except Exception as exc:  # noqa: BLE001
                results[name] = type(exc).__name__

    t1 = threading.Thread(target=worker, args=("A", "RA"))
    t2 = threading.Thread(target=worker, args=("B", "RB"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == ["StaleVersionError", "ok"]
    with session_scope() as s:
        assert get_version(s, fid) == 2  # only one batch applied


def test_concurrent_review_decisions_second_is_stale(field):
    fid = field["id"]
    with session_scope() as s:
        upload.apply_upload(s, fid, _jump_batch())
        ver = get_version(s, fid)

    barrier = threading.Barrier(2)
    outcomes = {}

    def decide(name, decision):
        barrier.wait()
        with session_scope() as s:
            try:
                review.submit_decision(
                    s, fid,
                    ReviewDecisionIn(
                        reading_no_a="R1", reading_no_b="R2",
                        decision=decision, base_version=ver, threshold="10",
                    ),
                )
                outcomes[name] = "ok"
            except Exception as exc:  # noqa: BLE001
                outcomes[name] = type(exc).__name__

    t1 = threading.Thread(target=decide, args=("A", "confirmed"))
    t2 = threading.Thread(target=decide, args=("B", "rejected"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(outcomes.values()) == ["StaleVersionError", "ok"]

    with session_scope() as s:
        rows = list(s.execute(
            select(ReviewConclusion).where(ReviewConclusion.field_id == fid)
        ).scalars())
        effective = [r for r in rows if r.is_effective]
        assert len(effective) == 1
        winner = "A" if outcomes["A"] == "ok" else "B"
        assert effective[0].decision == ("confirmed" if winner == "A" else "rejected")


def test_upload_racing_review_decision_makes_decision_stale(field):
    """An upload landing between a reviewer's query and submit must 409."""
    fid = field["id"]
    with session_scope() as s:
        upload.apply_upload(s, fid, _jump_batch())
        ver = get_version(s, fid)

    # Reviewer holds base_version=ver; meanwhile an erratum is uploaded.
    with session_scope() as s:
        upload.apply_upload(
            s, fid,
            UploadBatch(readings=[
                ReadingUpload(reading_no="R2", humidity="81.0",
                              sampled_at="2026-11-01T09:00:00Z",
                              change_type="correction")
            ]),
        )

    with session_scope() as s:
        try:
            review.submit_decision(
                s, fid,
                ReviewDecisionIn(reading_no_a="R1", reading_no_b="R2",
                                 decision="confirmed", base_version=ver,
                                 threshold="10"),
            )
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = type(exc).__name__
    assert raised == "StaleVersionError"


def test_snapshot_query_pair_and_conclusion_consistent(field):
    """Pair list and attached conclusions are read inside one snapshot."""
    fid = field["id"]
    with session_scope() as s:
        upload.apply_upload(s, fid, _jump_batch())
        ver = get_version(s, fid)

    with session_scope() as s:
        review.submit_decision(
            s, fid,
            ReviewDecisionIn(reading_no_a="R1", reading_no_b="R2",
                             decision="confirmed", base_version=ver,
                             threshold="10"),
        )

    with snapshot_session() as s:
        q = review.query_review(s, fid, Decimal("10"))
        assert q.snapshot_version == ver + 1
        flagged = [p for p in q.pairs if p.conclusion_status == "exceeds_threshold"]
        assert len(flagged) == 1
        assert flagged[0].decision == "confirmed"
        assert flagged[0].revision_a == 1
