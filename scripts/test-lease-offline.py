#!/usr/bin/env python3
"""Offline tests for the control-lease shell helper and run-smolvla client.

Starts a fake lease API on 127.0.0.1 (ephemeral port). No real robot, no
network, no torch/lerobot required: only the LeaseSession class is imported
from tools/run-smolvla.py.

Run:  python3 scripts/test-lease-offline.py
"""
import importlib.util
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "agilex-lease.sh"
RUNNER = ROOT / "tools" / "run-smolvla.py"

PASS = 0
FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print("PASS " + name)


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print("FAIL " + name + (("  " + str(detail)) if detail else ""))


class State:
    def __init__(self, ttl=20.0):
        self.lock = threading.Lock()
        self.ttl = float(ttl)
        self.secret = None
        self.controller = None
        self.expires = 0.0
        self.log = []

    def purge(self):
        if self.secret is not None and time.time() >= self.expires:
            self.secret = None
            self.controller = None
            self.expires = 0.0

    def status(self):
        held = self.secret is not None and time.time() < self.expires
        return {"held": held,
                "controller": self.controller if held else None,
                "expires_in": round(max(0.0, self.expires - time.time()), 1) if held else 0.0,
                "ttl_s": self.ttl}

    def acquire(self, controller, takeover=False, confirm=None):
        with self.lock:
            self.purge()
            held = self.secret is not None
            if held and not takeover:
                return {"ok": False, "error": "control lease held", **self.status()}
            if held and takeover and str(confirm or "") != "TAKEOVER":
                return {"ok": False, "error": "takeover requires confirm=TAKEOVER",
                        **self.status()}
            secret = secrets.token_urlsafe(24)
            self.secret = secret
            self.controller = str(controller or "unknown")[:64]
            self.expires = time.time() + self.ttl
            return {"ok": True, "lease": secret, "controller": self.controller,
                    "expires_in": self.ttl, "took_over": bool(held)}

    def check(self, secret):
        with self.lock:
            self.purge()
            return bool(secret and self.secret and secrets.compare_digest(secret, self.secret))

    def heartbeat(self, secret):
        with self.lock:
            self.purge()
            if not (secret and self.secret and secrets.compare_digest(secret, self.secret)):
                return {"ok": False, "error": "lease expired or not held"}
            self.expires = time.time() + self.ttl
            return {"ok": True, "controller": self.controller, "expires_in": self.ttl}

    def release(self, secret):
        with self.lock:
            self.purge()
            if not (secret and self.secret and secrets.compare_digest(secret, self.secret)):
                return {"ok": False, "error": "not the lease holder"}
            ctrl = self.controller
            self.secret = None
            self.controller = None
            self.expires = 0.0
            return {"ok": True, "controller": ctrl}


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeLease/1.0"

    def log_message(self, *args):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _secret(self, body):
        s = (self.headers.get("X-Control-Lease") or "").strip()
        if s:
            return s
        if isinstance(body, dict):
            return str(body.get("lease") or body.get("control_lease") or "")
        return ""

    def _require(self, body, path):
        secret = self._secret(body)
        valid = self.server.state.check(secret)
        self.server.state.log.append({
            "path": path, "controller": (body or {}).get("controller"),
            "header": bool((self.headers.get("X-Control-Lease") or "").strip()),
            "lease_valid": valid, "no_lease": not bool(secret),
        })
        if valid:
            return True
        self._send(409, {"ok": False, "error": "control lease required or expired",
                         "lease": self.server.state.status()})
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/lease/status":
            self._send(200, self.server.state.status())
        elif path == "/test/state":
            self._send(200, {"status": self.server.state.status(),
                             "log": self.server.state.log,
                             "requests": self.server.requests})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        self.server.requests += 1
        if path in self.server.fail_paths:
            code = self.server.fail_paths[path]
            self._send(code, {"ok": False, "error": "injected failure"})
            return
        if path == "/api/lease/acquire":
            if not self.server.lease_enabled:
                self._send(404, {"ok": False, "error": "not found"})
                return
            r = self.server.state.acquire(body.get("controller"),
                                          bool(body.get("takeover")),
                                          body.get("confirm"))
            self._send(200, r)
            return
        if path == "/api/lease/heartbeat":
            r = self.server.state.heartbeat(self._secret(body))
            self._send(200, r)
            return
        if path == "/api/lease/release":
            r = self.server.state.release(self._secret(body))
            self._send(200, r)
            return
        if path == "/test/expire":
            with self.server.state.lock:
                self.server.state.expires = 0.0
            self._send(200, {"ok": True})
            return
        if path == "/api/estop":
            self.server.state.log.append({"path": path, "controller": body.get("controller"),
                                          "header": False, "lease_valid": False,
                                          "no_lease": True})
            self._send(200, {"ok": True, "estop": True})
            return
        if path == "/api/estop/clear":
            secret = self._secret(body)
            valid = self.server.state.check(secret)
            self.server.state.log.append({
                "path": path, "controller": body.get("controller"),
                "header": bool((self.headers.get("X-Control-Lease") or "").strip()),
                "lease_valid": valid, "no_lease": not bool(secret)})
            if not valid:
                self._send(409, {"ok": False,
                                 "error": "control lease required or expired",
                                 "lease": self.server.state.status()})
                return
            if str(body.get("confirm") or "") != "CLEAR":
                self._send(409, {"ok": False, "error": "clear requires confirm=CLEAR"})
                return
            self._send(200, {"ok": True, "estop": False})
            return
        if path == "/api/drive":
            try:
                zero = (abs(float(body.get("lin") or 0.0)) <= 1e-9
                        and abs(float(body.get("ang") or 0.0)) <= 1e-9)
            except Exception:
                zero = False
            if not zero and not self.server.lease_enabled:
                self.server.state.log.append({"path": path, "controller": body.get("controller"),
                                              "header": False, "lease_valid": False,
                                              "no_lease": True})
                self._send(200, {"ok": True})
                return
            if zero:
                self.server.state.log.append({"path": path, "controller": body.get("controller"),
                                              "header": False, "lease_valid": False,
                                              "no_lease": True})
                self._send(200, {"ok": True, "applied": {"lin": 0, "ang": 0}})
                return
            if self._require(body, path):
                self._send(200, {"ok": True})
            return
        if not self.server.lease_enabled:
            self.server.state.log.append({"path": path, "controller": body.get("controller"),
                                          "header": False, "lease_valid": True,
                                          "no_lease": True})
            self._send(200, {"ok": True})
            return
        if self._require(body, path):
            self._send(200, {"ok": True})


