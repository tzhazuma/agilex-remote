#!/bin/bash
# install-server.sh — install (or repair) the AgileX remote server on the robot.
#
# Run this ON THE ROBOT as root:
#
#   sudo ./server/install-server.sh
#   sudo AGILEX_PAYLOAD=/path/to/server ./server/install-server.sh
#   sudo AGILEX_PASSWORD_FILE=/root/agilex-pw ./server/install-server.sh
#
# From a laptop use ./install.sh instead; it stages this directory and calls
# this script. The web-UI password has NO default. It comes from
# AGILEX_PASSWORD, AGILEX_PASSWORD_FILE, or an interactive prompt; without one
# the install refuses to run. The value is written to
# /etc/agilex-remote.env (0600) and referenced by the unit as a mandatory
# EnvironmentFile, so `systemctl cat` does not reveal it.
#
#   --env-only   create/repair the environment file and exit (password rotation
#                and offline tests; no service or unit changes). A supplied
#                AGILEX_PASSWORD/AGILEX_PASSWORD_FILE rotates the file; without
#                one an existing strong file is kept, and a weak/missing file is
#                refused. AGILEX_FORCE_ENV=1 forces a rewrite.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${AGILEX_PAYLOAD:-$HERE}"
DEST=/usr/local/share/agilex-remote
ENVF="${AGILEX_ENV_FILE:-/etc/agilex-remote.env}"
UNIT="${AGILEX_UNIT_FILE:-/etc/systemd/system/agilex-remote.service}"
PORT="${AGILEX_REMOTE_PORT:-8765}"
MODE=install

die() { echo "ERROR: $*" >&2; exit 1; }

case "$ENVF$UNIT" in
  *$'\n'*|*$'\r'*) die "environment file/unit path must be a single line" ;;
esac

case "${1:-}" in
  --env-only) MODE=env-only ;;
  "") ;;
  -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
  *) die "usage: $0 [--env-only]" ;;
esac

if [ "$MODE" = "install" ]; then
  [ "$(id -u)" = 0 ] || die "run as root: sudo $0"
elif [ "$(id -u)" != 0 ] && [ "$ENVF" = "/etc/agilex-remote.env" ]; then
  die "run as root to write the system environment file"
fi

valid_pw() {
  local pw="$1"
  [ -n "$pw" ] || return 1
  [ "${#pw}" -ge 8 ] || return 1
  case "$pw" in
    changeme) return 1 ;;
    *[[:space:]]*|*[\"\']*|*\\*) return 1 ;;
  esac
  return 0
}

