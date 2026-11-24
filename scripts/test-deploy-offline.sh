#!/usr/bin/env bash
# test-deploy-offline.sh — regression tests for deployment and credential safety.
#
# Runs with no robot, no root, no network and no sudo. It is deliberately
# paranoid: every check either reproduces a bug that shipped once or guards a
# security property that is easy to lose in a later edit.
#
#   ./scripts/test-deploy-offline.sh
#   AGILEX_IN_BUNDLE=1 ./scripts/test-deploy-offline.sh   # skip self-tests that
#                                                        # invoke verify-bundle
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
FAIL=0
PASS=0

pass() { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
note() { printf '  \033[33mNOTE\033[0m %s\n' "$*"; }

# Use a template with X's on the end: GNU and BSD mktemp disagree about -t.
TMP="$(mktemp -d "${TMPDIR:-/tmp}/agilex-deploy-test.XXXXXX")" || {
  echo "cannot create a temporary directory" >&2
  exit 2
}
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT HUP INT TERM

PYBIN="${PYTHON:-}"
if [ -z "$PYBIN" ]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys' >/dev/null 2>&1; then
      PYBIN="$c"
      break
    fi
  done
fi
if [ -z "$PYBIN" ]; then
  if command -v py >/dev/null 2>&1 && py -3 -c 'import sys' >/dev/null 2>&1; then
    PYBIN=py
  fi
fi
[ -n "$PYBIN" ] || { echo "no working python interpreter found" >&2; exit 2; }

mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null; }
is_windows() { case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac; return 1; }
expect_mode_600() {
  local name="$1" path="$2"
  if is_windows; then
    note "$name: mode check skipped on Windows (NTFS ACLs do not expose POSIX 0600)"
  elif [ "$(mode_of "$path")" = "600" ]; then
    pass "$name is mode 0600"
  else
    fail "$name mode is $(mode_of "$path"), expected 600"
  fi
}

copy_tree() {
  local dest="$1" src="$2"
  mkdir -p "$dest"
  ( cd "$src" && tar -cf - --exclude=.git --exclude=__pycache__ --exclude='*.pyc' . ) \
    | ( cd "$dest" && tar -xf - )
}

echo
echo "== required helper scripts =="
for f in agilex-lib.sh agilex-lease.sh rsudo.sh scan-secrets.sh verify-bundle.sh test-deploy-offline.sh; do
  if [ -f "$HERE/$f" ]; then pass "scripts/$f exists"; else fail "scripts/$f missing"; fi
done
if [ -f "$ROOT/server/install-server.sh" ]; then pass "server/install-server.sh exists"; else fail "server/install-server.sh missing"; fi

echo
echo "== shell syntax =="
while IFS= read -r f; do
  if bash -n "$f" 2>/dev/null; then
    pass "bash -n ${f#"$ROOT"/}"
  else
    fail "bash -n ${f#"$ROOT"/}"
  fi
done < <(find "$ROOT" -name '*.sh' -type f -not -path '*/.git/*' | sort)

echo
echo "== credential scanner: positive and negative fixtures =="
SCAN="$HERE/scan-secrets.sh"
if [ ! -x "$SCAN" ]; then
  fail "scan-secrets.sh is missing or not executable"
