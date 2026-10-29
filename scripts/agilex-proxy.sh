#!/usr/bin/env bash
# agilex-proxy.sh — SSH ProxyCommand that finds the robot and connects to it.
#
# WHY: the robot is on eduroam Wi-Fi with DHCP, so its IP changes on every
# reboot (observed <ip-a> -> <ip-b>). Rather than hardcoding an
# address, this script discovers the current one and hands the socket to ssh,
# so plain `ssh agilex` keeps working forever.
#
# Wired up in ~/.ssh/config as:
#     Host agilex
#         ProxyCommand /path/to/agilex-proxy.sh %h %p
#
# DISCOVERY ORDER (first hit wins):
#   1. cached address            - instant, the normal case
#   2. previously-seen addresses - instant, survives an IP change
#   3. mDNS "agilex.local"       - fast when the VPN forwards multicast (flaky)
#   4. sequential sweep          - slow but dependable last resort
#
# NOTE: the campus VPN fakes *successful TCP connects* to arbitrary ports, so a
# bare port check proves nothing. Every candidate here is validated by reading
# an actual SSH banner.
#
# IMPORTANT: stdout is the SSH transport - diagnostics must go to stderr only.
set -uo pipefail

HOST="${1:-agilex.local}"
PORT="${2:-22}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${AGILEX_STATE_DIR:-$HOME/.cache/agilex}"
CACHE="$STATE/current-ip"
SEEN="$STATE/seen-ips"
# The ntfy topic the robot publishes its address to (same value that was
# installed on the robot as /etc/agilex-report-ip.topic).
#
# Prefer ~/.config/agilex/ntfy-topic: this script also runs from a launchd
# LaunchAgent (the standing NoMachine tunnel), and macOS TCC refuses to execute
# anything under ~/Documents from a background agent - "Operation not
# permitted". bin/install-local-helpers.sh puts a copy there.
if [ -n "${AGILEX_NTFY_TOPIC_FILE:-}" ]; then
  NTFY_TOPIC_FILE="$AGILEX_NTFY_TOPIC_FILE"
elif [ -r "$HOME/.config/agilex/ntfy-topic" ]; then
  NTFY_TOPIC_FILE="$HOME/.config/agilex/ntfy-topic"
else
  NTFY_TOPIC_FILE="$DIR/../remote/robot-files/agilex-report-ip.topic"
fi
mkdir -p "$STATE" 2>/dev/null

# Sweep range. The robot has lived in two different /24s, so default to
# the surrounding /20 and let it be overridden.
SCAN_RANGE="${AGILEX_SCAN_RANGE:-192.168.1.0/24}"
SCAN_BUDGET="${AGILEX_SCAN_BUDGET:-600}"     # seconds before giving up (~245s per /24)

log() { printf '%s\n' "$*" >&2; }

# ssh hands us the transport on fd 0/1. Anything that reads from a file (the
# seen-ips loop below) would clobber fd 0, so stash the real stdin on fd 3 and
# hand it back to nc at connect time.
exec 3<&0

# Does this address really speak SSH? (the tunnel's fake connects do not)
#
# A 1-second budget proved FLAKY on this link: measured 2 timeouts in 5
# attempts, while 2s was 3/3. A false negative here is expensive - it throws
# away a perfectly good address (even one the robot just reported over ntfy)
# and falls through to a multi-minute sweep. So trusted candidates get a
# generous budget.
speaks_ssh() {
  [ -n "${1:-}" ] || return 1
  nc -G 3 -w 3 "$1" "$PORT" </dev/null 2>/dev/null | head -1 | grep -q '^SSH-'
}

# Cheaper probe for the sweep, which has ~254 candidates per subnet and is the
# slowest path anyway. Still 2s rather than 1s, for the same flakiness reason.
speaks_ssh_quick() {
  [ -n "${1:-}" ] || return 1
  nc -G 2 -w 2 "$1" "$PORT" </dev/null 2>/dev/null | head -1 | grep -q '^SSH-'
}

# Only one sweep at a time. Several ssh invocations in quick succession would
# otherwise each launch their own scan, and they would fight over a tunnel that
# is already the bottleneck.
LOCK="$STATE/sweep.lock"
acquire_sweep_lock() {
  local waited=0
  while ! mkdir "$LOCK" 2>/dev/null; do
    # reclaim a lock left behind by a killed run
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +20 2>/dev/null)" ]; then
      log "agilex: reclaiming a stale sweep lock"
      rm -rf "$LOCK" 2>/dev/null
    fi
    waited=$((waited + 1))
    if [ "$waited" -gt 900 ]; then
      log "agilex: another sweep is already running - giving up"
      exit 1
    fi
    sleep 1
  done
  trap 'rm -rf "$LOCK" 2>/dev/null' EXIT INT TERM
}

