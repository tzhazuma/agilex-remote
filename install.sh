#!/usr/bin/env bash
# install.sh — one entry point, two modes.
#
#   On the robot (as root):        sudo ./install.sh
#   From a laptop, over SSH:       ./install.sh agilex@agilex.local
#
# It works out which one you meant by looking for the robot's own filesystem.
#
# WHAT IT DOES
#   robot side  : hands off to server/install-server.sh, which installs the
#                 server + UI into /usr/local/share/agilex-remote, creates
#                 /etc/agilex-remote.env (0600), installs the systemd unit and
#                 starts it.
#   laptop side : refuses to package a tree that contains credentials, tars it,
#                 scp's it to a fresh mktemp directory on the robot, runs the
#                 robot side under sudo, verifies /api/status through an SSH
#                 tunnel, and removes BOTH staging directories.
#
# PASSWORD
#   There is no default. install-server.sh prompts (or reads AGILEX_PASSWORD /
#   AGILEX_PASSWORD_FILE) and refuses to install without a real password.
#
# REQUIREMENTS
#   robot : python3, ffmpeg, ROS 2 Humble, piper_sdk (pip), CAN interfaces up
#   laptop: ssh with key auth to the robot, curl
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${AGILEX_REMOTE_PORT:-${AGILEX_PORT:-8765}}"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- robot side
if [ -f "$HERE/server/agilex-remote-server.py" ] && [ -d /opt/ros ] && id agilex >/dev/null 2>&1; then
  say "installing on this machine (robot side)"
  [ "$(id -u)" = 0 ] || die "run as root:  sudo ./install.sh"

  for f in python3 ffmpeg; do
    command -v "$f" >/dev/null || die "$f not found - install it first"
  done

  [ -x "$HERE/server/install-server.sh" ] || die "server/install-server.sh missing"
  AGILEX_PAYLOAD="$HERE/server" exec /bin/bash "$HERE/server/install-server.sh"
fi

# --------------------------------------------------------------- laptop side
TARGET="${1:-}"
[ -n "$TARGET" ] || die "usage: ./install.sh user@robot   (or run on the robot as root)"
case "$TARGET" in
  -*) die "refusing a target that looks like an ssh option: $TARGET" ;;
  *[!A-Za-z0-9@._:-]*) die "refusing an unexpected target (allowed: user@host, letters/digits/._:-): $TARGET" ;;
esac
case "$PORT" in
  ''|*[!0-9]*) die "AGILEX_PORT/AGILEX_REMOTE_PORT must be numeric: $PORT" ;;
esac
for f in ssh scp tar curl; do
  command -v "$f" >/dev/null || die "$f not found on this laptop"
done

say "checking the tree for credentials"
if [ -x "$HERE/scripts/scan-secrets.sh" ]; then
  "$HERE/scripts/scan-secrets.sh" --root "$HERE" >&2 || die "refusing to package a tree with credentials (see above)"
else
  die "scripts/scan-secrets.sh missing"
fi

say "packaging $HERE"
umask 077
LOCAL_TMP="$(mktemp -d "${TMPDIR:-/tmp}/agilex-remote.XXXXXX")" || die "cannot create a local temp directory"
trap 'rm -rf "$LOCAL_TMP"' EXIT HUP INT TERM
TARBALL="$LOCAL_TMP/bundle.tar.gz"
tar -czf "$TARBALL" -C "$(dirname "$HERE")" \
    --exclude='.git' --exclude='.venv*' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='*.pem' --exclude='*.key' \
    --exclude='*password*' --exclude='client.env' \
    --exclude='windows-client' --exclude='test-server-offline.py' \
    "$(basename "$HERE")"

say "uploading to $TARGET"
STAGE="$(ssh -o BatchMode=yes -o ConnectTimeout=20 "$TARGET" 'mktemp -d /tmp/agilex-remote.XXXXXX')" \
  || die "cannot create a staging directory on $TARGET"
case "$STAGE" in
  /tmp/agilex-remote.*) ;;
  *) die "refusing an unexpected remote staging path: $STAGE" ;;
esac
case "$STAGE" in
  *[!A-Za-z0-9._/-]*) die "refusing a staging path with shell metacharacters: $STAGE" ;;
esac
REMOTE_DIR="$(basename "$HERE")"
case "$REMOTE_DIR" in
  *"'"*|*$'\n'*|*$'\r'*) die "refusing a local directory name with quotes/newlines: $REMOTE_DIR" ;;
esac

cleanup() {
  rm -rf "$LOCAL_TMP"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "rm -rf '$STAGE'" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

scp -q -o BatchMode=yes -o ConnectTimeout=20 "$TARBALL" "$TARGET:$STAGE/bundle.tar.gz"

say "installing (sudo on the robot)"
ssh -t "$TARGET" "set -e
  tar -xzf '$STAGE/bundle.tar.gz' -C '$STAGE'
  cd '$STAGE/$REMOTE_DIR'
  sudo ./install.sh"

rm -f "$TARBALL"
say "verifying through an SSH tunnel"
ssh -N -L "$PORT:127.0.0.1:$PORT" "$TARGET" &
TUNNEL=$!
trap 'kill $TUNNEL 2>/dev/null || true; cleanup' EXIT HUP INT TERM
sleep 2
if curl -fsS --max-time 6 "http://127.0.0.1:$PORT/api/status" >/dev/null; then
  echo "  OK - open http://127.0.0.1:$PORT/"
  echo "  guide: $HERE/../01-使用指南/INSTALL-CONNECT.md"
else
  echo "  could not reach the API through the tunnel; check: journalctl -u agilex-remote"
  exit 1
fi