else
  mkdir -p "$TMP/scan/assign" "$TMP/scan/topic" "$TMP/scan/key" "$TMP/scan/ip" "$TMP/scan/clean"
  # Build the fixture value at run time so this test file does not itself
  # contain a credential-shaped assignment.
  printf 'AGILEX_%s=%s\n' PW 'hunter2' > "$TMP/scan/assign/lib.sh"
  printf 'https://%s/%s\n' 'ntfy.sh' 'sampletopic123' > "$TMP/scan/topic/readme.md"
  printf -- '-----BEGIN %s PRIVATE KEY-----\n' OPENSSH > "$TMP/scan/key/id_test"
  printf 'host=%s.%s.%s.%s\n' 10 20 98 212 > "$TMP/scan/ip/doc.md"
  printf 'AGILEX_PASSWORD="${AGILEX_PASSWORD:-}"\n' > "$TMP/scan/clean/lib.sh"
  printf '%s\n' 'YOUR-NTFY-TOPIC-HERE' > "$TMP/scan/clean/topic.txt"

  out="$("$SCAN" --root "$TMP/scan/assign" 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && pass "scanner rejects a literal password assignment" \
    || fail "scanner missed a literal password assignment"
  out="$("$SCAN" --root "$TMP/scan/topic" 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && pass "scanner rejects a real-looking ntfy topic" \
    || fail "scanner missed a real-looking ntfy topic"
  out="$("$SCAN" --root "$TMP/scan/key" 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && pass "scanner rejects a private key block" \
    || fail "scanner missed a private key block"
  out="$("$SCAN" --root "$TMP/scan/ip" 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && pass "scanner rejects a real robot address" \
    || fail "scanner missed a real robot address"
  out="$("$SCAN" --root "$TMP/scan/clean" 2>&1)"; rc=$?
  [ "$rc" -eq 0 ] && pass "scanner accepts env-var and placeholder values" \
    || { fail "scanner flagged a clean fixture"; printf '%s\n' "$out" | head -5; }

  out="$("$SCAN" --root "$ROOT" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    pass "shipped tree has no hard-coded credentials"
  else
    fail "shipped tree still contains credentials"
    printf '%s\n' "$out" | grep '^FAIL' | head -8
  fi
  printf '%s\n' "$out" | grep '^WARN' | head -5
fi

echo
echo "== installer: password file is fail-closed and consistent =="
INST="$ROOT/install.sh"
SRV="$ROOT/server/install-server.sh"

if grep -q 'EnvironmentFile=-' "$INST" "$SRV" 2>/dev/null; then
  fail "an installer still marks EnvironmentFile optional (EnvironmentFile=-)"
else
  pass "EnvironmentFile is mandatory in both installers"
fi
if grep -q 'AGILEX_ALLOW_ANON=0' "$SRV"; then
  pass "install-server.sh pins AGILEX_ALLOW_ANON=0"
else
  fail "install-server.sh does not pin AGILEX_ALLOW_ANON=0"
fi
if grep -q 'server/install-server.sh' "$INST"; then
  pass "install.sh delegates the robot-side install to install-server.sh"
else
  fail "install.sh duplicates the robot-side install (unit drift risk)"
fi

