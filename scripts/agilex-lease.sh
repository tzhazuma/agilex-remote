#!/usr/bin/env bash
# agilex-lease.sh — minimal control-lease client for shell scripts.
#
# Source this file, do not execute it:
#
#   HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$HERE/agilex-lease.sh"
#   lease_acquire "my-script" || exit $?
#   trap 'lease_release' EXIT
#   lease_start_heartbeat 5
#   lease_curl /api/arm '{"arm":0,"joint":0,"delta_deg":0}'
#   lease_release
#
# The secret is kept in a shell variable and a 0600 curl config file. It is
# never printed and never placed in curl argv. `/api/estop` is exempt, so the
# panic path cannot be blocked. `/api/estop/clear` requires a lease and
# body {"confirm":"CLEAR"}; it is not a panic action. A zero-velocity
# /api/drive is also exempt.
#
# lease_acquire installs an EXIT trap that releases the lease. Set
# AGILEX_LEASE_NO_EXIT_TRAP=1 if the caller needs its own EXIT trap.
#
# Server compatibility: if /api/lease/acquire returns 404/405, the helper
# assumes an older server without leases and lets callers continue unleased.
#
# Exit codes: 0 ok, 1 transport/parse error or non-2xx HTTP, 3 lease held,
#             4 takeover refused, 5 heartbeat lost, 6 no active lease,
#             7 clear without confirm=CLEAR.
#             5 heartbeat lost, 6 no active lease for a leased request.
set -u

AGILEX_API="${AGILEX_API:-http://127.0.0.1:8765}"
AGILEX_CONTROLLER="${AGILEX_CONTROLLER:-$(hostname 2>/dev/null || echo agilex)-$$}"
AGILEX_LEASE_SUPPORTED="${AGILEX_LEASE_SUPPORTED:-1}"
AGILEX_LEASE_SECRET="${AGILEX_LEASE_SECRET:-}"
AGILEX_LEASE_CFG="${AGILEX_LEASE_CFG:-}"
AGILEX_LEASE_LOST="${AGILEX_LEASE_LOST:-0}"
AGILEX_LAST_HTTP_CODE=""
AGILEX_LEASE_HTTP_TIMEOUT="${AGILEX_LEASE_HTTP_TIMEOUT:-10}"
_AGILEX_LEASE_HB_PID=""

# python3 on Windows may be a Store stub that exits without running. Pick the
# first interpreter that actually executes; AGILEX_PYTHON overrides.
_LEASE_PY="${AGILEX_PYTHON:-${PYTHON:-}}"
if [ -n "$_LEASE_PY" ] && ! "$_LEASE_PY" -c 'import sys' >/dev/null 2>&1; then
  echo "lease: AGILEX_PYTHON/PYTHON does not run: $_LEASE_PY" >&2
  _LEASE_PY=""
fi
if [ -z "$_LEASE_PY" ]; then
  for _lease_cand in python3 python; do
    if command -v "$_lease_cand" >/dev/null 2>&1 \
       && "$_lease_cand" -c 'import sys' >/dev/null 2>&1; then
      _LEASE_PY="$_lease_cand"
      break
    fi
  done
fi
if [ -z "$_LEASE_PY" ]; then
  echo "lease: no working python interpreter" >&2
  return 1 2>/dev/null || exit 1
fi

_lease_xtrace_off() {
  case $- in *x*) _AGILEX_LEASE_XTRACE=1; set +x ;; esac
}
_lease_xtrace_on() {
  if [ "${_AGILEX_LEASE_XTRACE:-0}" = "1" ]; then set -x; unset _AGILEX_LEASE_XTRACE; fi
}

_lease_cleanup_cfg() {
  if [ -n "${AGILEX_LEASE_CFG:-}" ]; then rm -f "$AGILEX_LEASE_CFG"; fi
  AGILEX_LEASE_CFG=""
}

_lease_json_acquire() {
  "$_LEASE_PY" - "$1" "$2" "$3" <<'PY'
import json, sys
controller, takeover, confirm = sys.argv[1], sys.argv[2], sys.argv[3]
body = {"controller": controller, "takeover": takeover == "1"}
if confirm:
    body["confirm"] = confirm
print(json.dumps(body))
PY
}

_lease_json_with_controller() {
  "$_LEASE_PY" - "$1" "$2" <<'PY'
import json, sys
raw, controller = sys.argv[1], sys.argv[2]
d = json.loads(raw) if raw.strip() else {}
if not isinstance(d, dict):
    raise SystemExit("body must be a JSON object")
d.setdefault("controller", controller)
print(json.dumps(d))
PY
}

_lease_parse() {
  "$_LEASE_PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
def s(k):
    v = d.get(k, "")
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v).replace("\t", " ").replace("\n", " ")
print("\x1f".join(s(k) for k in
                 ("ok", "lease", "error", "controller", "expires_in", "took_over")))
'
}

