#!/usr/bin/env bash
# robot-desktop.sh — open the AgileX robot's desktop on this Mac.
#
# WHY A TUNNEL: the robot's IP changes on every reboot (eduroam DHCP), so
# pointing the client at a fixed address would rot. Going through the "agilex"
# SSH alias means the ProxyCommand discovers the current address, and the
# tunnel always lands on the right machine. It also means the robot's NoMachine
# port never has to be exposed to the campus network.
#
#   ./bin/robot-desktop.sh            # tunnel + launch NoMachine
#   ./bin/robot-desktop.sh --status   # just report what is up
#   ./bin/robot-desktop.sh --stop     # tear the tunnel down
#
# Then in NoMachine:  Protocol NX | Host localhost | Port 4400 | User agilex
#
# The LOCAL port is 4400, not 4000, on purpose: installing the NoMachine client
# on macOS brings up its own nxd, which briefly binds 4000 while starting, and
# that made the tunnel die with "Address already in use".
set -uo pipefail

LOCAL_PORT="${AGILEX_NX_LOCAL_PORT:-4400}"
REMOTE_PORT="${AGILEX_NX_REMOTE_PORT:-4000}"
SSH_ALIAS="${AGILEX_SSH_ALIAS:-agilex}"
TUNNEL_PIDFILE="${TMPDIR:-/tmp}/agilex-nx-tunnel.pid"

tunnel_up() { lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; }

robot_ip() {
  ssh -o BatchMode=yes -o ConnectTimeout=25 "$SSH_ALIAS" 'hostname -I' 2>/dev/null | awk '{print $1}'
}

status() {
  printf 'robot reachable : '
  ip="$(robot_ip)"
  if [ -n "$ip" ]; then echo "yes ($ip)"; else echo "NO"; fi
  printf 'local tunnel    : '
  tunnel_up && echo "listening on 127.0.0.1:$LOCAL_PORT" || echo "not running"
  printf 'NoMachine app   : '
  if [ -d /Applications/NoMachine.app ]; then
    echo "installed ($(/usr/bin/defaults read /Applications/NoMachine.app/Contents/Info.plist CFBundleShortVersionString 2>/dev/null))"
  else
    echo "NOT installed"
  fi
  printf 'VNC fallback    : '
  [ -d "/Applications/VNC Viewer.app" ] && echo "RealVNC Viewer present" || echo "absent"
}

stop() {
  if [ -f "$TUNNEL_PIDFILE" ]; then
    pid="$(cat "$TUNNEL_PIDFILE" 2>/dev/null)"
    [ -n "$pid" ] && kill "$pid" 2>/dev/null && echo "stopped tunnel pid $pid"
    rm -f "$TUNNEL_PIDFILE"
  fi
  pkill -f "ssh .*-L $LOCAL_PORT:127.0.0.1:$REMOTE_PORT" 2>/dev/null && echo "stopped stray tunnels"
  echo "tunnel down."
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --stop)   stop;   exit 0 ;;
  -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
esac

echo "### 1. robot reachable? ###"
if ! ssh -o BatchMode=yes -o ConnectTimeout=30 "$SSH_ALIAS" 'echo ok' 2>/dev/null | grep -q ok; then
  echo "ABORT: cannot reach the robot." >&2
  echo "       try: ./bin/find-robot.sh" >&2
  exit 1
fi
echo "yes ($(robot_ip))."

echo "### 2. SSH tunnel  localhost:$LOCAL_PORT -> robot:$REMOTE_PORT ###"
if tunnel_up; then
  echo "already up."
else
  if ssh -f -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=6 \
        -L "$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" "$SSH_ALIAS"; then
    echo "tunnel started."
    pgrep -f "ssh .*-L $LOCAL_PORT:127.0.0.1:$REMOTE_PORT" 2>/dev/null | head -1 > "$TUNNEL_PIDFILE"
  else
    echo "ABORT: could not open the tunnel (is local port $LOCAL_PORT busy?)." >&2
    echo "       ./bin/robot-desktop.sh --stop   then retry" >&2
    exit 1
  fi
fi
sleep 1
tunnel_up || { echo "ABORT: nothing listening on $LOCAL_PORT." >&2; exit 1; }

echo "### 3. launch NoMachine ###"
if [ -d /Applications/NoMachine.app ]; then
  open -a NoMachine && echo "launched."
else
  echo "NoMachine.app is not installed - install it with:"
  echo "    ./bin/install-nomachine-client.sh /tmp/nomachine.dmg"
fi

cat <<EOF

------------------------------------------------------------------
 In NoMachine, create a connection with:
     Protocol : NX
     Host     : localhost
     Port     : $LOCAL_PORT
     User     : agilex
 (the tunnel already authenticated you, so no robot password is needed)

 Keep this tunnel alive while you work; stop it with:
     ./bin/robot-desktop.sh --stop
------------------------------------------------------------------
EOF
