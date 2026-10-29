#!/usr/bin/env bash
# install.sh — one entry point, two modes.
#
#   On the robot (as root):        sudo ./install.sh
#   From a laptop, over SSH:       ./install.sh agilex@agilex.local
#
# It works out which one you meant by looking for the robot's own filesystem.
#
# WHAT IT DOES
#   robot side  : copies the server + UI into /usr/local/share/agilex-remote,
#                 installs and starts the systemd unit, prints the URL.
#   laptop side : tars this checkout, scp's it to the robot, runs the robot side
#                 under sudo, then checks /api/status through an SSH tunnel.
#
# REQUIREMENTS
#   robot : python3, ffmpeg, ROS 2 Humble, piper_sdk (pip), CAN interfaces up
#   laptop: ssh with key auth to the robot
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/usr/local/share/agilex-remote
UNIT=/etc/systemd/system/agilex-remote.service
PORT="${AGILEX_PORT:-8765}"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- robot side
if [ -f "$HERE/server/agilex-remote-server.py" ] && [ -d /opt/ros ] && id agilex >/dev/null 2>&1; then
  say "installing on this machine (robot side)"
  [ "$(id -u)" = 0 ] || die "run as root:  sudo ./install.sh"

  for f in python3 ffmpeg; do
    command -v "$f" >/dev/null || die "$f not found - install it first"
  done

  install -d -m 0755 "$DEST"
  install -m 0755 "$HERE/server/agilex-remote-server.py" "$DEST/agilex-remote-server.py"
  install -m 0644 "$HERE/server/remote-ui.html"          "$DEST/remote-ui.html"

  # Password for the web UI. Kept in an EnvironmentFile (0600) rather than in the
  # unit, so `systemctl cat` does not leak it.
  ENVF=/etc/agilex-remote.env
  if [ ! -f "$ENVF" ]; then
    PW="${AGILEX_PASSWORD:-}"
    if [ -z "$PW" ]; then
      if [ -t 0 ]; then
        read -r -s -p "web UI password (blank = 'changeme'): " PW; echo
      fi
    fi
    printf 'AGILEX_PASSWORD=%s\n' "${PW:-changeme}" > "$ENVF"
    chmod 0600 "$ENVF"
    chown root:root "$ENVF"
  fi

  say "systemd unit"
  cat > "$UNIT" <<UNIT_EOF
[Unit]
Description=AgileX virtual remote controller (HTTP + MJPEG + ROS/CAN bridge)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=agilex
Group=agilex
WorkingDirectory=$DEST
EnvironmentFile=-$ENVF
Environment=AGILEX_REMOTE_HOST=0.0.0.0
Environment=AGILEX_ALLOW_ANON=0
Environment=ROS_DOMAIN_ID=222
Environment=ROS_LOCALHOST_ONLY=1
Environment=BASH_ENV=/dev/null
ExecStartPre=+/bin/bash -c 'if ! ip link show can_left >/dev/null 2>&1 || ! ip link show can_right >/dev/null 2>&1; then cd /home/agilex/piper_ros && bash ./can_config.sh || true; sleep 2; fi'
ExecStart=/bin/bash -lc 'source /opt/ros/humble/setup.bash && source /home/agilex/agilex_ws/install/setup.bash 2>/dev/null; exec /usr/bin/python3 $DEST/agilex-remote-server.py'
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT_EOF

  systemctl daemon-reload
  systemctl enable agilex-remote >/dev/null 2>&1 || true
  systemctl restart agilex-remote
  sleep 5
  systemctl is-active agilex-remote

  IP="$(hostname -I | awk '{print $1}')"
  say "done"
  echo "  UI:      http://$IP:$PORT/"
  echo "  status:  curl -s http://127.0.0.1:$PORT/api/status | head -c 400"
  echo "  logs:    journalctl -u agilex-remote -f"
  exit 0
fi

# --------------------------------------------------------------- laptop side
TARGET="${1:-}"
[ -n "$TARGET" ] || die "usage: ./install.sh user@robot   (or run on the robot as root)"

say "packaging $HERE"
TARBALL="$(mktemp -t agilex-remote).tar.gz"
tar -czf "$TARBALL" -C "$(dirname "$HERE")" \
    --exclude='.git' --exclude='.venv*' --exclude='__pycache__' \
    --exclude='*.pyc' "$(basename "$HERE")"

say "uploading to $TARGET"
scp -q "$TARBALL" "$TARGET:/tmp/agilex-remote-bundle.tar.gz"
REMOTE_DIR="$(basename "$HERE")"

say "installing (sudo on the robot)"
ssh -t "$TARGET" "set -e
  rm -rf /tmp/agilex-remote-bundle && mkdir -p /tmp/agilex-remote-bundle
  tar -xzf /tmp/agilex-remote-bundle.tar.gz -C /tmp/agilex-remote-bundle
  cd /tmp/agilex-remote-bundle/$REMOTE_DIR
  sudo ./install.sh"

rm -f "$TARBALL"
say "verifying through an SSH tunnel"
ssh -N -L "$PORT:127.0.0.1:$PORT" "$TARGET" &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true' EXIT
sleep 2
if curl -fsS --max-time 6 "http://127.0.0.1:$PORT/api/status" >/dev/null; then
  echo "  OK - open http://127.0.0.1:$PORT/"
else
  echo "  could not reach the API through the tunnel; check: journalctl -u agilex-remote"
fi
