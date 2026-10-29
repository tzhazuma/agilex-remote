#!/usr/bin/env bash
# Diagnose the arm shaking: timeline + live arm status + linkage frames.
set -uo pipefail

echo "=== 1. service log, this session ==="
journalctl -u agilex-remote --since "15:18:00" --no-pager 2>/dev/null \
  | grep -Ei "mode |master/slave|teach|trajectory|reset|enable|error|fail|resync" \
  | tail -30 || echo "(no journal access as this user)"

echo
echo "=== 2. linkage frames 0x470 present? (2 s sniff) ==="
for bus in can_left can_right; do
  n=$(timeout 2 candump "$bus",470:7FF 2>/dev/null | wc -l)
  echo "$bus: $n frames on 0x470"
done

echo
echo "=== 3. GetArmStatus ==="
python3 - <<'PY'
import sys, time
sys.path.insert(0, "/home/agilex/.local/lib/python3.10/site-packages")
from piper_sdk import C_PiperInterface_V3
for ch in ("can_left", "can_right"):
    try:
        p = C_PiperInterface_V3(ch)
        p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
        time.sleep(0.8)
        print(ch, p.GetArmStatus())
        p.DisconnectPort()
    except Exception as e:
        print(ch, "FAILED", repr(e))
PY
