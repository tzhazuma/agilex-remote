#!/usr/bin/env bash
# 1) measure real bus load, 2) restart the service to rebuild the CAN sockets,
# 3) confirm frames are being sent again.
set -uo pipefail

echo "=== bus load (2 s each) ==="
for b in can_left can_right; do
  n=$(timeout 2 candump -n 100000 "$b" 2>/dev/null | wc -l)
  echo "$b: $n frames in 2 s  (~$((n / 2))/s)"
done

echo
echo "=== 0x470 (linkage) frames in 2 s ==="
for b in can_left can_right; do
  n=$(timeout 2 candump "$b",470:7FF 2>/dev/null | wc -l)
  echo "$b: $n"
done

echo
echo "=== restarting agilex-remote ==="
systemctl restart agilex-remote
sleep 6
systemctl is-active agilex-remote

echo
echo "=== SEND failures since restart ==="
journalctl -u agilex-remote --since "20 s ago" --no-pager 2>/dev/null \
  | grep -c "SEND_MESSAGE_FAILED" || true

echo
echo "=== last log lines ==="
journalctl -u agilex-remote --since "20 s ago" --no-pager 2>/dev/null | tail -8 | cut -c1-120

echo
echo "=== status after restart ==="
curl -s --max-time 6 http://127.0.0.1:8765/api/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
for a in d['arms']:
    j=[round(v,2) for v in a['joints_deg']]; t=[round(v,2) for v in a['targets_deg']]
    print(a['side'],'mode',a['mode'],'en',a['enabled'],'jitter',a['jitter'])
    print('   j',j); print('   t',t)
"