if [ -x "$SRV" ] || [ -f "$SRV" ]; then
  ENVDIR="$TMP/env"
  mkdir -p "$ENVDIR"
  ENVF="$ENVDIR/agilex-remote.env"
  GOODPW='TestPassw0rd!'
  out="$(AGILEX_ENV_FILE="$ENVF" AGILEX_PASSWORD="$GOODPW" bash "$SRV" --env-only 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && [ -f "$ENVF" ]; then
    pass "install-server.sh --env-only writes the environment file"
    expect_mode_600 "environment file" "$ENVF"
    grep -qx "AGILEX_PASSWORD=$GOODPW" "$ENVF" && pass "environment file contains the supplied password" \
      || fail "environment file does not contain the supplied password"
    case "$out" in
      *"$GOODPW"*) fail "install-server.sh echoed the password" ;;
      *) pass "install-server.sh never echoes the password" ;;
    esac
  else
    fail "install-server.sh --env-only did not create the file (rc=$rc)"
    printf '%s\n' "$out" | tail -5
  fi

  ENVF2="$ENVDIR/no-password.env"
  out="$(AGILEX_ENV_FILE="$ENVF2" env -u AGILEX_PASSWORD bash "$SRV" --env-only </dev/null 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ] && [ ! -e "$ENVF2" ]; then
    pass "install-server.sh refuses to install without a password"
  else
    fail "install-server.sh installed with no password (rc=$rc)"
  fi

  ENVF3="$ENVDIR/changeme.env"
  BADPW='changeme'
  out="$(AGILEX_ENV_FILE="$ENVF3" AGILEX_PASSWORD="$BADPW" bash "$SRV" --env-only </dev/null 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && pass "install-server.sh rejects the old 'changeme' default" \
    || fail "install-server.sh accepted 'changeme'"

  ENVF4="$ENVDIR/short.env"
  SHORTPW='short'
  out="$(AGILEX_ENV_FILE="$ENVF4" AGILEX_PASSWORD="$SHORTPW" bash "$SRV" --env-only </dev/null 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && pass "install-server.sh rejects a too-short password" \
    || fail "install-server.sh accepted a too-short password"

  ENVF5="$ENVDIR/file.env"
  FILEPW='FilePassw0rd!'
  printf '%s\n' "$FILEPW" > "$ENVDIR/pw.txt"
  out="$(AGILEX_ENV_FILE="$ENVF5" AGILEX_PASSWORD_FILE="$ENVDIR/pw.txt" env -u AGILEX_PASSWORD bash "$SRV" --env-only </dev/null 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && grep -qx "AGILEX_PASSWORD=$FILEPW" "$ENVF5"; then
    pass "install-server.sh supports AGILEX_PASSWORD_FILE for automation"
  else
    fail "install-server.sh did not read AGILEX_PASSWORD_FILE (rc=$rc)"
  fi

  ENVF6="$ENVDIR/weak.env"
  WEAKPW='short'
  printf 'AGILEX_PASSWORD=%s\n' "$WEAKPW" > "$ENVF6"
  out="$(AGILEX_ENV_FILE="$ENVF6" env -u AGILEX_PASSWORD bash "$SRV" --env-only </dev/null 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ] && grep -qx "AGILEX_PASSWORD=$WEAKPW" "$ENVF6"; then
    pass "install-server.sh refuses a pre-existing weak env file"
  else
    fail "install-server.sh kept a pre-existing weak password (rc=$rc)"
  fi

  ENVF7="$ENVDIR/keep.env"
  KEEPW='KeepPassw0rd!'
  printf 'AGILEX_PASSWORD=%s\n' "$KEEPW" > "$ENVF7"
  out="$(AGILEX_ENV_FILE="$ENVF7" env -u AGILEX_PASSWORD bash "$SRV" --env-only </dev/null 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && grep -qx "AGILEX_PASSWORD=$KEEPW" "$ENVF7"; then
    pass "install-server.sh keeps an existing strong password"
  else
    fail "install-server.sh did not preserve a strong existing password (rc=$rc)"
  fi

  NEWW='RotatedPassw0rd!'
  out="$(AGILEX_ENV_FILE="$ENVF7" AGILEX_PASSWORD="$NEWW" bash "$SRV" --env-only </dev/null 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && grep -qx "AGILEX_PASSWORD=$NEWW" "$ENVF7"; then
    pass "install-server.sh rotates the password when one is supplied"
  else
    fail "install-server.sh did not rotate the existing password (rc=$rc)"
  fi
fi

echo
echo "== local helper installer =="
HELPERS="$HERE/install-local-helpers.sh"
if [ -f "$HELPERS" ]; then
  H="$TMP/home-plain"
  mkdir -p "$H"
  out="$(HOME="$H" bash "$HELPERS" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && [ -x "$H/.local/bin/agilex-proxy.sh" ]; then
    pass "helper installs without an ntfy topic"
  else
    fail "helper install fails when no ntfy topic is configured (rc=$rc)"
    printf '%s\n' "$out" | tail -5
  fi
  [ ! -e "$H/.config/agilex/ntfy-topic" ] && pass "no topic file is invented" \
    || fail "helper invented an ntfy topic file"

  H2="$TMP/home-topic"
  mkdir -p "$H2"
  TOPICVAL='sampletopic123'
  printf '%s\n' "$TOPICVAL" > "$TMP/topic.txt"
  out="$(HOME="$H2" AGILEX_NTFY_TOPIC_FILE="$TMP/topic.txt" bash "$HELPERS" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && [ -f "$H2/.config/agilex/ntfy-topic" ]; then
    pass "helper installs an explicitly supplied topic file"
    expect_mode_600 "installed topic file" "$H2/.config/agilex/ntfy-topic"
  else
    fail "helper did not install the supplied topic file (rc=$rc)"
  fi
  case "$out" in
    *"$TOPICVAL"*) fail "helper printed the ntfy topic" ;;
    *) pass "helper does not print the ntfy topic" ;;
  esac
else
  fail "install-local-helpers.sh missing"
fi

echo
echo "== verify-bundle portability and detection =="
VB="$HERE/verify-bundle.sh"
if [ ! -f "$VB" ]; then
  fail "verify-bundle.sh missing"
