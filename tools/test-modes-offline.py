#!/usr/bin/env python3
"""Unit-test the arm operating modes against a FAKE Piper interface.

Runs anywhere: a stub `piper_sdk` is injected before importing the server, so no
CAN traffic is generated and no hardware is touched. What this proves is that
each mode emits the RIGHT FRAME with the RIGHT ARGUMENTS, which is the part that
cannot be checked by looking at the arm.
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
# Works both in the source tree (remote/robot-files) and in the published
# layout (server/), so the bundle is testable exactly as shipped.
_CANDIDATES = [
    os.path.join(HERE, "..", "server", "agilex-remote-server.py"),
    os.path.join(HERE, "..", "remote", "robot-files", "agilex-remote-server.py"),
]
SERVER = next((p for p in _CANDIDATES if os.path.exists(p)), _CANDIDATES[0])

calls = []


class FakeIface:
    def __init__(self, *a, **k):
        pass

    def MotionCtrl_2(self, **kw):
        calls.append(("MotionCtrl_2", kw))

    def MotionCtrl_1(self, **kw):
        calls.append(("MotionCtrl_1", kw))

    def JointMitCtrl(self, *a):
        calls.append(("JointMitCtrl", a))

    def JointCtrl(self, *a):
        calls.append(("JointCtrl", a))

    def GripperCtrl_V3(self, *a):
        calls.append(("GripperCtrl_V3", a))

    def MasterSlaveConfig(self, **kw):
        calls.append(("MasterSlaveConfig", kw))

    def MoveCAxisUpdateCtrl(self, *a):
        calls.append(("MoveCAxisUpdateCtrl", a))

    def EndPoseCtrl(self, *a):
        calls.append(("EndPoseCtrl", a))


stub = types.ModuleType("piper_sdk")
stub.C_PiperInterface_V3 = FakeIface
sys.modules["piper_sdk"] = stub

spec = importlib.util.spec_from_file_location("srv", SERVER)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

FAIL = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def make_arm():
    """Build an ArmBridge without running __init__ (no threads, no CAN)."""
    a = srv.ArmBridge.__new__(srv.ArmBridge)
    a.channel = "can_fake"
    a.side = "left"
    a.limits = [(-150.0, 150.0)] * 6
    a.ok = True
    a.iface = FakeIface()
    a.enabled = True
    a.firmware = "fake"
    a.error_code = 0
    import threading
    a.lock = threading.Lock()
    a.joints = [10.0] * 6
    a._measured = [10.0] * 6
    a._meas_ok = True
    a.joints_enabled = [True] * 6
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
    a._stop = threading.Event()
    a.cmd_hz = 0.0
    a.feedback_hz = 0.0
    return a


def last(name):
    for c in reversed(calls):
        if c[0] == name:
            return c
    return None


# --- 1. MOVE J is the default and emits move_mode=1, is_mit_mode=0 ----------
a = make_arm()
calls.clear()
a.set_mode("pos_j")
check("set_mode(pos_j) is a no-op when already pos_j", calls == [], repr(calls))

a.set_mode("pos_p")
m = last("MotionCtrl_2")
check("pos_p -> move_mode=0x00",
      m and m[1].get("move_mode") == 0x00, repr(m))
check("pos_p -> is_mit_mode defaults to 0 (not MIT)",
      m and m[1].get("is_mit_mode", 0) == 0, repr(m))
check("pos_p recorded in self.mode", a.mode == "pos_p", a.mode)

# --- 2. MIT sets is_mit_mode=0xAD ------------------------------------------
calls.clear()
a.set_mode("mit")
m = last("MotionCtrl_2")
check("mit -> is_mit_mode=0xAD", m and m[1].get("is_mit_mode") == 0xAD, repr(m))
check("mit -> mode stored", a.mode == "mit", a.mode)

g = a.set_mit(kp=40, kd=3, t_ref=0.5)
check("set_mit scalar expands to 6 motors", g["kp"] == [40.0] * 6, str(g))
g = a.set_mit(kp=[1, 2, 3])
check("set_mit list keeps tail", g["kp"] == [1.0, 2.0, 3.0, 40.0, 40.0, 40.0],
      str(g))

# --- 3. MOVE C uses move_mode=3 and the 0/1/2 + execute sequence -----------
calls.clear()
a.set_mode("movec")
m = last("MotionCtrl_2")
check("movec -> move_mode=0x03", m and m[1].get("move_mode") == 0x03, repr(m))
calls.clear()
r = a.run_movec([[100, 0, 200, 0, 0, 0]] * 2)
check("movec rejects != 3 points", r["ok"] is False, str(r))
calls.clear()
r = a.run_movec([[1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11, 12], [13, 14, 15, 16, 17, 18]])
seq = [c[0] for c in calls]
check("movec dispatches 3 EndPoseCtrl", seq.count("EndPoseCtrl") == 3, str(seq))
check("movec ends with execute (0x03)",
      [c[1] for c in calls if c[0] == "MoveCAxisUpdateCtrl"][-1] == (0x03,),
      str(seq))

# --- 4. Trajectory load / play / advance / stop ---------------------------
a = make_arm()
a.set_mode("pos_j")
r = a.load_trajectory("t1", [[10.0] * 6, [12.0] * 6, [14.0] * 6], dt=0.05)
check("traj load ok", r["ok"] and r["points"] == 3, str(r))
check("traj rejects <2 points", a.load_trajectory("x", [[0] * 6])["ok"] is False)
a.play_trajectory(loop=False, speed=1.0)
check("traj playing", a.traj["playing"] is True)
a._advance_trajectory(1e18)          # force the clock due
check("traj advanced one point", a.traj["idx"] == 1, str(a.traj["idx"]))
check("traj target applied", abs(a.joints[0] - 10.0) < 1e-6, str(a.joints[0]))
a.stop_trajectory()
check("traj stop clears playing", a.traj["playing"] is False)

# runaway guard: a point far from measured must be clamped
a = make_arm()
a.load_trajectory("far", [[10.0] * 6, [150.0] * 6], dt=0.01)
a.play_trajectory()
a._advance_trajectory(1e18)          # point 0 = 10 (equal to measured)
a._advance_trajectory(1e18)          # point 1 = 150, must be clamped
check("traj respects MAX_TARGET_LAG",
      a.joints[0] <= 10.0 + srv.MAX_TARGET_LAG_DEG + 1e-6, str(a.joints[0]))

# --- 5. Drag teach frames ------------------------------------------------
a = make_arm()
calls.clear()
r = a.set_teaching(True)
check("teach start -> grag_teach_ctrl=0x01",
      last("MotionCtrl_1")[1].get("grag_teach_ctrl") == 0x01, repr(calls))
check("teach flag set", a.teaching is True)
r = a.set_teaching(False)
check("teach stop -> 0x02",
      last("MotionCtrl_1")[1].get("grag_teach_ctrl") == 0x02, repr(calls))
check("teach flag cleared", a.teaching is False)
r = a.set_teaching(False, execute=True)
check("teach execute -> 0x03",
      last("MotionCtrl_1")[1].get("grag_teach_ctrl") == 0x03, repr(calls))

# --- 6. Master / slave config -------------------------------------------
a = make_arm()
calls.clear()
a.set_master_slave(True, "leader")
check("leader -> linkage_config 0xFA",
      last("MasterSlaveConfig")[1].get("linkage_config") == 0xFA, repr(calls))
check("leader role stored", a.master_slave == {"enabled": True, "role": "leader"},
      str(a.master_slave))
a.set_master_slave(True, "follower")
check("follower -> 0xFC",
      last("MasterSlaveConfig")[1].get("linkage_config") == 0xFC, repr(calls))
a.set_master_slave(False)
check("disable -> linkage_config 0x00",
      last("MasterSlaveConfig")[1].get("linkage_config") == 0x00, repr(calls))
check("master/slave cleared", a.master_slave["enabled"] is False)

# --- 7. state() exposes the new fields ----------------------------------
st = a.state()
for key in ("mode", "teaching", "mit", "traj", "master_slave", "movec_active"):
    check(f"state() has {key}", key in st)

print()
if FAIL:
    print(f"{len(FAIL)} FAILED: {FAIL}")
    sys.exit(1)
print("ALL MODE TESTS PASSED")
