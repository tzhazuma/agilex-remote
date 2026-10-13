#!/usr/bin/env bash
# install-local-helpers.sh — install the SSH discovery helper where macOS will
# actually let it run.
#
# WHY: agilex-proxy.sh is used as an SSH ProxyCommand. That is fine when *you*
# run ssh from a terminal, but a launchd LaunchAgent (the standing NoMachine
# tunnel, com.azuma.agilex-nx-tunnel) cannot execute anything under
# ~/Documents - macOS TCC refuses it with "Operation not permitted", and the
# tunnel then dies with "Connection closed by UNKNOWN port 65535".
#
# So the canonical copy for background use lives in ~/.local/bin, and the ntfy
# topic goes to ~/.config/agilex. ~/.ssh/config points at the installed copy.
#
# Re-run this after editing bin/agilex-proxy.sh.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="$(dirname "$DIR")"

BIN_DIR="$HOME/.local/bin"
CFG_DIR="$HOME/.config/agilex"
mkdir -p "$BIN_DIR" "$CFG_DIR"

echo "### installing helper ###"
install -m 0755 "$DIR/agilex-proxy.sh" "$BIN_DIR/agilex-proxy.sh"
install -m 0600 "$BASE/remote/robot-files/agilex-report-ip.topic" "$CFG_DIR/ntfy-topic"
ls -l "$BIN_DIR/agilex-proxy.sh" "$CFG_DIR/ntfy-topic"

echo
echo "### ~/.ssh/config should reference ###"
echo "    ProxyCommand $BIN_DIR/agilex-proxy.sh %h %p"
if grep -q "$BIN_DIR/agilex-proxy.sh" "$HOME/.ssh/config" 2>/dev/null; then
  echo "    (already referenced)"
else
  echo "    WARNING: ~/.ssh/config does not reference it yet."
fi

echo
echo "### quick check ###"
if "$BIN_DIR/agilex-proxy.sh" --help >/dev/null 2>&1; then :; fi
if [ -r "$CFG_DIR/ntfy-topic" ]; then
  echo "topic: $(cat "$CFG_DIR/ntfy-topic")"
fi
echo "done."