else
  if grep -Eq 'mktemp[[:space:]]+-t|mktemp[[:space:]]+-d[[:space:]]+-t' "$VB" "$INST"; then
    fail "install scripts still use non-portable mktemp -t"
  else
    pass "install scripts use a GNU/BSD portable mktemp template"
  fi

  if [ "${AGILEX_IN_BUNDLE:-0}" = "1" ]; then
    note "skipping verify-bundle self-tests while running inside verify-bundle"
  else
    out="$(AGILEX_VERIFY_SKIP_SUITES=1 PYTHON="$PYBIN" bash "$VB" 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
      pass "verify-bundle passes with a working interpreter"
    else
      fail "verify-bundle failed (rc=$rc)"
      printf '%s\n' "$out" | tail -12
    fi

    # Simulate the Windows Store python3 stub: present on PATH, exits 49.
    mkdir -p "$TMP/fakebin"
    cat > "$TMP/fakebin/python3" <<'EOF'
#!/usr/bin/env bash
exit 49
EOF
    chmod +x "$TMP/fakebin/python3"
    out="$(env -u PYTHON -u PY AGILEX_VERIFY_SKIP_SUITES=1 PATH="$TMP/fakebin:$PATH" bash "$VB" 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
      pass "verify-bundle falls back when python3 exists but does not run"
    else
      fail "verify-bundle did not fall back to python (rc=$rc)"
      printf '%s\n' "$out" | tail -12
    fi

    copy_tree "$TMP/tree-secret" "$ROOT"
    printf 'AGILEX_%s=%s\n' PW 'hunter2' >> "$TMP/tree-secret/scripts/agilex-lib.sh"
    out="$(cd "$TMP/tree-secret" && AGILEX_VERIFY_SKIP_SUITES=1 PYTHON="$PYBIN" bash scripts/verify-bundle.sh 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ] && printf '%s\n' "$out" | grep -qiE 'secret|credential'; then
      pass "verify-bundle fails on a planted credential"
    else
      fail "verify-bundle did not fail on a planted credential (rc=$rc)"
    fi

    copy_tree "$TMP/tree-norsudo" "$ROOT"
    rm -f "$TMP/tree-norsudo/scripts/rsudo.sh"
    out="$(cd "$TMP/tree-norsudo" && AGILEX_VERIFY_SKIP_SUITES=1 PYTHON="$PYBIN" bash scripts/verify-bundle.sh 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ] && printf '%s\n' "$out" | grep -q 'rsudo'; then
      pass "verify-bundle flags a missing rsudo.sh"
    else
      fail "verify-bundle missed a missing rsudo.sh (rc=$rc)"
    fi
  fi
fi

echo
echo "== rsudo.sh: password never enters argv =="
RSUDO="$HERE/rsudo.sh"
if [ ! -x "$RSUDO" ]; then
  fail "rsudo.sh missing or not executable"
else
  mkdir -p "$TMP/fakebin"
  cat > "$TMP/fakebin/ssh-nopasswd" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_SSH_ARGV"
case "$*" in
  *"sudo -n true"*) exit 0 ;;
esac
cat > "$FAKE_SSH_STDIN"
exit 0
EOF
  cat > "$TMP/fakebin/ssh-password" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_SSH_ARGV"
case "$*" in
  *"sudo -n true"*) exit 1 ;;
