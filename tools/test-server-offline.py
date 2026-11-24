#!/usr/bin/env python3
"""Offline tests for the remote-server security and status fixes.

No CAN, no ROS, no robot, no network beyond a loopback HTTP server bound to an
ephemeral port.  Run a subset with e.g.:

    python tools/test-server-offline.py auth status
    python tools/test-server-offline.py            # everything

The test file is deliberately independent of test-modes-offline.py: that file
exercises the mode frame encoding with a fake Piper, this one exercises the
HTTP/auth/state contracts that the browser depends on.
"""
import importlib.util
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server", "agilex-remote-server.py")
UI = os.path.join(HERE, "..", "server", "remote-ui.html")

spec = importlib.util.spec_from_file_location("srv", SERVER)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

FAIL = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class _JointState:
    joint_1 = joint_2 = joint_3 = joint_4 = joint_5 = joint_6 = 0


class _JointMsg:
    joint_state = _JointState()


class _GripperState:
    grippers_angle = 0


class _GripperMsg:
    gripper_state = _GripperState()


class _FocStatus:
    motor_overheating = False
    driver_error_status = False
    driver_overcurrent = False
    driver_overheating = False
    voltage_too_low = False
    sensor_status = False


class _Motor:
    foc_status = _FocStatus()


class _DriverStates:
    motor_1 = motor_2 = motor_3 = motor_4 = motor_5 = motor_6 = _Motor()


class _ArmStatus:
    def __init__(self, text):
        self.arm_status = text


class FakeIface:
    def __init__(self):
        self.calls = []
        self.reported_mode = "MOVE_J"
        self.status_readable = True
        self.enable_works = True
        self.disable_works = True
        self.joints_enabled = [False] * 6

    def GetArmStatus(self):
        if not self.status_readable:
            raise RuntimeError("CAN read failed")
        return _ArmStatus("Mode Feed: %s(0x1)\nArm Status: NORMAL(0x0)"
                          % self.reported_mode)

    def GetArmJointMsgs(self):
        return _JointMsg()

    def GetArmEnableStatus(self):
        return list(self.joints_enabled)

    def GetDriverStates(self):
        return _DriverStates()

    def GetArmGripperMsgs(self):
        return _GripperMsg()

    def EnableArm(self, **kw):
        self.calls.append(("EnableArm", kw))
        if self.enable_works:
            self.joints_enabled = [True] * 6

    def DisableArm(self, **kw):
        self.calls.append(("DisableArm", kw))
        if self.disable_works:
            self.joints_enabled = [False] * 6

    def EmergencyStop(self, *a):
        self.calls.append(("EmergencyStop", a))

    def MotionCtrl_2(self, **kw):
        self.calls.append(("MotionCtrl_2", kw))
        if self.status_readable and not getattr(self, "pin_mode", False):
            self.reported_mode = {0x00: "MOVE_P", 0x01: "MOVE_J",
                                  0x03: "MOVE_C"}.get(kw.get("move_mode"), "MOVE_J")

    def MotionCtrl_1(self, **kw):
        self.calls.append(("MotionCtrl_1", kw))

    def JointCtrl(self, *a):
        self.calls.append(("JointCtrl", a))

    def JointMitCtrl(self, *a):
        self.calls.append(("JointMitCtrl", a))

    def GripperCtrl_V3(self, *a):
        self.calls.append(("GripperCtrl_V3", a))

    def MasterSlaveConfig(self, **kw):
        self.calls.append(("MasterSlaveConfig", kw))

    def MoveCAxisUpdateCtrl(self, *a):
        self.calls.append(("MoveCAxisUpdateCtrl", a))

    def EndPoseCtrl(self, *a):
        self.calls.append(("EndPoseCtrl", a))


class FakeRos:
    def __init__(self, ok=True):
        self.ok = ok
        self.pub = []

    def publish(self, lin, ang):
        self.pub.append((lin, ang))

    def state(self):
        return {"connected": bool(self.ok), "mode": "commanded",
                "battery_v": 24.0, "soc_pct": None, "error_code": 0,
                "odom_x": 0.0, "odom_y": 0.0}


