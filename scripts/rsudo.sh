#!/usr/bin/env bash
# rsudo.sh — run one command (or one local script) as root on the robot.
#
#   ./scripts/rsudo.sh -c 'systemctl restart agilex-remote'
#   ./scripts/rsudo.sh -f scripts/reset-can.sh
#
# The sudo password is NEVER stored in this repository and NEVER placed in an
# ssh argv. It comes from AGILEX_SUDO_PASSWORD, or from an interactive prompt.
# The command/script is streamed on stdin to `sudo -S ... bash -s`, so nothing
# is staged in /tmp on either side.
#
# If the robot has passwordless sudo, that is used first and no password is
# needed. Configure the target with AGILEX_SSH_ALIAS (an ssh_config Host) or
# AGILEX_HOST (default agilex.local). No address is hard-coded.
set -euo pipefail

HOST="${AGILEX_SSH_ALIAS:-${AGILEX_HOST:-agilex.local}}"
SSH_BIN="${AGILEX_SSH_BIN:-ssh}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=20)
[ -n "${AGILEX_SSH_PORT:-}" ] && SSH_OPTS+=(-p "$AGILEX_SSH_PORT")

usage() { sed -n '2,16p' "$0"; }

MODE=""
PAYLOAD=""
while [ $# -gt 0 ]; do
  case "$1" in
    -c) [ $# -ge 2 ] || { echo "rsudo: -c needs a command" >&2; exit 2; }; MODE=cmd; PAYLOAD="$2"; shift 2 ;;
    -f) [ $# -ge 2 ] || { echo "rsudo: -f needs a file" >&2; exit 2; }; MODE=file; PAYLOAD="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "rsudo: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[ -n "$MODE" ] || { usage >&2; exit 2; }

if [ "$MODE" = "file" ]; then
  [ -r "$PAYLOAD" ] || { echo "rsudo: cannot read $PAYLOAD" >&2; exit 2; }
  SCRIPT="$(cat "$PAYLOAD")"
else
  SCRIPT="$PAYLOAD"
fi
[ -n "$SCRIPT" ] || { echo "rsudo: empty command" >&2; exit 2; }

# Passwordless sudo is the safe, non-interactive path.
if "$SSH_BIN" "${SSH_OPTS[@]}" "$HOST" 'sudo -n true' >/dev/null 2>&1; then
  printf '%s\n' "$SCRIPT" | "$SSH_BIN" "${SSH_OPTS[@]}" "$HOST" 'sudo -n bash -s'
  exit $?
fi

PW="${AGILEX_SUDO_PASSWORD:-}"
if [ -z "$PW" ]; then
  if [ -t 0 ]; then
    printf 'sudo password for %s: ' "$HOST" >&2
    IFS= read -r -s PW
    printf '\n' >&2
  else
    echo "rsudo: no passwordless sudo and no AGILEX_SUDO_PASSWORD (stdin is not a TTY)" >&2
    exit 3
  fi
fi
[ -n "$PW" ] || { echo "rsudo: empty password" >&2; exit 3; }
case "$PW" in
  *$'\n'*|*$'\r'*) echo "rsudo: password must be a single line" >&2; exit 3 ;;
esac

# One line of password followed by the script: sudo consumes the first line,
# bash -s reads the rest. Both travel on stdin, never in argv or a file.
{ printf '%s\n' "$PW"; printf '%s\n' "$SCRIPT"; } \
  | "$SSH_BIN" "${SSH_OPTS[@]}" "$HOST" "sudo -S -p '' bash -s"
