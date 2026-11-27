#!/usr/bin/env bash
# On-hardware mode round-trip against the arm's own read-back.
#
# ⚠️ THIS IS NOT A NO-MOTION TEST. Switching to MOVE P can re-plan the arm, and
# MIT impedance makes a Piper compliant (gravity can move it). Run it only with
# the workspace clear, an operator on the E-stop, and the arms DISABLED unless
# you have deliberately opted in.
#
# Two separate opt-ins:
#   AGILEX_ALLOW_MODE_TESTS=1   required for any run
#   AGILEX_ALLOW_MIT_TEST=1     additionally required for the MIT switch
#   AGILEX_ALLOW_MIT_WHILE_ENABLED=1  override the "arms must be disabled" gate
set -uo pipefail

if [ "${AGILEX_ALLOW_MODE_TESTS:-0}" != "1" ]; then
  cat >&2 <<'EOF'
REFUSING TO RUN: this test switches arm modes and is not motion-free.

  clear the workspace, put a hand on the E-stop, then re-run with:
      AGILEX_ALLOW_MODE_TESTS=1 ./scripts/verify-modes.sh

  The MIT branch additionally needs AGILEX_ALLOW_MIT_TEST=1.
EOF
  exit 2
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/agilex-lease.sh"
lease_acquire "verify-modes-$$" || exit $?
lease_start_heartbeat 5

python3 - <<'PY'
import json, os, time, urllib.request

B = os.environ.get("AGILEX_API", "http://127.0.0.1:8765")
FAIL = []
LEASE = os.environ.get("AGILEX_LEASE_SECRET", "")
CONTROLLER = os.environ.get("AGILEX_CONTROLLER", "verify-modes")
ALLOW_MIT = os.environ.get("AGILEX_ALLOW_MIT_TEST") == "1"
ALLOW_MIT_ENABLED = os.environ.get("AGILEX_ALLOW_MIT_WHILE_ENABLED") == "1"

def call(p, body=None, t=10):
    d = None
    headers = {"Content-Type": "application/json"}
    if LEASE:
        headers["X-Control-Lease"] = LEASE
    if body is not None:
        if isinstance(body, dict):
            body = dict(body)
            body.setdefault("controller", CONTROLLER)
        d = json.dumps(body).encode()
    r = urllib.request.Request(B + p, data=d, headers=headers,
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
enabled = [bool(a.get("enabled")) for a in st["arms"]]
print("baseline:", [[round(v,2) for v in b] for b in base])
print("enabled :", enabled)

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

# MIT: is_mit_mode=0xAD, move_mode stays 1 -> the arm still reports MOVE_J.
# This makes the arm compliant, so it is a separate, explicit opt-in and is
# refused while any arm is enabled unless the operator overrides that gate.
if not ALLOW_MIT:
    print("SKIP mit round-trip (set AGILEX_ALLOW_MIT_TEST=1 to include it)")
elif any(enabled) and not ALLOW_MIT_ENABLED:
    print("SKIP mit round-trip (an arm is enabled; disable both or set "
          "AGILEX_ALLOW_MIT_WHILE_ENABLED=1)")
else:
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
