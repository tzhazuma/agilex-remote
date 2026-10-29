#!/usr/bin/env bash
# On-hardware plumbing test for the new operating modes.
#
# SAFETY: deliberately does NOT switch to MIT or start drag-teach while the arms
# are enabled - both make a Piper compliant, i.e. gravity takes over, and this
# test must never be the reason the arm moves. Those paths are covered by
# tools/test-modes-offline.py against a fake interface. What is tested here is
# the HTTP plumbing, the mode round-trip through MOVE P (which holds the same
# target), and a trajectory whose points equal the measured pose (no motion).
set -euo pipefail
python3 - <<'PY'
import json, time, urllib.request

BASE = "http://127.0.0.1:8765"
FAIL = []

def call(path, body=None, timeout=8):
    url = BASE + path
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)

st = call("/api/status")
check("status ok", st.get("ok") is True)
check("two arms", len(st.get("arms", [])) == 2, len(st.get("arms", [])))
a0 = st["arms"][0]
check("arm0 reports mode", "mode" in a0, list(a0)[:8])
check("arm0 mode is pos_j", a0.get("mode") == "pos_j", a0.get("mode"))
check("arm0 reports traj", "traj" in a0)
check("arm0 reports master_slave", "master_slave" in a0)

base = [a["joints_deg"] for a in st["arms"]]
print("baseline joints:", [[round(v, 2) for v in b] for b in base])

# ---- MOVE P round trip: same target, so the arm should hold --------------
r = call("/api/arm/mode", {"arm": 0, "mode": "pos_p"})
check("mode -> pos_p accepted", r.get("ok") is True, r)
check("mode -> pos_p reported", r.get("mode") == "pos_p", r)
time.sleep(0.6)
st2 = call("/api/status")
check("status reflects pos_p", st2["arms"][0].get("mode") == "pos_p",
      st2["arms"][0].get("mode"))
d = max(abs(st2["arms"][0]["joints_deg"][i] - base[0][i]) for i in range(6))
check("pos_p held the pose (<1.0 deg drift)", d < 1.0, round(d, 3))

r = call("/api/arm/mode", {"arm": 0, "mode": "pos_j"})
check("mode -> pos_j accepted", r.get("ok") is True, r)

# ---- trajectory: points equal to the measured pose => zero motion --------
st3 = call("/api/status")
meas = st3["arms"][0]["joints_deg"]
pts = [list(meas), list(meas), list(meas)]
r = call("/api/traj/load", {"arm": 0, "name": "selftest", "points": pts, "dt": 0.05})
check("traj load accepted", r.get("ok") is True, r)
check("traj load reports 3 points", r.get("points") == 3, r)
r = call("/api/traj/play", {"arm": 0, "loop": False, "speed": 1.0})
check("traj play accepted", r.get("ok") is True, r)
time.sleep(0.5)
st4 = call("/api/status")
tr = st4["arms"][0].get("traj") or {}
check("traj advanced", tr.get("idx", 0) >= 1, tr)
r = call("/api/traj/stop", {"arm": 0})
check("traj stop accepted", r.get("ok") is True, r)
st5 = call("/api/status")
check("traj stopped", (st5["arms"][0].get("traj") or {}).get("playing") is False)

# ---- master/slave OFF is idempotent and must not link anything -----------
r = call("/api/master-slave", {"on": False})
check("master-slave off accepted", r.get("ok") is True, r)
st6 = call("/api/status")
check("master-slave reported off",
      all(not (a.get("master_slave") or {}).get("enabled") for a in st6["arms"]))

# ---- no unintended motion overall ---------------------------------------
d = max(abs(st6["arms"][0]["joints_deg"][i] - base[0][i]) for i in range(6))
check("left arm did not move overall (<1.5 deg)", d < 1.5, round(d, 3))
d = max(abs(st6["arms"][1]["joints_deg"][i] - base[1][i]) for i in range(6))
check("right arm untouched (<0.5 deg)", d < 0.5, round(d, 3))
print("final joints:", [[round(v, 2) for v in a["joints_deg"]] for a in st6["arms"]])

print()
if FAIL:
    print("%d FAILED: %s" % (len(FAIL), FAIL))
    raise SystemExit(1)
print("ALL ON-HARDWARE PLUMBING TESTS PASSED")
PY