def make_arm():
    a = srv.ArmBridge.__new__(srv.ArmBridge)
    a.channel = "can_fake"
    a.side = "left"
    a.limits = [(-150.0, 150.0)] * 6
    a.ok = True
    a.iface = FakeIface()
    a.enabled = False
    a.firmware = "fake"
    a.error_code = 0
    a.lock = threading.Lock()
    a.joints = [0.0] * 6
    a._measured = [0.0] * 6
    a._meas_ok = True
    a._last_feedback = time.time()
    a._last_mode_check = time.time()
    a.joints_enabled = [False] * 6
    a.faults = [[] for _ in range(6)]
    a.gripper = 0.0
    a._grip_target = 0.0
    a.home_deg = [0.0] * 6
    a.mode = "pos_j"
    a.mit = {"kp": [30.0] * 6, "kd": [2.0] * 6, "t_ref": [0.0] * 6,
             "vel_ref": [0.0] * 6}
    a.teaching = False
    a.movec = {"points": [], "active": False}
    a.traj = {"name": None, "points": [], "dt": 0.05, "idx": 0,
              "playing": False, "loop": False, "speed": 1.0,
              "t_next": 0.0, "plays": 0}
    a.master_slave = {"enabled": False, "role": None}
    a.mode_actual = None
    a.mode_verified = False
    a._last_posp_sent = None
    a._stop = threading.Event()
    a.cmd_hz = 50.0
    a.feedback_hz = 5.0
    return a


def make_control(arms, ros=None):
    c = srv.Control.__new__(srv.Control)
    c.ros = ros or FakeRos()
    c.arms = list(arms)
    c.arm = c.arms[0] if c.arms else None
    c.lock = threading.Lock()
    c.lin = c.ang = 0.0
    c.last_drive = 0.0
    c.estop = False
    c.started = time.time()
    return c


class FastSleep:
    def __enter__(self):
        self._orig = srv.time.sleep
        srv.time.sleep = lambda *_: None

    def __exit__(self, *exc):
        srv.time.sleep = self._orig


# ---------------------------------------------------------------------------
# auth / session
# ---------------------------------------------------------------------------
class _AuthHandler:
    _authorised = srv.Handler._authorised
    _cookie_token = srv.Handler._cookie_token
    _from_loopback = srv.Handler._from_loopback

    def __init__(self, ip, cookie=None, path="/api/status"):
        self.client_address = (ip, 12345)
        self.headers = {"Cookie": cookie} if cookie else {}
        self.path = path
        self.command = "GET"
        self.sessions = _AuthHandler.sessions


def test_session_store_ttl():
    store = srv.SessionStore(ttl=0.05, max_sessions=4)
    token = store.create()
    check("session token is accepted before expiry", store.valid(token) is True)
    time.sleep(0.08)
    check("session token expires", store.valid(token) is False)


def test_session_store_revoke():
    store = srv.SessionStore(ttl=60, max_sessions=4)
    token = store.create()
    store.revoke(token)
    check("logout revokes the session token", store.valid(token) is False)


def test_session_store_cap():
    store = srv.SessionStore(ttl=60, max_sessions=2)
    first = store.create()
    store.create()
    store.create()
    check("session store evicts when full", store.valid(first) is False)


def test_session_cookie_flags():
    cookie = srv.session_cookie("tok", 3600)
    low = cookie.lower()
    check("cookie is HttpOnly", "httponly" in low, cookie)
    check("cookie is SameSite=Strict", "samesite=strict" in low, cookie)
    check("cookie has Max-Age", "max-age=3600" in low, cookie)
    check("clear cookie expires immediately", "max-age=0" in srv.clear_cookie().lower())