write_env() {
  local pw="${AGILEX_PASSWORD:-}" pwfile="${AGILEX_PASSWORD_FILE:-}" existing="" rotate=0
  if [ -n "$pw" ] || [ -n "$pwfile" ] || [ "${AGILEX_FORCE_ENV:-0}" = "1" ]; then
    rotate=1
  fi

  if [ -s "$ENVF" ] && [ "$rotate" = 0 ]; then
    chmod 0600 "$ENVF" 2>/dev/null || true
    existing="$(sed -n 's/^AGILEX_PASSWORD=//p' "$ENVF" | head -n 1)"
    existing="${existing%\"}"; existing="${existing#\"}"
    existing="${existing%\'}"; existing="${existing#\'}"
    if valid_pw "$existing"; then
      echo "keeping existing $ENVF"
      return 0
    fi
    echo "existing $ENVF is missing a strong AGILEX_PASSWORD - a new one is required" >&2
  fi

  if [ -z "$pw" ] && [ -n "$pwfile" ]; then
    [ -r "$pwfile" ] || die "cannot read AGILEX_PASSWORD_FILE=$pwfile"
    pw="$(head -n 1 "$pwfile" | tr -d '\r')"
  fi
  if [ -z "$pw" ]; then
    if [ -t 0 ]; then
      printf 'web UI password (min 8 chars, no whitespace/quotes): ' >&2
      IFS= read -r -s pw
      printf '\n' >&2
    else
      die "AGILEX_PASSWORD is unset and stdin is not a TTY; refusing to install with the server's built-in default"
    fi
  fi
  [ -n "$pw" ] || die "empty password"
  [ "${#pw}" -ge 8 ] || die "password must be at least 8 characters"
  case "$pw" in
    changeme) die "refusing the old 'changeme' default; choose a real password" ;;
    *[[:space:]]*) die "password must not contain whitespace" ;;
    *[\"\']*) die "password must not contain quotes" ;;
    *\\*) die "password must not contain backslashes" ;;
  esac

  install -d -m 0755 "$(dirname "$ENVF")"
  umask 077
  TMP_ENV_FILE="$(mktemp "$(dirname "$ENVF")/.agilex-remote.env.XXXXXX")"
  trap 'rm -f "$TMP_ENV_FILE"' EXIT
  printf 'AGILEX_PASSWORD=%s\n' "$pw" > "$TMP_ENV_FILE"
  chmod 0600 "$TMP_ENV_FILE"
  if [ "$(id -u)" = 0 ]; then chown root:root "$TMP_ENV_FILE"; fi
  mv -f "$TMP_ENV_FILE" "$ENVF"
  trap - EXIT
  echo "wrote $ENVF (mode 0600)"
}

write_env
if [ "$MODE" = "env-only" ]; then
  exit 0
fi

[ -d "$SRC" ] || die "$SRC missing - stage the payload or set AGILEX_PAYLOAD to the server directory"
for f in agilex-remote-server.py remote-ui.html; do
  [ -f "$SRC/$f" ] || die "$SRC/$f missing - stage the payload (AGILEX_PAYLOAD=...) first"
done

echo "=== install files ==="
install -d -m 0755 "$DEST"
install -m 0755 "$SRC/agilex-remote-server.py" "$DEST/agilex-remote-server.py"
install -m 0644 "$SRC/remote-ui.html"          "$DEST/remote-ui.html"
ls -l "$DEST"

echo
echo "=== systemd unit ==="
cat > "$UNIT" <<UNIT
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
WorkingDirectory=$DEST
# Mandatory, not "-": a missing/insecure environment file must fail the unit
# instead of silently falling back to the password in the Python source.
EnvironmentFile=$ENVF
Environment=AGILEX_REMOTE_HOST=0.0.0.0
Environment=AGILEX_REMOTE_PORT=$PORT
# 0.0.0.0 is required so the Android phone can reach the controller directly.
# Off-loopback callers must present the cookie handed out by GET / ; set
# AGILEX_ALLOW_ANON=1 only if you accept anyone on the network driving it.
Environment=AGILEX_ALLOW_ANON=0
Environment=ROS_DOMAIN_ID=222
Environment=ROS_LOCALHOST_ONLY=1
# Bring in the ROS 2 + workspace overlays the robot normally gets from .bashrc
Environment=BASH_ENV=/dev/null
# The agilex_ws overlay carries tracer_msgs/msg/TracerStatus; piper_ros carries
# the arm workspace. Use ";" so a missing optional overlay does not abort the
# exec line (the server reports the affected panel as disconnected).
ExecStartPre=+/bin/bash -c 'if ! ip link show can_left >/dev/null 2>&1 || ! ip link show can_right >/dev/null 2>&1; then cd /home/agilex/piper_ros && bash ./can_config.sh || true; sleep 2; fi'
ExecStart=/bin/bash -lc 'source /opt/ros/humble/setup.bash; source /home/agilex/agilex_ws/install/setup.bash 2>/dev/null; source /home/agilex/piper_ros/install/setup.bash 2>/dev/null; exec /usr/bin/python3 $DEST/agilex-remote-server.py'
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
ss -tlnp 2>/dev/null | grep "$PORT" || echo "NOT LISTENING"
