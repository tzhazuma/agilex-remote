#!/usr/bin/env bash
# Verify the mode round-trip against the HARDWARE read-back, with no motion:
# every mode change resyncs the target to the measured pose first.
set -uo pipefail
python3 - <<'PY'
import json, time, urllib.request

B = "http://127.0.0.1:8765"
FAIL = []

def call(p, body=None, t=10):
    d = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(B + p, data=d,
                               headers={"Content-Type": "application/json"},
                               method="POST" if body is not None else "GET")
    with urllib.request.urlopen(r, timeout=t) as x:
        return json.loads(x.read().decode())

def check(n, c, d=""):
    print(("PASS " if c else "FAIL ") + n + (("  " + str(d)) if d and not c else ""))
    if not c:
        FAIL.append(n)

st = call("/api/status")
a0 = st["arms"][0]
check("status exposes mode_actual", "mode_actual" in a0, list(a0))
check("status exposes mode_verified", "mode_verified" in a0)
base = [a["joints_deg"] for a in st["arms"]]
print("baseline:", [[round(v,2) for v in b] for b in base])

# pos_j -> pos_p -> pos_j, checking the ARM's own report each time
r = call("/api/arm/mode", {"arm": 0, "mode": "pos_p"})
check("pos_p accepted", r.get("ok") is True, r)
check("pos_p: arm reports MOVE_P", r.get("mode") == "pos_p", r)
time.sleep(1.0)
s = call("/api/status")["arms"][0]
check("pos_p confirmed by hardware read-back", s.get("mode_actual") == "MOVE_P", s.get("mode_actual"))

r = call("/api/arm/mode", {"arm": 0, "mode": "pos_j"})
check("pos_j accepted", r.get("ok") is True, r)
time.sleep(1.0)
s = call("/api/status")["arms"][0]
check("pos_j confirmed by hardware read-back", s.get("mode_actual") == "MOVE_J", s.get("mode_actual"))
check("mode_verified true", s.get("mode_verified") is True, s.get("mode_verified"))

# mit: is_mit_mode=0xAD, move_mode stays 1 -> arm still reports MOVE_J
r = call("/api/arm/mode", {"arm": 0, "mode": "mit"})
check("mit accepted", r.get("ok") is True, r)
time.sleep(1.0)
r = call("/api/arm/mode", {"arm": 0, "mode": "pos_j"})
check("back to pos_j accepted", r.get("ok") is True, r)

time.sleep(1.5)
st2 = call("/api/status")
d = max(abs(st2["arms"][0]["joints_deg"][i] - base[0][i]) for i in range(6))
check("left arm did not move during mode round-trip (<1.5 deg)", d < 1.5, round(d, 3))
d = max(abs(st2["arms"][1]["joints_deg"][i] - base[1][i]) for i in range(6))
check("right arm untouched (<0.5 deg)", d < 0.5, round(d, 3))
print("final:", [[round(v,2) for v in a["joints_deg"]] for a in st2["arms"]])

print()
if FAIL:
    print("%d FAILED: %s" % (len(FAIL), FAIL)); raise SystemExit(1)
print("MODE ROUND-TRIP VERIFIED AGAINST HARDWARE READ-BACK")
PY
