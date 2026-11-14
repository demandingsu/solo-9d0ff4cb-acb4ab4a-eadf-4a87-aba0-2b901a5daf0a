"""End-to-end tests for upload, detection and the review API."""
from __future__ import annotations


def _upload(client, field_id, items, expected_version=None):
    body = {"readings": items}
    if expected_version is not None:
        body["expected_version"] = expected_version
    return client.post(f"/api/v1/fields/{field_id}/readings/batch", json=body)


def _review(client, field_id, threshold):
    return client.get(f"/api/v1/fields/{field_id}/review", params={"threshold": threshold})


def test_docs_and_openapi_available(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/api/v1/fields/{field_id}/review" in spec["paths"]


def test_upload_basic_and_version_bumps(client, field):
    fid = field["id"]
    r = _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "31.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["version_before"] == 1
    assert data["version_after"] == 2
    assert len(data["applied"]) == 2


def test_detect_adjacent_jump_pair(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "55.0", "sampled_at": "2026-11-01T09:00:00Z"},
        {"reading_no": "R3", "humidity": "56.0", "sampled_at": "2026-11-01T10:00:00Z"},
    ])
    r = _review(client, fid, 10)
    assert r.status_code == 200
    data = r.json()
    assert data["snapshot_version"] == 2
    # R1->R2 diff=25 exceeds 10; R2->R3 diff=1 does not.
    pairs = data["pairs"]
    assert len(pairs) == 1
    p = pairs[0]
    assert p["reading_no_a"] == "R1"
    assert p["reading_no_b"] == "R2"
    assert p["revision_a"] == 1 and p["revision_b"] == 1
    assert p["humidity_diff"] == "25.00"
    assert p["conclusion_status"] == "exceeds_threshold"
    assert p["decision"] is None

    # Threshold boundary: diff == threshold is NOT a jump.
    r2 = _review(client, fid, 25)
    assert r2.json()["pairs"] == []


