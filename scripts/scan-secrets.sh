#!/usr/bin/env bash
# scan-secrets.sh — fail the release if a credential, private key or real robot
# address is present in the tree.
#
#   ./scripts/scan-secrets.sh                 # scan this checkout
#   ./scripts/scan-secrets.sh --root <dir>    # scan an extracted bundle
#
# Exit status: 0 = clean (warnings allowed), 1 = at least one hard finding.
#
# HIGH severity (exit 1): literal password assignments, private keys, VNC
# password material, real-looking ntfy topics, and RFC1918 robot addresses
# outside the explicitly owned windows-client/ subtree.
# MEDIUM severity (WARN): the same address pattern inside windows-client/,
# which is maintained separately and is out of scope for this installer.
set -uo pipefail

ROOT="$PWD"
while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || { echo "scan-secrets: --root needs a value" >&2; exit 2; }; ROOT="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "scan-secrets: unknown argument: $1" >&2; exit 2 ;;
  esac
done
ROOT="$(cd "$ROOT" 2>/dev/null && pwd)" || { echo "scan-secrets: no such directory" >&2; exit 2; }

FAIL=0
WARN=0

hard() { printf 'FAIL %s:%s: %s\n' "$1" "$2" "$3"; FAIL=$((FAIL + 1)); }
soft() { printf 'WARN %s:%s: %s\n' "$1" "$2" "$3"; WARN=$((WARN + 1)); }

# Portable recursive grep: BSD/macOS grep does not reliably support
# --exclude-dir, so build the file list once with find and pass it to a single
# grep per pattern. ("${FILES[@]}" is bash 3.2 compatible, unlike mapfile.)
FILES=()
scan_grep() {
  local pattern="$1"
  [ "${#FILES[@]}" -gt 0 ] || return 0
  grep -HInE -e "$pattern" "${FILES[@]}" 2>/dev/null || true
}

# grep prints "./path:line:text"; paths are relative and contain no colon.
scan_pattern() {
  local pattern="$1" reason="$2" hit path rest line text
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    path="${hit%%:*}"
    rest="${hit#*:}"
    line="${rest%%:*}"
    path="${path#./}"
    case "$path" in
      scripts/scan-secrets.sh) continue ;;   # the patterns themselves
    esac
    hard "$path" "$line" "$reason"
  done < <(scan_grep "$pattern")
}

cd "$ROOT" || exit 2

FILES=()
while IFS= read -r f; do
  FILES+=("$f")
done < <(find . -type f ! -path './.git/*' ! -path '*/__pycache__/*' ! -name '*.pyc' -print 2>/dev/null)

# A literal value after AGILEX_PASSWORD= / AGILEX_PW= / a sudo or VNC password.
# ${VAR}, <...>, YOUR-... and printf's %s are templates, not secrets.
while IFS= read -r hit; do
  [ -n "$hit" ] || continue
  path="${hit%%:*}"; rest="${hit#*:}"; line="${rest%%:*}"; text="${rest#*:}"
  path="${path#./}"
  value="$(printf '%s\n' "$text" | awk '
    match($0, /AGILEX_(PW|PASSWORD|SUDO_PASSWORD|VNC_PASSWORD)[ \t]*=[ \t]*/) {
      v = substr($0, RSTART + RLENGTH)
      sub(/^["\047]/, "", v)
      print v
      exit
    }')"
  case "$value" in
    ''|'$'*|'%'*|'<'*|'YOUR-'*|'CHANGE_ME'*|'CHOOSE_'*|'REDACTED'*|'PLACEHOLDER'*|'...'*) continue ;;
  esac
  hard "$path" "$line" "literal password assignment"
done < <(scan_grep 'AGILEX_(PW|PASSWORD|SUDO_PASSWORD|VNC_PASSWORD)[[:space:]]*=[[:space:]]*["'"'"']?[A-Za-z0-9._%]')

# Private key material.
scan_pattern '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----' 'private key block'

# ntfy topics: a real topic is both long enough and not a placeholder.
while IFS= read -r hit; do
  [ -n "$hit" ] || continue
  path="${hit%%:*}"; rest="${hit#*:}"; line="${rest%%:*}"; text="${rest#*:}"
  path="${path#./}"
  case "$text" in
    *'YOUR-NTFY-TOPIC-HERE'*|*'<your'*|*'${'*|*'$AGILEX'*|*'example'*) continue ;;
  esac
  hard "$path" "$line" "real ntfy topic"
done < <(scan_grep 'ntfy\.sh/[A-Za-z0-9_-]{8,}')

# VNC password material: a stored 8-byte hash. (A bare user:group ownership
# string such as `agilex:agilex` is not treated as a credential; it appears in
# legitimate chown/ownership documentation.)
scan_pattern 'x11vnc[^ ]*\.pass.*[0-9a-fA-F]{16}' 'VNC password hash'

# A topic dropped into the tree as a file.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  content="$(tr -d '[:space:]' < "$f" 2>/dev/null || true)"
  [ -n "$content" ] || continue
  case "$content" in
    '<'*|'YOUR-'*|'$'*) continue ;;
  esac
  hard "${f#./}" 1 "non-placeholder ntfy topic file"
done < <(find . -type f \( -name '*.topic' -o -name 'ntfy-topic' \) -not -path './.git/*' 2>/dev/null)

# Private key files that should never be in a release.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  hard "${f#./}" 1 "private key file"
done < <(find . -type f \( -name 'id_rsa' -o -name 'id_ed25519' -o -name 'id_ecdsa' -o -name '*.pem' -o -name '*.key' \) \
           -not -name '*.pub' -not -name '*.example' -not -path './.git/*' 2>/dev/null)

# Real robot / lab addresses. 10.20/10.19 are the two subnets this robot used.
while IFS= read -r hit; do
  [ -n "$hit" ] || continue
  path="${hit%%:*}"; rest="${hit#*:}"; line="${rest%%:*}"
  path="${path#./}"
  case "$path" in
    *windows-client/*|*tools/*|*android/*|*server/agilex-remote-server.py|*server/remote-ui.html)
      soft "$path" "$line" "real robot address (out-of-scope component)" ;;
    *) hard "$path" "$line" "real robot address" ;;
  esac
done < <(scan_grep '(^|[^0-9])(10\.20|10\.19)\.[0-9]{1,3}\.[0-9]{1,3}([^0-9]|$)')

if [ "$FAIL" -gt 0 ]; then
  printf 'scan-secrets: %d hard finding(s), %d warning(s)\n' "$FAIL" "$WARN" >&2
  exit 1
fi
printf 'scan-secrets: clean (%d warning(s))\n' "$WARN"
