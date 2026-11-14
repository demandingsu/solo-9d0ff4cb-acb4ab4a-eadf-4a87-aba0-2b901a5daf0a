#!/usr/bin/env python3
"""End-to-end demo for the moisture QC platform.

Runs entirely against a running server (default http://localhost:8001) using
only the Python standard library, so it works inside the minimal container
image too. It walks through:

  field creation -> upload -> jump review -> same-time pending ->
  reviewer confirm/reject -> erratum invalidating an old conclusion ->
  void -> late arrival -> stale submission (409) -> history replay.

Usage:
    python samples/demo.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pprint import pformat


def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict | list]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def step(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, payload) -> None:
    print(f"[{label}] HTTP {status}")
    print(pformat(payload, width=100, sort_dicts=False))


def main(base_url: str) -> None:
    step("1. 创建田块")
    s, f = call("POST", f"{base_url}/api/v1/fields",
                {"code": f"DEMO-{__import__('time').time_ns() % 100000}", "name": "演示田块"})
    show("create_field", s, f)
    fid = f["id"]

    step("2. 批量上传：30.0 -> 62.5（突跳）-> 63.0")
    s, up = call("POST", f"{base_url}/api/v1/fields/{fid}/readings/batch", {
        "readings": [
            {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
            {"reading_no": "R2", "humidity": "62.5", "sampled_at": "2026-11-01T09:00:00Z"},
            {"reading_no": "R3", "humidity": "63.0", "sampled_at": "2026-11-01T10:00:00Z"},
        ]
    })
    show("upload", s, up)

    step("3. 复核查询（阈值 10）：R1-R2 差值 32.5 超阈值，返回读数号/修订号/差值")
    s, review = call("GET", f"{base_url}/api/v1/fields/{fid}/review?threshold=10")
    show("review", s, review)
    snapshot = review["snapshot_version"]
    assert review["pairs"][0]["humidity_diff"] == "32.50"

    step("4. 同一采样时刻再传两条 -> 标记 pending_check，不任意配对")
    s, _ = call("POST", f"{base_url}/api/v1/fields/{fid}/readings/batch", {
        "readings": [
            {"reading_no": "R4", "humidity": "35.0", "sampled_at": "2026-11-01T11:00:00Z"},
            {"reading_no": "R5", "humidity": "78.0", "sampled_at": "2026-11-01T11:00:00Z"},
        ]
    })
    s, review2 = call("GET", f"{base_url}/api/v1/fields/{fid}/review?threshold=10")
    show("review_with_pending", s, review2)
    assert any(p["conclusion_status"] == "pending_check" for p in review2["pairs"])
    snapshot = review2["snapshot_version"]

    step("5. 质控员确认 R1-R2 突跳（依据所见 snapshot 版本）")
    s, decision = call("POST", f"{base_url}/api/v1/fields/{fid}/review/decisions", {
        "reading_no_a": "R1", "reading_no_b": "R2", "decision": "confirmed",
        "reason": "确认传感器异常", "reviewer": "qc-zhang",
        "threshold": "10", "base_version": snapshot,
    })
    show("confirm", s, decision)
    assert s == 200

    step("6. 过期提交：仍用旧 base_version 下相反结论 -> 409，原结论不改写")
    s, stale = call("POST", f"{base_url}/api/v1/fields/{fid}/review/decisions", {
        "reading_no_a": "R1", "reading_no_b": "R2", "decision": "rejected",
        "threshold": "10", "base_version": snapshot,
    })
    show("stale_decision", s, stale)
    assert s == 409

    step("7. 勘误 R2 为 31.5：旧结论失效但保留；突跳消失")
    s, corr = call("POST", f"{base_url}/api/v1/fields/{fid}/readings/batch", {
        "readings": [{
            "reading_no": "R2", "humidity": "31.5",
            "sampled_at": "2026-11-01T09:00:00Z",
            "change_type": "correction", "note": "探头接线松动",
        }]
    })
    show("correction", s, corr)
    assert corr["invalidated_conclusions"], "旧结论应被标记失效"
    s, review3 = call("GET", f"{base_url}/api/v1/fields/{fid}/review?threshold=10")
    show("review_after_correction", s, review3)
    # R1-R2 jump disappears (diff now 1.5). Other pairs (e.g. R2->R3) are
    # evaluated independently on the new adjacency/values.
    exceeds_pairs = {(p["reading_no_a"], p["reading_no_b"])
                     for p in review3["pairs"]
                     if p["conclusion_status"] == "exceeds_threshold"}
    assert ("R1", "R2") not in exceeds_pairs

    step("8. 结论追溯：旧结论仍在，但 is_effective=false")
    s, conclusions = call(
        "GET", f"{base_url}/api/v1/fields/{fid}/review/conclusions?include_inactive=true"
    )
    show("conclusions_audit", s, conclusions)

    step("9. 作废 R3 + 迟到读数 R6 插入 08:30 改变相邻关系")
    call("POST", f"{base_url}/api/v1/fields/{fid}/readings/batch", {
        "readings": [{
            "reading_no": "R3", "humidity": "63.0",
            "sampled_at": "2026-11-01T10:00:00Z", "change_type": "void",
        }]
    })
    s, late = call("POST", f"{base_url}/api/v1/fields/{fid}/readings/batch", {
        "readings": [{
            "reading_no": "R6", "humidity": "95.0",
            "sampled_at": "2026-11-01T08:30:00Z", "change_type": "late",
        }]
    })
    show("late_arrival", s, late)
    s, review4 = call("GET", f"{base_url}/api/v1/fields/{fid}/review?threshold=10")
    show("review_final", s, review4)

    step("10. 历史回放（事件流按 seq 顺序）")
    s, events = call("GET", f"{base_url}/api/v1/events?field_id={fid}")
    show("replay", s, [{"seq": e["seq"], "event_type": e["event_type"],
                        "version": e["version"]} for e in events])

    step("11. 过期上传（expected_version=1）-> 409")
    s, stale_up = call("POST", f"{base_url}/api/v1/fields/{fid}/readings/batch", {
        "expected_version": 1,
        "readings": [{"reading_no": "RX", "humidity": "50.0",
                      "sampled_at": "2026-11-01T12:00:00Z"}],
    })
    show("stale_upload", s, stale_up)
    assert s == 409

    print("\n全部演示步骤通过 ✔")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001"
    main(base.rstrip("/"))