esac
cat > "$FAKE_SSH_STDIN"
exit 0
EOF
  chmod +x "$TMP/fakebin/ssh-nopasswd" "$TMP/fakebin/ssh-password"

  : > "$TMP/argv1"; : > "$TMP/stdin1"
  out="$(FAKE_SSH_ARGV="$TMP/argv1" FAKE_SSH_STDIN="$TMP/stdin1" \
        AGILEX_SSH_BIN="$TMP/fakebin/ssh-nopasswd" AGILEX_HOST=robot.invalid \
        "$RSUDO" -c 'systemctl is-active agilex-remote' 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] && grep -q 'systemctl is-active agilex-remote' "$TMP/stdin1"; then
    pass "rsudo.sh runs commands over stdin"
  else
    fail "rsudo.sh did not run the command (rc=$rc)"
    printf '%s\n' "$out" | tail -5
  fi
  grep -q 'systemctl is-active agilex-remote' "$TMP/argv1" \
    && fail "rsudo.sh put the command in ssh argv" \
    || pass "rsudo.sh keeps the command out of ssh argv"

  : > "$TMP/argv2"; : > "$TMP/stdin2"
  FAKEPW='hunter2'
  out="$(FAKE_SSH_ARGV="$TMP/argv2" FAKE_SSH_STDIN="$TMP/stdin2" \
        AGILEX_SSH_BIN="$TMP/fakebin/ssh-password" AGILEX_HOST=robot.invalid \
        AGILEX_SUDO_PASSWORD="$FAKEPW" \
        "$RSUDO" -c 'true' 2>&1)"; rc=$?
  first="$(head -n 1 "$TMP/stdin2" 2>/dev/null)"
  if [ "$rc" -eq 0 ] && [ "$first" = "$FAKEPW" ]; then
    pass "rsudo.sh feeds the sudo password on stdin"
  else
    fail "rsudo.sh did not feed the sudo password on stdin (rc=$rc)"
  fi
  if grep -q "$FAKEPW" "$TMP/argv2"; then
    fail "rsudo.sh leaked the password into ssh argv"
  else
    pass "rsudo.sh keeps the password out of ssh argv"
  fi

  out="$(env -u AGILEX_SUDO_PASSWORD AGILEX_SSH_BIN="$TMP/fakebin/ssh-password" \
        AGILEX_HOST=robot.invalid "$RSUDO" -c 'true' </dev/null 2>&1)"; rc=$?
  [ "$rc" -ne 0 ] && pass "rsudo.sh fails closed without a password or TTY" \
    || fail "rsudo.sh proceeded without a password"
fi

echo
echo "== lease helper / run-smolvla client (fake API) =="
if [ -f "$HERE/test-lease-offline.py" ]; then
  out="$("$PYBIN" "$HERE/test-lease-offline.py" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    n="$(printf '%s' "$out" | grep -c '^PASS' || true)"
    if [ "${n:-0}" -ge 23 ]; then
      pass "lease fake-API tests: $n checks"
    else
      fail "only $n lease assertions (minimum 23)"
    fi
  else
    fail "lease fake-API tests failed (rc=$rc)"
    printf '%s\n' "$out" | tail -20
  fi
else
  fail "scripts/test-lease-offline.py missing"
fi
for f in verify-modes.sh mode-smoke.sh; do
  if grep -q 'agilex-lease.sh' "$HERE/$f" && grep -q 'lease_acquire' "$HERE/$f"; then
    pass "$f acquires a control lease"
  else
    fail "$f does not acquire a control lease"
  fi
done
if grep -q 'class LeaseSession' "$ROOT/tools/run-smolvla.py" \
   && grep -q 'LeaseLost' "$ROOT/tools/run-smolvla.py"; then
  pass "run-smolvla.py has a lease client with 409 handling"
else
  fail "run-smolvla.py has no lease client"
fi
if grep -q 'lease_clear_estop' "$HERE/agilex-lease.sh" \
   && grep -q 'confirm=CLEAR' "$HERE/agilex-lease.sh"; then
  pass "helper implements clear-with-lease-and-confirm"
else
  fail "helper does not implement clear-with-lease-and-confirm"
fi

echo
echo "== dangerous mode tests are labelled and gated =="
README="$ROOT/README.md"
[ -f "$README" ] || README="$ROOT/../01-使用指南/README.md"
if [ -f "$README" ]; then
  if grep -Eqi 'verify-modes\.sh.*no motion|mode-smoke\.sh.*no motion' "$README"; then
    fail "README still calls the on-robot mode tests 'no motion'"
  else
    pass "README no longer calls mode tests 'no motion'"
  fi
  if grep -q '1\.2 rad/s' "$README"; then
    fail "README still claims a 1.2 rad/s default"
  else
    pass "README no longer claims a 1.2 rad/s default"
  fi
  grep -q '0\.7853' "$README" && pass "README documents the server's 0.7853 rad/s ceiling" \
    || fail "README is missing the server's angular ceiling"
else
  note "README not present in this bundle; documentation checks skipped"
fi
for f in "$HERE/verify-modes.sh" "$HERE/mode-smoke.sh"; do
  if grep -q 'AGILEX_ALLOW_MODE_TESTS' "$f"; then
    pass "$(basename "$f") requires an explicit motion-test opt-in"
  else
    fail "$(basename "$f") can run without an explicit opt-in"
  fi