def test_authorised_cookie():
    store = srv.SessionStore(ttl=60, max_sessions=4)
    _AuthHandler.sessions = store
    token = store.create()
    old_anon = srv.ALLOW_ANON
    srv.ALLOW_ANON = False
    try:
        check("loopback is trusted without a cookie",
              _AuthHandler("127.0.0.1")._authorised() is True)
        check("non-loopback without a cookie is denied",
              _AuthHandler("198.51.100.10")._authorised() is False)
        check("valid session cookie is accepted",
              _AuthHandler("198.51.100.10", "a=1; agilex_sid=%s; b=2" % token)._authorised() is True)
        check("wrong session cookie is denied",
              _AuthHandler("198.51.100.10", "agilex_sid=wrong")._authorised() is False)
        check("?token= no longer authenticates off-loopback",
              _AuthHandler("198.51.100.10", None,
                           "/api/status?token=%s" % token)._authorised() is False)
    finally:
        srv.ALLOW_ANON = old_anon


class OffLoopbackHandler(srv.Handler):
    """Exercise the password path over a loopback socket in tests."""
    def _from_loopback(self):
        return False


def test_login_cookie_and_logout():
    old_pw = srv.PASSWORD
    srv.PASSWORD = "test-password"
    try:
        def run(port):
            st, _, body = http_json(port, "/api/login", {"password": "wrong"})
            check("wrong password is rejected off-loopback", st == 401, (st, body))

            st, hdrs, body = http_json(port, "/api/login", {"password": "test-password"})
            check("correct password logs in", st == 200 and body.get("ok") is True,
                  (st, body))
            sc = hdrs.get("Set-Cookie") or ""
            check("login sets a session cookie", "agilex_sid=" in sc, sc)
            check("login cookie is HttpOnly", "HttpOnly" in sc, sc)
            token = sc.split("agilex_sid=", 1)[1].split(";", 1)[0] if "agilex_sid=" in sc else ""
            cookie = "agilex_sid=" + token

            st, _, body = http_json(port, "/api/status", cookie=cookie)
            check("session cookie reads status", st == 200 and body.get("ok") is True,
                  (st, body))
            st, _, _ = http_json(port, "/api/status?token=" + token)
            check("session token in the URL is ignored", st == 401, st)

            st, _, _ = http_json(port, "/api/logout", {}, cookie=cookie)
            check("logout succeeds", st == 200, st)
            st, _, _ = http_json(port, "/api/status", cookie=cookie)
            check("logout revokes the session", st == 401, st)

        with_server(FakeControl(FakeArm()), run, handler=OffLoopbackHandler)
    finally:
        srv.PASSWORD = old_pw


def test_loopback_login_does_not_mint_cookie():
    def run(port):
        st, hdrs, body = http_json(port, "/api/login", {"password": "not-checked"})
        check("loopback login succeeds without the password",
              st == 200 and body.get("loopback") is True, (st, body))
        check("loopback login issues no cookie",
              not (hdrs.get("Set-Cookie") or ""), hdrs.get("Set-Cookie"))

    with_server(FakeControl(FakeArm()), run)


# ---------------------------------------------------------------------------
# status truthfulness
# ---------------------------------------------------------------------------
def test_connected_requires_fresh_feedback():
    a = make_arm()
    a._last_feedback = time.time()
    check("fresh feedback -> connected", a.state()["connected"] is True)
    a._last_feedback = time.time() - 10.0
    st = a.state()
    check("stale feedback -> not connected", st["connected"] is False, st["connected"])
    check("stale feedback is reported", st["feedback_ok"] is False, st["feedback_ok"])


def test_enabled_counts_all_motors():
    a = make_arm()
    a.enabled = True
    a.joints_enabled = [True] * 5 + [False]
    st = a.state()
    check("motors_enabled counts the live motors", st["motors_enabled"] == 5, st["motors_enabled"])
    check("enabled_all is false with 5/6", st["enabled_all"] is False, st["enabled_all"])
    a.joints_enabled = [True] * 6
    check("enabled_all is true with 6/6", a.state()["enabled_all"] is True)


