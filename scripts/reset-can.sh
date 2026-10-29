#!/usr/bin/env bash
# Recover the arm CAN buses: confirm bitrate, reset the controllers, and report
# whether the arms come back. Stops the service first so its sockets are rebuilt.
set -uo pipefail

echo "=== bitrate / state before ==="
for b in can_left can_right can0; do
  printf "%-10s " "$b"
  ip -details link show "$b" 2>/dev/null | tr '\n' ' ' | grep -oE "bitrate [0-9]+|can state [A-Z-]+|state [A-Z]+" | tr '\n' ' '
  echo
done

echo
echo "=== chassis bus alive? (1 s) ==="
echo "can0 frames: $(timeout 1 candump can0 2>/dev/null | wc -l)"

echo
echo "=== stopping service, resetting arm buses ==="
systemctl stop agilex-remote
for b in can_left can_right; do
  ip link set "$b" down
  ip link set "$b" type can bitrate 1000000 restart-ms 100
  ip link set "$b" up
  echo "$b reset"
done
sleep 1

echo
echo "=== frames after reset, before any client (1 s each) ==="
for b in can_left can_right; do
  echo "$b: $(timeout 1 candump "$b" 2>/dev/null | wc -l) frames/s"
done

echo
echo "=== state after reset ==="
for b in can_left can_right; do
  printf "%-10s " "$b"
  ip -details link show "$b" 2>/dev/null | grep -oE "can state [A-Z-]+"
done

echo
echo "=== restarting service ==="
systemctl start agilex-remote
sleep 7
systemctl is-active agilex-remote
echo "send failures in last 8 s: $(journalctl -u agilex-remote --since '8 s ago' --no-pager 2>/dev/null | grep -c SEND_MESSAGE_FAILED)"
journalctl -u agilex-remote --since "12 s ago" --no-pager 2>/dev/null | grep -E "arm can_" | cut -c1-120

echo
curl -s --max-time 6 http://127.0.0.1:8765/api/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
for a in d['arms']:
    print(a['side'],'en',a['enabled'],'j',[round(v,2) for v in a['joints_deg']])
" 2>/dev/null