_lease_http() {
  # _lease_http METHOD PATH [BODY] [CONFIG] -> body + "\n" + HTTP code
  local method="$1" path="$2" body="${3:-}" cfg="${4:-}"
  local args=(-sS -m "$AGILEX_LEASE_HTTP_TIMEOUT" -X "$method" -w '\n%{http_code}')
  if [ -n "$cfg" ]; then args+=(--config "$cfg"); fi
  if [ "$method" != "GET" ]; then
    args+=(-H 'Content-Type: application/json')
    if [ -n "$body" ]; then args+=(--data-binary "$body"); fi
  fi
  curl "${args[@]}" "$AGILEX_API$path"
}

_lease_split_code() {
  # Sets _LEASE_BODY and AGILEX_LAST_HTTP_CODE from _lease_http output.
  local out="$1"
  AGILEX_LAST_HTTP_CODE="${out##*$'\n'}"
  _LEASE_BODY="${out%$'\n'*}"
}

_lease_zero_drive() {
  "$_LEASE_PY" - "$1" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1] or "{}")
    print("1" if abs(float(d.get("lin") or 0.0)) <= 1e-9
          and abs(float(d.get("ang") or 0.0)) <= 1e-9 else "0")
except Exception:
    print("0")
PY
}

_lease_json_confirm() {
  "$_LEASE_PY" - "$1" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1] or "{}")
    print(str(d.get("confirm") or ""))
except Exception:
    print("")
PY
}

lease_install_exit_trap() {
  trap 'lease_release' EXIT
}

lease_acquire() {
  local controller="$AGILEX_CONTROLLER" takeover=0 confirm=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --takeover) takeover=1; shift ;;
      --confirm) [ $# -ge 2 ] || { echo "lease: --confirm needs a value" >&2; return 1; }; confirm="$2"; shift 2 ;;
      -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; return 0 ;;
      *) controller="$1"; shift ;;
    esac
  done
  local body out ok secret error held expires took
  body="$(_lease_json_acquire "$controller" "$takeover" "$confirm")"
  out="$(_lease_http POST /api/lease/acquire "$body")" || {
    echo "lease: cannot reach $AGILEX_API" >&2; return 1;
  }
  _lease_split_code "$out"
  case "$AGILEX_LAST_HTTP_CODE" in
    404|405)
      AGILEX_LEASE_SUPPORTED=0
      AGILEX_LEASE_SECRET=""
      _lease_cleanup_cfg
      echo "lease: server does not support leases; continuing without one" >&2
      return 0 ;;
    200|409) ;;
    *) echo "lease: acquire HTTP $AGILEX_LAST_HTTP_CODE: $_LEASE_BODY" >&2; return 1 ;;
  esac
  _lease_xtrace_off
  IFS=$'\x1f' read -r ok secret error held expires took <<< "$(printf '%s' "$_LEASE_BODY" | _lease_parse)"
  if [ "$ok" != "1" ]; then
    _lease_xtrace_on
    echo "lease: acquire refused: ${error:-unknown}" >&2
    if [ -n "$held" ] && [ "$held" != "None" ]; then
      echo "lease: held by ${held}, expires in ${expires}s" >&2
    fi
    case "$error" in
      *held*) return 3 ;;
      *takeover*) return 4 ;;
      *) return 1 ;;
    esac
  fi
  case "$secret" in
    ''|*[!A-Za-z0-9_-]*) _lease_xtrace_on; echo "lease: invalid secret in response" >&2; return 1 ;;
  esac
  _lease_cleanup_cfg
  local old_umask
  old_umask="$(umask)"
  umask 077
  AGILEX_LEASE_CFG="$(mktemp "${TMPDIR:-/tmp}/agilex-lease.XXXXXX")" || {
    umask "$old_umask"
    _lease_xtrace_on; echo "lease: cannot create secret file" >&2; return 1;
  }
  chmod 600 "$AGILEX_LEASE_CFG" 2>/dev/null || true
  printf 'header = "X-Control-Lease: %s"\n' "$secret" > "$AGILEX_LEASE_CFG"
  umask "$old_umask"
  AGILEX_LEASE_SECRET="$secret"
  AGILEX_LEASE_SUPPORTED=1
  AGILEX_LEASE_LOST=0
  export AGILEX_LEASE_SECRET AGILEX_CONTROLLER AGILEX_LEASE_SUPPORTED
  _lease_xtrace_on
  if [ "${AGILEX_LEASE_NO_EXIT_TRAP:-0}" != "1" ]; then lease_install_exit_trap; fi
  echo "lease: acquired by ${controller} (expires in ${expires}s)" >&2
  return 0
}

