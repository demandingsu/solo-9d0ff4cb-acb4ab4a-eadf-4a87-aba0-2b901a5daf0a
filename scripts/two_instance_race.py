"""Two-instance shared-database concurrency check (run against live servers).

Fires concurrent uploads and review decisions at BOTH instances using the same
stale/expected versions and asserts:
  * exactly one writer per race wins; losers get HTTP 409;
  * no 500s and no duplicate/rewritten effective conclusion.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

URL1, URL2 = sys.argv[1], sys.argv[2]


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# 1. create field + jump readings via instance 1
s, f = call("POST", f"{URL1}/api/v1/fields",
            {"code": f"RACE-{time.time_ns()}", "name": "并发田"})
fid = f["id"]
s, up = call("POST", f"{URL1}/api/v1/fields/{fid}/readings/batch", {
    "readings": [
        {"reading_no": "R1", "humidity": "30.0", "sampled_at": "2026-11-01T08:00:00Z"},
        {"reading_no": "R2", "humidity": "80.0", "sampled_at": "2026-11-01T09:00:00Z"},
    ]
})
base = up["version_after"]

# 2. race: two decisions (same base_version) against two different instances
results = []
lock = threading.Lock()


def decide(url, decision):
    code, body = call("POST", f"{url}/api/v1/fields/{fid}/review/decisions", {
        "reading_no_a": "R1", "reading_no_b": "R2", "decision": decision,
        "threshold": "10", "base_version": base,
    })
    with lock:
        results.append((decision, code))


t1 = threading.Thread(target=decide, args=(URL1, "confirmed"))
t2 = threading.Thread(target=decide, args=(URL2, "rejected"))
t1.start(); t2.start(); t1.join(); t2.join()
print("decision race:", sorted(results))
codes = sorted(c for _, c in results)
assert codes == [200, 409], results

# 3. race: two uploads with same expected_version, one per instance.
# Re-read the current version first: the decision race above bumped it.
s, review_now = call("GET", f"{URL1}/api/v1/fields/{fid}/review?threshold=10")
upload_base = review_now["snapshot_version"]
up_results = []


def upload(url, no, hour):
    code, body = call("POST", f"{url}/api/v1/fields/{fid}/readings/batch", {
        "expected_version": upload_base,
        "readings": [{"reading_no": no, "humidity": "40.0",
                      "sampled_at": f"2026-11-01T{hour:02d}:00:00Z"}],
    })
    with lock:
        up_results.append((no, code))


u1 = threading.Thread(target=upload, args=(URL1, "RA", 12))
u2 = threading.Thread(target=upload, args=(URL2, "RB", 13))
u1.start(); u2.start(); u1.join(); u2.join()
print("upload race:  ", sorted(up_results))
assert sorted(c for _, c in up_results) == [200, 409], up_results

# 4. exactly one effective conclusion survives
s, conclusions = call("GET",
                      f"{URL1}/api/v1/fields/{fid}/review/conclusions?include_inactive=true")
effective = [c for c in conclusions if c["is_effective"]]
print("effective conclusions:", [(c["decision"], c["conclusion_id"]) for c in effective])
assert len(effective) == 1

# 5. snapshot consistency: review on instance 2 sees the decision from instance 1
s, review = call("GET", f"{URL2}/api/v1/fields/{fid}/review?threshold=10")
flagged = [p for p in review["pairs"] if p["conclusion_status"] == "exceeds_threshold"]
print("instance-2 review sees decision:",
      [(p["reading_no_a"], p["reading_no_b"], p["decision"]) for p in flagged])
assert any(p["decision"] in ("confirmed", "rejected") for p in flagged)

print("\n双实例并发校验通过 ✔")