def test_enabled_reports_reality():
    a = make_arm()
    a.enabled = False
    a.joints_enabled = [True, True, False, False, False, False]
    st = a.state()
    check("enabled reflects live motors even when intent is off",
          st["enabled"] is True, st["enabled"])
    check("enable_requested still reports intent",
          st.get("enable_requested") is False, st.get("enable_requested"))
    a.enabled = True
    a.joints_enabled = [False] * 6
    st = a.state()
    check("enabled is false when intent is on but no motor is live",
          st["enabled"] is False, st["enabled"])
    check("enable_requested is true", st.get("enable_requested") is True)


def test_mode_verified_needs_a_reading():
    a = make_arm()
    a.mode_actual = None
    a.mode_verified = True
    a._last_mode_check = time.time()
    check("mode_verified is false when the arm was never read",
          a.state()["mode_verified"] is False, a.state()["mode_verified"])


def test_mode_verified_goes_stale():
    a = make_arm()
    a.mode_actual = "MOVE_J"
    a.mode_verified = True
    a._last_mode_check = time.time() - 60.0
    check("a stale mode reading is not verified",
          a.state()["mode_verified"] is False, a.state()["mode_verified"])


def test_set_mode_unreadable_is_not_verified():
    a = make_arm()
    a.iface.status_readable = False
    ok = a.set_mode("pos_p")
    check("unreadable mode is not reported as success", ok is False, ok)
    check("unreadable mode is not verified", a.mode_verified is False, a.mode_verified)
    check("unreadable mode leaves mode_actual None", a.mode_actual is None, a.mode_actual)


# ---------------------------------------------------------------------------
# bounded targets
# ---------------------------------------------------------------------------
def test_absolute_target_is_lag_bounded():
    a = make_arm()
    a._measured = [0.0] * 6
    a.set_target(joints_deg=[100.0] * 6)
    check("absolute pose is limited to MAX_TARGET_LAG",
          all(abs(v) <= srv.MAX_TARGET_LAG_DEG + 1e-9 for v in a.joints), a.joints)
    a.set_target(joint=0, deg=-100.0)
    check("absolute single joint is limited to MAX_TARGET_LAG",
          a.joints[0] >= -srv.MAX_TARGET_LAG_DEG - 1e-9, a.joints[0])


def test_home_is_lag_bounded():
    a = make_arm()
    a._measured = [0.0] * 6
    a.home_deg = [100.0] * 6
    t = a.go_home()
    check("home is limited to MAX_TARGET_LAG per press",
          t is not None and all(abs(v) <= srv.MAX_TARGET_LAG_DEG + 1e-9 for v in t), t)


def test_target_rejects_non_finite():
    a = make_arm()
    check("NaN absolute target is rejected",
          a.set_target(joint=0, deg=float("nan")) is None)


# ---------------------------------------------------------------------------
# MOVE C default guard
# ---------------------------------------------------------------------------
def test_movec_disabled_by_default():
    a = make_arm()
    a.enabled = True
    old = srv.ENABLE_MOVEC
    srv.ENABLE_MOVEC = False
    try:
        r = a.run_movec([[0, 0, 100000, 0, 0, 0]] * 3)
        check("MOVE C is rejected by default", r["ok"] is False, r)
        check("MOVE C default rejection is explicit",
              "AGILEX_ENABLE_MOVEC" in r.get("error", ""), r.get("error"))
        check("MOVE C mode switch is rejected by default",
              a.set_mode("movec") is False)
    finally:
        srv.ENABLE_MOVEC = old


def test_movec_enabled_requires_enable():
    a = make_arm()
    a.enabled = False
    old = srv.ENABLE_MOVEC
    srv.ENABLE_MOVEC = True
    try:
        r = a.run_movec([[0, 0, 100000, 0, 0, 0]] * 3)
        check("MOVE C refuses a disabled arm", r["ok"] is False, r)
        check("MOVE C disabled-arm error is explicit",
              "disabled" in r.get("error", ""), r.get("error"))
    finally:
        srv.ENABLE_MOVEC = old


