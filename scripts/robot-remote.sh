#!/usr/bin/env bash
# robot-remote.sh — open the virtual remote controller for the AgileX robot.
#
# WHAT IT DOES
#   1. makes sure the robot is reachable (SSH alias auto-discovers its IP)
#   2. starts tracer_base on the robot if it is not already running
#      (this is what bridges /cmd_vel -> CAN; see the WARNING below)
#   3. makes sure the standing SSH tunnel for port 8765 is up
#   4. opens the controller in your browser
#
#   ./bin/robot-remote.sh            # start everything and open the browser
#   ./bin/robot-remote.sh --status   # report state, change nothing
#   ./bin/robot-remote.sh --stop     # stop tracer_base + the server
#
# ⚠️ WARNING: launching tracer_base calls EnableCommandedMode() on the chassis.
#    That takes it OUT of RC-handset mode, so the physical remote will stop
#    driving it until the chassis is power-cycled. That is inherent to driving
#    it over ROS. The script asks before doing it the first time.
#
# SAFETY: the server clamps velocity to 0.08 m/s / 0.15 rad/s, has a 700 ms
#    watchdog that forces a stop if commands stop arriving, and exposes an
#    E-STOP. Movements are intentionally tiny.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_PORT="${AGILEX_REMOTE_LOCAL_PORT:-8765}"
REMOTE_PORT="${AGILEX_REMOTE_REMOTE_PORT:-8765}"
SSH_ALIAS="${AGILEX_SSH_ALIAS:-agilex}"

ssh_robot() { ssh -o BatchMode=yes -o ConnectTimeout=30 "$SSH_ALIAS" "$@"; }
tunnel_up() { lsof -nP -iTCP:"$LOCAL_PORT" -sTCP:LISTEN >/dev/null 2>&1; }

status() {
  printf 'robot reachable : '
  ip="$(ssh_robot 'hostname -I' 2>/dev/null | awk '{print $1}')"
  [ -n "$ip" ] && echo "yes ($ip)" || echo "NO"
  printf 'remote server   : '
  ssh_robot 'systemctl is-active agilex-remote.service' 2>/dev/null || echo "unknown"
  printf 'tracer_base     : '
  ssh_robot 'pgrep -f tracer_base_node >/dev/null && echo running || echo "not running"' 2>/dev/null
  printf 'cameras         : '
  ssh_robot 'ls /dev/video0 /dev/video2 2>/dev/null | tr "\n" " " || echo "MISSING"' 2>/dev/null
  printf 'ssh tunnel      : '
  tunnel_up && echo "listening on 127.0.0.1:$LOCAL_PORT" || echo "not running"
  printf 'controller HTTP : '
  if tunnel_up; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:$LOCAL_PORT/api/status" 2>/dev/null)"
    echo "HTTP $code"
  else
    echo "n/a"
  fi
}

stop_all() {
  echo "stopping tracer_base + the remote server on the robot..."
  ssh_robot 'pkill -f tracer_base_node 2>/dev/null; sudo -n systemctl stop agilex-remote.service 2>/dev/null; echo stopped' 2>&1 | tail -2
  launchctl bootout "gui/$(id -u)/com.example.agilex-remote-tunnel" 2>/dev/null && echo "tunnel agent stopped" || echo "(tunnel agent not running)"
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --stop)   stop_all; exit 0 ;;
  -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
esac

echo "### 1. robot reachable? ###"
if ! ssh_robot 'echo ok' 2>/dev/null | grep -q ok; then
  echo "ABORT: cannot reach the robot. Try ./bin/find-robot.sh" >&2
  exit 1
fi
echo "yes ($(ssh_robot 'hostname -I' 2>/dev/null | awk '{print $1}'))."

echo "### 2. cameras present? ###"
cam="$(ssh_robot 'ls /dev/video0 /dev/video2 2>/dev/null | wc -l' 2>/dev/null)"
if [ "${cam:-0}" -lt 2 ]; then
  echo "cameras missing (/dev/video*) - reloading uvcvideo on the robot"
  "$DIR/rsudo.sh" -c 'modprobe -r uvcvideo 2>/dev/null; sleep 2; modprobe uvcvideo 2>/dev/null; sleep 4; ls /dev/video*' 2>&1 | tail -4
