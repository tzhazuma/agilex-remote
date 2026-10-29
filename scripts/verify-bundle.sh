#!/usr/bin/env bash
# verify-bundle.sh — prove the shipped tree works, from a CLEAN extraction.
#
# This exists because a stale copy of tools/test-modes-offline.py was once
# copied over the fixed one, silently dropping the regression tests for the
# 2026-10-29 shaking bug. The bundle still "built" and still shipped. Running
# the shipped tests from a throwaway directory is the cheapest way to catch
# that class of mistake, so it is a script rather than a habit.
#
#   ./scripts/verify-bundle.sh              # verify the current tree
#   ./scripts/verify-bundle.sh foo.zip      # verify a built zip
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="${PYTHON:-${PY:-python3}}"
FAIL=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }

WORK="$(mktemp -d -t agilex-verify)"
trap 'rm -rf "$WORK"' EXIT

if [ $# -ge 1 ]; then
  echo "== extracting $1 =="
  unzip -q "$1" -d "$WORK"
else
  echo "== copying the working tree =="
  ( cd "$ROOT" && tar -cf - --exclude=.git --exclude='__pycache__' --exclude='*.pyc' . ) \
    | ( cd "$WORK" && tar -xf - )
fi

echo
echo "== shipped tree =="
[ -f "$WORK/server/agilex-remote-server.py" ] && ok "server present" || bad "server missing"
[ -f "$WORK/server/remote-ui.html" ]          && ok "UI present"     || bad "UI missing"
[ -f "$WORK/install.sh" ]                     && ok "install.sh present" || bad "install.sh missing"
[ -x "$WORK/install.sh" ]                     && ok "install.sh executable" || bad "install.sh not executable"

echo
echo "== python =="
echo "  interpreter: $("$PY" -c 'import sys; print(sys.executable, sys.version.split()[0])' 2>/dev/null || echo "$PY")"
if "$PY" -m py_compile "$WORK/server/agilex-remote-server.py" 2>/dev/null; then
  ok "server compiles"
else
  bad "server does not compile"
fi

echo
echo "== shipped unit tests (this is the part that catches stale files) =="
if out="$("$PY" "$WORK/tools/test-modes-offline.py" 2>&1)"; then
  n="$(printf '%s' "$out" | grep -c '^PASS' || true)"
  ok "offline tests: $n assertions passed"
  if [ "${n:-0}" -lt 40 ]; then
    bad "only $n assertions — the regression tests are probably missing"
  fi
else
  bad "offline tests failed"
  printf '%s\n' "$out" | tail -15
fi

echo
echo "== shell syntax =="
for f in "$WORK/install.sh" "$WORK"/scripts/*.sh; do
  [ -f "$f" ] || continue
  if bash -n "$f" 2>/dev/null; then ok "$(basename "$f")"; else bad "$(basename "$f")"; fi
done

echo
echo "== secrets =="
# The Android application id is intentionally retained and disclosed in the
# README; everything else must be absent.
hits="$(grep -rIn 'azuma\|tangzh\|tzhazuma' "$WORK" 2>/dev/null \
        | grep -v 'com\.azuma\.agilexremote' \
        | grep -v 'com/azuma/agilexremote' \
        | grep -v 'verify-bundle\.sh' \
        | grep -v 'package id is' || true)"
if [ -z "$hits" ]; then ok "no credentials or usernames"; else bad "secret-like strings:"; printf '%s\n' "$hits" | head -5; fi
ip="$(grep -rIn '10\.20\.[0-9]\{1,3\}\.[0-9]\{1,3\}\|10\.19\.[0-9]\{1,3\}\.[0-9]\{1,3\}' "$WORK" 2>/dev/null || true)"
if [ -z "$ip" ]; then ok "no real robot/GPU addresses"; else bad "addresses found:"; printf '%s\n' "$ip" | head -5; fi
if grep -q 'AGILEX_PASSWORD", "changeme"' "$WORK/server/agilex-remote-server.py"; then
  ok "password default is the placeholder"
else
  bad "password default is not 'changeme'"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "BUNDLE VERIFIED"
else
  echo "$FAIL CHECK(S) FAILED"
  exit 1
fi