def test_movec_rejects_non_finite():
    a = make_arm()
    a.enabled = True
    old = srv.ENABLE_MOVEC
    srv.ENABLE_MOVEC = True
    try:
        r = a.run_movec([[float("nan"), 0, 100000, 0, 0, 0]] * 3)
        check("MOVE C rejects NaN points", r["ok"] is False, r)
    finally:
        srv.ENABLE_MOVEC = old


def test_movec_requires_live_motors():
    a = make_arm()
    a.enabled = True
    a.joints_enabled = [False] * 6
    old = srv.ENABLE_MOVEC
    srv.ENABLE_MOVEC = True
    try:
        r = a.run_movec([[0, 0, 100000, 0, 0, 0]] * 3)
        check("MOVE C refuses an arm with no live motors", r["ok"] is False, r)
    finally:
        srv.ENABLE_MOVEC = old


# ---------------------------------------------------------------------------
# enable verification and E-STOP semantics
# ---------------------------------------------------------------------------
def test_set_enabled_verifies_motors():
    with FastSleep():
        a = make_arm()
        a.iface.enable_works = True
        ok = a.set_enabled(True)
        check("enable returns true when motors come up", ok is True, ok)
        check("enable records intent", a.enabled is True)

        b = make_arm()
        b.iface.enable_works = False
        ok = b.set_enabled(True)
        check("enable returns false when no motor comes up", ok is False, ok)
        check("failed enable does not claim enabled",
              b.state()["enabled"] is False, b.state()["enabled"])

        c = make_arm()
        c.enabled = True
        c.joints_enabled = [True] * 6
        c.iface.disable_works = True
        ok = c.set_enabled(False)
        check("disable returns true when motors drop", ok is True, ok)

        d = make_arm()
        d.enabled = True
        d.joints_enabled = [True] * 6
        d.iface.joints_enabled = [True] * 6
        d.iface.disable_works = False
        ok = d.set_enabled(False)
        check("disable returns false when motors stay enabled", ok is False, ok)


def test_enable_reports_unverified_mode():
    with FastSleep():
        a = make_arm()
        a.iface.status_readable = False
        a.iface.enable_works = True
        ok = a.set_enabled(True)
        check("enable still reports motor success when mode is unreadable", ok is True, ok)
        check("enable does not claim a verified mode", a.mode_verified is False,
              a.mode_verified)
        check("state does not claim a verified mode", a.state()["mode_verified"] is False,
              a.state()["mode_verified"])


def test_estop_is_torque_off_not_reenable():
    a = make_arm()
    a.enabled = True
    a.estop()
    names = [c[0] for c in a.iface.calls]
    check("arm E-STOP clears enable intent", a.enabled is False)
    check("arm E-STOP disables all motors",
          ("DisableArm", {"motor_num": 7}) in a.iface.calls, a.iface.calls)
    check("arm E-STOP does not re-enable", "EnableArm" not in names, names)


def test_control_estop_zeros_and_latches():
    a1, a2 = make_arm(), make_arm()
    a1.enabled = a2.enabled = True
    c = make_control([a1, a2])
    c.set_estop(True)
    check("E-STOP latches", c.estop is True)
    check("E-STOP zeroes linear velocity", c.lin == 0.0, c.lin)
    check("E-STOP zeroes angular velocity", c.ang == 0.0, c.ang)
    check("E-STOP publishes a zero twist", c.ros.pub[-1] == (0.0, 0.0), c.ros.pub)
    check("E-STOP disables both arms", a1.enabled is False and a2.enabled is False)
    names = [x[0] for x in a1.iface.calls + a2.iface.calls]
    check("E-STOP calls DisableArm on both arms",
          names.count("DisableArm") == 2, names)

    c.set_estop(False)
    check("clearing E-STOP does not clear the latch",
          c.estop is False)
    check("clearing E-STOP does not re-enable arms",
          a1.enabled is False and a2.enabled is False)
    names = [x[0] for x in a1.iface.calls + a2.iface.calls]
    check("clearing E-STOP never calls EnableArm", "EnableArm" not in names, names)