fi

echo "### 3. remote server ###"
svc="$(ssh_robot 'systemctl is-active agilex-remote.service' 2>/dev/null)"
if [ "$svc" != "active" ]; then
  echo "not active ($svc) - starting it (needs root)"
  "$DIR/rsudo.sh" -c 'systemctl start agilex-remote.service; sleep 5; systemctl is-active agilex-remote.service; journalctl -u agilex-remote.service --no-pager -n 8 | tail -8' 2>&1 | tail -10
else
  echo "already active."
fi

echo "### 4. tracer_base (bridges /cmd_vel -> CAN) ###"
if ssh_robot 'pgrep -f tracer_base_node >/dev/null' 2>/dev/null; then
  echo "already running."
else
  cat <<'WARN'

  ⚠️  tracer_base is NOT running. Starting it will:
        - put the chassis into CAN-commanded mode
        - STOP the physical RC handset from driving the robot
      (power-cycle the chassis to get the handset back)

WARN
  printf '  Start it now? [y/N] '
  read -r ans </dev/tty || ans=n
  case "$ans" in
    [yY]*)
      ssh_robot 'nohup bash -lc "source /opt/ros/humble/setup.bash; source ~/agilex_ws/install/setup.bash; exec ros2 launch tracer_base tracer_base.launch.py" >/tmp/tracer_base.log 2>&1 & echo started pid $!' 2>&1 | tail -2
      sleep 8
      ssh_robot 'pgrep -f tracer_base_node >/dev/null && echo "tracer_base: running" || { echo "tracer_base: FAILED"; tail -15 /tmp/tracer_base.log; }' 2>&1 | tail -16
      ;;
    *) echo "  skipped - the camera and arm panels will still work, but driving will not." ;;
  esac
fi

echo "### 5. SSH tunnel for port $LOCAL_PORT ###"
if tunnel_up; then
  echo "already up."
else
  if [ -f "$HOME/Library/LaunchAgents/com.example.agilex-remote-tunnel.plist" ]; then
    launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.example.agilex-remote-tunnel.plist" 2>/dev/null
    sleep 4
  fi
  if ! tunnel_up; then
    echo "launchd agent did not come up; starting a foreground tunnel for this session"
    ssh -f -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
        -L "$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" "$SSH_ALIAS" && sleep 2
  fi
fi
tunnel_up && echo "tunnel up." || { echo "ABORT: no tunnel on $LOCAL_PORT" >&2; exit 1; }

echo "### 6. verify the controller answers ###"
code="$(curl -s -o /tmp/agilex-status.json -w '%{http_code}' -m 15 "http://127.0.0.1:$LOCAL_PORT/api/status" 2>/dev/null)"
echo "  /api/status -> HTTP $code"
[ "$code" = "200" ] && python3 -c "
import json;d=json.load(open('/tmp/agilex-status.json'))
print('  chassis:', d['chassis']['connected'], '| arm:', d['arm']['connected'], 'enabled=',d['arm']['enabled'])
print('  limits :', d['limits'], 'watchdog', d['watchdog_ms'],'ms')
" 2>/dev/null

echo "### 7. open the controller ###"
URL="http://127.0.0.1:$LOCAL_PORT/"
open "$URL" && echo "opened $URL"

cat <<EOF

------------------------------------------------------------------
 Virtual remote controller: $URL
   - two live camera views (arm camera + floor camera)
   - drive: keyboard WASD/arrows, mouse D-pad, on-screen joystick,
            or a physical gamepad (press a button to wake it)
   - arm: J1..J6 nudges, enable/disable, gripper
   - E-STOP: the red button, or Space / Esc

 Hard caps: $(python3 -c "import json;d=json.load(open('/tmp/agilex-status.json'));print(d['limits']['max_lin'],'m/s,',d['limits']['max_ang'],'rad/s,',d['limits']['max_joint_step_deg'],'deg/joint-step')" 2>/dev/null || echo "0.08 m/s, 0.15 rad/s, 2 deg")
 A 700 ms watchdog stops the robot if commands stop arriving.

 State: ./bin/robot-remote.sh --status
 Stop : ./bin/robot-remote.sh --stop
------------------------------------------------------------------
EOF