def start_server(ttl=20.0, lease_enabled=True):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.state = State(ttl)
    srv.lease_enabled = lease_enabled
    srv.requests = 0
    srv.fail_paths = {}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_port


def stop_server(srv):
    srv.shutdown()
    srv.server_close()


def run_bash(script, api, controller, tmpdir, timeout=30):
    env = os.environ.copy()
    env.update({"AGILEX_API": api, "AGILEX_CONTROLLER": controller,
                "TMPDIR": tmpdir, "AGILEX_LEASE_HTTP_TIMEOUT": "5",
                "HELPER": str(HELPER)})
    p = subprocess.run(["bash", "-c", script], env=env, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def wait_ready(path, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.02)
    return False


def load_runner():
    spec = importlib.util.spec_from_file_location("run_smolvla", str(RUNNER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shell(api, srv):
    tmp = tempfile.mkdtemp(prefix="agilex-lease-test.")
    ready = os.path.join(tmp, "ready")
    stop = os.path.join(tmp, "stop")
    script = r'''
set -u
. "$HELPER"
lease_acquire "$AGILEX_CONTROLLER" || { echo "ACQUIRE_RC=$?"; exit 1; }
: > "$READY"
lease_curl /api/arm '{"arm":0,"joint":0,"delta_deg":0}'; echo "ARM_EARLY_RC=$?"
lease_curl /api/estop/clear '{"confirm":"WRONG"}'; echo "CLEAR_BAD_RC=$?"
lease_curl /api/estop/clear '{"confirm":"CLEAR"}'; echo "CLEAR_RC=$?"
while [ ! -e "$STOP" ]; do sleep 0.05; done
lease_curl /api/arm '{"arm":0,"joint":0,"delta_deg":0}'; echo "ARM_RC=$?"
lease_release
'''
    env_script = (script.replace("$HELPER", str(HELPER))
                        .replace("$READY", ready).replace("$STOP", stop))
    env = os.environ.copy()
    env.update({"AGILEX_API": api, "AGILEX_CONTROLLER": "shell-1", "TMPDIR": tmp})
    p = subprocess.Popen(["bash", "-c", env_script], env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not wait_ready(ready):
        p.kill()
        out, err = p.communicate()
        bad("shell helper acquired the lease", out + err)
        return
    cfgs = list(Path(tmp).glob("agilex-lease.*"))
    if len(cfgs) != 1:
        bad("shell helper created exactly one 0600 secret file", cfgs)
        secret = ""
    else:
        secret = ""
        text = cfgs[0].read_text()
        marker = "X-Control-Lease: "
        if marker in text:
            secret = text.split(marker, 1)[1].split('"', 1)[0]
        mode = stat.S_IMODE(cfgs[0].stat().st_mode)
        if os.name == "nt" or mode == 0o600:
            ok("lease secret file is 0600")
        else:
            bad("lease secret file mode is %o" % mode)
        if secret:
            ok("lease secret file contains a secret")
        else:
            bad("lease secret file has no secret")

    rc2, out2, err2 = run_bash('. "$HELPER"; lease_acquire shell-2; echo RC=$?',
                               api, "shell-2", tmp)
    if "RC=3" in out2:
        ok("second controller is refused while the lease is held")
    else:
        bad("second controller was not refused", out2 + err2)

    rc3, out3, err3 = run_bash('. "$HELPER"; lease_acquire shell-2 --takeover; echo RC=$?',
                               api, "shell-2", tmp)
    if "RC=4" in out3:
        ok("takeover without confirm is refused")
    else:
        bad("takeover without confirm was not refused", out3 + err3)

    rc4, out4, err4 = run_bash(
        '. "$HELPER"; lease_acquire shell-2 --takeover --confirm TAKEOVER; echo RC=$?',
        api, "shell-2", tmp)
    if "RC=0" in out4:
        ok("takeover with confirm succeeds")
    else:
        bad("takeover with confirm failed", out4 + err4)

    with open(stop, "w") as fh:
        fh.write("go")
    out, err = p.communicate(timeout=15)
    if "ARM_RC=9" in out:
        ok("old holder gets 409 after takeover")
    else:
        bad("old holder did not get 409 after takeover", out + err)
    if "CLEAR_BAD_RC=7" in out and "CLEAR_RC=0" in out:
        ok("clear requires confirm=CLEAR and a lease")
    else:
        bad("clear confirm/lease contract failed", out + err)

    combined = out + err
    if secret and secret not in combined:
        ok("shell helper never prints the secret")
    else:
        bad("shell output leaked the secret")
    if secret and all(secret not in json.dumps(r) for r in srv.state.log):
        ok("fake API log contains no secret")
    else:
        bad("fake API log leaked the secret")

    # The request must carry both the lease header and the controller field.
    arm_logs = [r for r in srv.state.log
                if r["path"] == "/api/arm" and r["header"] and r["lease_valid"]
                and r["controller"] == "shell-1"]
    if arm_logs:
        ok("control request carries X-Control-Lease and controller")
    else:
        bad("control request missing lease header/controller",
            [r for r in srv.state.log if r["path"] == "/api/arm"][-3:])

    if not list(Path(tmp).glob("agilex-lease.*")):
        ok("shell helper removes the secret file on exit")
    else:
        bad("shell helper left the secret file behind")

    # E-STOP and zero drive must work without a lease.
    rc5, out5, err5 = run_bash(
        '. "$HELPER"; lease_curl /api/estop "{}"; echo ESTOP_RC=$?; '
        'lease_curl /api/drive "{\\"lin\\":0,\\"ang\\":0}"; echo ZERO_RC=$?',
        api, "shell-no-lease", tmp)
    if "ESTOP_RC=0" in out5 and "ZERO_RC=0" in out5:
        ok("E-STOP and zero drive bypass the lease")
    else:
        bad("E-STOP/zero drive were blocked by the lease", out5 + err5)

    rc6, out6, err6 = run_bash(
        '. "$HELPER"; lease_curl /api/estop/clear \'{"confirm":"CLEAR"}\'; '
        'echo CLEAR_NOL_RC=$?', api, "shell-no-lease", tmp)
    if "CLEAR_NOL_RC=6" in out6:
        ok("clear without a lease is refused locally")
    else:
        bad("clear without a lease was not refused", out6 + err6)

    for code, expected in ((500, "1"), (409, "9")):
        srv.fail_paths["/api/estop"] = code
        rc7, out7, err7 = run_bash(
            '. "$HELPER"; lease_curl /api/estop "{}"; echo ESTOP_FAIL_RC=$?',
            api, "shell-no-lease", tmp)
        srv.fail_paths.pop("/api/estop", None)
        if "ESTOP_FAIL_RC=" + expected in out7:
            ok("E-STOP HTTP %d returns %s" % (code, expected))
        else:
            bad("E-STOP HTTP %d did not return %s" % (code, expected), out7 + err7)

        srv.fail_paths["/api/drive"] = code
        rc8, out8, err8 = run_bash(
            '. "$HELPER"; lease_curl /api/drive \'{"lin":0,"ang":0}\'; '
            'echo ZERO_FAIL_RC=$?', api, "shell-no-lease", tmp)
        srv.fail_paths.pop("/api/drive", None)
        if "ZERO_FAIL_RC=" + expected in out8:
            ok("zero drive HTTP %d returns %s" % (code, expected))
        else:
            bad("zero drive HTTP %d did not return %s" % (code, expected), out8 + err8)

    srv.fail_paths["/api/estop/clear"] = 500
    rc9, out9, err9 = run_bash(
        '. "$HELPER"; lease_acquire clearfail; '
        'lease_curl /api/estop/clear \'{"confirm":"CLEAR"}\'; echo CLEAR_FAIL_RC=$?; '
        'lease_release', api, "clearfail", tmp)
    srv.fail_paths.pop("/api/estop/clear", None)
    if "CLEAR_FAIL_RC=1" in out9:
        ok("clear HTTP 500 returns nonzero")
    else:
        bad("clear HTTP 500 returned success", out9 + err9)
    shutil.rmtree(tmp, ignore_errors=True)


def test_expiry_and_heartbeat(api):
    tmp = tempfile.mkdtemp(prefix="agilex-lease-expiry.")
    rc, out, err = run_bash(
        '. "$HELPER"; lease_acquire "expiry"; sleep 6.4; '
        'lease_curl /api/arm "{\\"arm\\":0}"; echo ARM_RC=$?; lease_release',
        api, "expiry", tmp, timeout=30)
    if "ARM_RC=9" in out:
        ok("expired lease yields 409")
    else:
        bad("expired lease was not rejected", out + err)

    rc, out, err = run_bash(
        '. "$HELPER"; lease_acquire "heartbeat"; sleep 1.2; lease_heartbeat; echo HB=$?; '
        'sleep 1.2; lease_curl /api/arm "{\\"arm\\":0}"; echo ARM_RC=$?; lease_release',
        api, "heartbeat", tmp, timeout=30)
    if "HB=0" in out and "ARM_RC=0" in out:
        ok("heartbeat keeps a short lease alive")
    else:
        bad("heartbeat did not keep the lease alive", out + err)
    shutil.rmtree(tmp, ignore_errors=True)


def test_old_server():
    srv, api = start_server(ttl=20, lease_enabled=False)
    try:
        tmp = tempfile.mkdtemp(prefix="agilex-lease-old.")
        rc, out, err = run_bash(
            '. "$HELPER"; lease_acquire "old"; echo ACQ=$?; '
            'lease_curl /api/arm "{\\"arm\\":0}"; echo ARM_RC=$?',
            api, "old", tmp)
        if "ACQ=0" in out and "ARM_RC=0" in out and "does not support leases" in err:
            ok("helper continues against an old server without leases")
        else:
            bad("helper did not fall back for an old server", out + err)
        try:
            mod = load_runner()
            lease = mod.LeaseSession(api, "py-old")
            acquired = lease.acquire()
            lease.post("/api/arm", {"arm": 0})
            if not acquired:
                ok("Python LeaseSession falls back on an old server")
            else:
                bad("Python LeaseSession claimed a lease on an old server")
        except Exception as e:
            bad("Python LeaseSession old-server fallback failed", type(e).__name__)
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        stop_server(srv)


def test_python_client(api, srv):
    mod = load_runner()
    lease = mod.LeaseSession(api, "py-1")
    try:
        if lease.acquire():
            ok("Python LeaseSession acquires")
        else:
            bad("Python LeaseSession did not acquire")
        lease.post("/api/arm", {"arm": 0})
        logs = [r for r in srv.state.log if r["path"] == "/api/arm"]
        if logs and logs[-1]["header"] and logs[-1]["lease_valid"] \
                and logs[-1]["controller"] == "py-1":
            ok("Python control request carries lease and controller")
        else:
            bad("Python control request missing lease/controller", logs[-3:])
        if lease.heartbeat().get("ok"):
            ok("Python heartbeat succeeds")
        else:
            bad("Python heartbeat failed")
        try:
            lease.post("/api/estop/clear", {})
        except mod.LeaseError:
            ok("Python clear without confirm is refused")
        else:
            bad("Python clear without confirm was accepted")
        if lease.post("/api/estop/clear", {"confirm": "CLEAR"}).get("ok"):
            ok("Python clear with lease+confirm succeeds")
        else:
            bad("Python clear with lease+confirm failed")
    finally:
        lease.release()

    # A fresh session must be refused while another holds it.
    held = mod.LeaseSession(api, "py-2")
    holder = mod.LeaseSession(api, "py-holder")
    try:
        holder.acquire()
        try:
            held.acquire()
        except mod.LeaseError:
            ok("Python acquire refuses a held lease")
        else:
            bad("Python acquire accepted a held lease")
        try:
            held.acquire(takeover=True)
        except mod.LeaseError:
            ok("Python takeover without confirm is refused")
        else:
            bad("Python takeover without confirm was accepted")
        if held.acquire(takeover=True, confirm="TAKEOVER"):
            ok("Python takeover with confirm succeeds")
        else:
            bad("Python takeover with confirm failed")
    finally:
        holder.release()
        held.release()

    # After release, a control request gets 409 and raises LeaseLost.
    lease = mod.LeaseSession(api, "py-lost")
    lease.acquire()
    lease.release()
    try:
        lease.post("/api/arm", {"arm": 0})
    except mod.LeaseLost:
        ok("Python maps 409 to LeaseLost")
    except Exception as e:
        bad("Python raised the wrong exception for 409", type(e).__name__)
    else:
        bad("Python accepted a control request after release")


def test_dry_run_no_network(api, srv):
    before = srv.requests
    with tempfile.TemporaryDirectory(prefix="agilex-smolvla-dry.") as td:
        env = os.environ.copy()
        env.update({"AGILEX_API": api, "PYTHONPATH": str(ROOT / "tools")})
        p = subprocess.run(
            [sys.executable, str(RUNNER), "--dry-run", "--steps", "1",
             "--ckpt", os.path.join(td, "missing-checkpoint"), "--api", api],
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60)
    if p.returncode == 0:
        ok("run-smolvla --dry-run exits 0")
    else:
        bad("run-smolvla --dry-run exited %d" % p.returncode,
            p.stdout[-300:] + p.stderr[-300:])
    if srv.requests == before:
        ok("run-smolvla --dry-run makes no HTTP requests")
    else:
        bad("run-smolvla --dry-run contacted the API (%d requests)" % (srv.requests - before))
    if "DRY RUN: offline" in p.stdout:
        ok("run-smolvla --dry-run announces offline mode")
    else:
        bad("run-smolvla --dry-run did not print offline mode", p.stdout[-300:] + p.stderr[-300:])


def main():
    srv, api = start_server(ttl=20)
    try:
        test_shell(api, srv)
        test_python_client(api, srv)
        test_dry_run_no_network(api, srv)
    finally:
        stop_server(srv)

    srv2, api2 = start_server(ttl=6)
    try:
        test_expiry_and_heartbeat(api2)
    finally:
        stop_server(srv2)

    test_old_server()

    print()
    if FAIL:
        print("%d LEASE TEST(S) FAILED" % FAIL)
        return 1
    print("ALL LEASE TESTS PASSED (%d checks)" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