# ---------------------------------------------------------------------------
# HTTP endpoint truthfulness
# ---------------------------------------------------------------------------
class FakeArm:
    def __init__(self, enabled=False, motors=None, set_enabled_result=True,
                 feedback_ok=True):
        self.set_enabled_result = set_enabled_result
        self.calls = []
        self.home_deg = [0.0] * 6
        self._state = {
            "side": "left", "can": "can_left", "connected": True,
            "enabled": bool(enabled),
            "motors_enabled": (sum(1 for x in motors if x) if motors is not None
                               else (6 if enabled else 0)),
            "enabled_all": bool(enabled and (motors is None or all(motors))),
            "mode": "pos_j", "mode_actual": "MOVE_J", "mode_verified": True,
            "feedback_ok": feedback_ok, "feedback_age_s": 0.1,
            "joints_deg": [0.0] * 6, "targets_deg": [0.0] * 6,
            "joints_enabled": list(motors) if motors is not None else [bool(enabled)] * 6,
            "faults": [[] for _ in range(6)],
            "gripper_deg": 0.0, "gripper_target_deg": 0.0,
            "error_code": 0, "home_deg": [0.0] * 6,
            "jitter": {"cmd_hz": 50.0, "feedback_hz": 5.0},
            "teaching": False, "mit": {"kp": [], "kd": [], "t_ref": []},
            "movec_active": False,
            "traj": {"name": None, "points": 0, "dt": 0.05, "idx": 0,
                     "playing": False, "loop": False, "speed": 1.0, "plays": 0},
            "master_slave": {"enabled": False, "role": None},
        }

    def set_enabled(self, on):
        self.calls.append(("set_enabled", on))
        return self.set_enabled_result

    def set_target(self, joints_deg=None, joint=None, deg=None):
        self.calls.append(("set_target", joints_deg, joint, deg))
        if joints_deg is not None:
            return [15.0] * 6
        return [15.0] * 6

    def go_home(self):
        self.calls.append(("go_home",))
        return [15.0] * 6

    def state(self):
        return dict(self._state)


class FakeControl:
    def __init__(self, arm, ros_ok=True):
        self.arm = arm
        self.ros = FakeRos(ok=ros_ok)
        self.estop = False
        self.applied = (0.0, 0.0)

    def pick_arm(self, body):
        return self.arm

    def drive(self, lin, ang):
        self.applied = (float(lin), float(ang))
        return self.applied

    def set_estop(self, on):
        self.estop = bool(on)

    def status(self):
        return {"ok": True, "estop": self.estop, "chassis": self.ros.state(),
                "arms": [self.arm.state()], "arm": self.arm.state(),
                "limits": {}, "watchdog_ms": 300}


def post_json(port, path, body):
    st, _, obj = http_json(port, path, body)
    return obj