lease_heartbeat() {
  [ "$AGILEX_LEASE_SUPPORTED" = "1" ] || return 0
  [ -n "$AGILEX_LEASE_SECRET" ] && [ -f "$AGILEX_LEASE_CFG" ] || return 0
  local out ok secret error ctrl expires took
  out="$(_lease_http POST /api/lease/heartbeat '{}' "$AGILEX_LEASE_CFG")" || {
    AGILEX_LEASE_LOST=1; echo "lease: heartbeat transport failure" >&2; return 5;
  }
  _lease_split_code "$out"
  case "$AGILEX_LAST_HTTP_CODE" in
    404|405) AGILEX_LEASE_SUPPORTED=0; return 0 ;;
  esac
  _lease_xtrace_off
  IFS=$'\x1f' read -r ok secret error ctrl expires took <<< "$(printf '%s' "$_LEASE_BODY" | _lease_parse)"
  _lease_xtrace_on
  if [ "$ok" != "1" ]; then
    AGILEX_LEASE_LOST=1
    echo "lease: heartbeat failed: ${error:-unknown}" >&2
    return 5
  fi
  return 0
}

lease_start_heartbeat() {
  [ "$AGILEX_LEASE_SUPPORTED" = "1" ] || return 0
  [ -n "$AGILEX_LEASE_SECRET" ] || return 0
  local interval="${1:-5}"
  case "$interval" in ''|*[!0-9]*) interval=5 ;; esac
  ( while :; do
      sleep "$interval" || exit 0
      lease_heartbeat || exit 0
    done ) &
  _AGILEX_LEASE_HB_PID=$!
}

_lease_stop_heartbeat() {
  if [ -n "${_AGILEX_LEASE_HB_PID:-}" ]; then
    kill "$_AGILEX_LEASE_HB_PID" 2>/dev/null || true
    wait "$_AGILEX_LEASE_HB_PID" 2>/dev/null || true
    _AGILEX_LEASE_HB_PID=""
  fi
}

lease_release() {
  _lease_stop_heartbeat
  if [ "$AGILEX_LEASE_SUPPORTED" = "1" ] && [ -n "$AGILEX_LEASE_SECRET" ] \
     && [ -f "$AGILEX_LEASE_CFG" ]; then
    _lease_http POST /api/lease/release '{}' "$AGILEX_LEASE_CFG" >/dev/null 2>&1 || true
  fi
  _lease_cleanup_cfg
  AGILEX_LEASE_SECRET=""
}

lease_curl() {
  # lease_curl PATH [JSON] -> response body on stdout; AGILEX_LAST_HTTP_CODE set.
  # Returns 0 only for HTTP 2xx; 409 -> 9; other non-2xx -> 1.
  local path="$1" body="${2:-}" out
  body="$(_lease_json_with_controller "$body" "$AGILEX_CONTROLLER")" || return 1
  case "$path" in
    /api/estop)
      out="$(_lease_http POST "$path" "$body")" || return 1
      _lease_split_code "$out"
      printf '%s\n' "$_LEASE_BODY"
      case "$AGILEX_LAST_HTTP_CODE" in
        2*) return 0 ;;
        409) return 9 ;;
        *) return 1 ;;
      esac ;;
    /api/estop/clear)
      [ "$AGILEX_LEASE_SUPPORTED" = "1" ] || {
        echo "lease: clear requires a lease-capable server" >&2; return 6;
      }
      [ -n "$AGILEX_LEASE_SECRET" ] && [ -f "$AGILEX_LEASE_CFG" ] || {
        echo "lease: clear requires an active lease" >&2; return 6;
      }
      [ "$(_lease_json_confirm "$body")" = "CLEAR" ] || {
        echo "lease: clear requires body confirm=CLEAR" >&2; return 7;
      } ;;
  esac
  if [ "$path" = "/api/drive" ] && [ "$(_lease_zero_drive "$body")" = "1" ]; then
    out="$(_lease_http POST "$path" "$body")" || return 1
    _lease_split_code "$out"
    printf '%s\n' "$_LEASE_BODY"
    case "$AGILEX_LAST_HTTP_CODE" in
      2*) return 0 ;;
      409) return 9 ;;
      *) return 1 ;;
    esac
  fi
  if [ "$AGILEX_LEASE_SUPPORTED" != "1" ]; then
    out="$(_lease_http POST "$path" "$body")" || return 1
    _lease_split_code "$out"
    printf '%s\n' "$_LEASE_BODY"
    case "$AGILEX_LAST_HTTP_CODE" in
      2*) return 0 ;;
      409) return 9 ;;
      *) return 1 ;;
    esac
  fi
  [ -n "$AGILEX_LEASE_SECRET" ] && [ -f "$AGILEX_LEASE_CFG" ] || {
    echo "lease: no active lease for $path" >&2; return 6;
  }
  out="$(_lease_http POST "$path" "$body" "$AGILEX_LEASE_CFG")" || return 1
  _lease_split_code "$out"
  printf '%s\n' "$_LEASE_BODY"
  case "$AGILEX_LAST_HTTP_CODE" in
    2*) return 0 ;;
    409) return 9 ;;
    *) return 1 ;;
  esac
}

lease_clear_estop() {
  lease_curl /api/estop/clear '{"confirm":"CLEAR"}'
}

lease_get() {
  local path="$1" out
  out="$(_lease_http GET "$path")" || return 1
  _lease_split_code "$out"
  printf '%s\n' "$_LEASE_BODY"
}

lease_status() { lease_get /api/lease/status; }
