#!/usr/bin/env bash
# find-robot.sh — print the AgileX robot's current IP address.
#
# The robot is on eduroam Wi-Fi with DHCP, so its address changes on every
# reboot (observed <ip-a> -> <ip-b>). This script just asks the
# SSH layer where it is: `ssh agilex` goes through the ProxyCommand in
# ~/.ssh/config (bin/agilex-proxy.sh), which does the real discovery -
# cache -> known IPs -> mDNS -> sequential sweep - and caches what it finds.
#
# Doing the discovery here instead would duplicate that logic and be worse at
# it: a parallel sweep of this campus subnet is unreliable, because the VPN
# tunnel stops answering once too many connects are in flight.
#
# USAGE
#   ./find-robot.sh            # print the IP (may take a while if it changed)
#   ./find-robot.sh --quiet    # same, but only the IP on stdout
set -uo pipefail
STATE="${AGILEX_STATE_DIR:-$HOME/.cache/agilex}"
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

log() { [ "$QUIET" = 1 ] || printf '%s\n' "$*" >&2; }

log "asking the robot where it is (sweeps only if the address changed) ..."
if out="$(ssh -o BatchMode=yes -o ConnectTimeout=30 agilex 'hostname -I' 2>/dev/null)"; then
  ip="${out%% *}"
  if [ -n "$ip" ]; then
    mkdir -p "$STATE" 2>/dev/null
    printf '%s\n' "$ip" > "$STATE/current-ip" 2>/dev/null
    log "robot is at $ip"
    echo "$ip"
    exit 0
  fi
fi

log "could not reach the robot."
log "  - is it powered on and on eduroam?"
log "  - widen the sweep:  AGILEX_SCAN_RANGE=<your-subnet>/16 $0"
log "  - permanent fix:    ask campus IT for a DHCP reservation on MAC ac:e0:10:51:69:06"
exit 1
