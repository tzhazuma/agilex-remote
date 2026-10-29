#!/usr/bin/env bash
# robot-vnc.sh — open the AgileX robot's real desktop (display :0) on this Mac over VNC.
#
# This is the primary way to see the robot's screen (it replaced NoMachine).
# It shares the desktop the robot is ALREADY logged into — not a new virtual
# one — so it shows exactly what is on the robot's own monitor.
#
#   ./bin/robot-vnc.sh            # ensure the tunnel is up, launch VNC Viewer
#   ./bin/robot-vnc.sh --status   # report every link in the chain
#   ./bin/robot-vnc.sh --probe    # end-to-end RFB handshake only
#   ./bin/robot-vnc.sh --restart  # bounce the SSH tunnel
#   ./bin/robot-vnc.sh --stop     # tear the tunnel down (and keep it down)
#
# Then in VNC Viewer:  Address  localhost:5900     Password  changeme
#
# WHY A TUNNEL: the robot's IP changes on every reboot (eduroam DHCP), so a VNC
# client pointed at a fixed address would rot. Going through the "agilex" SSH
# alias means the ProxyCommand in ~/.ssh/config discovers the current address,
# so the tunnel always lands on the right machine. It also means the robot's
# x11vnc — which binds 127.0.0.1 only — is never exposed to the campus network.
#
# The standing tunnel is a launchd agent (com.example.agilex-vnc-tunnel,
# RunAtLoad + KeepAlive), the same pattern as the NoMachine tunnel
# (com.example.agilex-nx-tunnel), so it survives reboots, VPN blips and wake from
# sleep. This script never touches the NoMachine agent.
#
# Robot side (see reports/vnc.md): systemd unit x11vnc.service, password file
# /etc/x11vnc.pass, display :0 via -auth /run/user/1000/gdm/Xauthority.
# --- end of help ---
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOCAL_PORT="${AGILEX_VNC_LOCAL_PORT:-5900}"
REMOTE_PORT="${AGILEX_VNC_REMOTE_PORT:-5900}"
SSH_ALIAS="${AGILEX_SSH_ALIAS:-agilex}"
VNC_PW="${AGILEX_VNC_PASSWORD:-changeme}"
VNC_APP="${AGILEX_VNC_APP:-/Applications/VNC Viewer.app}"
LABEL="com.example.agilex-vnc-tunnel"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ -x /usr/bin/python3 ]; then PYBIN=/usr/bin/python3; else PYBIN=python3; fi

tunnel_up()    { lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; }
agent_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
robot()        { "$DIR/rrun.sh" -c "$1" 2>/dev/null; }
robot_ip()     { robot 'hostname -I' | awk '{print $1}'; }

# Full RFB handshake against 127.0.0.1:$LOCAL_PORT: banner, security types, DES
# challenge/response with the real password, and the ServerInit that reports the
# desktop size. This proves the whole chain (tunnel -> x11vnc -> display :0 ->
# password file), not merely that a port is open.
rfb_probe() {
  "$PYBIN" - "$LOCAL_PORT" "$VNC_PW" <<'PY'
import socket, struct, subprocess, sys
port, pw = int(sys.argv[1]), sys.argv[2]
def rd(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c: raise EOFError("server closed the connection")
        b += c
    return b
def mirror(x):
    r = 0
    for i in range(8):
        if x & (1 << i): r |= 1 << (7 - i)
    return r
try:
    s = socket.create_connection(("127.0.0.1", port), 8); s.settimeout(8)
    banner = rd(s, 12)
    print(f"  banner         : {banner.decode(errors='replace').strip()!r}")
    s.sendall(b"RFB 003.008\n")
    n = rd(s, 1)[0]; types = list(rd(s, n))
    print(f"  security types : {types}" + ("  (2 = VNC password)" if 2 in types else ""))
    if 2 not in types:
        print("  !! server is not asking for a VNC password"); sys.exit(1)
    s.sendall(bytes([2]))
    ch = rd(s, 16)
    key = bytes(mirror(c) for c in pw.encode()[:8].ljust(8, b"\0"))
    enc = subprocess.run(["/usr/bin/openssl", "enc", "-des-ecb", "-K", key.hex(), "-nopad"],
                         input=ch, capture_output=True, check=True).stdout
    s.sendall(enc)
    res = struct.unpack(">I", rd(s, 4))[0]
    if res != 0:
        print(f"  auth           : FAILED (SecurityResult={res}) - wrong password?"); sys.exit(1)
    print("  auth           : OK - password accepted")
    s.sendall(b"\x01")
    w, h = struct.unpack(">HH", rd(s, 4)); rd(s, 16)
    nl = struct.unpack(">I", rd(s, 4))[0]
    name = rd(s, nl).decode(errors="replace")
    print(f"  desktop        : {w}x{h}, name={name!r}")
    s.close(); sys.exit(0)
except SystemExit:
    raise
except Exception as e:
    print(f"  !! RFB probe failed: {type(e).__name__}: {e}"); sys.exit(1)
PY
}

status() {
  printf 'robot reachable  : '
  ip="$(robot_ip)"
  if [ -n "$ip" ]; then echo "yes ($ip)"; else echo "NO (try ./bin/find-robot.sh)"; fi

  printf 'launchd agent    : '
  if agent_loaded; then echo "loaded ($LABEL)"; else echo "NOT loaded"; fi

  printf 'ssh tunnel       : '
  tunnel_up && echo "listening on 127.0.0.1:$LOCAL_PORT" || echo "not running"

  printf 'robot x11vnc     : '
  svc="$(robot 'systemctl is-active x11vnc.service; systemctl is-enabled x11vnc.service' | tr '\n' ' ')"
  echo "${svc:-(no answer)}"

  printf 'VNC Viewer app   : '
  if [ -d "$VNC_APP" ]; then
    echo "installed ($(/usr/bin/defaults read "$VNC_APP/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null))"
  else
    echo "NOT installed at $VNC_APP"
  fi

  if tunnel_up; then
    echo 'end-to-end RFB   :'
    rfb_probe || echo "  (chain is broken - see reports/vnc.md)"
  else
    echo 'end-to-end RFB   : skipped (no tunnel)'
  fi
}

