#!/usr/bin/env bash
# check-connection.sh — end-to-end "can I actually use this robot right now" check.
#
#   ./scripts/check-connection.sh                 # run ON the robot
#   ./scripts/check-connection.sh agilex@10.0.0.5 # run FROM a laptop over SSH
#
# Checks the things that actually break: the service, both CAN buses, whether
# the arms report themselves connected, what mode the ARM says it is in (not
# what the software thinks), the command rate, and whether both cameras return
# a real JPEG. Prints PASS/FAIL and exits non-zero on any failure.
set -uo pipefail

# BASH_SOURCE is unset when this file is piped in over ssh (`bash -s`), and
# `set -u` would abort on it. The remote branch does not need these paths.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"
ROOT="$(dirname "$HERE")"
TARGET="${1:-}"
[ -n "${AGILEX_LOCAL:-}" ] && TARGET=""
PORT="${AGILEX_PORT:-8765}"
FAIL=0

ok()  { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
warn(){ printf '  \033[33mWARN\033[0m %s\n' "$*"; }

if [ -n "$TARGET" ]; then
  echo "== running the checks on $TARGET =="
  # Re-run this same file on the robot with no target argument, so it takes the
  # local branch there. AGILEX_LOCAL stops it from trying to ssh again.
  ssh -o BatchMode=yes "$TARGET" "AGILEX_LOCAL=1 AGILEX_PORT='$PORT' bash -s" < "$0"
  exit $?
fi

echo "== service =="
if systemctl is-active --quiet agilex-remote 2>/dev/null; then
  ok "agilex-remote is active"
else
  bad "agilex-remote is not active (journalctl -u agilex-remote -n 50)"
fi

echo
echo "== API =="
STATUS="$(curl -fsS --max-time 6 "http://127.0.0.1:$PORT/api/status" 2>/dev/null)"
if [ -n "$STATUS" ]; then
  ok "/api/status responds"
else
  bad "/api/status did not respond on port $PORT"
fi

echo
echo "== CAN buses =="
for b in can0 can_left can_right; do
  if ! ip link show "$b" >/dev/null 2>&1; then
    bad "$b missing (run the piper CAN setup script)"
    continue
  fi
  n="$(timeout 1 candump "$b" 2>/dev/null | wc -l | tr -d ' ')"
  st="$(ip -details link show "$b" 2>/dev/null | grep -oE 'can state [A-Z-]+' | head -1)"
  if [ "${n:-0}" -gt 0 ]; then
    ok "$b: $n frames/s, $st"
  else
    bad "$b: 0 frames/s, $st — bus is silent (arm power? e-stop? cable?)"
  fi
done

echo
echo "== arms (as reported by the hardware) =="
if [ -n "$STATUS" ]; then
  printf '%s' "$STATUS" | python3 -c '
import json, sys
d = json.load(sys.stdin)
fails = 0
for a in d.get("arms", []):
    side = a.get("side", "?")
    conn = a.get("connected")
    en = a.get("enabled")
    mode = a.get("mode")
    actual = a.get("mode_actual")
    hz = (a.get("jitter") or {}).get("cmd_hz")
    n = sum(1 for x in (a.get("joints_enabled") or []) if x)
    print(("  \033[32mPASS\033[0m " if conn else "  \033[31mFAIL\033[0m ")
          + f"{side}: connected={conn} enabled={en} motors={n}/6 "
            f"mode={mode} arm_says={actual} cmd={hz} Hz")
    if not conn:
        fails += 1
    if conn and actual not in (None, "MOVE_J") and mode == "pos_j":
        print(f"  \033[33mWARN\033[0m {side}: software thinks pos_j but the arm reports {actual}")
    if conn and isinstance(hz, (int, float)) and hz < 30:
        print(f"  \033[33mWARN\033[0m {side}: command rate {hz} Hz is low (expected ~50)")
sys.exit(1 if fails else 0)
' || FAIL=$((FAIL + 1))
else
  bad "skipped: no status"
fi

echo
echo "== cameras =="
for c in 0 2; do
  f="$(mktemp "${TMPDIR:-/tmp}/agilex-cam$c.XXXXXX" 2>/dev/null || echo "/tmp/agilex-cam$c.jpg")"
  code="$(curl -s -o "$f" -w '%{http_code}' --max-time 25 "http://127.0.0.1:$PORT/frame?cam=$c" 2>/dev/null)"
  magic="$(head -c 2 "$f" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
  if [ "$code" = "200" ] && [ "$magic" = "ffd8" ]; then
    ok "cam$c: JPEG $(wc -c < "$f" | tr -d ' ') bytes"
  else
    bad "cam$c: HTTP ${code:-none} — device busy or unplugged (fuser -v /dev/video$c)"
  fi
  rm -f "$f"
done

echo
echo "== port =="
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  ok "listening on :$PORT"
else
  bad "nothing listening on :$PORT"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "CONNECTION OK — open http://<robot-ip>:$PORT/ (or tunnel it: ssh -N -L $PORT:127.0.0.1:$PORT <user>@<robot>)"
else
  echo "$FAIL CHECK(S) FAILED — see docs/INSTALL-CONNECT.md section 6"
  exit 1
fi
