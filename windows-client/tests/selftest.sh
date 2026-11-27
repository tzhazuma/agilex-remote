#!/usr/bin/env bash
# selftest.sh - offline checks for the Windows client.
#
# It does NOT contact the robot. It starts a local Python HTTP server that
# serves a fixed /api/status fixture, then exercises:
#   - bash syntax and PowerShell parsing
#   - status / check / --strict
#   - duplicate-start reuse (no SSH is started)
#   - stop refusing to kill an unknown listener
#   - Chinese/space-containing state paths
#
# Requires: bash, curl, python 3. Optional: powershell.exe, cmd.exe.
set -uo pipefail

SELF="${BASH_SOURCE[0]}"
case "$SELF" in
  [A-Za-z]:\\*|[A-Za-z]:/*)
    if command -v cygpath >/dev/null 2>&1; then
      SELF="$(cygpath -u "$SELF" 2>/dev/null || printf '%s' "$SELF")"
    fi
    ;;
esac
HERE="$(cd -- "$(dirname -- "$SELF")" && pwd -P)"
CLIENT_DIR="$(cd -- "$HERE/.." && pwd -P)"
SCRIPT="$CLIENT_DIR/agilex-remote-win.sh"
PS1="$CLIENT_DIR/agilex-remote-win.ps1"
CMD="$CLIENT_DIR/agilex-remote.cmd"
FIXTURE="$HERE/fixtures/status.json"

PASS=0
FAIL=0
HTTP_PID=""
TMP=""

pass() { printf '  PASS  %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL + 1)); }

cleanup() {
  if [ -n "${HTTP_PID:-}" ]; then
    kill "$HTTP_PID" 2>/dev/null || true
    local i=0
    while [ "$i" -lt 20 ] && kill -0 "$HTTP_PID" 2>/dev/null; do
      sleep 0.2
      i=$((i + 1))
    done
    kill -9 "$HTTP_PID" 2>/dev/null || true
  fi
  [ -n "${TMP:-}" ] && rm -rf "$TMP" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

find_python() {
  local p
  for p in "${AGILEX_PYTHON:-}" python py python3; do
    [ -n "$p" ] || continue
    if command -v "$p" >/dev/null 2>&1 &&
       "$p" -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' >/dev/null 2>&1; then
      printf '%s\n' "$p"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)" || {
  echo "ERROR: python 3 is required for the offline selftest" >&2
  exit 2
}
command -v curl >/dev/null 2>&1 || {
  echo "ERROR: curl is required for the offline selftest" >&2
  exit 2
}

new_port() {
  "$PY" -c 'import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()'
}

to_windows_path() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$p" 2>/dev/null | tr -d '\r' && return 0
  fi
  if command -v wslpath >/dev/null 2>&1; then
    wslpath -w "$p" 2>/dev/null | tr -d '\r' && return 0
  fi
  return 1
}

echo "== syntax =="
if bash -n "$SCRIPT"; then pass "bash -n agilex-remote-win.sh"; else fail "bash -n agilex-remote-win.sh"; fi
if bash -n "$SELF"; then pass "bash -n selftest.sh"; else fail "bash -n selftest.sh"; fi

if command -v powershell.exe >/dev/null 2>&1 || command -v pwsh >/dev/null 2>&1; then
  # Parse the file CONTENT, not a host-translated path. The old ParseFile()
  # check depended on cygpath and failed under WSL-style /mnt/c paths even
  # though the script was valid. Windows PowerShell can also be forced into
  # ConstrainedLanguage by host policy, where the Parser .NET API is blocked;
  # that is an environment restriction, not a syntax error. Try the native
  # Windows PowerShell first, then pwsh, and only FAIL on a real parse error.
  PS_PARSED=0
  PS_ERRORS=""
  for PS_EXE in powershell.exe pwsh.exe pwsh; do
    command -v "$PS_EXE" >/dev/null 2>&1 || continue
    PS_PARSE_OUT="$("$PS_EXE" -NoProfile -Command \
      '$src=[Console]::In.ReadToEnd(); $e=$null; [System.Management.Automation.Language.Parser]::ParseInput($src,[ref]$null,[ref]$e) | Out-Null; if($e.Count){$e | ForEach-Object { $_.Message }; exit 1}' \
      < "$PS1" 2>&1)"
    PS_PARSE_RC=$?
    if [ "$PS_PARSE_RC" -eq 0 ]; then
      pass "PowerShell parser accepts agilex-remote-win.ps1 ($PS_EXE)"
      PS_PARSED=1
      break
    fi
    case "$PS_PARSE_OUT" in
      *ConstrainedLanguage*|*CannotCreateType*|*MethodInvocationNotSupported*)
        echo "  NOTE  $PS_EXE is in ConstrainedLanguage; trying the next parser" >&2
        continue
        ;;
      *)
        PS_ERRORS="$PS_PARSE_OUT"
        break
        ;;
    esac
  done
  if [ "$PS_PARSED" -eq 0 ]; then
    if [ -n "$PS_ERRORS" ]; then
      fail "PowerShell parser rejects agilex-remote-win.ps1"
      printf '%s\n' "$PS_ERRORS" >&2
    else
      echo "  SKIP  no PowerShell parser available outside ConstrainedLanguage"
    fi
  fi
else
  echo "  SKIP  powershell.exe/pwsh not found"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/agilex-win-selftest.XXXXXX")" || {
  echo "ERROR: mktemp failed" >&2
  exit 2
}
STATE="$TMP/状态 目录"
FIXTURE_ROOT="$TMP/fixture"
mkdir -p "$FIXTURE_ROOT/api" "$STATE"
cp "$FIXTURE" "$FIXTURE_ROOT/api/status"

PORT="$(new_port)" || {
  echo "ERROR: could not allocate a local test port" >&2
  exit 2
}

"$PY" -m http.server "$PORT" --bind 127.0.0.1 --directory "$FIXTURE_ROOT" \
  > "$TMP/http.log" 2>&1 &
HTTP_PID=$!

for _ in $(seq 1 50); do
  if curl --noproxy '*' -fsS -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if curl --noproxy '*' -fsS -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  pass "local fixture server on 127.0.0.1:$PORT"
else
  fail "local fixture server did not start"
  echo "--- http.log ---" >&2
  cat "$TMP/http.log" >&2 2>/dev/null || true
  exit 1
fi

export AGILEX_STATE_DIR="$STATE"
export AGILEX_LOCAL_PORT="$PORT"
export AGILEX_STATUS_URL="http://127.0.0.1:$PORT/api/status"
export AGILEX_HOST="127.0.0.1"
export AGILEX_SSH_BIN="/bin/false"   # any accidental tunnel fails immediately

echo
echo "== status =="
OUT="$TMP/status.out"
if "$SCRIPT" status --no-browser > "$OUT" 2>&1; then
  pass "status exits 0"
else
  fail "status exits nonzero"
fi
if grep -q "HTTP 200" "$OUT"; then pass "status sees HTTP 200"; else fail "status did not report HTTP 200"; fi
if grep -q "arm left" "$OUT" && grep -q "arm right" "$OUT"; then
  pass "status prints both arms"
else
  fail "status did not print both arms"
fi

echo
echo "== check =="
OUT="$TMP/check.out"
if "$SCRIPT" check > "$OUT" 2>&1; then
  pass "check exits 0"
else
  fail "check exits nonzero"
fi
if grep -q "READ-ONLY CHECK OK" "$OUT"; then
  pass "check reports READ-ONLY CHECK OK"
else
  fail "check did not report success"
fi
if "$SCRIPT" check --strict > "$TMP/check-strict.out" 2>&1; then
  pass "check --strict exits 0 for connected fixture"
else
  fail "check --strict exits nonzero for connected fixture"
fi

echo
echo "== duplicate start =="
OUT="$TMP/start.out"
if "$SCRIPT" start --no-browser > "$OUT" 2>&1; then
  pass "start exits 0 when a healthy controller already listens"
else
  fail "start exits nonzero when a healthy controller already listens"
fi
if grep -q "reusing" "$OUT"; then
  pass "start reuses the existing listener instead of opening SSH"
else
  fail "start did not report reuse"
fi
if grep -q "tunnel started" "$OUT"; then
  fail "start mistakenly opened a new SSH tunnel"
else
  pass "start did not open a new SSH tunnel"
fi

echo
echo "== stop does not kill an unknown listener =="
OUT="$TMP/stop.out"
if "$SCRIPT" stop > "$OUT" 2>&1; then
  # It is acceptable to return 0 when there was nothing of ours to stop, but
  # the fixture listener must survive either way.
  :
fi
if curl --noproxy '*' -fsS -m 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  pass "fixture listener survived stop"
else
  fail "stop killed a listener it did not start"
fi
if grep -q "not this tool's tunnel" "$OUT" 2>/dev/null; then
  pass "stop identified the listener as not ours"
else
  fail "stop did not explain that the listener was not ours"
fi

echo
echo "== Chinese/space state path =="
OUT="$TMP/chinese.out"
if AGILEX_STATE_DIR="$TMP/中文 状态 2" "$SCRIPT" status --no-browser > "$OUT" 2>&1; then
  pass "status works with a Chinese/space state path"
else
  fail "status failed with a Chinese/space state path"
fi
if grep -q "中文 状态 2" "$OUT"; then
  pass "state path is printed literally"
else
  fail "state path was not preserved"
fi

echo
echo "== argument validation =="
if "$SCRIPT" --local-port 99999 status >/dev/null 2>&1; then
  fail "invalid port 99999 was accepted"
else
  pass "invalid port 99999 is rejected"
fi
if "$SCRIPT" --definitely-not-an-option status >/dev/null 2>&1; then
  fail "unknown option was accepted"
else
  pass "unknown option is rejected"
fi

echo
echo "== host key handling (fake ssh-keyscan, offline) =="
if command -v ssh-keygen >/dev/null 2>&1; then
  HK="$TMP/hostkey"
  mkdir -p "$HK"
  ssh-keygen -q -t ed25519 -N '' -f "$HK/robot-key" >/dev/null 2>&1
  ssh-keygen -q -t ed25519 -N '' -f "$HK/other-key" >/dev/null 2>&1
  cat > "$HK/fake-keyscan" <<'FAKE'
#!/usr/bin/env bash
printf '127.0.0.1 %s\n' "$(cat "$AGILEX_FAKE_PUB")"
FAKE
  chmod +x "$HK/fake-keyscan"
  export AGILEX_FAKE_PUB="$HK/robot-key.pub"

  OUT="$TMP/hk-accept.out"
  AGILEX_SSH_BIN="/bin/false" AGILEX_SSH_KEYSCAN_BIN="$HK/fake-keyscan" \
    "$SCRIPT" start --host 127.0.0.1 --accept-host-key \
    --local-port "$(new_port)" --state-dir "$HK/accept" --no-browser > "$OUT" 2>&1
  HK_RC=$?
  if [ "$HK_RC" -eq 4 ]; then
    pass "first host key accepted, then the deliberately broken tunnel exits 4"
  else
    fail "unexpected exit code for first host key test: $HK_RC"
  fi
  if grep -q '^agilex-robot ssh-' "$HK/accept/known_hosts" 2>/dev/null; then
    pass "known_hosts contains agilex-robot keys"
  else
    fail "known_hosts does not contain agilex-robot keys"
  fi

  FP="$(ssh-keygen -E sha256 -lf "$HK/robot-key.pub" 2>/dev/null | awk '{print $2; exit}')"
  OUT="$TMP/hk-pin.out"
  AGILEX_SSH_BIN="/bin/false" AGILEX_SSH_KEYSCAN_BIN="$HK/fake-keyscan" \
    "$SCRIPT" start --host 127.0.0.1 --fingerprint "$FP" \
    --local-port "$(new_port)" --state-dir "$HK/pin" --no-browser > "$OUT" 2>&1
  HK_RC=$?
  if [ "$HK_RC" -eq 4 ] && grep -q 'pinned fingerprint' "$OUT"; then
    pass "pinned fingerprint is accepted without an interactive prompt"
  else
    fail "pinned fingerprint path did not behave as expected (rc=$HK_RC)"
  fi

  mkdir -p "$HK/mismatch"
  printf 'agilex-robot %s\n' "$(awk '{print $1 " " $2}' "$HK/other-key.pub")" > "$HK/mismatch/known_hosts"
  BEFORE_HASH="$(sha256sum "$HK/mismatch/known_hosts" | awk '{print $1}')"
  OUT="$TMP/hk-mismatch.out"
  AGILEX_SSH_BIN="/bin/false" AGILEX_SSH_KEYSCAN_BIN="$HK/fake-keyscan" \
    "$SCRIPT" start --host 127.0.0.1 --accept-host-key \
    --local-port "$(new_port)" --state-dir "$HK/mismatch" --no-browser > "$OUT" 2>&1
  HK_RC=$?
  AFTER_HASH="$(sha256sum "$HK/mismatch/known_hosts" | awk '{print $1}')"
  if [ "$HK_RC" -ne 0 ] && [ "$BEFORE_HASH" = "$AFTER_HASH" ] &&
     grep -q 'CHANGED' "$OUT"; then
    pass "a changed host key is refused and the trusted file is unchanged"
  else
    fail "changed-host-key protection failed (rc=$HK_RC, unchanged=$([ "$BEFORE_HASH" = "$AFTER_HASH" ] && echo yes || echo no))"
  fi
else
  echo "  SKIP  ssh-keygen not found"
fi

echo
echo "== launchers =="
if "$SCRIPT" --help 2>&1 | grep -q "agilex-remote-win.sh \[start\]"; then
  pass "--help prints usage"
else
  fail "--help does not print usage"
fi
WIN_CMD="$(to_windows_path "$CMD" || true)"
if command -v cmd.exe >/dev/null 2>&1 && [ -n "$WIN_CMD" ]; then
  if cmd.exe //c "$WIN_CMD" --help >/dev/null 2>&1; then
    pass "agilex-remote.cmd forwards --help"
  else
    fail "agilex-remote.cmd failed to forward --help"
  fi
else
  echo "  SKIP  cmd.exe or Windows path conversion unavailable"
fi

echo
echo "=============================================="
printf 'PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo "SELFTEST OK (offline; no robot was contacted)"
  exit 0
fi
echo "SELFTEST FAILED"
exit 1