# The robot's host key(s) on file, so a sweep can tell it apart from the other
# SSH hosts that live on this campus subnet.
#
# ALL key types, not just ed25519. known_hosts can hold an rsa entry from an
# earlier connection alongside the ed25519 one; filtering to ed25519 alone made
# this return the wrong key (or nothing), and the sweep then rejected the real
# robot - it found <robot-ip>, decided it "is not the robot", and scanned the
# entire /20 before giving up, while plain `ssh agilex` worked the whole time.
expected_key() {
  ssh-keygen -F "${AGILEX_KEY_ALIAS:-agilex-robot}" 2>/dev/null \
    | grep -v '^#' | awk '{print $2" "$3}'
}

is_the_robot() {
  local want got line
  want="$(expected_key)"
  [ -n "$want" ] || return 0            # nothing on file: let ssh do the checking
  # Ask for every type the server offers; accept a match against any of them.
  got="$(ssh-keyscan -T 4 -t rsa,ecdsa,ed25519 "$1" 2>/dev/null \
         | grep -v '^#' | awk '{print $2" "$3}')"
  [ -n "$got" ] || return 1             # could not read a key: not provably ours
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    case "$got" in
      *"$line"*) return 0 ;;
    esac
  done <<EOF
$want
EOF
  return 1
}

remember() { printf '%s\n' "$1" > "$CACHE" 2>/dev/null; }

connect() {
  remember "$1"
  # keep a de-duplicated history for the next cold start
  { [ -f "$SEEN" ] && cat "$SEEN"; echo "$1"; } 2>/dev/null \
    | awk '!seen[$0]++' | tail -20 > "$SEEN.tmp" 2>/dev/null \
    && mv "$SEEN.tmp" "$SEEN" 2>/dev/null
  log "agilex: connecting to $1:$PORT"
  exec nc "$1" "$PORT" <&3
}

# ---- 1. cache ---------------------------------------------------------------
# Verified with the host key: an old address can be handed to a DIFFERENT
# machine by DHCP, and connecting to that would surface as a baffling
# "host key verification failed" instead of just finding the robot.
if [ -f "$CACHE" ]; then
  ip="$(cat "$CACHE" 2>/dev/null)"
  if [ -n "$ip" ] && speaks_ssh "$ip"; then
    if is_the_robot "$ip"; then
      connect "$ip"
    else
      log "agilex: cached $ip speaks SSH but is not the robot - stale, ignoring"
    fi
  fi
fi

# ---- 2. ntfy: the robot announces its own address ---------------------------
# Authoritative and instant - no scanning. The robot republishes on boot and
# every 5 minutes (agilex-report-ip.timer), so this survives any DHCP change.
NTFY_TOPIC="${AGILEX_NTFY_TOPIC:-}"
if [ -z "$NTFY_TOPIC" ] && [ -r "$NTFY_TOPIC_FILE" ]; then
  NTFY_TOPIC="$(tr -d '[:space:]' < "$NTFY_TOPIC_FILE")"
fi
if [ -n "$NTFY_TOPIC" ]; then
  # Match the "message" KEY, not the value of "event":"message" - a naive
  # field scan matches that value first and returns "topic" instead.
  ip="$(curl -sS -m 8 "https://ntfy.sh/$NTFY_TOPIC/json?poll=1" 2>/dev/null \
        | grep -o '"message":"[^"]*"' | tail -1 \
        | sed 's/^"message":"//; s/"$//')"
  if [ -n "$ip" ]; then
    log "agilex: ntfy reports $ip"
    if speaks_ssh "$ip" && is_the_robot "$ip"; then
      connect "$ip"
    fi
    log "agilex: ntfy address $ip did not answer - continuing"
  fi
fi

# ---- 3. previously-seen addresses -------------------------------------------
if [ -f "$SEEN" ]; then
  while IFS= read -r ip; do
    [ -n "$ip" ] && speaks_ssh "$ip" && is_the_robot "$ip" && connect "$ip"
  done < "$SEEN"
fi

# ---- 4. mDNS ----------------------------------------------------------------
ip="$(dscacheutil -q host -a name "$HOST" 2>/dev/null \
      | awk '/^ip_address:/{print $2; exit}')"
if [ -n "${ip:-}" ]; then
  log "agilex: mDNS resolved $HOST -> $ip"
  speaks_ssh "$ip" && is_the_robot "$ip" && connect "$ip"
