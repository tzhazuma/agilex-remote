#!/bin/bash
# Report this robot's current IP to a free ntfy.sh topic.
#
# WHY: the robot is on eduroam Wi-Fi with DHCP, so its address changes on every
# reboot. Scanning for it is slow (the campus VPN cannot do parallel probes), so
# instead the robot announces itself: the Mac reads the latest message off the
# topic and knows the address instantly.
#
# Installed as agilex-report-ip.timer -> runs 30s after boot and every 5 min.
# Read side: agilex-setup/bin/agilex-proxy.sh on the Mac.
set -u

TOPIC_FILE=/etc/agilex-report-ip.topic
[ -r "$TOPIC_FILE" ] || { echo "missing $TOPIC_FILE" >&2; exit 0; }
TOPIC="$(tr -d '[:space:]' < "$TOPIC_FILE")"
[ -n "$TOPIC" ] || exit 0

# Prefer the address that actually carries the default route - `hostname -I`
# can list several interfaces and the first is not always the routable one.
IP="$(ip -4 route get 1.1.1.1 2>/dev/null \
      | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
[ -n "$IP" ] || IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || { echo "could not determine IP" >&2; exit 1; }

if curl -sS -m 25 --retry 2 --retry-delay 2 \
     -H "Title: agilex robot" \
     -H "Tags: robot" \
     -d "$IP" \
     "https://ntfy.sh/$TOPIC" >/dev/null 2>&1; then
  logger -t agilex-report-ip "reported $IP to ntfy.sh/$TOPIC"
  exit 0
fi

logger -t agilex-report-ip "FAILED to report $IP"
exit 1