def http_json(port, path, body=None, cookie=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            obj = json.loads(raw)
        except Exception:
            obj = {"raw": raw}
        return e.code, e.headers, obj


def with_server(control, fn, handler=None):
    h = handler or srv.Handler
    h.control = control
    h.sessions = srv.SessionStore(ttl=60, max_sessions=8)
    h.recorder = None
    h.hubs = {}
    h.ui_bytes = b"ok"
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), h)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        return fn(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_api_enable_reports_failure():
    def run(port):
        return post_json(port, "/api/arm/enable", {"arm": 0, "enable": True})
    arm = FakeArm(enabled=False, set_enabled_result=False)
    r = with_server(FakeControl(arm), run)
    check("enable endpoint returns ok=false when enable failed", r.get("ok") is False, r)
    check("enable endpoint reports the real enabled state", r.get("enabled") is False, r)
    check("enable endpoint reports motor count", r.get("motors_enabled") == 0, r)


def test_api_drive_reports_ros_failure():
    def run(port):
        return post_json(port, "/api/drive", {"lin": 0.1, "ang": 0.0})
    r = with_server(FakeControl(FakeArm(), ros_ok=False), run)
    check("drive endpoint returns ok=false when ROS is down", r.get("ok") is False, r)
    check("drive endpoint explains the failure", "ROS" in (r.get("error") or ""), r)


def test_api_lag_limited_flags():
    def run_target(port):
        return post_json(port, "/api/arm/target", {"arm": 0, "joints_deg": [100.0] * 6})

    def run_home(port):
        return post_json(port, "/api/arm/home", {"arm": 0})

    arm = FakeArm()
    rt = with_server(FakeControl(arm), run_target)
    check("target endpoint reports lag_limited", rt.get("lag_limited") is True, rt)
    check("target endpoint returns the bounded targets",
          rt.get("targets_deg") == [15.0] * 6, rt)

    rh = with_server(FakeControl(FakeArm()), run_home)
    check("home endpoint reports lag_limited", rh.get("lag_limited") is True, rh)


def test_api_estop_reports_unverified_arms():
    def run(port):
        return post_json(port, "/api/estop", {})
    arm = FakeArm(enabled=True, motors=[True] * 6)
    r = with_server(FakeControl(arm), run)
    check("E-STOP endpoint latches", r.get("estop") is True, r)
    check("E-STOP endpoint reports arms_disabled=false when torque-off is unconfirmed",
          r.get("arms_disabled") is False, r)
    check("E-STOP endpoint warns about unconfirmed torque-off",
          bool(r.get("warnings")), r)

    arm2 = FakeArm(enabled=False, motors=[False] * 6)
    r2 = with_server(FakeControl(arm2), run)
    check("E-STOP endpoint reports arms_disabled=true when motors are off",
          r2.get("arms_disabled") is True, r2)


# ---------------------------------------------------------------------------
# UI static checks (no DOM available; these guard the contract strings)
# ---------------------------------------------------------------------------
def ui_text():
    with open(UI, encoding="utf-8") as f:
        return f.read()


def test_ui_defaults_and_warnings():
    h = ui_text()
    check("UI no longer defaults to 50% speed", "speedPct: 50" not in h)
    check("UI derives the initial speed from default_lin", "default_lin" in h)
    check("UI warns that E-STOP removes torque", "torque" in h.lower())
    check("UI surfaces unconfirmed arm torque-off", "arms_disabled" in h)
    check("UI surfaces lag-limited targets", "lag_limited" in h)
    check("UI labels MOVE C as disabled/experimental", "disabled (experimental)" in h)
    check("UI surfaces mode verification", "mode_verified" in h)


GROUPS = {
    "auth": [test_session_store_ttl, test_session_store_revoke,
             test_session_store_cap, test_session_cookie_flags,
             test_authorised_cookie, test_login_cookie_and_logout,
             test_loopback_login_does_not_mint_cookie],
    "status": [test_connected_requires_fresh_feedback,
               test_enabled_counts_all_motors,
               test_enabled_reports_reality,
               test_mode_verified_needs_a_reading,
               test_mode_verified_goes_stale,
               test_set_mode_unreadable_is_not_verified],
    "lag": [test_absolute_target_is_lag_bounded, test_home_is_lag_bounded,
            test_target_rejects_non_finite],
    "movec": [test_movec_disabled_by_default, test_movec_enabled_requires_enable,
              test_movec_rejects_non_finite, test_movec_requires_live_motors],
    "estop": [test_set_enabled_verifies_motors,
              test_enable_reports_unverified_mode,
              test_estop_is_torque_off_not_reenable,
              test_control_estop_zeros_and_latches],
    "api": [test_api_enable_reports_failure, test_api_drive_reports_ros_failure,
            test_api_lag_limited_flags, test_api_estop_reports_unverified_arms],
    "ui": [test_ui_defaults_and_warnings],
}


def main():
    wanted = sys.argv[1:] or list(GROUPS)
    unknown = [g for g in wanted if g not in GROUPS]
    if unknown:
        print("unknown groups:", unknown, "available:", list(GROUPS))
        return 2
    for g in wanted:
        print("==", g, "==")
        for fn in GROUPS[g]:
            fn()
    print()
    if FAIL:
        print("%d FAILED: %s" % (len(FAIL), FAIL))
        return 1
    print("ALL SERVER OFFLINE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