def test_same_sampling_time_is_pending_not_paired(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "80.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R3", "humidity": "31.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ])
    r = _review(client, fid, 5)
    data = r.json()
    assert len(data["pending_times"]) == 1
    pending = [p for p in data["pairs"] if p["conclusion_status"] == "pending_check"]
    assert {p["reading_no_a"] for p in pending} == {"R1", "R2"}
    # No arbitrary exceeds-threshold pairing involving the ambiguous time.
    exceeds = [p for p in data["pairs"] if p["conclusion_status"] == "exceeds_threshold"]
    assert exceeds == []

    # Deciding on a same-time pair must be refused.
    ver = data["snapshot_version"]
    rd = client.post(
        f"/api/v1/fields/{fid}/review/decisions",
        json={
            "reading_no_a": "R1", "reading_no_b": "R2",
            "decision": "confirmed", "base_version": ver, "threshold": "5",
        },
    )
    assert rd.status_code == 409


def test_review_decision_confirm_and_reject(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "60.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ])
    ver = _review(client, fid, 10).json()["snapshot_version"]

    ok = client.post(
        f"/api/v1/fields/{fid}/review/decisions",
        json={
            "reading_no_a": "R1", "reading_no_b": "R2",
            "decision": "confirmed", "reason": "确为传感器异常",
            "reviewer": "qc-zhang", "base_version": ver, "threshold": "10",
        },
    )
    assert ok.status_code == 200, ok.text
    cid = ok.json()["conclusion_id"]
    assert ok.json()["is_effective"] is True

    # Query now carries the decision.
    again = _review(client, fid, 10).json()
    p = again["pairs"][0]
    assert p["decision"] == "confirmed"
    assert p["conclusion_id"] == cid
    assert p["decided_by"] == "qc-zhang"
    # Decision bumped the field version.
    assert again["snapshot_version"] == ver + 1

    # Re-deciding supersedes (append-only): old row retained but ineffective.
    ver2 = again["snapshot_version"]
    rd = client.post(
        f"/api/v1/fields/{fid}/review/decisions",
        json={
            "reading_no_a": "R2", "reading_no_b": "R1",  # reversed order, same identity
            "decision": "rejected", "base_version": ver2, "threshold": "10",
        },
    )
    assert rd.status_code == 200
    history = client.get(f"/api/v1/fields/{fid}/review/conclusions").json()
    assert len(history) == 2
    effective = [c for c in history if c["is_effective"]]
    assert len(effective) == 1
    assert effective[0]["decision"] == "rejected"
    old = [c for c in history if not c["is_effective"]][0]
    assert old["conclusion_id"] == cid
    assert old["invalidate_reason"] == "superseded_by_new_decision"


def test_stale_decision_is_rejected_and_never_rewrites(client, field):
    """Two reviewers / concurrent upload: stale base_version -> 409."""
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "60.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ])
    ver = _review(client, fid, 10).json()["snapshot_version"]

    # Reviewer A decides first.
    a = client.post(
        f"/api/v1/fields/{fid}/review/decisions",
        json={"reading_no_a": "R1", "reading_no_b": "R2",
              "decision": "confirmed", "base_version": ver, "threshold": "10"},
    )
    assert a.status_code == 200
    first_id = a.json()["conclusion_id"]

    # Reviewer B still holds the old snapshot version -> rejected.
    b = client.post(
        f"/api/v1/fields/{fid}/review/decisions",
        json={"reading_no_a": "R1", "reading_no_b": "R2",
              "decision": "rejected", "base_version": ver, "threshold": "10"},
    )
    assert b.status_code == 409
    body = b.json()
    assert body["submitted_version"] == ver
    assert body["current_version"] == ver + 1

    # First conclusion untouched and still effective.
    live = client.get(
        f"/api/v1/fields/{fid}/review/conclusions",
        params={"include_inactive": "false"},
    ).json()
    assert len(live) == 1
    assert live[0]["conclusion_id"] == first_id
    assert live[0]["decision"] == "confirmed"


def test_correction_invalidates_old_conclusion_and_creates_revision(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "60.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ])
    ver = _review(client, fid, 10).json()["snapshot_version"]
    client.post(
        f"/api/v1/fields/{fid}/review/decisions",
        json={"reading_no_a": "R1", "reading_no_b": "R2",
              "decision": "confirmed", "base_version": ver, "threshold": "10"},
    )

    # Erratum changes R2's humidity so the jump disappears.
    up = _upload(client, fid, [
        {"reading_no": "R2", "humidity": "31.0",
         "sampled_at": "2026-11-01T09:00:00Z", "change_type": "correction",
         "note": "勘误：探头接线松动"},
    ])
    assert up.status_code == 200
    assert up.json()["invalidated_conclusions"]

    # No jump any more; old conclusion no longer shown as effective.
    rev = _review(client, fid, 10).json()
    assert rev["pairs"] == []

    all_c = client.get(f"/api/v1/fields/{fid}/review/conclusions").json()
    assert all(c["is_effective"] is False for c in all_c)
    assert len(all_c) == 1  # history retained

    # Reading history shows revision 2 and superseded revision 1 inactive.
    readings = client.get(f"/api/v1/fields/{fid}/readings").json()
    r2_rows = [r for r in readings if r["reading_no"] == "R2"]
    assert sorted(r["revision"] for r in r2_rows) == [1, 2]
    active = [r for r in r2_rows if r["is_active"]]
    assert len(active) == 1 and active[0]["revision"] == 2
    assert active[0]["humidity"] == "31.00"
    assert active[0]["supersedes_id"] is not None


def test_void_removes_from_active_but_keeps_history(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "90.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ])
    r = _upload(client, fid, [
        {"reading_no": "R2", "sampled_at": "2026-11-01T09:00:00Z",
         "humidity": "90.0", "change_type": "void", "note": "整批作废"},
    ])
    assert r.status_code == 200
    review_data = _review(client, fid, 10).json()
    assert review_data["pairs"] == []

    rows = client.get(f"/api/v1/fields/{fid}/readings").json()
    r2 = [x for x in rows if x["reading_no"] == "R2"][0]
    assert r2["is_void"] is True and r2["is_active"] is False


def test_late_reading_changes_adjacency(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R3", "humidity": "60.0", "sampled_at": "2026-11-01T10:00:00Z"},
    ])
    assert len(_review(client, fid, 10).json()["pairs"]) == 1

    # Late arrival slots between R1 and R3, changing adjacency.
    r = _upload(client, fid, [
        {"reading_no": "R2", "humidity": "31.0", "sampled_at": "2026-11-01T09:00:00Z",
         "change_type": "late"},
    ])
    assert r.status_code == 200
    data = _review(client, fid, 10).json()
    # R1-R2 diff=1 not flagged; R2-R3 diff=29 flagged.
    assert len(data["pairs"]) == 1
    assert data["pairs"][0]["reading_no_a"] == "R2"
    assert data["pairs"][0]["reading_no_b"] == "R3"


def test_stale_upload_version_rejected(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
    ])
    # Another writer moves the field on.
    _upload(client, fid, [
        {"reading_no": "R2", "humidity": "31.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ])
    stale = _upload(client, fid, [
        {"reading_no": "R3", "humidity": "32.0", "sampled_at": "2026-11-01T10:00:00Z"},
    ], expected_version=1)
    assert stale.status_code == 409
    assert stale.json()["current_version"] == 3


def test_decision_on_non_adjacent_pair_refused(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "31.0", "sampled_at": "2026-11-01T09:00:00Z"},
        {"reading_no": "R3", "humidity": "80.0", "sampled_at": "2026-11-01T10:00:00Z"},
    ])
    ver = _review(client, fid, 10).json()["snapshot_version"]
    r = client.post(
        f"/api/v1/fields/{fid}/review/decisions",
        json={"reading_no_a": "R1", "reading_no_b": "R3",
              "decision": "confirmed", "base_version": ver, "threshold": "10"},
    )
    assert r.status_code == 409


def test_history_replay_events(client, field):
    fid = field["id"]
    _upload(client, fid, [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "70.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ])
    ver = _review(client, fid, 10).json()["snapshot_version"]
    client.post(
        f"/api/v1/fields/{fid}/review/decisions",
        json={"reading_no_a": "R1", "reading_no_b": "R2",
              "decision": "confirmed", "base_version": ver, "threshold": "10"},
    )
    events = client.get("/api/v1/events", params={"field_id": fid}).json()
    types = [e["event_type"] for e in events]
    assert "field_created" in types
    assert "upload_batch" in types
    assert "review_decision" in types
    # Sequential ordering for replay.
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)

    # after_seq cursor.
    later = client.get("/api/v1/events", params={"field_id": fid, "after_seq": seqs[1]}).json()
    assert all(e["seq"] > seqs[1] for e in later)


def test_humidity_validation(client, field):
    fid = field["id"]
    r = _upload(client, fid, [
        {"reading_no": "R1", "humidity": "120.0", "sampled_at": "2026-11-01T08:00:00Z"},
    ])
    assert r.status_code == 422
    r2 = _upload(client, fid, [
        {"reading_no": "R1", "humidity": "-1.0", "sampled_at": "2026-11-01T08:00:00Z"},
    ])
    assert r2.status_code == 422


def test_duplicate_upload_conflict(client, field):
    fid = field["id"]
    payload = [{"reading_no": "R1", "humidity": "30.0",
                "sampled_at": "2026-11-01T08:00:00Z"}]
    assert _upload(client, fid, payload).status_code == 200
    assert _upload(client, fid, payload).status_code == 409


def test_duplicate_field_code_conflict(client, field):
    # The `field` fixture already created a field with a unique code.
    r = client.post("/api/v1/fields", json={"code": field["code"], "name": "重复田块"})
    assert r.status_code == 409
