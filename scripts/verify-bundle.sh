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
#
# Python: uses $PYTHON or $PY if set, otherwise the first of python3 / python /
# "py -3" that actually runs. A present-but-broken python3 (the Windows Store
# stub exits without output) is skipped instead of aborting the check.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
FAIL=0
PY_CMD=()

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 2; }

find_python() {
  local cand
  if [ -n "${PYTHON:-}" ]; then
    "$PYTHON" -c 'import sys' >/dev/null 2>&1 || die "PYTHON=$PYTHON does not run"
    PY_CMD=("$PYTHON"); return
  fi
  if [ -n "${PY:-}" ]; then
    "$PY" -c 'import sys' >/dev/null 2>&1 || die "PY=$PY does not run"
    PY_CMD=("$PY"); return
  fi
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys' >/dev/null 2>&1; then
      PY_CMD=("$cand"); return
    fi
  done
  if command -v py >/dev/null 2>&1 && py -3 -c 'import sys' >/dev/null 2>&1; then
    PY_CMD=(py -3); return
  fi
  die "no working python interpreter (tried python3, python, py -3)"
}
find_python
run_py() { "${PY_CMD[@]}" "$@"; }

# GNU and BSD mktemp disagree about -t. A template ending in X's works on both.
WORK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/agilex-verify.XXXXXX")" || die "cannot create a temporary directory"
WORK="$WORK_ROOT"
trap 'rm -rf "$WORK_ROOT"' EXIT HUP INT TERM

if [ $# -ge 1 ]; then
  echo "== extracting $1 =="
  command -v unzip >/dev/null || die "unzip not found"
  unzip -q "$1" -d "$WORK_ROOT"
else
  echo "== copying the working tree =="
  ( cd "$ROOT" && tar -cf - --exclude=.git --exclude='__pycache__' --exclude='*.pyc' . ) \
    | ( cd "$WORK_ROOT" && tar -xf - )
fi

# A delivery zip usually has one top-level directory (02-代码/, agilex-remote/).
# Descend into it when it is unambiguous, so both layouts verify.
if [ ! -f "$WORK/server/agilex-remote-server.py" ]; then
  candidate=""
  for d in "$WORK_ROOT"/*/; do
    [ -d "$d" ] || continue
    if [ -f "$d/server/agilex-remote-server.py" ]; then
      [ -z "$candidate" ] || { candidate="__multiple__"; break; }
      candidate="${d%/}"
    fi
  done
  if [ -n "$candidate" ] && [ "$candidate" != "__multiple__" ]; then
    WORK="$candidate"
  fi
fi

echo
echo "== shipped tree =="
for f in server/agilex-remote-server.py server/remote-ui.html server/install-server.sh \
         install.sh scripts/rsudo.sh scripts/agilex-lib.sh scripts/scan-secrets.sh \
         scripts/verify-bundle.sh scripts/test-deploy-offline.sh; do
  if [ -f "$WORK/$f" ]; then ok "$f present"; else bad "$f missing"; fi
done
[ -x "$WORK/install.sh" ] && ok "install.sh executable" || bad "install.sh not executable"
[ -x "$WORK/server/install-server.sh" ] && ok "install-server.sh executable" || bad "install-server.sh not executable"
[ -x "$WORK/scripts/rsudo.sh" ] && ok "rsudo.sh executable" || bad "rsudo.sh not executable"

echo
echo "== python =="
echo "  interpreter: $(run_py -c 'import sys; print(sys.executable, sys.version.split()[0])' 2>/dev/null || echo "${PY_CMD[*]}")"
if run_py -m py_compile "$WORK/server/agilex-remote-server.py" 2>/dev/null; then
  ok "server compiles"
else
  bad "server does not compile"
fi

echo
echo "== shipped unit tests (this is the part that catches stale files) =="
if out="$(run_py "$WORK/tools/test-modes-offline.py" 2>&1)"; then
  n="$(printf '%s' "$out" | grep -c '^PASS' || true)"
  ok "offline mode tests: $n assertions passed"
  if [ "${n:-0}" -lt 40 ]; then
    bad "only $n assertions — the regression tests are probably missing"
  fi
else
  bad "offline mode tests failed"
  printf '%s\n' "$out" | tail -15
fi

echo
echo "== deployment / credential tests =="
if out="$(AGILEX_IN_BUNDLE=1 PYTHON="${PY_CMD[0]}" bash "$WORK/scripts/test-deploy-offline.sh" 2>&1)"; then
  ok "deployment tests passed"
else
  bad "deployment tests failed"
  printf '%s\n' "$out" | tail -25
fi

echo
echo "== shell syntax =="
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if bash -n "$f" 2>/dev/null; then ok "${f#"$WORK"/}"; else bad "${f#"$WORK"/}"; fi
done < <(find "$WORK" -name '*.sh' -type f -not -path '*/.git/*' | sort)

echo
echo "== credentials and addresses =="
if [ -f "$WORK/scripts/scan-secrets.sh" ]; then
  if out="$(bash "$WORK/scripts/scan-secrets.sh" --root "$WORK" 2>&1)"; then
    ok "no hard-coded credentials or real addresses"
    printf '%s\n' "$out" | grep '^WARN' | sed 's/^/  /' || true
  else
    bad "credential/address scan failed"
    printf '%s\n' "$out" | grep -E '^(FAIL|scan-secrets)' | head -10
  fi
else
  bad "scan-secrets.sh missing"
fi

echo
echo "== installer consistency =="
if grep -q 'EnvironmentFile=-' "$WORK/server/install-server.sh" 2>/dev/null; then
  bad "install-server.sh still has an optional EnvironmentFile"
else
  ok "EnvironmentFile is mandatory"
fi
if grep -q 'AGILEX_ALLOW_ANON=0' "$WORK/server/install-server.sh"; then
  ok "install-server.sh pins AGILEX_ALLOW_ANON=0"
else
  bad "install-server.sh does not pin AGILEX_ALLOW_ANON=0"
fi
if grep -q 'server/install-server.sh' "$WORK/install.sh"; then
  ok "install.sh delegates to install-server.sh (one unit definition)"
else
  bad "install.sh duplicates the robot-side install"
fi
if grep -q 'AGILEX_PASSWORD", "changeme"' "$WORK/server/agilex-remote-server.py"; then
  warn "server code still has the built-in 'changeme' default (out of scope); installers override it and fail closed"
else
  ok "server code has no built-in password default"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  echo "BUNDLE VERIFIED"
else
  echo "$FAIL CHECK(S) FAILED"
  exit 1
fi
