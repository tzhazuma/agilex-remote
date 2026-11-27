#!/usr/bin/env bash
# agilex-remote-win.sh - Windows 10/11 client for the AgileX web remote.
#
# This is a client-side helper only. It never uploads files, never changes the
# robot, and never sends a motion command. It:
#   1. finds the robot over SSH (cache / ntfy / mDNS / known address),
#   2. confirms its SSH host key once, in a dedicated known_hosts file,
#   3. opens an SSH local forward 127.0.0.1:<local> -> 127.0.0.1:8765,
#   4. GETs /api/status (read-only) and opens the web UI.
#
# It is written for Git Bash (MSYS2) on Windows and can also run from the
# PowerShell wrapper in this directory. See README.md for usage.
#
# Environment: bash 4.4+, OpenSSH ssh/ssh-keyscan/ssh-keygen, curl, netstat.
# Optional: python (pretty-prints /api/status).
set -uo pipefail

# ---------------------------------------------------------------------------
# Paths. Git Bash may receive a native Windows path when started from
# PowerShell; convert it before dirname/pwd so spaces and Chinese names work.
# ---------------------------------------------------------------------------
SELF="${BASH_SOURCE[0]}"
case "$SELF" in
  [A-Za-z]:\\*|[A-Za-z]:/*)
    if command -v cygpath >/dev/null 2>&1; then
      SELF="$(cygpath -u "$SELF" 2>/dev/null || printf '%s' "$SELF")"
    fi
    ;;
esac
SCRIPT_DIR="$(cd -- "$(dirname -- "$SELF")" 2>/dev/null && pwd -P)" || SCRIPT_DIR="."

HOME_DIR="${HOME:-${USERPROFILE:-.}}"

to_unix_path() {
  case "${1:-}" in
    [A-Za-z]:\\*|[A-Za-z]:/*)
      if command -v cygpath >/dev/null 2>&1; then
        cygpath -u "$1" 2>/dev/null || printf '%s\n' "$1"
      else
        printf '%s\n' "$1"
      fi
      ;;
    *) printf '%s\n' "${1:-}" ;;
  esac
}

# Optional machine-local settings. CLI > environment > client.env > defaults.
# Only AGILEX_* keys are read, and values are never evaluated as shell code.
CONFIG_FILE="$(to_unix_path "${AGILEX_WIN_CONFIG:-$SCRIPT_DIR/client.env}")"
load_config_file() {
  local file="$1" line key value
  [ -r "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      ''|'#'*) continue ;;
    esac
    case "$line" in
      *=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    case "$key" in
      AGILEX_[A-Za-z0-9_]*) ;;
      *) continue ;;
    esac
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    # Do not override a value already provided by the environment.
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < "$file"
}
load_config_file "$CONFIG_FILE"

# ---------------------------------------------------------------------------
# Defaults (environment overrides are documented in README.md)
# ---------------------------------------------------------------------------
CMD="start"
ROBOT_HOST="${AGILEX_HOST:-}"
ROBOT_USER="${AGILEX_USER:-agilex}"
SSH_PORT="${AGILEX_SSH_PORT:-22}"
LOCAL_PORT="${AGILEX_LOCAL_PORT:-${AGILEX_REMOTE_LOCAL_PORT:-8765}}"
REMOTE_PORT="${AGILEX_REMOTE_REMOTE_PORT:-${AGILEX_PORT:-8765}}"
IDENTITY_FILE="${AGILEX_IDENTITY_FILE:-}"
STATE_DIR="$(to_unix_path "${AGILEX_STATE_DIR:-$HOME_DIR/.cache/agilex-win}")"
EXPECTED_FP="${AGILEX_HOST_FINGERPRINT:-}"
ACCEPT_HOST_KEY="${AGILEX_ACCEPT_HOST_KEY:-0}"
TRUST_NEW_HOST_KEY="${AGILEX_TRUST_NEW_HOST_KEY:-0}"
OPEN_BROWSER="${AGILEX_BROWSER:-1}"
STATUS_URL_OVERRIDE="${AGILEX_STATUS_URL:-}"
START_TIMEOUT="${AGILEX_START_TIMEOUT:-25}"
SCAN_TIMEOUT="${AGILEX_SCAN_TIMEOUT:-4}"
SSH_BIN="${AGILEX_SSH_BIN:-ssh}"
SSH_KEYSCAN_BIN="${AGILEX_SSH_KEYSCAN_BIN:-ssh-keyscan}"
SSH_KEYGEN_BIN="${AGILEX_SSH_KEYGEN_BIN:-ssh-keygen}"
STRICT_CHECK=0
WAIT_SECONDS=0
VERBOSE=0
USER_EXPLICIT=0
TUNNEL_PID=""

STATE_DIR="${STATE_DIR%/}"
CACHE_FILE="$STATE_DIR/current-host"
SEEN_FILE="$STATE_DIR/seen-hosts"
KNOWN_HOSTS="$STATE_DIR/known_hosts"
PID_FILE="$STATE_DIR/tunnel.pid"
TUNNEL_LOG="$STATE_DIR/tunnel.log"
TUNNEL_INFO="$STATE_DIR/tunnel.info"
LOCK_DIR="$STATE_DIR/start.lock"
SCAN_KEYS_FILE="$STATE_DIR/scanned-keys"
BODY_FILE="$STATE_DIR/last-status.json"
LOCK_OWNED=0

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
log()  { printf '[agilex-win] %s\n' "$*" >&2; }
warn() { printf '[agilex-win] WARNING: %s\n' "$*" >&2; }
die()  { printf '[agilex-win] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }
debug() { [ "$VERBOSE" = 1 ] && printf '[agilex-win] DEBUG: %s\n' "$*" >&2 || true; }

usage() {
  cat <<'USAGE'
AgileX remote controller - Windows client (Git Bash / PowerShell)

Usage:
  agilex-remote-win.sh [start] [options]   start tunnel + open browser
  agilex-remote-win.sh stop                stop only the tunnel this tool started
  agilex-remote-win.sh status              read-only local status
  agilex-remote-win.sh check [--strict]    read-only end-to-end /api/status check
  agilex-remote-win.sh find                discover and print the robot address
  agilex-remote-win.sh help                this text

Options:
  --host HOST              use HOST (name or IP) instead of discovery
  --user USER              SSH user (default: agilex)
  --ssh-port PORT          SSH port (default: 22)
  --local-port PORT        local forwarding port (default: 8765)
  --remote-port PORT       robot-side port (default: 8765)
  --identity FILE          SSH private key to use (never copied or printed)
  --state-dir DIR          state/cache/known_hosts directory
  --accept-host-key        accept a first-seen host key without a prompt
  --trust-new-host-key     allow replacing a changed agilex-robot host key
  --fingerprint SHA256:... require this host key fingerprint
  --no-browser             do not open the browser
  --wait SECONDS           check: wait for the API (default: 0)
  --strict                 check: fail if chassis/arms report disconnected
  --verbose                extra diagnostics
  -h, --help               this text

Discovery order (when --host is not given):
  cached address -> ntfy (optional) -> previously seen -> agilex.local
  -> AGILEX_EXTRA_HOSTS. A full subnet sweep is intentionally NOT implemented:
  it is slow, and this client must not touch unrelated hosts.

Security:
  Host keys are stored in <state-dir>/known_hosts under the stable alias
  "agilex-robot". The first key is shown as a SHA256 fingerprint and must be
  accepted explicitly. StrictHostKeyChecking=no is never used.

Environment (subset):
  AGILEX_HOST AGILEX_USER AGILEX_SSH_PORT AGILEX_LOCAL_PORT
  AGILEX_REMOTE_LOCAL_PORT (old name) AGILEX_REMOTE_REMOTE_PORT AGILEX_PORT
  AGILEX_IDENTITY_FILE AGILEX_STATE_DIR AGILEX_HOST_FINGERPRINT
  AGILEX_ACCEPT_HOST_KEY AGILEX_TRUST_NEW_HOST_KEY AGILEX_BROWSER
  AGILEX_STATUS_URL AGILEX_START_TIMEOUT AGILEX_SCAN_TIMEOUT
  AGILEX_SSH_BIN AGILEX_NTFY_TOPIC AGILEX_NTFY_TOPIC_FILE AGILEX_PYTHON
  AGILEX_EXTRA_HOSTS AGILEX_WIN_CONFIG

Examples:
  ./agilex-remote-win.sh --host 192.168.1.50
  ./agilex-remote-win.sh --host agilex.local --accept-host-key
  ./agilex-remote-win.sh status
  ./agilex-remote-win.sh check --wait 10
  ./agilex-remote-win.sh stop
USAGE
}

# ---------------------------------------------------------------------------
# Argument parsing and validation
# ---------------------------------------------------------------------------
need_value() {
  [ "$#" -ge 2 ] && [ -n "$2" ] || die "option $1 needs a value"
}

parse_args() {
  [ -n "${AGILEX_USER:-}" ] && USER_EXPLICIT=1

  while [ "$#" -gt 0 ]; do
    case "$1" in
      start|stop|status|check|find) CMD="$1"; shift ;;
      help|-h|--help) usage; exit 0 ;;
      --host)
        need_value "$@"; ROBOT_HOST="$2"; shift 2 ;;
      --host=*)
        ROBOT_HOST="${1#*=}"; [ -n "$ROBOT_HOST" ] || die "--host needs a value"; shift ;;
      --user)
        need_value "$@"; ROBOT_USER="$2"; USER_EXPLICIT=1; shift 2 ;;
      --user=*)
        ROBOT_USER="${1#*=}"; [ -n "$ROBOT_USER" ] || die "--user needs a value"; USER_EXPLICIT=1; shift ;;
      --ssh-port)
        need_value "$@"; SSH_PORT="$2"; shift 2 ;;
      --ssh-port=*)
        SSH_PORT="${1#*=}"; shift ;;
      --local-port)
        need_value "$@"; LOCAL_PORT="$2"; shift 2 ;;
      --local-port=*)
        LOCAL_PORT="${1#*=}"; shift ;;
      --remote-port)
        need_value "$@"; REMOTE_PORT="$2"; shift 2 ;;
      --remote-port=*)
        REMOTE_PORT="${1#*=}"; shift ;;
      --identity)
        need_value "$@"; IDENTITY_FILE="$2"; shift 2 ;;
      --identity=*)
        IDENTITY_FILE="${1#*=}"; shift ;;
      --state-dir)
        need_value "$@"; STATE_DIR="$(to_unix_path "$2")"; shift 2 ;;
      --state-dir=*)
        STATE_DIR="$(to_unix_path "${1#*=}")"; shift ;;
      --accept-host-key)
        ACCEPT_HOST_KEY=1; shift ;;
      --trust-new-host-key)
        TRUST_NEW_HOST_KEY=1; shift ;;
      --fingerprint)
        need_value "$@"; EXPECTED_FP="$2"; shift 2 ;;
      --fingerprint=*)
        EXPECTED_FP="${1#*=}"; shift ;;
      --no-browser)
        OPEN_BROWSER=0; shift ;;
      --wait)
        need_value "$@"; WAIT_SECONDS="$2"; shift 2 ;;
      --wait=*)
        WAIT_SECONDS="${1#*=}"; shift ;;
      --strict)
        STRICT_CHECK=1; shift ;;
      --verbose|-v)
        VERBOSE=1; shift ;;
      --)
        shift; break ;;
      -*)
        die "unknown option: $1 (use --help)" ;;
      *)
        die "unexpected argument: $1 (use --help)" ;;
    esac
  done
  [ "$#" -eq 0 ] || die "unexpected argument: $1 (use --help)"

  # user@host is accepted for convenience.
  case "$ROBOT_HOST" in
    *@*)
      local embedded_user="${ROBOT_HOST%@*}"
      ROBOT_HOST="${ROBOT_HOST#*@}"
      if [ "$USER_EXPLICIT" = 0 ] && [ -n "$embedded_user" ]; then
        ROBOT_USER="$embedded_user"
      fi
      ;;
  esac

  STATE_DIR="${STATE_DIR%/}"
  CACHE_FILE="$STATE_DIR/current-host"
  SEEN_FILE="$STATE_DIR/seen-hosts"
  KNOWN_HOSTS="$STATE_DIR/known_hosts"
  PID_FILE="$STATE_DIR/tunnel.pid"
  TUNNEL_LOG="$STATE_DIR/tunnel.log"
  TUNNEL_INFO="$STATE_DIR/tunnel.info"
  LOCK_DIR="$STATE_DIR/start.lock"
  SCAN_KEYS_FILE="$STATE_DIR/scanned-keys"
  BODY_FILE="$STATE_DIR/last-status.json"

  validate_config
}

is_port() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
  esac
  local n
  n=$((10#$1))
  [ "$n" -ge 1 ] && [ "$n" -le 65535 ]
}

validate_config() {
  is_port "$SSH_PORT" || die "invalid --ssh-port: $SSH_PORT"
  is_port "$LOCAL_PORT" || die "invalid --local-port: $LOCAL_PORT"
  is_port "$REMOTE_PORT" || die "invalid --remote-port: $REMOTE_PORT"
  is_port "$START_TIMEOUT" || die "invalid AGILEX_START_TIMEOUT: $START_TIMEOUT"
  is_port "$SCAN_TIMEOUT" || die "invalid AGILEX_SCAN_TIMEOUT: $SCAN_TIMEOUT"
  case "${WAIT_SECONDS:-0}" in
    ''|*[!0-9]*) die "invalid --wait: $WAIT_SECONDS" ;;
  esac
  [ "$WAIT_SECONDS" -le 600 ] || die "--wait is limited to 600 seconds"

  case "$ROBOT_USER" in
    ''|-*|*[[:space:]]*) die "invalid SSH user: $ROBOT_USER" ;;
  esac
  if [ -n "$ROBOT_HOST" ]; then
    case "$ROBOT_HOST" in
      -*|*[[:space:]]*) die "invalid host: $ROBOT_HOST" ;;
    esac
  fi

  case "$STATE_DIR" in
    ''|/) die "invalid state directory: $STATE_DIR" ;;
    *\"*|*$'\n'*|*$'\r'*) die "state directory may not contain quotes or newlines: $STATE_DIR" ;;
  esac

  if [ -n "$IDENTITY_FILE" ]; then
    IDENTITY_FILE="$(to_unix_path "$IDENTITY_FILE")"
    [ -r "$IDENTITY_FILE" ] || die "identity file is not readable: $IDENTITY_FILE"
  fi
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1"
}

require_tunnel_tools() {
  require_tool "$SSH_BIN"
  require_tool "$SSH_KEYSCAN_BIN"
  require_tool "$SSH_KEYGEN_BIN"
  require_tool timeout
}

require_read_tools() {
  require_tool curl
}

ensure_state() {
  umask 077
  mkdir -p "$STATE_DIR" 2>/dev/null || die "cannot create state directory: $STATE_DIR"
  chmod 700 "$STATE_DIR" 2>/dev/null || true
  [ -e "$KNOWN_HOSTS" ] && chmod 600 "$KNOWN_HOSTS" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Local port / process helpers
# ---------------------------------------------------------------------------
port_listener_pid() {
  local port="$1" line
  line="$(netstat -ano -p tcp 2>/dev/null | tr -d '\r' | awk -v p=":$port" '
    $1 == "TCP" {
      local_addr = $2
      foreign = $3
      if ((local_addr == "127.0.0.1" p ||
           local_addr == "0.0.0.0" p ||
           local_addr == "[::1]" p ||
           local_addr == "[::]" p) &&
          ($4 == "LISTENING" || foreign == "0.0.0.0:0" || foreign == "[::]:0")) {
        print $NF
        exit
      }
    }' 2>/dev/null)"
  [ -n "$line" ] && printf '%s\n' "$line"
}

port_listening() {
  local port="$1"
  [ -n "$(port_listener_pid "$port")" ] && return 0
  # On Windows netstat is authoritative. The /dev/tcp fallback can block for
  # seconds on a closed port (SYN retry), so use it only if netstat is absent
  # or failed.
  if command -v netstat >/dev/null 2>&1 && netstat -ano -p tcp >/dev/null 2>&1; then
    return 1
  fi
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    exec 3>&-
    return 0
  fi
  return 1
}

pid_alive() {
  local pid="${1:-}"
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$pid" 2>/dev/null
}

pid_cmdline() {
  local pid="${1:-}"
  [ -r "/proc/$pid/cmdline" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null
}

pid_winpid() {
  local pid="${1:-}"
  ps -p "$pid" 2>/dev/null | awk 'NR == 2 {print $4; exit}'
}

winpid_to_msys_pid() {
  local winpid="${1:-}"
  [ -n "$winpid" ] || return 1
  ps -W 2>/dev/null | awk -v w="$winpid" '$4 == w {print $1; exit}'
}

pid_matches_tunnel() {
  local pid="${1:-}" cmd
  cmd="$(pid_cmdline "$pid")" || return 1
  case "$cmd" in
    *ssh*"-L 127.0.0.1:$LOCAL_PORT:127.0.0.1:$REMOTE_PORT"*) return 0 ;;
    *ssh*"-L127.0.0.1:$LOCAL_PORT:127.0.0.1:$REMOTE_PORT"*) return 0 ;;
    *ssh*"-L"*"$LOCAL_PORT:127.0.0.1:$REMOTE_PORT"*) return 0 ;;
  esac
  return 1
}

read_pid_file() {
  local pid=""
  [ -r "$PID_FILE" ] && pid="$(tr -dc '0-9' < "$PID_FILE" 2>/dev/null)"
  [ -n "$pid" ] && printf '%s\n' "$pid"
}

# ---------------------------------------------------------------------------
# HTTP / status helpers
# ---------------------------------------------------------------------------
local_status_url() {
  printf 'http://127.0.0.1:%s/api/status' "$LOCAL_PORT"
}

status_url() {
  if [ -n "$STATUS_URL_OVERRIDE" ]; then
    printf '%s' "$STATUS_URL_OVERRIDE"
  else
    local_status_url
  fi
}

http_code() {
  local url="$1" body="$2" timeout="${3:-6}" code
  code="$(curl --noproxy '*' -sS -m "$timeout" -o "$body" -w '%{http_code}' "$url" 2>/dev/null)" || true
  printf '%s' "${code:-000}"
}

is_agilex_status_body() {
  local body="$1"
  [ -s "$body" ] || return 1
  grep -q '"ok"[[:space:]]*:[[:space:]]*true' "$body" 2>/dev/null &&
    grep -q '"limits"' "$body" 2>/dev/null &&
    grep -q '"chassis"' "$body" 2>/dev/null
}

wait_for_api() {
  local seconds="${1:-0}" deadline code
  deadline=$((SECONDS + seconds))
  while :; do
    code="$(http_code "$(status_url)" "$BODY_FILE" 6)"
    if [ "$code" = "200" ] && is_agilex_status_body "$BODY_FILE"; then
      return 0
    fi
    [ "$SECONDS" -ge "$deadline" ] && return 1
    sleep 0.5
  done
}

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

print_status_summary() {
  local body="$1" py
  if py="$(find_python)"; then
    "$py" - "$body" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        d = json.load(fh)
except Exception as exc:
    print("  status JSON: parse failed (%s)" % exc)
    raise SystemExit(1)

ch = d.get("chassis") or {}
lim = d.get("limits") or {}
print("  chassis : connected=%s estop=%s" % (ch.get("connected"), d.get("estop")))
print("  limits  : max_lin=%s m/s max_ang=%s rad/s joint_step=%s deg" % (
    lim.get("max_lin"), lim.get("max_ang"), lim.get("max_joint_step_deg")))
print("  watchdog: %s ms" % d.get("watchdog_ms"))

arms = d.get("arms") or []
if not arms and d.get("arm"):
    arms = [d.get("arm")]
for arm in arms:
    enabled = sum(1 for x in (arm.get("joints_enabled") or []) if x)
    jitter = arm.get("jitter") or {}
    print("  arm %-5s: connected=%s enabled=%s mode=%s arm_says=%s motors=%s/6 cmd_hz=%s" % (
        arm.get("side", "?"), arm.get("connected"), arm.get("enabled"),
        arm.get("mode"), arm.get("mode_actual"), enabled, jitter.get("cmd_hz")))
PY
    return $?
  fi

  echo "  (python not found; raw JSON follows)"
  head -c 500 "$body" 2>/dev/null
  echo
  return 0
}

status_health_ok() {
  local body="$1" py
  if ! py="$(find_python)"; then
    echo "  python not found; cannot evaluate --strict"
    return 1
  fi
  "$py" - "$body" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        d = json.load(fh)
except Exception:
    raise SystemExit(1)

bad = []
ch = d.get("chassis") or {}
if ch.get("connected") is not True:
    bad.append("chassis")
for arm in (d.get("arms") or []):
    if arm.get("connected") is not True:
        bad.append("arm:%s" % arm.get("side", "?"))
if d.get("estop") is True:
    bad.append("estop")
if bad:
    print("  disconnected/blocked: %s" % ", ".join(bad))
    raise SystemExit(1)
raise SystemExit(0)
PY
}

# ---------------------------------------------------------------------------
# Host discovery
# ---------------------------------------------------------------------------
ntfy_lookup() {
  local topic="${AGILEX_NTFY_TOPIC:-}" file ip
  if [ -z "$topic" ]; then
    for file in "${AGILEX_NTFY_TOPIC_FILE:-}" "$STATE_DIR/ntfy-topic" "$HOME_DIR/.config/agilex/ntfy-topic"; do
      [ -n "$file" ] || continue
      if [ -r "$file" ]; then
        topic="$(tr -d '[:space:]' < "$file" 2>/dev/null)"
        [ -n "$topic" ] && break
      fi
    done
  fi
  [ -n "$topic" ] || return 1
  # Do not allow a topic to inject URL syntax, and never print it.
  case "$topic" in
    *[!A-Za-z0-9_-]*) return 1 ;;
  esac
  command -v curl >/dev/null 2>&1 || return 1
  ip="$(curl --noproxy '*' -sS -m 8 "https://ntfy.sh/$topic/json?poll=1" 2>/dev/null \
        | grep -o '"message":"[^"]*"' | tail -1 \
        | sed 's/^"message":"//; s/"$//')"
  [ -n "$ip" ] && printf '%s\n' "$ip"
}

candidate_hosts() {
  if [ -n "$ROBOT_HOST" ]; then
    printf '%s\n' "$ROBOT_HOST"
    return 0
  fi

  if [ -r "$CACHE_FILE" ]; then
    sed -n '1p' "$CACHE_FILE" 2>/dev/null
  fi

  ntfy_lookup 2>/dev/null || true

  if [ -r "$SEEN_FILE" ]; then
    cat "$SEEN_FILE" 2>/dev/null
  fi

  printf '%s\n' "agilex.local"

  if [ -n "${AGILEX_EXTRA_HOSTS:-}" ]; then
    printf '%s\n' "$AGILEX_EXTRA_HOSTS" | tr ',' ' ' | tr ' ' '\n' | sed '/^$/d'
  fi
}

dedupe_hosts() {
  tr -d '\r' | awk '{
    gsub(/^[ \t]+|[ \t]+$/, "")
    if ($0 != "" && !seen[$0]++) print
  }'
}

scan_keys_for() {
  local host="$1" args=(-T "$SCAN_TIMEOUT" -p "$SSH_PORT") out
  case "$host" in
    *:*) ;;                       # IPv6 literal: do not force IPv4
    *) args=(-4 "${args[@]}") ;;
  esac
  out="$(timeout "$((SCAN_TIMEOUT + 6))" "$SSH_KEYSCAN_BIN" "${args[@]}" "$host" 2>/dev/null \
        | tr -d '\r' | awk '!/^#/ && NF >= 3 {print $2 " " $3}' | sort -u)"
  [ -n "$out" ] && printf '%s\n' "$out"
}

cache_host() {
  local host="$1"
  ensure_state
  printf '%s\n' "$host" > "$CACHE_FILE.tmp.$$" 2>/dev/null &&
    mv -f "$CACHE_FILE.tmp.$$" "$CACHE_FILE" 2>/dev/null || true
  {
    printf '%s\n' "$host"
    if [ -r "$SEEN_FILE" ]; then cat "$SEEN_FILE"; fi
  } 2>/dev/null | awk 'NF && !seen[$0]++ {line[++n] = $0} END {for (i = 1; i <= n && i <= 20; i++) print line[i]}' > "$SEEN_FILE.tmp.$$" 2>/dev/null
  if [ -s "$SEEN_FILE.tmp.$$" ]; then
    mv -f "$SEEN_FILE.tmp.$$" "$SEEN_FILE" 2>/dev/null || true
  else
    rm -f "$SEEN_FILE.tmp.$$" 2>/dev/null || true
  fi
}

discover_host() {
  local candidate keys
  if [ -n "$ROBOT_HOST" ]; then
    log "probing $ROBOT_HOST ..."
    keys="$(scan_keys_for "$ROBOT_HOST")"
    if [ -z "$keys" ]; then
      warn "no SSH banner/host key from $ROBOT_HOST"
      return 1
    fi
    SCANNED_KEYS="$keys"
    ensure_state
    printf '%s\n' "$SCANNED_KEYS" > "$SCAN_KEYS_FILE"
    cache_host "$ROBOT_HOST"
    return 0
  fi

  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    case "$candidate" in
      -*) warn "ignoring invalid candidate: $candidate"; continue ;;
      *@*|*[[:space:]]*) warn "ignoring invalid candidate: $candidate"; continue ;;
    esac
    log "probing $candidate ..."
    keys="$(scan_keys_for "$candidate")"
    if [ -n "$keys" ]; then
      ROBOT_HOST="$candidate"
      SCANNED_KEYS="$keys"
      ensure_state
      printf '%s\n' "$SCANNED_KEYS" > "$SCAN_KEYS_FILE"
      cache_host "$ROBOT_HOST"
      log "found SSH host key at $ROBOT_HOST"
      return 0
    fi
  done <<EOF
$(candidate_hosts 2>/dev/null | dedupe_hosts)
EOF

  warn "robot not found by cache/ntfy/mDNS/known address."
  warn "Pass --host <name-or-IP>, or put the ntfy topic in $STATE_DIR/ntfy-topic."
  warn "A full subnet sweep is intentionally not performed by this client."
  return 1
}

# ---------------------------------------------------------------------------
# Host key verification (TOFU with an explicit fingerprint prompt)
# ---------------------------------------------------------------------------
fingerprint_for_line() {
  local line="$1" tmp="$STATE_DIR/.fingerprint.$$"
  [ -n "$line" ] || return 1
  ensure_state
  printf '%s\n' "$line" > "$tmp" 2>/dev/null || return 1
  "$SSH_KEYGEN_BIN" -E sha256 -lf "$tmp" 2>/dev/null | awk '{print $2; exit}'
  rm -f "$tmp" 2>/dev/null || true
}

fingerprint_md5_for_line() {
  local line="$1" tmp="$STATE_DIR/.fingerprint-md5.$$"
  [ -n "$line" ] || return 1
  ensure_state
  printf '%s\n' "$line" > "$tmp" 2>/dev/null || return 1
  "$SSH_KEYGEN_BIN" -E md5 -lf "$tmp" 2>/dev/null | awk '{print $2; exit}'
  rm -f "$tmp" 2>/dev/null || true
}

print_fingerprints() {
  local line keytype fp
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    keytype="${line%% *}"
    fp="$(fingerprint_for_line "$line")"
    printf '  %-22s %s\n' "$keytype" "${fp:-unavailable}" >&2
  done <<EOF
$SCANNED_KEYS
EOF
}

verify_pinned_fingerprint() {
  local want="${EXPECTED_FP//\"/}" line fp_sha fp_md5
  want="${want//\'/}"
  [ -n "$want" ] || return 0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    fp_sha="$(fingerprint_for_line "$line")"
    fp_md5="$(fingerprint_md5_for_line "$line")"
    if [ "$want" = "$fp_sha" ] || [ "$want" = "$fp_md5" ]; then
      return 0
    fi
  done <<EOF
$SCANNED_KEYS
EOF
  return 1
}

system_known_hosts_match() {
  local kh="$HOME_DIR/.ssh/known_hosts" name line stored
  [ -r "$kh" ] || return 1
  for name in agilex-robot agilex.local "$ROBOT_HOST" "[$ROBOT_HOST]:$SSH_PORT"; do
    [ -n "$name" ] || continue
    stored="$("$SSH_KEYGEN_BIN" -F "$name" -f "$kh" 2>/dev/null \
             | awk '!/^#/ && NF >= 3 {print $2 " " $3}')"
    [ -n "$stored" ] || continue
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      if printf '%s\n' "$SCANNED_KEYS" | grep -qxF "$line"; then
        return 0
      fi
    done <<EOF
$stored
EOF
  done
  return 1
}

write_known_hosts() {
  local tmp="$KNOWN_HOSTS.tmp.$$" line
  ensure_state
  {
    printf '%s\n' "# Managed by windows-client/agilex-remote-win.sh. Do not edit by hand."
    printf '%s\n' "# HostKeyAlias=agilex-robot keeps this stable across DHCP address changes."
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      printf 'agilex-robot %s\n' "$line"
    done <<EOF
$SCANNED_KEYS
EOF
  } > "$tmp" 2>/dev/null || die "cannot write $tmp"
  chmod 600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$KNOWN_HOSTS" 2>/dev/null || die "cannot replace $KNOWN_HOSTS"
}

confirm() {
  local prompt="$1" ans=""
  printf '%s' "$prompt" >&2
  if [ -t 0 ]; then
    IFS= read -r ans 2>/dev/null || return 1
  elif ( : < /dev/tty ) 2>/dev/null; then
    IFS= read -r ans < /dev/tty 2>/dev/null || return 1
  else
    return 1
  fi
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_host_key() {
  local stored line match=0 old_fp
  ensure_state
  [ -n "${SCANNED_KEYS:-}" ] || die "internal error: no scanned host keys"

  if [ -n "$EXPECTED_FP" ]; then
    if ! verify_pinned_fingerprint; then
      printf 'Pinned fingerprint did not match. Scanned keys:\n' >&2
      print_fingerprints
      die "host key does not match --fingerprint; refusing to connect"
    fi
    log "host key matches the pinned fingerprint"
  fi

  stored="$(awk '$1 == "agilex-robot" && NF >= 3 {print $2 " " $3}' "$KNOWN_HOSTS" 2>/dev/null)"
  if [ -n "$stored" ]; then
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      if printf '%s\n' "$stored" | grep -qxF "$line"; then
        match=1
        break
      fi
    done <<EOF
$SCANNED_KEYS
EOF
    if [ "$match" = 1 ]; then
      # Refresh the file so newly offered key types are also trusted. The
      # existing entry already proved this is the same robot.
      write_known_hosts
      debug "host key matches $KNOWN_HOSTS"
      return 0
    fi

    warn "the robot's SSH host key has CHANGED (or a different machine now answers)."
    printf 'Previously trusted:\n' >&2
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      old_fp="$(fingerprint_for_line "$line")"
      printf '  %-22s %s\n' "${line%% *}" "${old_fp:-unavailable}" >&2
    done <<EOF
$stored
EOF
    printf 'Now offered:\n' >&2
    print_fingerprints

    if [ -n "$EXPECTED_FP" ]; then
      write_known_hosts
      warn "replaced the stored host key because the new key matches --fingerprint"
      return 0
    fi
    if [ "$TRUST_NEW_HOST_KEY" = 1 ] &&
       confirm "Type 'yes' to replace the stored key: "; then
      write_known_hosts
      warn "host key replaced at your explicit request"
      return 0
    fi
    die "refusing to replace the host key. Verify the fingerprint out-of-band, then use --trust-new-host-key (or --fingerprint)."
  fi

  # No key stored yet.
  if system_known_hosts_match; then
    log "host key matches an existing entry in $HOME_DIR/.ssh/known_hosts"
    write_known_hosts
    return 0
  fi

  printf 'First connection to %s. Verify this SHA256 fingerprint out-of-band:\n' "$ROBOT_HOST" >&2
  print_fingerprints
  if [ -n "$EXPECTED_FP" ]; then
    log "host key matches the pinned fingerprint; accepting it"
    write_known_hosts
    return 0
  fi
  if [ "$ACCEPT_HOST_KEY" = 1 ]; then
    warn "accepting the first-seen host key because --accept-host-key was given"
    write_known_hosts
    return 0
  fi
  if confirm "Accept this host key for agilex-robot? [y/N] "; then
    write_known_hosts
    log "host key accepted and stored in $KNOWN_HOSTS"
    return 0
  fi
  die "host key not accepted; nothing was started"
}

# ---------------------------------------------------------------------------
# Tunnel lifecycle
# ---------------------------------------------------------------------------
acquire_lock() {
  local waited=0 created now owner
  ensure_state
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    owner=""
    [ -r "$LOCK_DIR/pid" ] && owner="$(tr -dc '0-9' < "$LOCK_DIR/pid" 2>/dev/null)"
    if [ -n "$owner" ] && ! pid_alive "$owner"; then
      debug "reclaiming stale lock owned by dead pid $owner"
      rm -rf "$LOCK_DIR" 2>/dev/null || true
      continue
    fi
    created=""
    [ -r "$LOCK_DIR/created" ] && created="$(tr -dc '0-9' < "$LOCK_DIR/created" 2>/dev/null)"
    if [ -n "$created" ]; then
      now="$(date +%s)"
      if [ $((now - created)) -gt 120 ]; then
        debug "reclaiming lock older than 120s"
        rm -rf "$LOCK_DIR" 2>/dev/null || true
        continue
      fi
    elif [ -d "$LOCK_DIR" ]; then
      created="$(stat -c %Y "$LOCK_DIR" 2>/dev/null || printf '0')"
      now="$(date +%s)"
      if [ "$created" != "0" ] && [ $((now - created)) -gt 120 ]; then
        debug "reclaiming lock with no owner and old mtime"
        rm -rf "$LOCK_DIR" 2>/dev/null || true
        continue
      fi
    fi
    waited=$((waited + 1))
    if [ "$waited" -gt 30 ]; then
      die "another start is already in progress (lock: $LOCK_DIR)"
    fi
    sleep 1
  done
  LOCK_OWNED=1
  printf '%s\n' "$$" > "$LOCK_DIR/pid" 2>/dev/null || true
  date +%s > "$LOCK_DIR/created" 2>/dev/null || true
  trap 'release_lock' EXIT INT TERM
}

release_lock() {
  local owner=""
  if [ "$LOCK_OWNED" = 1 ]; then
    [ -r "$LOCK_DIR/pid" ] && owner="$(tr -dc '0-9' < "$LOCK_DIR/pid" 2>/dev/null)"
    if [ -z "$owner" ] || [ "$owner" = "$$" ]; then
      rm -rf "$LOCK_DIR" 2>/dev/null || true
    fi
    LOCK_OWNED=0
  fi
}

build_ssh_opts() {
  SSH_OPTS=(
    -F /dev/null
    -T
    -N
    -o BatchMode=yes
    -o ConnectTimeout=10
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=3
    -o ExitOnForwardFailure=yes
    -o StrictHostKeyChecking=yes
    -o CheckHostIP=no
    -o HostKeyAlias=agilex-robot
    -o "UserKnownHostsFile=\"$KNOWN_HOSTS\""
    -o GlobalKnownHostsFile=/dev/null
    -o RequestTTY=no
    -p "$SSH_PORT"
    -l "$ROBOT_USER"
    -L "127.0.0.1:$LOCAL_PORT:127.0.0.1:$REMOTE_PORT"
  )
  if [ -n "$IDENTITY_FILE" ]; then
    SSH_OPTS+=(-i "$IDENTITY_FILE" -o IdentitiesOnly=yes)
  fi
}

start_tunnel_process() {
  local pid
  ensure_state
  : > "$TUNNEL_LOG" 2>/dev/null || true
  build_ssh_opts
  debug "starting: $SSH_BIN ${SSH_OPTS[*]} $ROBOT_HOST"
  nohup "$SSH_BIN" "${SSH_OPTS[@]}" "$ROBOT_HOST" >> "$TUNNEL_LOG" 2>&1 &
  pid=$!
  TUNNEL_PID="$pid"
  disown 2>/dev/null || true
  printf '%s\n' "$pid" > "$PID_FILE" 2>/dev/null || die "cannot write $PID_FILE"
  chmod 600 "$PID_FILE" 2>/dev/null || true
  {
    printf 'host=%s\n' "$ROBOT_HOST"
    printf 'user=%s\n' "$ROBOT_USER"
    printf 'ssh_port=%s\n' "$SSH_PORT"
    printf 'local_port=%s\n' "$LOCAL_PORT"
    printf 'remote_port=%s\n' "$REMOTE_PORT"
    printf 'started=%s\n' "$(date +%s)"
  } > "$TUNNEL_INFO" 2>/dev/null || true
}

wait_for_tunnel() {
  local pid="$1" deadline code
  deadline=$((SECONDS + START_TIMEOUT))
  while :; do
    if ! pid_alive "$pid"; then
      return 1
    fi
    if port_listening "$LOCAL_PORT"; then
      code="$(http_code "$(local_status_url)" "$BODY_FILE" 6)"
      if [ "$code" = "200" ] && is_agilex_status_body "$BODY_FILE"; then
        return 0
      fi
    fi
    [ "$SECONDS" -ge "$deadline" ] && return 1
    sleep 0.5
  done
}

show_tunnel_log_tail() {
  [ -s "$TUNNEL_LOG" ] || return 0
  printf -- '--- %s (last 20 lines) ---\n' "$TUNNEL_LOG" >&2
  tail -20 "$TUNNEL_LOG" >&2 2>/dev/null || true
  printf -- '--- end log ---\n' >&2
}

open_browser() {
  local url="$1"
  if [ "$OPEN_BROWSER" != 1 ]; then
    log "browser opening disabled (AGILEX_BROWSER=0 or --no-browser)"
    return 0
  fi
  if command -v powershell.exe >/dev/null 2>&1; then
    if powershell.exe -NoProfile -Command "Start-Process '$url'" >/dev/null 2>&1; then
      return 0
    fi
  fi
  if command -v cmd.exe >/dev/null 2>&1; then
    if cmd.exe //c start "" "$url" >/dev/null 2>&1; then
      return 0
    fi
  fi
  warn "could not open the browser automatically; open this URL manually: $url"
  return 1
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
cmd_start() {
  local existing_pid="" code="" winpid="" pid=""

  ensure_state
  require_read_tools

  if port_listening "$LOCAL_PORT"; then
    code="$(http_code "$(local_status_url)" "$BODY_FILE" 6)"
    if [ "$code" = "200" ] && is_agilex_status_body "$BODY_FILE"; then
      log "an AgileX controller is already reachable on 127.0.0.1:$LOCAL_PORT; reusing it"
      print_status_summary "$BODY_FILE" || true
      log "URL: http://127.0.0.1:$LOCAL_PORT/"
      open_browser "http://127.0.0.1:$LOCAL_PORT/" || true
      return 0
    fi

    existing_pid="$(read_pid_file)"
    if [ -n "$existing_pid" ] && pid_alive "$existing_pid" && pid_matches_tunnel "$existing_pid"; then
      warn "tunnel process $existing_pid is running, but /api/status returned HTTP ${code:-000}"
      log "waiting up to ${START_TIMEOUT}s for the robot-side service ..."
      if wait_for_tunnel "$existing_pid"; then
        print_status_summary "$BODY_FILE" || true
        log "URL: http://127.0.0.1:$LOCAL_PORT/"
        open_browser "http://127.0.0.1:$LOCAL_PORT/" || true
        return 0
      fi
      show_tunnel_log_tail
      warn "the tunnel is up but the controller did not answer; nothing on the robot was restarted."
      die "robot service may be down. Keep or remove the tunnel with: $0 stop" 6
    fi

    winpid="$(port_listener_pid "$LOCAL_PORT")"
    warn "port 127.0.0.1:$LOCAL_PORT is already in use (Windows PID ${winpid:-unknown}), and it is not a working AgileX controller."
    warn "This client will not kill an unknown process."
    die "free the port, stop the other tunnel, or choose another: --local-port 8766" 5
  fi

  acquire_lock

  # Another start may have won the race while we waited for the lock.
  if port_listening "$LOCAL_PORT"; then
    code="$(http_code "$(local_status_url)" "$BODY_FILE" 6)"
    if [ "$code" = "200" ] && is_agilex_status_body "$BODY_FILE"; then
      log "another start completed first; reusing 127.0.0.1:$LOCAL_PORT"
      print_status_summary "$BODY_FILE" || true
      open_browser "http://127.0.0.1:$LOCAL_PORT/" || true
      return 0
    fi
    die "port 127.0.0.1:$LOCAL_PORT became busy while starting; refusing to start a second tunnel" 5
  fi

  require_tunnel_tools
  discover_host || die "could not find the robot" 2
  log "robot: $ROBOT_USER@$ROBOT_HOST (SSH port $SSH_PORT)"
  ensure_host_key

  start_tunnel_process
  pid="$TUNNEL_PID"
  log "tunnel started (pid $pid); waiting for /api/status ..."
  if ! wait_for_tunnel "$pid"; then
    if pid_alive "$pid"; then
      show_tunnel_log_tail
      warn "tunnel process $pid is still running, but /api/status did not become ready."
      warn "Nothing on the robot was restarted. Inspect the robot service, or run: $0 stop"
      exit 6
    fi
    rm -f "$PID_FILE" "$TUNNEL_INFO" 2>/dev/null || true
    show_tunnel_log_tail
    die "SSH tunnel exited before the controller answered (see log above)" 4
  fi

  log "tunnel ready: 127.0.0.1:$LOCAL_PORT -> $ROBOT_HOST:127.0.0.1:$REMOTE_PORT"
  print_status_summary "$BODY_FILE" || true
  log "URL: http://127.0.0.1:$LOCAL_PORT/"
  open_browser "http://127.0.0.1:$LOCAL_PORT/" || true
  return 0
}

cmd_stop() {
  local pid="" winpid="" mpid="" stopped=0

  pid="$(read_pid_file)"
  if [ -n "$pid" ]; then
    if pid_alive "$pid"; then
      if pid_matches_tunnel "$pid"; then
        log "stopping tunnel pid $pid ..."
        kill "$pid" 2>/dev/null || true
        local i=0
        while [ "$i" -lt 20 ] && pid_alive "$pid"; do
          sleep 0.2
          i=$((i + 1))
        done
        if pid_alive "$pid"; then
          warn "tunnel did not exit; sending SIGKILL to pid $pid"
          kill -9 "$pid" 2>/dev/null || true
        fi
        stopped=1
      else
        warn "pid $pid is alive but is not this tool's tunnel; removing the stale pid file only"
      fi
    fi
    rm -f "$PID_FILE" "$TUNNEL_INFO" 2>/dev/null || true
  fi

  if port_listening "$LOCAL_PORT"; then
    winpid="$(port_listener_pid "$LOCAL_PORT")"
    mpid="$(winpid_to_msys_pid "$winpid" 2>/dev/null || true)"
    if [ -n "$mpid" ] && pid_alive "$mpid" && pid_matches_tunnel "$mpid"; then
      log "stopping tunnel pid $mpid (Windows PID $winpid) ..."
      kill "$mpid" 2>/dev/null || true
      local j=0
      while [ "$j" -lt 20 ] && pid_alive "$mpid"; do
        sleep 0.2
        j=$((j + 1))
      done
      pid_alive "$mpid" && kill -9 "$mpid" 2>/dev/null || true
      stopped=1
    else
      warn "port 127.0.0.1:$LOCAL_PORT is still in use by Windows PID ${winpid:-unknown}; not this tool's tunnel, so it was not killed."
      warn "Stop that process yourself, or use another --local-port."
      [ "$stopped" = 1 ] && return 0
      return 1
    fi
  fi

  if [ "$stopped" = 1 ]; then
    log "local tunnel stopped. The robot was not contacted or changed."
  else
    log "no tunnel started by this client is running."
  fi
  return 0
}

cmd_status() {
  local pid="" winpid="" mpid="" code="000"

  ensure_state
  printf 'AgileX Windows client status\n'
  printf '  state dir : %s\n' "$STATE_DIR"

  if [ -r "$CACHE_FILE" ]; then
    printf '  robot host: %s (cached)\n' "$(sed -n '1p' "$CACHE_FILE" 2>/dev/null)"
  else
    printf '  robot host: unknown (no cache yet)\n'
  fi

  pid="$(read_pid_file)"
  if [ -n "$pid" ] && pid_alive "$pid" && pid_matches_tunnel "$pid"; then
    printf '  tunnel    : running (pid %s, Windows PID %s)\n' "$pid" "$(pid_winpid "$pid")"
  elif [ -n "$pid" ] && pid_alive "$pid"; then
    printf '  tunnel    : pid %s is alive but does not look like this tunnel (stale pid file?)\n' "$pid"
  else
    printf '  tunnel    : not running\n'
  fi

  winpid="$(port_listener_pid "$LOCAL_PORT")"
  if [ -n "$winpid" ]; then
    mpid="$(winpid_to_msys_pid "$winpid" 2>/dev/null || true)"
    printf '  listener  : 127.0.0.1:%s (Windows PID %s%s)\n' "$LOCAL_PORT" "$winpid" "${mpid:+, MSYS pid $mpid}"
  else
    printf '  listener  : nothing on 127.0.0.1:%s\n' "$LOCAL_PORT"
  fi

  if [ -n "$STATUS_URL_OVERRIDE" ]; then
    printf '  API URL   : %s (override)\n' "$STATUS_URL_OVERRIDE"
  else
    printf '  API URL   : %s\n' "$(local_status_url)"
  fi
  code="$(http_code "$(status_url)" "$BODY_FILE" 6)"
  printf '  API       : HTTP %s\n' "$code"
  if [ "$code" = "200" ] && is_agilex_status_body "$BODY_FILE"; then
    print_status_summary "$BODY_FILE" || true
  elif [ "$code" = "200" ]; then
    printf '  note      : HTTP 200 but the body does not look like an AgileX status\n'
  fi
  printf '  UI        : http://127.0.0.1:%s/\n' "$LOCAL_PORT"
  return 0
}

cmd_check() {
  local code="000" failures=0 listener_ok=0

  ensure_state
  require_read_tools

  printf '== local tunnel ==\n'
  if port_listening "$LOCAL_PORT"; then
    listener_ok=1
    printf '  PASS  listener on 127.0.0.1:%s (Windows PID %s)\n' "$LOCAL_PORT" "$(port_listener_pid "$LOCAL_PORT")"
  elif [ -n "$STATUS_URL_OVERRIDE" ]; then
    printf '  SKIP  no local listener; using status URL override\n'
  else
    printf '  FAIL  nothing listening on 127.0.0.1:%s (start it with: %s start)\n' "$LOCAL_PORT" "$0"
    failures=$((failures + 1))
  fi

  if [ "$WAIT_SECONDS" -gt 0 ]; then
    printf '== wait ==\n'
    if wait_for_api "$WAIT_SECONDS"; then
      printf '  PASS  API became ready within %ss\n' "$WAIT_SECONDS"
    else
      printf '  FAIL  API did not become ready within %ss\n' "$WAIT_SECONDS"
      failures=$((failures + 1))
    fi
  fi

  printf '== API ==\n'
  code="$(http_code "$(status_url)" "$BODY_FILE" 8)"
  if [ "$code" = "200" ]; then
    printf '  PASS  /api/status -> HTTP 200\n'
  else
    printf '  FAIL  /api/status -> HTTP %s\n' "$code"
    failures=$((failures + 1))
  fi

  if [ "$code" = "200" ]; then
    if is_agilex_status_body "$BODY_FILE"; then
      printf '  PASS  body looks like an AgileX status\n'
      if print_status_summary "$BODY_FILE"; then
        :
      else
        printf '  FAIL  status JSON could not be parsed\n'
        failures=$((failures + 1))
      fi
      if [ "$STRICT_CHECK" = 1 ]; then
        if status_health_ok "$BODY_FILE"; then
          printf '  PASS  --strict: chassis and arms report connected, E-STOP clear\n'
        else
          printf '  FAIL  --strict: chassis/arms are not all connected, or python is missing\n'
          failures=$((failures + 1))
        fi
      fi
    else
      printf '  FAIL  HTTP 200 but the body is not an AgileX status\n'
      failures=$((failures + 1))
    fi
  fi

  printf '== result ==\n'
  if [ "$failures" -eq 0 ]; then
    printf 'READ-ONLY CHECK OK - no motion command was sent.\n'
    return 0
  fi
  printf '%s CHECK(S) FAILED - no motion command was sent.\n' "$failures"
  if [ "$listener_ok" = 0 ] && [ -z "$STATUS_URL_OVERRIDE" ]; then
    return 2
  fi
  return 3
}

cmd_find() {
  require_tool "$SSH_KEYSCAN_BIN"
  require_tool timeout
  ensure_state
  discover_host || die "robot not found" 2
  printf '%s\n' "$ROBOT_HOST"
  return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
parse_args "$@"

case "$CMD" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  check)  cmd_check ;;
  find)   cmd_find ;;
  *)      usage; exit 1 ;;
esac
