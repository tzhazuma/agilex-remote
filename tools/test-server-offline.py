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
import types
import urllib.error
import urllib.request

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "server", "agilex-remote-server.py")
UI = os.path.join(HERE, "..", "server", "remote-ui.html")

spec = importlib.util.spec_from_file_location("srv", SERVER)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# The fakes below report an all-zero joint pose. Production rejects that by
# default because it is indistinguishable from a silent bus, so opt the test
# fixtures in explicitly; the default remains covered by the dedicated check in
# test_limits_bus_and_feedback_sanity (allow_zero=False).
srv.ALLOW_ZERO_POSE = True

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
        self.joint_raw = [0] * 6
        self.raise_motionctrl_1 = False

    def GetArmStatus(self):
        if not self.status_readable:
            raise RuntimeError("CAN read failed")
        return _ArmStatus("Mode Feed: %s(0x1)\nArm Status: NORMAL(0x0)"
                          % self.reported_mode)

    def GetArmJointMsgs(self):
        js = types.SimpleNamespace(**{
            "joint_%d" % i: self.joint_raw[i - 1] for i in range(1, 7)})
        return types.SimpleNamespace(joint_state=js)

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
        if self.raise_motionctrl_1:
            raise RuntimeError("CAN write failed")

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

    def clear_fault(self):
        self.pub.append(("clear_fault",))
        return True

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
    a._last_feedback = time.monotonic()
    a._last_mode_check = time.monotonic()
    a._feedback_fault = False
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
    c.leases = srv.LeaseStore()
    c._lease_generation = c.leases.status().get("generation", 0)
    c._lease_held = False
    c._lease_controller = None
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
            check("login/logout are excluded from the control audit",
                  all(e.get("path") not in ("/api/login", "/api/logout")
                      for e in srv.AUDIT.events), srv.AUDIT.events)

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
    a._last_feedback = time.monotonic()
    check("fresh feedback -> connected", a.state()["connected"] is True)
    a._last_feedback = time.monotonic() - 10.0
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
    a._last_mode_check = time.monotonic()
    check("mode_verified is false when the arm was never read",
          a.state()["mode_verified"] is False, a.state()["mode_verified"])


def test_mode_verified_goes_stale():
    a = make_arm()
    a.mode_actual = "MOVE_J"
    a.mode_verified = True
    a._last_mode_check = time.monotonic() - 60.0
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
        check("enable is refused when mode is unreadable", ok is False, ok)
        check("enable does not claim a verified mode", a.mode_verified is False,
              a.mode_verified)
        check("enable stops output when mode is unverified", a.enabled is False,
              a.enabled)
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
        self.ok = True
        self.mode = "pos_j"
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
            "feedback_fault": False,
        }

    def feedback_ok(self):
        return bool(self._state["feedback_ok"])

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

    def nudge(self, joint, delta_deg):
        self.calls.append(("nudge", joint, delta_deg))
        return [0.0] * 6

    def nudge_gripper(self, delta_deg):
        self.calls.append(("nudge_gripper", delta_deg))
        return 0.0

    def reset(self):
        self.calls.append(("reset",))
        return True

    def set_mode(self, mode):
        self.calls.append(("set_mode", mode))
        return True

    def set_mit(self, **kw):
        self.calls.append(("set_mit", kw))
        return {"ok": True, "kp": [], "kd": [], "t_ref": []}

    def run_movec(self, points):
        self.calls.append(("run_movec", points))
        return {"ok": True, "points": points}

    def play_trajectory(self, **kw):
        self.calls.append(("play_trajectory", kw))
        return {"ok": True}

    def set_teaching(self, on, execute=False):
        self.calls.append(("set_teaching", on, execute))
        return {"ok": True, "teaching": bool(on)}

    def set_master_slave(self, on, role=None):
        self.calls.append(("set_master_slave", on, role))
        return {"ok": True, "enabled": bool(on), "role": role}

    def state(self):
        return dict(self._state)


class FakeControl:
    def __init__(self, arm, ros_ok=True):
        self.arm = arm
        self.ros = FakeRos(ok=ros_ok)
        self.estop = False
        self.applied = (0.0, 0.0)
        self.drive_calls = []
        self.leases = srv.LeaseStore()

    def pick_arm(self, body):
        return self.arm

    def drive(self, lin, ang):
        self.drive_calls.append((float(lin), float(ang)))
        self.applied = (float(lin), float(ang))
        return self.applied

    def set_estop(self, on):
        self.estop = bool(on)

    def status(self):
        return {"ok": True, "estop": self.estop, "chassis": self.ros.state(),
                "arms": [self.arm.state()], "arm": self.arm.state(),
                "limits": {}, "watchdog_ms": 300,
                "lease": self.leases.status()}


DEFAULT_LEASE = None


def post_json(port, path, body, lease="default"):
    if lease == "default":
        lease = DEFAULT_LEASE
    st, _, obj = http_json(port, path, body, lease=lease)
    return obj


