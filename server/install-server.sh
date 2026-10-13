#!/bin/bash
# Install the virtual-remote server on the AgileX robot.
# Run as root via: ./bin/rsudo.sh -f remote/60-remote-server-install.sh
# Expects the payload staged at /tmp/agilex-remote (see bin/remote-deploy.sh).
set -euo pipefail

SRC=/tmp/agilex-remote
DEST=/usr/local/share/agilex-remote
[ -d "$SRC" ] || { echo "ERROR: $SRC missing - stage the payload first" >&2; exit 1; }
for f in agilex-remote-server.py remote-ui.html; do
  [ -f "$SRC/$f" ] || { echo "ERROR: $SRC/$f missing" >&2; exit 1; }
done

echo "=== install files ==="
install -d -m 0755 "$DEST"
install -m 0755 "$SRC/agilex-remote-server.py" "$DEST/agilex-remote-server.py"
install -m 0644 "$SRC/remote-ui.html"          "$DEST/remote-ui.html"
ls -l "$DEST"

echo
echo "=== systemd unit ==="
cat > /etc/systemd/system/agilex-remote.service <<'UNIT'
[Unit]
Description=AgileX virtual remote controller server (HTTP + MJPEG + ROS/CAN bridge)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Runs as the agilex user, NOT root: the arm SDK and ROS workspaces live in that
# account, and CAN access is granted by group membership.
User=agilex
Group=agilex
WorkingDirectory=/usr/local/share/agilex-remote
Environment=AGILEX_REMOTE_HOST=0.0.0.0
Environment=AGILEX_REMOTE_PORT=8765
# 0.0.0.0 is required so the Android phone can reach the controller directly.
# Off-loopback callers must present the cookie handed out by GET / ; set
# AGILEX_ALLOW_ANON=1 only if you accept anyone on the network driving it.
Environment=AGILEX_ALLOW_ANON=0
Environment=ROS_DOMAIN_ID=222
Environment=ROS_LOCALHOST_ONLY=1
# Bring in the ROS 2 + workspace overlays the robot normally gets from .bashrc
Environment=BASH_ENV=/dev/null
# The agilex_ws overlay is REQUIRED, not optional: tracer_msgs/msg/TracerStatus
# lives there, and without it the server cannot subscribe to /tracer_status -
# which is the only topic that actually carries the chassis state (/odom stayed
# silent in testing). Missing it degrades the chassis panel to "disconnected".
ExecStart=/bin/bash -lc 'source /opt/ros/humble/setup.bash && source /home/agilex/agilex_ws/install/setup.bash && source /home/agilex/piper_ros/install/setup.bash && exec /usr/bin/python3 /usr/local/share/agilex-remote/agilex-remote-server.py'
Restart=on-failure
RestartSec=5
# Never let a stuck stream take the box down
TimeoutStopSec=10
KillMode=mixed

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
echo "unit written."

echo
echo "=== the uvcvideo recovery trap ==="
# Seen live on 2026-10-13: the cameras stayed on USB but /dev/video* vanished
# because uvcvideo failed to rebind after a camera re-enumerated. The service
# needs those nodes, so recover them if missing.
if ! ls /dev/video0 >/dev/null 2>&1; then
  echo "/dev/video0 missing - reloading uvcvideo"
  modprobe -r uvcvideo 2>/dev/null || true
  sleep 2
  modprobe uvcvideo 2>/dev/null || true
  sleep 4
fi
ls /dev/video* 2>/dev/null || echo "WARNING: still no video devices - camera may be unplugged"

echo
echo "=== enable + start ==="
systemctl enable agilex-remote.service >/dev/null 2>&1
systemctl restart agilex-remote.service
sleep 6
echo -n "active: "; systemctl is-active agilex-remote.service
echo "--- recent journal ---"
journalctl -u agilex-remote.service --no-pager -n 20 2>/dev/null | tail -20
echo "--- listening ---"
ss -tlnp 2>/dev/null | grep 8765 || echo "NOT LISTENING"
