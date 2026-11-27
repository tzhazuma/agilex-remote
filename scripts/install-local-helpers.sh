#!/usr/bin/env bash
# install-local-helpers.sh — install the SSH discovery helper where macOS will
# actually let it run.
#
# WHY: agilex-proxy.sh is used as an SSH ProxyCommand. That is fine when *you*
# run ssh from a terminal, but a launchd LaunchAgent (the standing NoMachine
# tunnel, com.example.agilex-nx-tunnel) cannot execute anything under
# ~/Documents - macOS TCC refuses it with "Operation not permitted", and the
# tunnel then dies with "Connection closed by UNKNOWN port 65535".
#
# So the canonical copy for background use lives in ~/.local/bin. The ntfy
# topic, if you use one, is installed as ~/.config/agilex/ntfy-topic (0600).
# The topic is optional and is NEVER printed.
#
# Re-run this after editing scripts/agilex-proxy.sh or scripts/rsudo.sh.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(dirname "$DIR")"

BIN_DIR="$HOME/.local/bin"
CFG_DIR="$HOME/.config/agilex"
mkdir -p "$BIN_DIR" "$CFG_DIR"

echo "### installing helpers ###"
install -m 0755 "$DIR/agilex-proxy.sh" "$BIN_DIR/agilex-proxy.sh"
install -m 0755 "$DIR/rsudo.sh"        "$BIN_DIR/rsudo.sh"
ls -l "$BIN_DIR/agilex-proxy.sh" "$BIN_DIR/rsudo.sh"

echo
echo "### ntfy topic (optional) ###"
TOPIC_SRC=""
for candidate in \
  "${AGILEX_NTFY_TOPIC_FILE:-}" \
  "$HOME/.config/agilex/ntfy-topic" \
  "$BASE/remote/robot-files/agilex-report-ip.topic"; do
  if [ -n "$candidate" ] && [ -r "$candidate" ]; then
    TOPIC_SRC="$candidate"
    break
  fi
done
if [ -n "$TOPIC_SRC" ]; then
  install -m 0600 "$TOPIC_SRC" "$CFG_DIR/ntfy-topic"
  echo "installed $CFG_DIR/ntfy-topic (contents not shown)"
else
  echo "no topic configured - skipping (ntfy discovery is optional)"
  echo "set AGILEX_NTFY_TOPIC_FILE=/path/to/topic and re-run to enable it"
fi

echo
echo "### ~/.ssh/config should reference ###"
echo "    ProxyCommand $BIN_DIR/agilex-proxy.sh %h %p"
if grep -q "$BIN_DIR/agilex-proxy.sh" "$HOME/.ssh/config" 2>/dev/null; then
  echo "    (already referenced)"
else
  echo "    WARNING: ~/.ssh/config does not reference it yet."
fi
echo "done."