def http_json(port, path, body=None, cookie=None, lease=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if body is not None else {}
    headers["Connection"] = "close"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    if cookie:
        req.add_header("Cookie", cookie)
    if lease:
        req.add_header("X-Control-Lease", lease)
    for attempt in range(2):
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
        except (ConnectionAbortedError, ConnectionResetError, urllib.error.URLError) as e:
            # Windows can abort a just-closed loopback connection; retry once.
            if attempt == 0:
                time.sleep(0.05)
                continue
            return 0, {}, {"error": "connection aborted: %s" % e}


def with_server(control, fn, handler=None):
    global DEFAULT_LEASE
    h = handler or srv.Handler
    h.control = control
    h.sessions = srv.SessionStore(ttl=60, max_sessions=8)
    h.recorder = None
    h.hubs = {}
    h.ui_bytes = b"ok"
    srv.AUDIT.clear()
    srv.AUDIT.echo = False
    if getattr(control, "leases", None) is None:
        control.leases = srv.LeaseStore()
    DEFAULT_LEASE = control.leases.acquire("test-controller")["lease"]
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
    check("UI has a control-lease pill", "leasePill" in h)
    check("UI has take/release lease buttons", "leaseTakeBtn" in h and "leaseReleaseBtn" in h)
    check("UI sends the lease header", "X-Control-Lease" in h)
    check("UI requires the takeover confirm token", "TAKEOVER" in h)
    check("UI acquires a lease on demand", "ensureLease" in h and "needsLease" in h)
    check("UI sends confirm=CLEAR for E-STOP clear", "confirm: 'CLEAR'" in h)
    check("UI shows feedback fault state", "feedback_fault" in h)


def test_lease_store_contention():
    store = srv.LeaseStore(ttl=60, clock=lambda: 0.0)
    a = store.acquire("A")
    check("first controller gets a lease", a.get("ok") is True, a)
    b = store.acquire("B")
    check("second controller is refused", b.get("ok") is False, b)
    bad = store.acquire("B", takeover=True, confirm="wrong")
    check("takeover needs the exact confirm token", bad.get("ok") is False, bad)
    good = store.acquire("B", takeover=True, confirm="TAKEOVER")
    check("confirmed takeover succeeds", good.get("ok") is True, good)
    check("takeover is marked", good.get("took_over") is True, good)
    check("old secret is invalid after takeover",
          store.check(a.get("lease")) is False)
    check("new secret is valid", store.check(good.get("lease")) is True)
    check("controller_for returns the new owner",
          store.controller_for(good.get("lease")) == "B")
    st = store.status()
    check("status has no lease secret", "lease" not in st, st)
    check("status has no hash", "hash" not in json.dumps(st), st)


def test_lease_store_expiry():
    now = [0.0]
    store = srv.LeaseStore(ttl=2.0, clock=lambda: now[0])
    r = store.acquire("A")
    now[0] = 1.0
    hb = store.heartbeat(r["lease"])
    check("heartbeat extends a live lease", hb.get("ok") is True, hb)
    now[0] = 4.0
    check("expired lease fails check", store.check(r["lease"]) is False)
    check("expired lease status is not held", store.status()["held"] is False)
    check("expired lease cannot be released", store.release(r["lease"])["ok"] is False)
    check("expired lease cannot heartbeat", store.heartbeat(r["lease"])["ok"] is False)


def test_lease_wrong_secret():
    store = srv.LeaseStore(ttl=60, clock=lambda: 0.0)
    r = store.acquire("A")
    check("wrong secret fails check", store.check("wrong") is False)
    check("wrong secret cannot release", store.release("wrong")["ok"] is False)
    check("wrong secret cannot heartbeat", store.heartbeat("wrong")["ok"] is False)
    check("holder can release", store.release(r["lease"])["ok"] is True)
    check("double release is refused", store.release(r["lease"])["ok"] is False)


def test_lease_api_gate():
    def run(port):
        st, _, obj = http_json(port, "/api/arm",
                               {"arm": 0, "joint": 0, "delta_deg": 0.1}, lease=None)
        check("control without a lease is rejected", st == 409, (st, obj))
        check("rejection names the lease", "lease" in (obj.get("error") or "").lower(),
              obj)

        st, _, obj = http_json(port, "/api/arm",
                               {"arm": 0, "joint": 0, "delta_deg": 0.1},
                               lease=DEFAULT_LEASE)
        check("control with a lease is accepted", st == 200 and obj.get("ok") is True,
              (st, obj))

        st, _, obj = http_json(port, "/api/drive", {"lin": 0.0, "ang": 0.0},
                               lease=None)
        check("zero drive is exempt from the lease", st == 200, (st, obj))
        st, _, obj = http_json(port, "/api/drive", {"lin": 0.1, "ang": 0.0},
                               lease=None)
        check("nonzero drive requires a lease", st == 409, (st, obj))
        st, _, obj = http_json(port, "/api/estop", {}, lease=None)
        check("E-STOP is exempt from the lease", st == 200 and obj.get("estop") is True,
              (st, obj))
        st, _, obj = http_json(port, "/api/estop/clear",
                               {"confirm": "CLEAR"}, lease=DEFAULT_LEASE)
        check("E-STOP clear needs the lease and confirm, then succeeds",
              st == 200 and obj.get("estop") is False, (st, obj))
        st, _, obj = http_json(port, "/api/lease/status")
        check("GET lease status is open without a lease",
              st == 200 and obj.get("held") is True, (st, obj))
        st, _, obj = http_json(port, "/api/status")
        check("GET /api/status exposes the lease state",
              st == 200 and obj.get("lease", {}).get("held") is True, (st, obj))

        st, _, obj = http_json(port, "/api/lease/acquire", {"controller": "B"})
        check("second acquire is refused while held", obj.get("ok") is False, obj)
        st, _, obj = http_json(port, "/api/lease/acquire",
                               {"controller": "B", "takeover": True,
                                "confirm": "NO"})
        check("takeover with wrong confirm is refused", obj.get("ok") is False, obj)
        st, _, obj = http_json(port, "/api/lease/acquire",
                               {"controller": "B", "takeover": True,
                                "confirm": "TAKEOVER"})
        check("takeover with confirm succeeds", obj.get("ok") is True, obj)
        new = obj.get("lease")

        st, _, obj = http_json(port, "/api/arm",
                               {"arm": 0, "joint": 0, "delta_deg": 0.1},
                               lease=DEFAULT_LEASE)
        check("old lease is invalid after takeover", st == 409, (st, obj))
        st, _, obj = http_json(port, "/api/arm",
                               {"arm": 0, "joint": 0, "delta_deg": 0.1}, lease=new)
        check("new lease is accepted", st == 200 and obj.get("ok") is True, (st, obj))
        st, _, obj = http_json(port, "/api/lease/release", {}, lease=DEFAULT_LEASE)
        check("old holder cannot release", obj.get("ok") is False, obj)
        st, _, obj = http_json(port, "/api/lease/release", {}, lease=new)
        check("new holder can release", obj.get("ok") is True, obj)
        blob = json.dumps(srv.AUDIT.events)
        check("acquired lease secret is redacted from the audit",
              new not in blob and DEFAULT_LEASE not in blob)
        check("lease acquire is audited",
              any(e.get("path") == "/api/lease/acquire" for e in srv.AUDIT.events))

    with_server(FakeControl(FakeArm()), run)


def test_audit_redaction():
    body = srv.summarize_body("/api/arm", {
        "arm": 0, "joint": 1, "delta_deg": 0.5,
        "password": "PW", "lease": "LEASE", "token": "TOK", "cookie": "COOKIE"})
    text = json.dumps(body)
    check("audit body keeps whitelisted fields",
          body.get("arm") == 0 and body.get("joint") == 1 and body.get("delta_deg") == 0.5,
          body)
    for secret in ("PW", "LEASE", "TOK", "COOKIE"):
        check("audit body drops " + secret, secret not in text, text)
    result = srv.summarize_result({"ok": True, "lease": "LEASE", "error": None}, 200)
    check("audit result redacts the lease secret",
          "LEASE" not in json.dumps(result) and result.get("lease") == "<redacted>",
          result)

    def run(port):
        secret = DEFAULT_LEASE
        st, _, obj = http_json(port, "/api/arm", {
            "arm": 0, "joint": 0, "delta_deg": 0.1, "controller": "auditor",
            "password": "PW", "token": "TOK", "cookie": "COOKIE",
        }, lease=secret)
        check("audited control succeeds", st == 200 and obj.get("ok") is True, (st, obj))
        blob = json.dumps(srv.AUDIT.events)
        check("audit log never contains the lease secret", secret not in blob)
        for value in ("PW", "TOK", "COOKIE"):
            check("audit log never contains " + value, value not in blob)
        events = [e for e in srv.AUDIT.events if e.get("path") == "/api/arm"]
        check("control endpoint was audited", bool(events), srv.AUDIT.events)
        if events:
            ev = events[-1]
            check("audit timestamp is UTC", ev.get("ts", "").endswith("Z"), ev)
            check("audit records client addr", ev.get("client") == "127.0.0.1", ev)
            check("audit controller is the lease holder",
                  ev.get("controller") == "test-controller", ev)
            check("audit marks the holder verified",
                  ev.get("controller_verified") is True, ev)
            check("audit keeps the body controller separate",
                  (ev.get("body") or {}).get("body_controller") == "auditor", ev)
            check("audit body has no password",
                  "password" not in json.dumps(ev.get("body")), ev)
        # an exempt endpoint without a lease is attributed but not verified
        http_json(port, "/api/estop", {"controller": "victim"}, lease=None)
        ev = [e for e in srv.AUDIT.events if e.get("path") == "/api/estop"][-1]
        check("exempt endpoint is marked controller_verified=false",
              ev.get("controller_verified") is False, ev)
        check("exempt endpoint keeps the claimed controller for context",
              ev.get("controller") == "victim", ev)

    with_server(FakeControl(FakeArm()), run)


def test_estop_rejects_motion_endpoints():
    arm = FakeArm()
    control = FakeControl(arm)
    control.estop = True
    cases = [
        ("/api/arm", {"arm": 0, "joint": 0, "delta_deg": 0.1}),
        ("/api/arm/target", {"arm": 0, "joint": 0, "deg": 1.0}),
        ("/api/arm/enable", {"arm": 0, "enable": True}),
        ("/api/arm/reset", {"arm": 0}),
        ("/api/arm/home", {"arm": 0}),
        ("/api/gripper", {"arm": 0, "delta_deg": 1.0}),
        ("/api/arm/mode", {"arm": 0, "mode": "pos_p"}),
        ("/api/arm/mit", {"arm": 0, "kp": 30.0}),
        ("/api/arm/movec", {"arm": 0, "points": [[0] * 6] * 3}),
        ("/api/traj/load", {"arm": 0, "name": "x", "points": [[0] * 6] * 2}),
        ("/api/traj/load-file", {"arm": 0, "path": "x"}),
        ("/api/traj/play", {"arm": 0}),
        ("/api/teach", {"arm": 0, "on": True}),
        ("/api/master-slave", {"on": True}),
        ("/api/chassis/clear-fault", {}),
    ]

    def run(port):
        for path, body in cases:
            st, _, obj = http_json(port, path, body, lease=DEFAULT_LEASE)
            check("E-STOP rejects " + path, st == 423, (st, obj))
        check("E-STOP rejection made no arm calls", arm.calls == [], arm.calls)
        st, _, obj = http_json(port, "/api/drive", {"lin": 0.0, "ang": 0.0},
                               lease=None)
        check("E-STOP still allows zero drive", st == 200, (st, obj))
        st, _, obj = http_json(port, "/api/drive", {"lin": 0.1, "ang": 0.0},
                               lease=DEFAULT_LEASE)
        check("E-STOP rejects non-zero drive", st == 423, (st, obj))
        st, _, obj = http_json(port, "/api/estop", {}, lease=None)
        check("E-STOP itself needs no lease",
              st == 200 and obj.get("estop") is True, (st, obj))
        st, _, obj = http_json(port, "/api/estop/clear", {}, lease=None)
        check("E-STOP clear without lease is rejected", st == 409, (st, obj))
        st, _, obj = http_json(port, "/api/estop/clear", {"confirm": "WRONG"},
                               lease=DEFAULT_LEASE)
        check("E-STOP clear with wrong confirm is rejected", st == 409, (st, obj))
        st, _, obj = http_json(port, "/api/estop/clear", {"confirm": "CLEAR"},
                               lease=DEFAULT_LEASE)
        check("E-STOP clear with lease+confirm succeeds",
              st == 200 and obj.get("estop") is False, (st, obj))
        check("E-STOP clear did not enable arms",
              all(c[0] != "set_enabled" for c in arm.calls), arm.calls)
        ev = [e for e in srv.AUDIT.events
              if e.get("path") == "/api/estop/clear" and e["result"].get("ok")]
        check("E-STOP clear is audited with confirm",
              bool(ev) and ev[-1]["body"].get("confirm") == "CLEAR", ev)

    with_server(control, run)


def test_estop_cancels_continuous_motion():
    a = make_arm()
    a.enabled = True
    a.traj["playing"] = True
    a.teaching = True
    a.master_slave = {"enabled": True, "role": "leader"}
    a.movec = {"points": [[0] * 6] * 3, "active": True}
    a.joints = [5.0] * 6
    a._measured = [0.0] * 6
    a.estop()
    names = [c[0] for c in a.iface.calls]
    check("E-STOP clears trajectory", a.traj["playing"] is False)
    check("E-STOP clears teaching", a.teaching is False)
    check("E-STOP clears master/slave", a.master_slave["enabled"] is False)
    check("E-STOP clears MOVE C", a.movec["active"] is False)
    check("E-STOP resyncs target to measured", a.joints == [0.0] * 6, a.joints)
    check("E-STOP disables the arm", a.enabled is False)
    check("E-STOP still calls DisableArm", "DisableArm" in names, names)
    check("E-STOP sends master/slave OFF", "MasterSlaveConfig" in names, names)
    check("E-STOP sends teach stop", "MotionCtrl_1" in names, names)
    check("E-STOP never re-enables", "EnableArm" not in names, names)


def test_feedback_fault_stops_hardware_modes():
    a = make_arm()
    a.enabled = True
    a.teaching = True
    a.master_slave = {"enabled": True, "role": "leader"}
    a.movec = {"points": [], "active": True}
    a._latch_feedback_fault("sensor failure")
    names = [c[0] for c in a.iface.calls]
    m1 = [c[1] for c in a.iface.calls if c[0] == "MotionCtrl_1"]
    ms = [c[1] for c in a.iface.calls if c[0] == "MasterSlaveConfig"]
    check("fault ends drag teach with the documented stop frame",
          {"emergency_stop": 0, "track_ctrl": 0, "grag_teach_ctrl": 2} in m1, m1)
    check("fault unlinks master/slave",
          any(x.get("linkage_config") == 0 for x in ms), ms)
    check("fault aborts MOVE C with MotionCtrl_1 emergency stop",
          any(x.get("emergency_stop") == 1 for x in m1), m1)
    check("fault does not drop torque with DisableArm",
          "DisableArm" not in names, names)
    check("fault disables our output", a.enabled is False)


def test_movec_stop_and_fallback():
    a = make_arm()
    a.enabled = True
    a.movec = {"points": [], "active": True}
    a.cancel_motion_hold("test")
    names = [c[0] for c in a.iface.calls]
    m1 = [c[1] for c in a.iface.calls if c[0] == "MotionCtrl_1"]
    check("MOVE C stop uses MotionCtrl_1 emergency stop",
          {"emergency_stop": 1, "track_ctrl": 0, "grag_teach_ctrl": 0} in m1, m1)
    check("working MOVE C stop does not DisableArm", "DisableArm" not in names,
          names)

    b = make_arm()
    b.enabled = True
    b.movec = {"points": [], "active": True}
    b.iface.raise_motionctrl_1 = True
    b.cancel_motion_hold("test")
    names = [c[0] for c in b.iface.calls]
    check("MOVE C stop frame was attempted", "MotionCtrl_1" in names, names)
    check("failed MOVE C stop falls back to DisableArm", "DisableArm" in names,
          names)

    c = make_arm()
    c.enabled = True
    c.teaching = True
    c.master_slave = {"enabled": True, "role": "leader"}
    c.movec = {"points": [], "active": True}
    c.iface.raise_motionctrl_1 = True
    c.cancel_motion_hold("test")
    names = [x[0] for x in c.iface.calls]
    check("failed teach stop still unlinks master/slave",
          "MasterSlaveConfig" in names, names)
    check("failed teach stop still attempts the MOVE C stop",
          names.count("MotionCtrl_1") == 2, names)
    check("failed MOVE C stop falls back to DisableArm", "DisableArm" in names,
          names)


def test_plain_move_j_fault_keeps_torque():
    a = make_arm()
    a.enabled = True
    a.traj["playing"] = True
    a._latch_feedback_fault("sensor failure")
    names = [c[0] for c in a.iface.calls]
    check("plain MOVE J fault stops the trajectory", a.traj["playing"] is False)
    check("plain MOVE J fault sends no hardware stop frame",
          "MotionCtrl_1" not in names and "MasterSlaveConfig" not in names, names)
    check("plain MOVE J fault keeps torque", "DisableArm" not in names, names)


def test_lease_generation_and_monotonic():
    now = [0.0]
    store = srv.LeaseStore(ttl=5.0, clock=lambda: now[0])
    g0 = store.status()["generation"]
    store.acquire("A")
    g1 = store.status()["generation"]
    check("acquire increments lease generation", g1 == g0 + 1, (g0, g1))
    store.acquire("B", takeover=True, confirm="TAKEOVER")
    check("takeover increments lease generation",
          store.status()["generation"] == g1 + 1)
    now[0] = 6.0
    st = store.status()
    check("expiry increments lease generation",
          st["held"] is False and st["generation"] == g1 + 2, st)
    default = srv.LeaseStore()
    check("lease clock is monotonic",
          getattr(default.clock, "__name__", "") == "monotonic")


def test_control_lease_loss_cancels_motion():
    now = [0.0]
    store = srv.LeaseStore(ttl=5.0, clock=lambda: now[0])
    a1, a2 = make_arm(), make_arm()
    a1.enabled = a2.enabled = True
    c = make_control([a1, a2])
    c.leases = store
    store.acquire("A")
    c._lease_generation = store.status()["generation"]
    c._lease_held = True
    a1.traj["playing"] = True
    a1.teaching = True
    a1.master_slave = {"enabled": True, "role": "leader"}
    a1.movec = {"points": [], "active": True}
    now[0] = 6.0
    c._lease_check()
    names = [x[0] for x in a1.iface.calls]
    m1 = [x[1] for x in a1.iface.calls if x[0] == "MotionCtrl_1"]
    ms = [x[1] for x in a1.iface.calls if x[0] == "MasterSlaveConfig"]
    check("lease expiry cancels trajectory", a1.traj["playing"] is False)
    check("lease expiry cancels teaching", a1.teaching is False)
    check("lease expiry cancels master/slave", a1.master_slave["enabled"] is False)
    check("lease expiry cancels MOVE C", a1.movec["active"] is False)
    check("lease expiry sends the teach stop frame",
          {"emergency_stop": 0, "track_ctrl": 0, "grag_teach_ctrl": 2} in m1, m1)
    check("lease expiry unlinks master/slave",
          any(x.get("linkage_config") == 0 for x in ms), ms)
    check("lease expiry stops MOVE C on hardware",
          any(x.get("emergency_stop") == 1 for x in m1), m1)
    check("lease expiry holds instead of disabling", a1.enabled is True)
    check("lease expiry does not DisableArm", "DisableArm" not in names, names)
    check("lease expiry zeroes drive", c.lin == 0.0 and c.ang == 0.0)
    check("lease expiry publishes zero twist", c.ros.pub[-1] == (0.0, 0.0),
          c.ros.pub)

    store.acquire("A")
    c._lease_check()          # free -> held: must NOT cancel
    store.acquire("B", takeover=True, confirm="TAKEOVER")
    a1.traj["playing"] = True
    c._lease_check()
    check("takeover cancels the old trajectory", a1.traj["playing"] is False)
    check("takeover also holds, not disables", a1.enabled is True)


def test_first_lease_acquire_does_not_cancel_motion():
    store = srv.LeaseStore(ttl=5.0)
    a = make_arm()
    a.enabled = True
    c = make_control([a])
    c.leases = store
    lease = store.acquire("A")["lease"]
    a.traj["playing"] = True
    a.teaching = True
    a.master_slave = {"enabled": True, "role": "leader"}
    a.movec = {"points": [], "active": True}
    c._lease_check()
    check("first acquire keeps a running trajectory", a.traj["playing"] is True)
    check("first acquire keeps drag teach", a.teaching is True)
    check("first acquire keeps master/slave", a.master_slave["enabled"] is True)
    check("first acquire keeps MOVE C", a.movec["active"] is True)
    check("first acquire does not zero the robot", c.ros.pub == [], c.ros.pub)
    store.release(lease)
    c._lease_check()
    check("release still cancels the trajectory", a.traj["playing"] is False)
    check("release still zeroes the chassis", c.ros.pub[-1] == (0.0, 0.0),
          c.ros.pub)


def test_feedback_stale_blocks_motion():
    a = make_arm()
    a._meas_ok = False
    a._last_feedback = time.monotonic() - 10.0
    a.joints = [0.0] * 6
    a._grip_target = 0.0
    check("stale feedback blocks nudge", a.nudge(0, 1.0) is None)
    check("stale feedback blocks absolute target",
          a.set_target(joint=0, deg=10.0) is None)
    check("stale feedback blocks home", a.go_home() is None)
    check("stale feedback blocks gripper", a.nudge_gripper(1.0) is None)
    check("stale feedback blocks trajectory play",
          a.play_trajectory()["ok"] is False)
    check("stale feedback blocks teach",
          a.set_teaching(True)["ok"] is False)
    check("stale feedback blocks master/slave",
          a.set_master_slave(True, "leader")["ok"] is False)
    check("stale feedback leaves the target unchanged", a.joints == [0.0] * 6)
    a._latch_feedback_fault("test")
    check("feedback fault stops output", a.enabled is False)
    check("feedback fault is visible", a.state()["feedback_fault"] is True)
    check("feedback fault does not DisableArm",
          "DisableArm" not in [c[0] for c in a.iface.calls])


def test_feedback_gate_returns_409():
    arm = FakeArm(feedback_ok=False)
    control = FakeControl(arm)
    cases = [
        ("/api/arm", {"arm": 0, "joint": 0, "delta_deg": 1.0}),
        ("/api/arm/target", {"arm": 0, "joint": 0, "deg": 1.0}),
        ("/api/arm/home", {"arm": 0}),
        ("/api/gripper", {"arm": 0, "delta_deg": 1.0}),
        ("/api/arm/mode", {"arm": 0, "mode": "pos_j"}),
        ("/api/arm/mit", {"arm": 0, "kp": 30}),
        ("/api/arm/movec", {"arm": 0, "points": [[0] * 6] * 3}),
        ("/api/teach", {"arm": 0, "on": True}),
        ("/api/master-slave", {"on": True, "leader": 0, "follower": 1}),
    ]

    def run(port):
        for path, body in cases:
            st, _, obj = http_json(port, path, body, lease=DEFAULT_LEASE)
            check("stale feedback gate 409 on " + path, st == 409, (st, obj))
        check("stale feedback gate reaches no arm", arm.calls == [], arm.calls)
        st, _, obj = http_json(port, "/api/teach", {"arm": 0, "on": False},
                               lease=DEFAULT_LEASE)
        check("stale feedback still allows leaving teach", st == 200, (st, obj))

    with_server(control, run)


def test_state_is_json_safe_with_bad_feedback():
    a = make_arm()
    a._measured = [float("nan")] + [1.0] * 5
    st = a.state()
    check("non-finite measured pose is serialized as null",
          st["joints_deg"][0] is None and st["connected"] is False,
          st["joints_deg"])
    try:
        json.dumps(st, allow_nan=False)
        safe = True
    except ValueError:
        safe = False
    check("state has no bare NaN tokens", safe)


def test_feedback_stale_stops_loop_output():
    a = make_arm()
    a.enabled = True
    a._meas_ok = False
    a._last_feedback = time.monotonic() - 10.0
    t = threading.Thread(target=a._loop, daemon=True)
    t.start()
    time.sleep(0.12)
    a._stop.set()
    t.join(timeout=1.0)
    names = [c[0] for c in a.iface.calls]
    for forbidden in ("JointCtrl", "GripperCtrl_V3", "JointMitCtrl", "MotionCtrl_2"):
        check("stale feedback stops " + forbidden, forbidden not in names, names)
    check("stale feedback leaves the loop disabled", a.enabled is False)


def test_teaching_and_master_do_not_reassert_mode():
    a = make_arm()
    a.enabled = True
    a.teaching = True
    a._last_mode_check = 0.0
    t = threading.Thread(target=a._loop, daemon=True)
    t.start()
    time.sleep(0.12)
    a._stop.set()
    t.join(timeout=1.0)
    names = [c[0] for c in a.iface.calls]
    check("teaching does not re-assert MotionCtrl_2", "MotionCtrl_2" not in names,
          names)
    check("teaching sends no JointCtrl", "JointCtrl" not in names, names)


def test_nan_endpoints_return_400_without_side_effects():
    arm = FakeArm()
    control = FakeControl(arm)
    cases = [
        ("/api/drive", {"lin": float("nan"), "ang": 0.0}),
        ("/api/arm", {"arm": 0, "joint": 0, "delta_deg": float("inf")}),
        ("/api/gripper", {"arm": 0, "delta_deg": float("nan")}),
        ("/api/arm/target", {"arm": 0, "joints_deg": [float("nan")] * 6}),
        ("/api/traj/load", {"arm": 0, "name": "x",
                            "points": [[float("nan")] * 6] * 2}),
        ("/api/arm/mit", {"arm": 0, "kp": float("nan")}),
        ("/api/arm/movec", {"arm": 0, "points": [[float("nan")] * 6] * 3}),
        ("/api/traj/play", {"arm": 0, "speed": float("nan")}),
        ("/api/master-slave", {"on": True, "leader": float("nan")}),
    ]

    def run(port):
        for path, body in cases:
            st, _, obj = http_json(port, path, body, lease=DEFAULT_LEASE)
            check("NaN input rejected by " + path, st == 400, (st, obj))
        check("NaN input made no arm calls", arm.calls == [], arm.calls)
        check("NaN input made no drive calls", control.drive_calls == [],
              control.drive_calls)

    with_server(control, run)


def test_mit_gains_are_bounded():
    a = make_arm()
    a.mit["kp"] = [30.0] * 6
    a.mit["kd"] = [2.0] * 6
    a.mit["t_ref"] = [0.0] * 6
    before = {k: list(v) for k, v in a.mit.items()}
    bad = [
        {"kp": -1}, {"kp": 1e308}, {"kp": float("nan")}, {"kp": []},
        {"kp": True}, {"kp": [30.0, float("inf")]},
        {"kd": -0.5}, {"kd": 1e308}, {"t_ref": 100}, {"t_ref": -100},
        {"t_ref": float("nan")},
    ]
    for kwargs in bad:
        r = a.set_mit(**kwargs)
        check("set_mit rejects " + repr(kwargs), r.get("ok") is False, r)
    check("rejected MIT gains leave the state untouched", a.mit == before, a.mit)
    r = a.set_mit(kp=30, kd=2, t_ref=0)
    check("in-box MIT gains accepted",
          r.get("ok") is True and r["kp"] == [30.0] * 6
          and r["kd"] == [2.0] * 6 and r["t_ref"] == [0.0] * 6, r)
    r = a.set_mit(kp=[1, 2, 3])
    check("short MIT list keeps the existing tail",
          r["kp"] == [1.0, 2.0, 3.0, 30.0, 30.0, 30.0], r)


def test_mit_and_body_types_over_http():
    arm = FakeArm()
    control = FakeControl(arm)

    def run(port):
        before = list(arm.calls)
        for body in ({"arm": 0, "kp": -1}, {"arm": 0, "kp": 1e308},
                     {"arm": 0, "kd": -1}, {"arm": 0, "t_ref": 100},
                     {"arm": 0, "kp": float("nan")},
                     {"arm": 0, "kp": [30, float("inf")]}):
            st, _, obj = http_json(port, "/api/arm/mit", body,
                                   lease=DEFAULT_LEASE)
            check("MIT bounds return 400 for " + repr(body), st == 400, (st, obj))
        check("rejected MIT gains never reach the arm", arm.calls == before,
              arm.calls)
        st, _, obj = http_json(port, "/api/arm/mit",
                               {"arm": 0, "kp": 30, "kd": 2, "t_ref": 0},
                               lease=DEFAULT_LEASE)
        check("valid MIT gains succeed over HTTP",
              st == 200 and obj.get("ok") is True, (st, obj))
        after_mit = len(arm.calls)
        for path in ("/api/arm", "/api/teach", "/api/drive",
                     "/api/lease/acquire", "/api/login"):
            st, _, obj = http_json(port, path, [1, 2, 3],
                                   lease=DEFAULT_LEASE)
            check("non-object body returns 400 on " + path, st == 400, (st, obj))
        check("non-object body reaches no endpoint", len(arm.calls) == after_mit,
              arm.calls)
        check("non-object body is audited by type",
              any(e.get("path") == "/api/arm"
                  and e.get("body", {}).get("type") == "list"
                  for e in srv.AUDIT.events), srv.AUDIT.events[-4:])

    with_server(control, run)


def test_limits_bus_and_feedback_sanity():
    good = [(-150, 150), (0, 180), (-170, 0), (-100, 100), (-70, 70), (-120, 120)]
    check("valid limits accepted", srv.validate_limits(good) is True)
    bad = list(good)
    bad[0] = (180, 0)
    check("reversed limits rejected", srv.validate_limits(bad) is False)
    check("NaN limits rejected", srv.validate_limits([(0, float("nan"))] * 6) is False)
    check("unreadable CAN RX fails closed", srv.bus_probe_ok(None, 10) is False)
    check("non-increasing CAN RX fails closed", srv.bus_probe_ok(10, 10) is False)
    check("increasing CAN RX passes", srv.bus_probe_ok(10, 11) is True)
    check("all-zero feedback rejected by default",
          srv.feedback_sane([0.0] * 6, allow_zero=False) is False)
    check("all-zero feedback accepted only on explicit opt-in",
          srv.feedback_sane([0.0] * 6, allow_zero=True) is True)
    check("NaN feedback rejected", srv.feedback_sane([float("nan")] * 6) is False)
    check("Inf feedback rejected", srv.feedback_sane([float("inf")] * 6) is False)
    check("normal feedback accepted", srv.feedback_sane([1.0] * 6) is True)
    a = make_arm()
    a.limits = list(good)
    a._measured = [0.0] * 6
    a._measured[1] = -100.0
    check("lag clamp never reverses a limit interval",
          a._clamp_target_to_lag(1, 10.0) == 0.0,
          a._clamp_target_to_lag(1, 10.0))


def test_drive_watchdog_monotonic_and_nan():
    c = make_control([])
    c.last_drive = 1000.0
    check("drive watchdog is not stale at 300 ms", c._drive_stale(1300.0) is False)
    check("drive watchdog is stale at 301 ms", c._drive_stale(1301.0) is True)
    check("non-finite drive rejected", c.drive(float("nan"), 0.0) is None)
    applied = c.drive(0.1, 0.0)
    check("finite drive accepted", applied == (0.1, 0.0), applied)
    check("drive timestamp uses monotonic",
          abs(c.last_drive - time.monotonic() * 1000.0) < 1000.0,
          c.last_drive)


def test_audit_drive_sampling():
    log = srv.AuditLog()
    log.echo = False
    check("first drive audit sample allowed", log.allow_drive_sample(0.0, 1.0) is True)
    check("second drive audit sample suppressed",
          log.allow_drive_sample(0.5, 1.0) is False)
    check("drive audit sample after interval allowed",
          log.allow_drive_sample(1.1, 1.0) is True)


def test_mode_rollback_disables_output():
    a = make_arm()
    a.enabled = True
    a.mode = "pos_p"
    a.iface.reported_mode = "MOVE_P"
    a.iface.status_readable = False
    ok = a.set_mode("pos_j")
    check("unreadable mode is rejected", ok is False, ok)
    check("unreadable mode rolls bookkeeping back", a.mode == "pos_p", a.mode)
    check("unreadable mode stops output", a.enabled is False, a.enabled)
    check("unreadable mode stays unverified", a.mode_verified is False)

    b = make_arm()
    b.enabled = True
    b.mode = "pos_p"
    b.iface.reported_mode = "MOVE_P"
    b.iface.status_readable = True
    b.iface.pin_mode = True
    ok = b.set_mode("pos_j")
    check("refused mode is rejected", ok is False, ok)
    check("refused mode rolls bookkeeping back", b.mode == "pos_p", b.mode)
    check("refused mode stops output", b.enabled is False, b.enabled)


def test_feedback_recovery_requires_enable():
    a = make_arm()
    a._latch_feedback_fault("test")
    a._meas_ok = True
    a._last_feedback = time.monotonic()
    check("recovered feedback still blocks nudge until reset/enable",
          a.nudge(0, 0.5) is None)
    with FastSleep():
        ok = a.set_enabled(True)
    check("explicit enable clears the feedback fault", ok is True, ok)
    check("feedback fault flag cleared", a._feedback_fault is False)
    check("nudge works after explicit enable", a.nudge(0, 0.5) is not None)


def test_runtime_feedback_rejects_bad_samples():
    old = srv.ALLOW_ZERO_POSE
    try:
        srv.ALLOW_ZERO_POSE = False
        a = make_arm()
        a.enabled = True
        a._meas_ok = True
        a._last_feedback = time.monotonic()
        for label, values in (
                ("all-zero", [0.0] * 6),
                ("NaN", [float("nan")] + [1.0] * 5),
                ("Inf", [float("inf")] + [1.0] * 5)):
            a._measured = list(values)
            check(label + " pose makes feedback not ok",
                  a.feedback_ok() is False)
            check(label + " pose blocks nudge", a.nudge(0, 1.0) is None)
            check(label + " pose blocks absolute target",
                  a.set_target(joint=0, deg=10.0) is None)
            check(label + " pose blocks home", a.go_home() is None)
            check(label + " pose blocks gripper", a.nudge_gripper(1.0) is None)
            check(label + " pose blocks trajectory play",
                  a.play_trajectory()["ok"] is False)
            check(label + " pose blocks teach",
                  a.set_teaching(True)["ok"] is False)
            check(label + " pose blocks master/slave",
                  a.set_master_slave(True, "leader")["ok"] is False)
        a._measured = [1.0] * 6
        check("finite non-zero pose makes feedback ok", a.feedback_ok() is True)
    finally:
        srv.ALLOW_ZERO_POSE = old


def test_zero_pose_opt_in_is_explicit():
    old = srv.ALLOW_ZERO_POSE
    try:
        srv.ALLOW_ZERO_POSE = True
        a = make_arm()
        a.enabled = True
        a._measured = [0.0] * 6
        a._meas_ok = True
        a._last_feedback = time.monotonic()
        check("opted-in zero pose counts as fresh feedback",
              a.feedback_ok() is True)
        check("opted-in zero pose allows a nudge", a.nudge(0, 1.0) is not None)
    finally:
        srv.ALLOW_ZERO_POSE = old


def test_read_measured_latches_bad_samples():
    old = srv.ALLOW_ZERO_POSE
    try:
        srv.ALLOW_ZERO_POSE = False
        a = make_arm()
        a.enabled = True
        a.iface.joint_raw = [0] * 6
        a._read_measured()
        check("all-zero read marks feedback bad", a._meas_ok is False)
        check("all-zero read latches a feedback fault", a._feedback_fault is True)
        check("all-zero read disables output", a.enabled is False)
        check("all-zero read sends no DisableArm",
              "DisableArm" not in [c[0] for c in a.iface.calls], a.iface.calls)

        b = make_arm()
        b.iface.joint_raw = [float("nan")] * 6
        b._read_measured()
        check("NaN read marks feedback bad", b._meas_ok is False)
        check("NaN read latches a feedback fault", b._feedback_fault is True)

        c = make_arm()
        c.iface.joint_raw = [float("inf")] + [1000] * 5
        c._read_measured()
        check("Inf read marks feedback bad", c._meas_ok is False)
        check("Inf read latches a feedback fault", c._feedback_fault is True)

        d = make_arm()
        d.iface.joint_raw = [0] * 6
        d._read_measured(latch=False)
        check("startup read with a bad sample does not latch",
              d._feedback_fault is False)
    finally:
        srv.ALLOW_ZERO_POSE = old


def test_drive_audit_sampling_and_zero_priority():
    def run(port):
        for _ in range(3):
            http_json(port, "/api/drive", {"lin": 0.1, "ang": 0.0},
                      lease=DEFAULT_LEASE)
        nonzero = [e for e in srv.AUDIT.events if e.get("path") == "/api/drive"]
        http_json(port, "/api/drive", {"lin": 0.0, "ang": 0.0},
                  lease=DEFAULT_LEASE)
        all_drive = [e for e in srv.AUDIT.events if e.get("path") == "/api/drive"]
        check("successful non-zero drive is sampled", len(nonzero) < 3,
              len(nonzero))
        check("zero drive is always audited", len(all_drive) > len(nonzero),
              (len(nonzero), len(all_drive)))

    with_server(FakeControl(FakeArm()), run)


def test_arm_rejects_invalid_limits_at_init():
    a = srv.ArmBridge("can_fake", [(180, 0)] * 6)
    check("invalid limits are rejected at init", a.ok is False, a.ok)
    check("invalid limits adopt no home pose", a.home_deg == [0.0] * 6, a.home_deg)


def test_can_counter_unreadable_fails_init():
    old_bus = srv.bus_rx_packets
    old_sleep = srv.time.sleep
    old_mod = sys.modules.get("piper_sdk")

    class StubIface:
        def __init__(self, *a, **k):
            pass

        def ConnectPort(self, **k):
            pass

    stub = types.ModuleType("piper_sdk")
    stub.C_PiperInterface_V3 = StubIface
    try:
        sys.modules["piper_sdk"] = stub
        srv.bus_rx_packets = lambda iface: None
        srv.time.sleep = lambda *_: None
        a = srv.ArmBridge("can_fake", [(-150, 150)] * 6)
        check("unreadable CAN RX counter fails closed at init", a.ok is False, a.ok)
        check("unreadable CAN RX adopts no home pose", a.home_deg == [0.0] * 6,
              a.home_deg)
    finally:
        srv.bus_rx_packets = old_bus
        srv.time.sleep = old_sleep
        if old_mod is None:
            sys.modules.pop("piper_sdk", None)
        else:
            sys.modules["piper_sdk"] = old_mod


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
               test_set_mode_unreadable_is_not_verified,
               test_feedback_stale_blocks_motion,
               test_feedback_gate_returns_409,
               test_feedback_stale_stops_loop_output,
               test_state_is_json_safe_with_bad_feedback,
               test_runtime_feedback_rejects_bad_samples,
               test_zero_pose_opt_in_is_explicit,
               test_read_measured_latches_bad_samples,
               test_teaching_and_master_do_not_reassert_mode,
               test_mode_rollback_disables_output,
               test_feedback_recovery_requires_enable],
    "lag": [test_absolute_target_is_lag_bounded, test_home_is_lag_bounded,
            test_target_rejects_non_finite],
    "movec": [test_movec_disabled_by_default, test_movec_enabled_requires_enable,
              test_movec_rejects_non_finite, test_movec_requires_live_motors],
    "estop": [test_set_enabled_verifies_motors,
              test_enable_reports_unverified_mode,
              test_estop_is_torque_off_not_reenable,
              test_control_estop_zeros_and_latches,
              test_estop_rejects_motion_endpoints,
              test_estop_cancels_continuous_motion,
              test_feedback_fault_stops_hardware_modes,
              test_movec_stop_and_fallback,
              test_plain_move_j_fault_keeps_torque],
    "api": [test_api_enable_reports_failure, test_api_drive_reports_ros_failure,
            test_api_lag_limited_flags, test_api_estop_reports_unverified_arms],
    "lease": [test_lease_store_contention, test_lease_store_expiry,
              test_lease_wrong_secret, test_lease_api_gate,
              test_lease_generation_and_monotonic,
              test_control_lease_loss_cancels_motion,
              test_first_lease_acquire_does_not_cancel_motion],
    "inputs": [test_nan_endpoints_return_400_without_side_effects,
               test_mit_gains_are_bounded,
               test_mit_and_body_types_over_http,
               test_limits_bus_and_feedback_sanity,
               test_arm_rejects_invalid_limits_at_init,
               test_can_counter_unreadable_fails_init,
               test_drive_watchdog_monotonic_and_nan],
    "audit": [test_audit_redaction, test_audit_drive_sampling,
              test_drive_audit_sampling_and_zero_priority],
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