done
if grep -q 'AGILEX_ALLOW_MIT_TEST' "$HERE/verify-modes.sh"; then
  pass "verify-modes.sh gates the MIT (compliant) mode switch"
else
  fail "verify-modes.sh switches to MIT without a separate opt-in"
fi

echo
echo "== speed limits are consistent with the server =="
if grep -q '0\.08 m/s' "$HERE/robot-remote.sh"; then
  fail "robot-remote.sh still claims the obsolete 0.08 m/s cap"
else
  pass "robot-remote.sh no longer claims the obsolete cap"
fi

echo
echo "== packaging hygiene =="
if grep -q 'client.env' "$INST"; then
  pass "install.sh excludes the machine-local client.env"
else
  fail "install.sh packages client.env (contains a real address)"
fi
if grep -q 'trap cleanup' "$INST" && grep -qF "rm -rf '\$STAGE'" "$INST"; then
  pass "install.sh cleans up its remote staging area"
else
  fail "install.sh leaves its remote staging area behind"
fi
if grep -q 'umask 077' "$INST"; then
  pass "install.sh creates the bundle with restrictive permissions"
else
  fail "install.sh does not restrict the bundle permissions"
fi

echo
echo "== install.sh laptop branch (fake ssh/scp, no robot) =="
FB="$TMP/fakebin-install"
mkdir -p "$FB" "$TMP/scp" "$TMP/ltmp"
cat > "$FB/ssh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_SSH_LOG"
last="${!#}"
case "$last" in
  *mktemp*) echo "/tmp/agilex-remote.FAKE123"; exit 0 ;;
  *"rm -rf"*) exit 0 ;;
esac
exit 0
EOF
cat > "$FB/scp" <<'EOF'
#!/usr/bin/env bash
args=("$@")
n=${#args[@]}
cp "${args[$((n-2))]}" "$FAKE_SCP_DIR/bundle.tar.gz"
EOF
cat > "$FB/curl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$FB/ssh" "$FB/scp" "$FB/curl"

: > "$TMP/ssh-install.log"
if ( cd "$ROOT" && TMPDIR="$TMP/ltmp" PATH="$FB:$PATH" \
       FAKE_SSH_LOG="$TMP/ssh-install.log" FAKE_SCP_DIR="$TMP/scp" \
       bash "$INST" robot@fake > "$TMP/install.out" 2>&1 ); then
  pass "install.sh laptop branch completes against a fake remote"
else
  fail "install.sh laptop branch failed"
  tail -12 "$TMP/install.out"
fi
if grep -q "rm -rf '/tmp/agilex-remote.FAKE123'" "$TMP/ssh-install.log"; then
  pass "install.sh removes the remote staging directory"
else
  fail "install.sh did not remove the remote staging directory"
fi
if [ -z "$(find "$TMP/ltmp" -mindepth 1 -print -quit 2>/dev/null)" ]; then
  pass "install.sh removes its local temporary directory"
else
  fail "install.sh left files in its local temporary directory"
fi
if [ -f "$TMP/scp/bundle.tar.gz" ]; then
  listing="$(tar -tzf "$TMP/scp/bundle.tar.gz" 2>/dev/null || true)"
  if printf '%s\n' "$listing" | grep -qE '(^|/)client\.env$'; then
    fail "install.sh packaged client.env"
  else
    pass "install.sh excludes client.env from the payload"
  fi
  if printf '%s\n' "$listing" | grep -q 'windows-client/'; then
    fail "install.sh packaged windows-client (contains a real address)"
  else
    pass "install.sh excludes windows-client from the payload"
  fi
  if printf '%s\n' "$listing" | grep -q 'test-server-offline.py'; then
    fail "install.sh packaged test-server-offline.py (contains a real address)"
  else
    pass "install.sh excludes the out-of-scope server test fixture"
  fi
  case "$listing" in
    *scripts/rsudo.sh*) pass "payload includes scripts/rsudo.sh" ;;
    *) fail "payload is missing scripts/rsudo.sh" ;;
  esac
else
  fail "install.sh did not upload a bundle"
fi

echo
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32mALL DEPLOY TESTS PASSED\033[0m (%d checks)\n' "$PASS"
else
  printf '\033[31m%d DEPLOY TEST(S) FAILED\033[0m (%d passed)\n' "$FAIL" "$PASS"
  exit 1
fi