fi

# ---- 5. sweep ---------------------------------------------------------------
# ORDER MATTERS: a DHCP lease usually comes back in the same /24 it was in
# before, so sweep the subnets the robot has actually lived in FIRST, and only
# then fall back to the configured range. Without this the sweep can spend its
# whole budget on irrelevant subnets and never reach the robot.
# A space-separated string, not an array: macOS ships bash 3.2, where
# "${arr[@]}" on an empty array aborts under `set -u`.
NETS=""
add_net() {
  local n="$1" x
  [ -n "$n" ] || return 0
  for x in $NETS; do [ "$x" = "$n" ] && return 0; done
  NETS="${NETS:+$NETS }$n"
}

if [ -f "$SEEN" ]; then
  while IFS= read -r ip; do
    [ -n "$ip" ] && add_net "${ip%.*}"
  done < "$SEEN"
fi

BASE="${SCAN_RANGE%/*}"; PREFIX="${SCAN_RANGE#*/}"
IFS=. read -r o1 o2 o3 _ <<<"$BASE"
case "$PREFIX" in
  24) add_net "$o1.$o2.$o3" ;;
  23) add_net "$o1.$o2.$o3"; add_net "$o1.$o2.$((o3+1))" ;;
  22) add_net "$o1.$o2.$o3"; add_net "$o1.$o2.$((o3+1))"; add_net "$o1.$o2.$((o3+2))"; add_net "$o1.$o2.$((o3+3))" ;;
  20) for k in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do add_net "$o1.$o2.$((o3+k))"; done ;;
  *)  log "agilex: unsupported scan prefix /$PREFIX"; exit 1 ;;
esac

acquire_sweep_lock

log "agilex: cached/mDNS lookup failed - sweeping $(echo $NETS | wc -w | tr -d ' ') subnet(s), up to ${SCAN_BUDGET}s ..."
log "agilex: order: $NETS"
log "agilex: tip - ask campus IT for a DHCP reservation on MAC ac:e0:10:51:69:06 to avoid this."

deadline=$(( $(date +%s) + SCAN_BUDGET ))

# Pick the probe strategy by looking at which interface actually routes to the
# target range:
#   - campus VPN (utun*) FAKES successful TCP connects and chokes once too many
#     are in flight, so it must be probed strictly sequentially
#   - a direct campus LAN link (en*) behaves normally and parallelises fine,
#     which turns a ~4 minute /24 into ~10 seconds
first_net="$(echo $NETS | awk '{print $1}')"
route_iface="$(route -n get "$first_net.1" 2>/dev/null | awk '/interface:/{print $2}')"
case "$route_iface" in
  utun*|tun*|ppp*|ipsec*)
    PARALLEL=1
    log "agilex: $first_net.0/24 routes via $route_iface (VPN) - probing sequentially" ;;
  *)
    PARALLEL=48
    log "agilex: $first_net.0/24 routes via ${route_iface:-unknown} (direct) - probing in parallel" ;;
esac

for net in $NETS; do
  log "agilex: scanning $net.0/24 ..."
  if [ "$PARALLEL" = 1 ]; then
    for i in $(seq 1 254); do
      [ "$(date +%s)" -ge "$deadline" ] && { log "agilex: sweep budget exhausted"; exit 1; }
      cand="$net.$i"
      if speaks_ssh_quick "$cand"; then
        if is_the_robot "$cand"; then
          log "agilex: found the robot at $cand"
          connect "$cand"
        else
          log "agilex: $cand speaks SSH but is not the robot - skipping"
        fi
      fi
    done
  else
    # Collect candidates in parallel, then confirm host keys one at a time -
    # the key check must not be parallelised, it is the expensive part.
    cands="$(seq 1 254 | xargs -P "$PARALLEL" -n 1 -I{} bash -c '
        ip="'"$net"'.{}"
        b="$(nc -G 2 -w 2 "$ip" "$PORT" </dev/null 2>/dev/null | head -1)"
        case "$b" in SSH-*) echo "$ip" ;; esac
      ' 2>/dev/null | sort -u)"
    for cand in $cands; do
      if is_the_robot "$cand"; then
        log "agilex: found the robot at $cand"
        connect "$cand"
      else
        log "agilex: $cand speaks SSH but is not the robot - skipping"
      fi
    done
  fi
done

log "agilex: NOT FOUND in $SCAN_RANGE. Widen with AGILEX_SCAN_RANGE, or check the robot is powered on."
exit 1