ensure_tunnel() {
  if [ ! -f "$PLIST" ]; then
    echo "ABORT: $PLIST is missing - install it first (see reports/vnc.md)." >&2
    return 1
  fi
  if ! agent_loaded; then
    echo "loading launchd agent $LABEL ..."
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
  fi
  if ! tunnel_up; then
    echo "starting the tunnel ..."
    launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null
  fi
  i=0
  while [ "$i" -lt 25 ]; do tunnel_up && return 0; sleep 1; i=$((i + 1)); done
  return 1
}

stop() {
  if agent_loaded; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && echo "stopped and unloaded $LABEL"
  else
    echo "$LABEL was not loaded"
  fi
  pkill -f "ssh .*-L $LOCAL_PORT:127.0.0.1:$REMOTE_PORT" 2>/dev/null && echo "stopped a stray tunnel"
  echo "tunnel down. (./bin/robot-vnc.sh brings it back; it also returns at next login"
  echo " unless you remove $PLIST.)"
}

case "${1:-}" in
  --status)  status; exit 0 ;;
  --probe)   rfb_probe; exit $? ;;
  --restart) launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null && echo "tunnel restarted." || echo "could not restart $LABEL" >&2; exit 0 ;;
  --stop)    stop; exit 0 ;;
  -h|--help) sed -n '2,/^# --- end of help ---$/p' "$0"; exit 0 ;;
esac

echo "### 1. robot reachable? ###"
if ! robot 'echo ok' | grep -q ok; then
  echo "ABORT: cannot reach the robot (alias '$SSH_ALIAS')." >&2
  echo "       try: ./bin/find-robot.sh" >&2
  exit 1
fi
echo "yes ($(robot_ip))."

echo "### 2. SSH tunnel  localhost:$LOCAL_PORT -> robot:127.0.0.1:$REMOTE_PORT ###"
if ensure_tunnel; then
  echo "up."
else
  echo "ABORT: nothing listening on 127.0.0.1:$LOCAL_PORT." >&2
  echo "       ./bin/robot-vnc.sh --stop   then retry" >&2
  exit 1
fi

echo "### 3. end-to-end VNC check ###"
rfb_probe || echo "  (the tunnel is up but the handshake failed - see reports/vnc.md)"

echo "### 4. robot-side service ###"
svc="$(robot 'systemctl is-active x11vnc.service')"
echo "x11vnc.service is ${svc:-unknown}."
if [ "$svc" != "active" ]; then
  echo "  fix with: ./bin/rsudo.sh -c 'systemctl restart x11vnc.service'"
fi

echo "### 5. launch VNC Viewer ###"
if [ -d "$VNC_APP" ]; then
  open -a "$VNC_APP" && echo "launched."
else
  echo "not found at $VNC_APP - install RealVNC Viewer or set AGILEX_VNC_APP."
fi

cat <<EOF

------------------------------------------------------------------
 In VNC Viewer, connect to:
     Address  : localhost:$LOCAL_PORT
     Password : $VNC_PW
 (RealVNC Viewer does not register the vnc:// URL scheme, so type the
  address once and let it save the connection.)

 The tunnel stays up on its own (launchd KeepAlive); check it any time:
     ./bin/robot-vnc.sh --status
 Stop it with:
     ./bin/robot-vnc.sh --stop
------------------------------------------------------------------
EOF
