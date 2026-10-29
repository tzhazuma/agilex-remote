#!/usr/bin/env python3
"""
agilex-remote-server.py — robot-side HTTP server for the virtual remote controller.

Runs ON the AgileX robot (it needs local ROS 2 + CAN access, because the robot
sets ROS_LOCALHOST_ONLY=1 so DDS never leaves the box). The Mac reaches it
through an SSH tunnel on 127.0.0.1:8765 and drives it from a browser.

WHY A SERVER AND NOT rosbridge: the browser only needs three verbs (drive, nudge
the arm, watch video). A tiny purpose-built server is far less to go wrong than
a generic ROS bridge, and it lets all the safety logic live in one place, close
to the hardware.

=======================  SAFETY MODEL  =======================
This drives real hardware. Three independent things have to fail before the
robot moves unintentionally:

 1. HARD CLAMPS. Velocity and joint steps are clamped server-side, so a buggy or
    hostile client cannot exceed them. The client's own limits are cosmetic.
 2. WATCHDOG. The drive loop publishes at 20 Hz. If no /api/drive has arrived
    for WATCHDOG_MS, it publishes a zero Twist. Closing the browser, losing the
    network, or a JS crash all result in STOP, not in "last command keeps going".
 3. ARM TARGET IS BOUNDED. Arm nudges move toward a target that is clamped to
    the documented joint limits, and the target can never jump more than
    MAX_JOINT_STEP_DEG per request.

Plus an explicit E-STOP that zeroes velocity and disables the arm.

NOTE: launching tracer_base (done by bin/robot-remote.sh) calls
EnableCommandedMode() on the chassis, which takes it out of RC-handset mode.
That is inherent to ROS control - the physical remote stops driving it.
"""

import csv
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
HOST = os.environ.get("AGILEX_REMOTE_HOST", "0.0.0.0")
PORT = int(os.environ.get("AGILEX_REMOTE_PORT", "8765"))

# ---------------------------------------------------------------------------
# ACCESS CONTROL — password login
# ---------------------------------------------------------------------------
# Binding 0.0.0.0 is needed so the Android phone can reach the controller
# directly. That also means anyone else on the campus network can reach it, so
# off-loopback clients must log in with a password.
#
# WHY A LOGIN PAGE AND NOT HTTP BASIC AUTH: Android WebView does not show the
# native Basic-auth dialog unless the app implements onReceivedHttpAuthRequest,
# so Basic auth silently hangs on Android. A plain HTML form works everywhere
# with no client-side support, and it needs no change to remote-ui.html because
# the server serves the login page itself.
#
# Loopback is exempt: the Mac reaches this through an SSH tunnel, which has
# already authenticated, so `ssh -L` users never see a login.
#
#   AGILEX_PASSWORD   - the password (default "changeme")
#   AGILEX_ALLOW_ANON - 1 disables the check entirely
ALLOW_ANON = os.environ.get("AGILEX_ALLOW_ANON", "0") == "1"
PASSWORD = os.environ.get("AGILEX_PASSWORD", "changeme")
SESSION_FILE = os.environ.get("AGILEX_SESSION_FILE",
                              "/usr/local/share/agilex-remote/.session")
COOKIE_NAME = "agilex_sid"


def load_or_make_session():
    """A stable session secret, so logins survive a service restart."""
    try:
        with open(SESSION_FILE) as f:
            s = f.read().strip()
            if s:
                return s
    except Exception:
        pass
    s = secrets.token_urlsafe(32)
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            f.write(s)
        os.chmod(SESSION_FILE, 0o600)
    except Exception as e:
        log(f"could not persist session secret ({e!r}); logins reset on restart")
    return s


ACCESS_TOKEN = None  # set in main()

LOGIN_PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>AgileX robot - sign in</title>
<style>
 :root{--bg:#0e1116;--panel:#161b22;--line:#2a313c;--fg:#e6edf3;--muted:#8b949e;--accent:#3fb950;--bad:#f85149}
 *{box-sizing:border-box}
 html,body{margin:0;height:100%%}
 body{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;
      display:flex;align-items:center;justify-content:center;padding:24px;padding-bottom:calc(24px + env(safe-area-inset-bottom))}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;width:100%%;max-width:380px}
 h1{font-size:17px;margin:0 0 4px}
 p{color:var(--muted);font-size:13px;margin:0 0 18px}
 label{display:block;font-size:12.5px;color:var(--muted);margin-bottom:6px}
 input{width:100%%;min-height:48px;background:#0d1117;border:1px solid var(--line);border-radius:10px;
       color:var(--fg);padding:10px 12px;font-size:16px}
 input:focus{outline:2px solid var(--accent);outline-offset:1px}
 button{width:100%%;min-height:48px;margin-top:14px;border:0;border-radius:10px;background:var(--accent);
        color:#06210c;font-weight:700;font-size:15px;cursor:pointer}
 button[disabled]{opacity:.6;cursor:default}
 .err{color:var(--bad);font-size:13px;margin-top:12px;min-height:18px}
</style></head><body>
<form class="card" id="f">
  <h1>AgileX robot</h1>
  <p>Sign in to control the robot. This device is not on the robot's own host.</p>
  <label for="pw">Password</label>
  <input id="pw" type="password" autocomplete="current-password" autofocus>
  <button id="go" type="submit">Sign in</button>
  <div class="err" id="e" role="alert"></div>
</form>
<script>
 var f=document.getElementById('f'),pw=document.getElementById('pw'),
     go=document.getElementById('go'),e=document.getElementById('e');
 f.addEventListener('submit',function(ev){
   ev.preventDefault(); e.textContent=''; go.disabled=true; go.textContent='Signing in...';
   fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
                       body:JSON.stringify({password:pw.value})})
     .then(function(r){return r.json().catch(function(){return {};}).then(function(j){return {s:r.status,j:j};});})
     .then(function(res){
       if(res.s===200 && res.j && res.j.ok){ location.replace('/'); return; }
       e.textContent=(res.j && res.j.error) || 'Sign-in failed'; pw.select();
     })
     .catch(function(){ e.textContent='Could not reach the robot'; })
     .then(function(){ go.disabled=false; go.textContent='Sign in'; });
 });
</script></body></html>"""
UI_PATH = os.environ.get(
    "AGILEX_REMOTE_UI", os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote-ui.html")
)

# Speed ceiling for the drive clamp. Raised to the chassis's documented maximum
# so the operator can run outdoors for data collection; the UI slider starts
# well below this (DEFAULT_LIN/ANG) and is ramped up deliberately.
MAX_LIN = float(os.environ.get("AGILEX_MAX_LIN", "2.3"))      # m/s
MAX_ANG = float(os.environ.get("AGILEX_MAX_ANG", "0.7853"))   # rad/s

# Where the UI's speed slider starts. Not enforced here - the UI reads these and
# uses them as the initial slider position.
DEFAULT_LIN = float(os.environ.get("AGILEX_DEFAULT_LIN", "0.5"))
DEFAULT_ANG = float(os.environ.get("AGILEX_DEFAULT_ANG", "0.3"))

# What the chassis CAN do, as opposed to what we allow. Surfaced so the UI can
# show the ceiling next to the safe default. Overridable, and the source string
# travels with the number so a guess is never mistaken for a spec.
HW_MAX_LIN = float(os.environ.get("AGILEX_HW_MAX_LIN", "2.3"))
HW_MAX_ANG = float(os.environ.get("AGILEX_HW_MAX_ANG", "0.7853"))
HW_MAX_SOURCE = os.environ.get(
    "AGILEX_HW_MAX_SOURCE",
    "linear 2.3 m/s documented (TRACER_UserManual_v1.2.pdf s1.2, via ugv_sdk/docs); "
    "angular 0.7853 rad/s is the v1 protocol 100%% reference, NOT a hardware rating")
MAX_JOINT_STEP_DEG = float(os.environ.get("AGILEX_MAX_JOINT_STEP", "2.0"))
# How far the held target may ever sit from the measured angle. Bounds the
# damage if a motor stops following; see ArmBridge.nudge for why.
MAX_TARGET_LAG_DEG = float(os.environ.get("AGILEX_MAX_TARGET_LAG", "15.0"))
# How far outside the datasheet joint limits a "hold where you are" resync
# may go. The arm really does rest a couple of degrees outside them (left
# J2 -2.06 vs 0..180, J3 +1.44 vs -170..0), and clamping that away creates a
# permanent target error the arm pushes against.
RESYNC_MARGIN_DEG = float(os.environ.get("AGILEX_RESYNC_MARGIN", "5.0"))

# Watchdog: how long after the last /api/drive the controller forces a stop.
#
# THIS IS A BRAKING DISTANCE, NOT A TIMEOUT. The robot coasts for this long
# before a stop is even commanded, so the distance it covers is
#   watchdog_ms/1000 * speed
# At the old 0.08 m/s cap, 700 ms was 5.6 cm - irrelevant. At 2.3 m/s it would
# be 1.6 METRES of coasting, which is a genuine hazard outdoors. So the window
# is tightened to 300 ms (0.69 m at full speed) and can be tuned per run.
WATCHDOG_MS = int(os.environ.get("AGILEX_WATCHDOG_MS", "300"))

CAN_CHANNEL = os.environ.get("AGILEX_ARM_CAN", "can_left")
CAN_CHANNEL_RIGHT = os.environ.get("AGILEX_ARM_CAN_RIGHT", "can_right")
CMD_VEL_TOPIC = os.environ.get("AGILEX_CMD_VEL_TOPIC", "/cmd_vel")
ODOM_TOPIC = os.environ.get("AGILEX_ODOM_TOPIC", "/odom")
STATUS_TOPIC = os.environ.get("AGILEX_STATUS_TOPIC", "/tracer_status")

VIDEO_DEV = {0: "/dev/video0", 2: "/dev/video2"}   # 0 = faces the ARM, 2 = faces the FLOOR
VIDEO_W, VIDEO_H, VIDEO_FPS = 640, 480, 12

# Documented Piper joint limits, degrees (from piper_sdk JointCtrl docstring)
JOINT_LIMITS_DEG = [
    (-150.0, 150.0),
    (0.0, 180.0),
    (-170.0, 0.0),
    (-100.0, 100.0),
    (-70.0, 70.0),
    (-120.0, 120.0),
]

log_lock = threading.Lock()


def log(*a):
    with log_lock:
        print(time.strftime("[%H:%M:%S]"), *a, file=sys.stderr, flush=True)


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# ----------------------------------------------------------------------------
# ROS bridge: /cmd_vel out, /odom in
# ----------------------------------------------------------------------------
class RosBridge:
    """Owns the rclpy node. Kept deliberately dumb - it just mirrors state."""

    def __init__(self):
        self.ok = False
        self.node = None
        self.pub = None
        self.connected = False
        self.can_iface = os.environ.get("AGILEX_CHASSIS_CAN", "can0")
        self.odom_x = 0.0
        self.odom_y = 0.0
        self.battery_v = None
        self.control_mode = None
        self.error_code = 0
        self._last_odom = 0.0
        self._last_status = 0.0
        try:
            import rclpy
            from rclpy.node import Node
            from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
            from geometry_msgs.msg import Twist
            from nav_msgs.msg import Odometry

            self.rclpy = rclpy
            self.Twist = Twist
            rclpy.init(args=None)
            self.node = rclpy.create_node("agilex_remote")
            qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                             history=HistoryPolicy.KEEP_LAST)
            self.pub = self.node.create_publisher(Twist, CMD_VEL_TOPIC, qos)
            self.node.create_subscription(Odometry, ODOM_TOPIC, self._on_odom, qos)

            # tracer_base publishes the chassis state on /tracer_status, not on
            # /odom - /odom stayed silent in testing while this topic carried
            # mode, error code, battery and actuator states. Subscribe if the
            # message type is importable (needs the agilex_ws overlay sourced).
            try:
                from tracer_msgs.msg import TracerStatus
                self.node.create_subscription(TracerStatus, STATUS_TOPIC,
                                              self._on_status, qos)
                log(f"subscribed {STATUS_TOPIC} (tracer_msgs available)")
            except Exception as e:
                log(f"tracer_msgs unavailable ({e!r}) - chassis detail will be limited")

            self.executor = rclpy.executors.MultiThreadedExecutor(num_threads=2)
            self.executor.add_node(self.node)
            threading.Thread(target=self.executor.spin, daemon=True).start()
            self.ok = True
            log(f"ROS bridge up: pub {CMD_VEL_TOPIC}, sub {ODOM_TOPIC}")
        except Exception as e:  # ROS missing/broken must not take down the video
            log(f"ROS bridge FAILED: {e!r} (drive will be unavailable)")

    def _on_odom(self, msg):
        self.connected = True
        self._last_odom = time.time()
        self.odom_x = msg.pose.pose.position.x
        self.odom_y = msg.pose.pose.position.y

    def _on_status(self, msg):
        self.connected = True
        self._last_status = time.time()
        self.control_mode = int(getattr(msg, "control_mode", -1))
        self.error_code = int(getattr(msg, "error_code", 0))
        bv = getattr(msg, "battery_voltage", None)
        if bv:
            # The vendor's own parser multiplies the raw CAN value by 0.1, which
            # reports 277 V for a 24 V pack. Raw is 2769/2770 -> 27.7 V at 0.01.
            # The ROS message has already applied 0.1, so undo it and rescale.
            self.battery_v = round(bv * 0.1, 2)

    def clear_fault(self):
        """
        Send the chassis fault-clear frame (CAN 0x441, payload 0x00).

        WHY THIS EXISTS: the Tracer latches certain faults, and the vendor manual
        (TRACER_UserManual_v1.2.pdf) is explicit that while one is latched the
        drive output is limited, the vehicle coasts to a stop, and "motion
        control commands have no effect... the host must send a fault-clear
        command; only after clearing can motion commands execute".

        That is exactly why the chassis accepted /cmd_vel and never moved: it had
        been reporting error_code 4 (motor-1 communication fault) the whole time.
        Payload 0x00 = clear all errors; 0x01/0x02 = clear motor 1/2 only.
        """
        try:
            r = subprocess.run(["cansend", self.can_iface, "441#00"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                log(f"chassis fault-clear sent on {self.can_iface} (0x441 = 00)")
                return True
            log(f"chassis fault-clear failed: {r.stderr.strip()!r}")
            return False
        except Exception as e:
            log(f"chassis fault-clear error: {e!r}")
            return False

    def publish(self, lin, ang):
        if not self.ok:
            return
        try:
            t = self.Twist()
            t.linear.x = float(lin)
            t.angular.z = float(ang)
            self.pub.publish(t)
        except Exception as e:
            log(f"publish failed: {e!r}")

    def state(self):
        fresh = (time.time() - max(self._last_odom, self._last_status)) < 3.0
        modes = {0: "standby", 1: "commanded", 2: "remote_control", 3: "reserved"}
        return {
            "connected": bool(self.ok and fresh),
            "mode": modes.get(self.control_mode, "unknown") if self.control_mode is not None else "unknown",
            "battery_v": self.battery_v,
            "soc_pct": None,
            "error_code": self.error_code,
            "odom_x": round(self.odom_x, 4),
            "odom_y": round(self.odom_y, 4),
        }


# ----------------------------------------------------------------------------
# Arm bridge: streams a held joint target at 50 Hz
# ----------------------------------------------------------------------------
TRAJ_DIR = os.environ.get("AGILEX_TRAJ_DIR", "/home/agilex/agilex-data/trajectories")


def parse_trajectory_file(path, side="left"):
    """Read a trajectory from disk. Returns (name, points, dt).

    points are 6 angles per frame in degrees; dt is seconds per frame.
    Raises ValueError with a readable message on anything unexpected - the HTTP
    layer turns that into a 400 rather than a stack trace.
    """
    path = os.path.abspath(os.path.expanduser(str(path)))
    if os.path.isdir(path):
        path = os.path.join(path, "timeline.csv")
    if not os.path.isfile(path):
        raise ValueError(f"no such file: {path}")
    name = os.path.basename(os.path.dirname(path)) or os.path.basename(path)

    if path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            pts = doc.get("points") or doc.get("trajectory") or []
            dt = float(doc.get("dt", 0.05))
            name = doc.get("name") or name
        else:
            pts, dt = doc, 0.05
        out = []
        for p in pts:
            vals = [float(v) for v in list(p)[:6]]
            if len(vals) < 6:
                raise ValueError("each point needs 6 angles")
            out.append(vals)
        return name, out, dt

    # ---- csv ---------------------------------------------------------------
    with open(path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 3:
        raise ValueError("csv has no data rows")
    head = [h.strip() for h in rows[0]]
    # Prefer the commanded-target columns for this side; fall back to measured.
    prefix = "L_" if side == "left" else "R_"
    cols = [f"{prefix}t{i}" for i in range(6)]
    if not all(c in head for c in cols):
        cols = [f"{prefix}j{i}" for i in range(6)]
    if not all(c in head for c in cols):
        cols = None
    idx = [head.index(c) for c in cols] if cols else None
    t_i = head.index("t") if "t" in head else None

    out, times = [], []
    for r in rows[1:]:
        if not r or len(r) < 2:
            continue
        try:
            if idx is not None:
                vals = [float(r[i]) for i in idx]
            else:
                vals = [float(v) for v in r[:6]]
        except (ValueError, IndexError):
            continue
        out.append(vals)
        if t_i is not None:
            try:
                times.append(float(r[t_i]))
            except (ValueError, IndexError):
                pass
    if len(out) < 2:
        raise ValueError("fewer than 2 usable rows")
    dt = 0.05
    if len(times) >= 2:
        deltas = sorted(times[i + 1] - times[i] for i in range(len(times) - 1))
        med = deltas[len(deltas) // 2]
        if 0.001 <= med <= 1.0:
            dt = med
    return name, out, dt


def list_trajectories():
    """What the UI can offer: json trajectories plus recorded episodes."""
    items = []
    for root, tag in ((TRAJ_DIR, "trajectory"), (EPISODE_ROOT, "episode")):
        try:
            names = sorted(os.listdir(root))
        except Exception:
            continue
        for n in names:
            p = os.path.join(root, n)
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, "timeline.csv")):
                items.append({"kind": tag, "name": n, "path": p})
            elif os.path.isfile(p) and n.lower().endswith(".json"):
                items.append({"kind": tag, "name": n, "path": p})
    return items


def bus_rx_packets(iface):
    """Kernel RX counter for a CAN interface, or None if it cannot be read.

    This is the only trustworthy "is anything alive on this bus" signal: the
    SDK happily opens a socket on a dead bus and reports all-zero joints.
    """
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_packets") as f:
            return int(f.read().strip())
    except Exception:
        return None


class ArmBridge:
    """
    The Piper controller expects a continuous command stream, so we hold a target
    and stream it. HTTP requests only ever nudge that target by a bounded delta.
    """

    RATE_HZ = 50
    FEEDBACK_HZ = 5
    # Percent of max joint speed the arm is allowed to use. Deliberately low:
    # the whole point of this controller is small, slow, safe nudges.
    SPEED_PCT = int(os.environ.get("AGILEX_ARM_SPEED_PCT", "25"))

    def __init__(self, channel, limits, side="left"):
        self.channel = channel
        self.side = side
        self.limits = limits          # list of (lo,hi) degrees
        self.ok = False
        self.iface = None
        self.enabled = False
        self.firmware = "?"
        self.error_code = 0
        self.lock = threading.Lock()
        self.joints = [0.0] * 6       # target, degrees
        self._measured = [0.0] * 6
        self._meas_ok = False
        self.joints_enabled = [False] * 6
        self.faults = [[] for _ in range(6)]
        self.gripper = 0.0
        self._grip_target = 0.0
        self.home_deg = [0.0] * 6     # captured at startup = the safe return pose
        # ---- operating mode ------------------------------------------------
        # mode is the CONTROL LAW the 50 Hz loop uses to produce motion:
        #   pos_j  MOVE J + JointCtrl          (default; joint-space position)
        #   pos_p  MOVE P + JointCtrl          (point-to-point, arm plans)
        #   mit    MIT impedance + JointMitCtrl (compliant, needs kp/kd)
        #   movec  MOVE C + EndPoseCtrl/MoveCAxisUpdateCtrl (Cartesian arc)
        # Teaching and trajectory playback are orthogonal to mode: they change
        # WHERE the target comes from, not how it is tracked.
        self.mode = "pos_j"
        self.mit = {"kp": [30.0] * 6, "kd": [2.0] * 6, "t_ref": [0.0] * 6,
                    "vel_ref": [0.0] * 6}
        self.teaching = False
        self.movec = {"points": [], "active": False}
        self.traj = {"name": None, "points": [], "dt": 0.05, "idx": 0,
                     "playing": False, "loop": False, "speed": 1.0,
                     "t_next": 0.0, "plays": 0}
        self.master_slave = {"enabled": False, "role": None}
        # What the ARM says it is in, as opposed to self.mode (bookkeeping).
        self.mode_actual = None
        self.mode_verified = True
        self._last_posp_sent = None
        self._stop = threading.Event()
        self.cmd_hz = 0.0
        self.feedback_hz = 0.0
        try:
            sys.path.insert(0, "/home/agilex/.local/lib/python3.10/site-packages")
            from piper_sdk import C_PiperInterface_V3
            self.iface = C_PiperInterface_V3(channel)
            # piper_init=False: we do NOT want the SDK's init sequence firing
            # frames on its own. Nothing is transmitted until we ask for it.
            self.iface.ConnectPort(can_init=False, piper_init=False, start_thread=True)
            # A bus with no powered node still opens fine and reports every joint
            # as 0.0. Adopting that as the measured pose - and as home - is how
            # "Home" ends up commanding an arbitrary all-zero pose. So require
            # real traffic before declaring the arm usable.
            rx0 = bus_rx_packets(channel)
            time.sleep(2.0)
            rx1 = bus_rx_packets(channel)
            if rx0 is not None and rx1 is not None and rx1 <= rx0:
                self.ok = False
                log(f"arm {channel} ({side}) NO FEEDBACK: bus silent "
                    f"(rx {rx0} -> {rx1} in 2 s). Arm unpowered, e-stop pressed, "
                    f"or CAN cable off. Marking NOT ok; no target or home adopted.")
                return
            self.ok = True
            self._read_measured()
            # Record what the arm says it is in, so /api/status reports the
            # hardware truth from the first second rather than "not verified".
            self.mode_actual = self._read_actual_mode()
            # Adopt whatever the arm is already doing as our starting intent, so
            # an arm left enabled by a previous session is usable immediately.
            self.enabled = any(self.joints_enabled)
            with self.lock:
                self.joints = list(self._measured)
                self._grip_target = self.gripper
            # The pose it was in when we started is the only pose we KNOW is
            # reachable and collision-free, so that is what "Home" returns to.
            self.home_deg = [round(v, 3) for v in self._measured]
            try:
                fw = self.iface.GetPiperFirmwareVersion()
                self.firmware = str(fw)
            except Exception:
                pass
            log(f"arm {channel} ({side}) up: joints={[round(v,2) for v in self._measured]} fw={self.firmware}")
            threading.Thread(target=self._loop, daemon=True).start()
            threading.Thread(target=self._feedback_loop, daemon=True).start()
        except Exception as e:
            log(f"arm {channel} FAILED: {e!r}")

    def _read_measured(self):
        try:
            js = self.iface.GetArmJointMsgs().joint_state
            raw = [js.joint_1, js.joint_2, js.joint_3, js.joint_4, js.joint_5, js.joint_6]
            self._measured = [v * 0.001 for v in raw]
            self._meas_ok = True
            st = self.iface.GetArmEnableStatus()
            self.joints_enabled = [bool(x) for x in st][:6]
            # NOTE: this does NOT touch self.enabled. self.enabled is the user's
            # INTENT and drives the control loop; joints_enabled is what the
            # hardware actually reports. Deriving intent from measurement here
            # meant a disable was undone a fraction of a second later, because
            # the loop re-read a still-enabled motor and re-asserted the mode.
            self._refresh_faults()
            g = self.iface.GetArmGripperMsgs()
            gs = getattr(g, "gripper_state", None)
            if gs is not None:
                self.gripper = getattr(gs, "grippers_angle", 0) * 0.001
        except Exception:
            self._meas_ok = False

    def _refresh_faults(self):
        """Per-motor driver faults (overheat / driver error). Read-only."""
        try:
            ds = self.iface.GetDriverStates()
            faults = []
            for i in range(1, 7):
                m = getattr(ds, f"motor_{i}", None)
                fs = getattr(m, "foc_status", None) if m is not None else None
                bad = []
                if fs is not None:
                    for flag in ("motor_overheating", "driver_error_status",
                                 "driver_overcurrent", "driver_overheating",
                                 "voltage_too_low", "sensor_status"):
                        if getattr(fs, flag, False):
                            bad.append(flag)
                faults.append(bad)
            self.faults = faults
        except Exception:
            pass

    def _loop(self):
        """
        50 Hz control loop: ONLY sends commands. Nothing here may block.

        JITTER FIX: feedback reads used to happen on this thread every 10th tick.
        GetDriverStates()/GetArmJointMsgs() are CAN round-trips that can take
        tens of milliseconds, so they stalled the command stream and the arm saw
        an irregular feed - a classic cause of shaking. Feedback now lives on its
        own slower thread, and the rate is measured so it can be reported.
        """
        n = 0
        last_mode = 0.0
        t_start = time.time()
        while not self._stop.is_set():
            t0 = time.time()
            n += 1
            if self.enabled:
                # Advance a playing trajectory first: it is a TARGET SOURCE, so
                # it has to update self.joints before the frame goes out.
                self._advance_trajectory(t0)
                # Re-assert the ACTIVE mode periodically. Sending it only at
                # enable time is NOT enough: an arm that was already enabled
                # (e.g. left enabled by an earlier session) never received it,
                # stayed in its previous mode, and moved a fraction of a degree
                # before stalling short of the target. That was a real bug.
                # 10s rather than 2s: each re-assert briefly re-enters the mode,
                # which is itself a small disturbance.
                if t0 - last_mode > 10.0:
                    last_mode = t0
                    try:
                        self._apply_mode()
                    except Exception as e:
                        if n % 200 == 0:
                            log(f"arm mode re-assert failed: {e!r}")
                with self.lock:
                    j = list(self.joints)
                    g = self._grip_target
                    kp = list(self.mit["kp"])
                    kd = list(self.mit["kd"])
                    t_ref = list(self.mit["t_ref"])
                    vel = list(self.mit["vel_ref"])
                if self.teaching or self.master_slave["enabled"]:
                    # Hand-guided (drag teach) or slaved to another arm: any
                    # command frame from us would fight the operator / the link.
                    pass
                elif self.mode == "mit":
                    # MIT impedance: position target plus per-motor stiffness and
                    # damping. JointCtrl is ignored in this mode, so keeping the
                    # old stream would make the arm look dead.
                    try:
                        for m in range(1, 7):
                            self.iface.JointMitCtrl(m, j[m - 1] * 1000.0,
                                                    vel[m - 1], kp[m - 1],
                                                    kd[m - 1], t_ref[m - 1])
                        self.iface.GripperCtrl_V3(int(round(g * 1000)), 0, 0x01, 0)
                    except Exception as e:
                        self.error_code = 1
                        if n % 200 == 0:
                            log(f"arm MIT stream error: {e!r}")
                elif self.mode == "movec":
                    # The arc is dispatched once by run_movec(); a JointCtrl
                    # stream here would override it on the next frame.
                    pass
                elif self.mode == "pos_p":
                    # MOVE P makes the arm plan its own path to the point. A 50 Hz
                    # position stream makes that planner fight the stream - this
                    # is what shook the arm on 2026-10-29. Send only on change.
                    if j != self._last_posp_sent:
                        try:
                            self.iface.JointCtrl(*[int(round(v * 1000)) for v in j])
                            self.iface.GripperCtrl_V3(int(round(g * 1000)), 0, 0x01, 0)
                            self._last_posp_sent = list(j)
                        except Exception as e:
                            self.error_code = 1
                            if n % 200 == 0:
                                log(f"arm MOVE P send error: {e!r}")
                else:
                    try:
                        self.iface.JointCtrl(*[int(round(v * 1000)) for v in j])
                        self.iface.GripperCtrl_V3(int(round(g * 1000)), 0, 0x01, 0)
                    except Exception as e:
                        self.error_code = 1
                        if n % 200 == 0:
                            log(f"arm stream error: {e!r}")
            elapsed = time.time() - t_start
            if elapsed > 0:
                self.cmd_hz = round(n / elapsed, 1)
            dt = time.time() - t0
            time.sleep(max(0.0, (1.0 / self.RATE_HZ) - dt))

    def _feedback_loop(self):
        """5 Hz: read joints / enable / faults / gripper, off the control path."""
        n = 0
        t_start = time.time()
        while not self._stop.is_set():
            n += 1
            self._read_measured()
            elapsed = time.time() - t_start
            if elapsed > 0:
                self.feedback_hz = round(n / elapsed, 1)
            time.sleep(1.0 / self.FEEDBACK_HZ)

    def reset(self):
        """
        One-click reset: clear a latched motion fault, re-enable, and re-sync the
        commanded target to where the arm ACTUALLY is.

        That last part is what stops the shaking: if the target has drifted away
        from the measured position (stale target, previous session, a fault that
        interrupted a move) the arm sits and fights it. Re-syncing makes the arm
        hold still. Deliberately causes no motion.
        """
        if not self.ok:
            return False
        try:
            # 0x02 = resume (0x01 = stop, 0x00 = invalid/no-op)
            try:
                self.iface.EmergencyStop(0x02)
                time.sleep(0.3)
            except Exception:
                pass
            self._read_measured()
            with self.lock:
                self.joints = list(self._measured)
                self._grip_target = self.gripper
            self.set_enabled(True)
            self._read_measured()
            log(f"arm {self.channel} reset: joints={[round(v,2) for v in self._measured]} "
                f"enabled={self.enabled} per-joint={self.joints_enabled}")
            return True
        except Exception as e:
            log(f"arm {self.channel} reset failed: {e!r}")
            return False

    def go_home(self):
        """Command the arm back to the pose it held when the server started."""
        if not self.ok:
            return None
        with self.lock:
            self.joints = [clamp(self.home_deg[i], self.limits[i][0], self.limits[i][1])
                           for i in range(6)]
            target = list(self.joints)
        log(f"arm {self.channel} homing to {[round(v,2) for v in target]}")
        return target

    def set_enabled(self, on):
        """
        Enable/disable the arm.

        Enabling is retried and then VERIFIED by re-reading the enable status,
        because a single EnableArm frame can be missed and the caller would
        otherwise be told "ok" while nothing happened - which is exactly how
        the unresponsive Enable button presented.
        """
        if not self.ok:
            return False
        try:
            if on:

                # RESYNC THE TARGET BEFORE ENABLING - this is the fix for the arm
                # lurching when it is switched back on.
                #
                # A disabled Piper arm goes limp, so gravity moves it. The held
                # target does not follow. Observed on the real robot: the target
                # stayed at the startup pose while the arm sagged roughly 20
                # degrees away from it, so every re-enable snapped the arm back to
                # the stale target - which looks exactly like "the arm is moving on
                # its own", and is what prompted killing this service.
                #
                # Enabling must mean "hold where you are", never "jump to wherever
                # we last wanted you". Reset the target to measured first, under
                # the lock, before any enable frame goes out.
                with self.lock:
                    self._resync_target_locked()
                log(f"arm {self.side} target resynced to measured before enable: "
                    + str([round(v, 1) for v in self.joints]))
                # RESUME first. MotionCtrl_1(0x02) is the documented "resume"
                # (0x01 = stop, 0x00 = invalid / no-op). Without it a latched
                # motion fault keeps the arm in REACH_TARGET_POS_FAILED: it
                # accepts EnableArm, reports only some motors enabled, and then
                # ignores every JointCtrl. That is exactly how "Enable arm does
                # nothing" presented. Sending 0x02 also cleared a motor-5 driver
                # error and brought all six motors up.
                try:
                    self.iface.EmergencyStop(0x02)
                    time.sleep(0.4)
                except Exception:
                    pass
                # Re-assert the ACTIVE mode, not hard-coded MOVE J: enabling
                # after a MIT / MOVE C session must not silently drop the arm
                # back into joint position mode.
                self._apply_mode()
                time.sleep(0.2)
                for attempt in range(3):
                    self.iface.EnableArm(motor_num=7, enable_flag=0x02)
                    time.sleep(0.6)
                    self._read_measured()
                    if all(self.joints_enabled):
                        break
                    log(f"arm enable attempt {attempt + 1}: {self.joints_enabled}")
            else:
                # Stop the control loop FIRST. It re-asserts MotionCtrl_2 every
                # 2 s whenever self.enabled is true, so leaving that set meant the
                # loop immediately re-enabled the arm and DisableArm looked
                # broken. Clear the flag, then disable, then verify.
                self.enabled = False
                time.sleep(0.1)
                self.iface.DisableArm(motor_num=7)
                for _ in range(6):
                    time.sleep(0.4)
                    self._read_measured()
                    if not any(self.joints_enabled):
                        break
                    self.enabled = False   # loop must not re-assert meanwhile
                    try:
                        self.iface.DisableArm(motor_num=7)
                    except Exception:
                        pass
            # Record the INTENT. self.enabled drives the control loop and is
            # reported (ANDed with the measured state); joints_enabled is purely
            # what the hardware says. Forgetting this made Enable report false
            # even though all six motors had come up.
            self.enabled = bool(on)
            log(f"arm {self.channel} enabled={self.enabled} per-joint={self.joints_enabled}")
            return True
        except Exception as e:
            log(f"arm enable failed: {e!r}")
            return False

    # ---------------------------------------------------------------- modes
    MODES = ("pos_j", "pos_p", "mit", "movec")

    def _apply_mode(self):
        """Push self.mode to the arm via MotionCtrl_2.

        Must be called WITHOUT self.lock held (it does a CAN round-trip).
        """
        if not self.ok:
            return False
        try:
            if self.mode == "mit":
                # is_mit_mode=0xAD switches the joint driver to impedance; the
                # position target then comes from JointMitCtrl, not JointCtrl.
                self.iface.MotionCtrl_2(ctrl_mode=0x01, move_mode=0x01,
                                        move_spd_rate_ctrl=self.SPEED_PCT,
                                        is_mit_mode=0xAD)
            elif self.mode == "movec":
                self.iface.MotionCtrl_2(ctrl_mode=0x01, move_mode=0x03,
                                        move_spd_rate_ctrl=self.SPEED_PCT)
            elif self.mode == "pos_p":
                self.iface.MotionCtrl_2(ctrl_mode=0x01, move_mode=0x00,
                                        move_spd_rate_ctrl=self.SPEED_PCT)
            else:
                self.iface.MotionCtrl_2(ctrl_mode=0x01, move_mode=0x01,
                                        move_spd_rate_ctrl=self.SPEED_PCT)
            return True
        except Exception as e:
            log(f"arm {self.side} mode {self.mode} apply failed: {e!r}")
            return False

    def _resync_target_locked(self):
        """Adopt the measured pose as the held target. Caller holds self.lock.

        This is "hold exactly where you are", so it must NOT hard-clamp: the arm
        can sit slightly outside its nominal range, and clamping there invents a
        permanent target error that the arm then pushes against (buzzing).
        The margin still rejects obviously bogus feedback.
        """
        m = RESYNC_MARGIN_DEG
        for j in range(6):
            lo, hi = self.limits[j]
            self.joints[j] = clamp(self._measured[j], lo - m, hi + m)

    def _read_actual_mode(self):
        """Ask the arm which move mode it is ACTUALLY in.

        self.mode is only bookkeeping - a MotionCtrl_2 frame can be ignored, and
        then we stream position targets into an arm that is planning its own
        trajectory, which is exactly what shook the arm on 2026-10-29.
        Returns e.g. "MOVE_J" / "MOVE_P" / "MOVE_C", or None if unreadable.
        """
        try:
            st = self.iface.GetArmStatus()
            txt = str(getattr(st, "arm_status", "") or "")
            m = re.search(r"Mode Feed:\s*([A-Z_]+)", txt)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    def set_mode(self, mode):
        """Switch control law. Resyncs the target to measured first.

        The resync matters for the same reason it does on enable: switching
        MOVE J -> MIT makes the arm go compliant, so gravity moves it, and a
        stale target would then be a spring pulling it somewhere unexpected.
        """
        if not self.ok:
            return False
        mode = str(mode or "").strip()
        if mode not in self.MODES:
            return False
        if mode == self.mode:
            return True
        self.teaching = False
        with self.lock:
            self.traj["playing"] = False
            self._resync_target_locked()
        old, self.mode = self.mode, mode
        ok = self._apply_mode()
        # Believe the hardware, not ourselves. One retry, then revert.
        expect = {"pos_j": "MOVE_J", "pos_p": "MOVE_P",
                  "movec": "MOVE_C", "mit": "MOVE_J"}[mode]
        actual = None
        for attempt in (1, 2):
            time.sleep(0.35)
            actual = self._read_actual_mode()
            if actual is None or actual == expect:
                break
            log(f"arm {self.side} mode {mode}: arm still reports {actual} "
                f"(attempt {attempt}), re-sending")
            self._apply_mode()
        self.mode_actual = actual
        if actual is not None and actual != expect:
            back = {"MOVE_J": "pos_j", "MOVE_P": "pos_p", "MOVE_C": "movec"}
            self.mode = back.get(actual, "pos_j")
            self.mode_verified = False
            log(f"arm {self.side} mode {mode} REJECTED by the arm (it is in "
                f"{actual}); bookkeeping reverted to {self.mode} so we do not "
                f"stream into the wrong mode")
            return False
        self.mode_verified = True
        log(f"arm {self.side} mode {old} -> {mode} (arm reports {actual}, sent={ok})")
        return ok

    def set_mit(self, kp=None, kd=None, t_ref=None):
        """Set MIT impedance gains. Per-joint lists or scalars, 6 motors."""
        def _six(v, cur):
            if v is None:
                return list(cur)
            if isinstance(v, (int, float)):
                return [float(v)] * 6
            out = [float(x) for x in list(v)[:6]]
            return out + list(cur)[len(out):]

        with self.lock:
            self.mit["kp"] = _six(kp, self.mit["kp"])
            self.mit["kd"] = _six(kd, self.mit["kd"])
            self.mit["t_ref"] = _six(t_ref, self.mit["t_ref"])
            return {"kp": list(self.mit["kp"]), "kd": list(self.mit["kd"]),
                    "t_ref": list(self.mit["t_ref"])}

    def run_movec(self, points):
        """Cartesian MOVE C through start -> via -> end.

        points: [[X,Y,Z,RX,RY,RZ], ...] in the SDK's integer units
        (0.001 mm and 0.001 deg). Exactly 3 points.
        EXPERIMENTAL: implemented from the SDK docstrings, not yet exercised on
        hardware, so the caller gets an explicit ok/error rather than a silent
        no-op.
        """
        if not self.ok:
            return {"ok": False, "error": "arm not connected"}
        try:
            pts = [[int(round(float(v))) for v in p[:6]] for p in (points or [])]
        except Exception as e:
            return {"ok": False, "error": f"bad points: {e!r}"}
        if len(pts) != 3:
            return {"ok": False,
                    "error": "MOVE C needs exactly 3 points (start, via, end)"}
        try:
            self.iface.MoveCAxisUpdateCtrl(0x03)     # clear any previous arc
            time.sleep(0.1)
            self.iface.MotionCtrl_2(ctrl_mode=0x01, move_mode=0x03,
                                    move_spd_rate_ctrl=self.SPEED_PCT)
            time.sleep(0.2)
            for i, p in enumerate(pts):
                self.iface.EndPoseCtrl(*p)
                time.sleep(0.15)
                self.iface.MoveCAxisUpdateCtrl(i)    # 0 start, 1 via, 2 end
                time.sleep(0.15)
            self.iface.MoveCAxisUpdateCtrl(0x03)     # execute
            self.movec = {"points": pts, "active": True}
            log(f"arm {self.side} MOVE C dispatched: {pts}")
            return {"ok": True, "points": pts}
        except Exception as e:
            log(f"arm {self.side} MOVE C failed: {e!r}")
            return {"ok": False, "error": repr(e)}

    # ----------------------------------------------------------- trajectories
    def load_trajectory(self, name, points, dt=0.05):
        """Load a joint trajectory: points = [[6 deg], ...], dt seconds/frame."""
        try:
            pts = [[clamp(float(v), self.limits[j][0], self.limits[j][1])
                    for j, v in enumerate(p[:6])] for p in (points or [])]
        except Exception as e:
            return {"ok": False, "error": f"bad points: {e!r}"}
        if len(pts) < 2:
            return {"ok": False, "error": "need at least 2 points"}
        dt = max(0.01, min(float(dt), 1.0))
        with self.lock:
            self.traj.update({"name": str(name or "unnamed"), "points": pts,
                              "dt": dt, "idx": 0, "playing": False,
                              "loop": False, "speed": 1.0, "plays": 0})
        log(f"arm {self.side} trajectory loaded: {name} "
            f"{len(pts)} points @ {dt:.3f}s")
        return {"ok": True, "name": name, "points": len(pts), "dt": dt}

    def load_trajectory_file(self, path, dt=None):
        """Load a trajectory from disk and hand it to load_trajectory()."""
        try:
            name, pts, file_dt = parse_trajectory_file(path, self.side)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return self.load_trajectory(name, pts, dt if dt else file_dt)

    def play_trajectory(self, loop=False, speed=1.0):
        with self.lock:
            if not self.traj["points"]:
                return {"ok": False, "error": "no trajectory loaded"}
            # Start from where the arm actually is so the first frame is not a
            # jump: the trajectory may have been captured from another pose.
            self._resync_target_locked()
            self.traj.update({"playing": True, "loop": bool(loop),
                              "idx": 0, "plays": 0,
                              "speed": max(0.1, min(float(speed), 4.0)),
                              "t_next": time.time()})
            self.teaching = False
        log(f"arm {self.side} trajectory play: {self.traj['name']} "
            f"loop={loop} speed={speed}")
        return {"ok": True, "name": self.traj["name"],
                "points": len(self.traj["points"])}

    def pause_trajectory(self):
        with self.lock:
            self.traj["playing"] = False
            return {"ok": True, "idx": self.traj["idx"]}

    def stop_trajectory(self):
        with self.lock:
            self.traj.update({"playing": False, "idx": 0})
            self._resync_target_locked()
        log(f"arm {self.side} trajectory stopped, target resynced to measured")
        return {"ok": True}

    def _advance_trajectory(self, now):
        """Step the trajectory clock. Called from the 50 Hz loop."""
        with self.lock:
            tr = self.traj
            if not tr["playing"] or not tr["points"]:
                return
            if now < tr["t_next"]:
                return
            pts = tr["points"]
            i = tr["idx"]
            if i >= len(pts):
                if tr["loop"]:
                    tr["idx"] = i = 0
                    tr["plays"] += 1
                else:
                    tr["playing"] = False
                    log(f"arm {self.side} trajectory finished: {tr['name']}")
                    return
            p = pts[i]
            # Same runaway guard as the manual path: a trajectory captured on
            # another arm must not yank this one across the room.
            for j in range(6):
                want = p[j]
                meas = self._measured[j]
                if want - meas > MAX_TARGET_LAG_DEG:
                    want = meas + MAX_TARGET_LAG_DEG
                elif meas - want > MAX_TARGET_LAG_DEG:
                    want = meas - MAX_TARGET_LAG_DEG
                self.joints[j] = clamp(want, self.limits[j][0],
                                       self.limits[j][1])
            tr["idx"] = i + 1
            tr["t_next"] = now + tr["dt"] / tr["speed"]

    # --------------------------------------------------------------- teaching
    def set_teaching(self, on, execute=False):
        """Drag-teach: 0x01 start record, 0x02 end record, 0x03 execute.

        While recording, the 50 Hz loop sends NOTHING: a Piper can only be
        hand-guided when no position target is being streamed at it. That is
        why teaching is a first-class state and not just a mode.
        """
        if not self.ok:
            return {"ok": False, "error": "arm not connected"}
        try:
            if on:
                with self.lock:
                    self.traj["playing"] = False
                    self._resync_target_locked()
                self.iface.MotionCtrl_1(emergency_stop=0x00,
                                        track_ctrl=0x00, grag_teach_ctrl=0x01)
                self.teaching = True
                log(f"arm {self.side} drag-teach RECORD started")
            elif execute:
                self.iface.MotionCtrl_1(emergency_stop=0x00,
                                        track_ctrl=0x00, grag_teach_ctrl=0x03)
                self.teaching = False
                log(f"arm {self.side} drag-teach EXECUTE")
            else:
                self.iface.MotionCtrl_1(emergency_stop=0x00,
                                        track_ctrl=0x00, grag_teach_ctrl=0x02)
                self.teaching = False
                with self.lock:
                    self._resync_target_locked()
                log(f"arm {self.side} drag-teach RECORD stopped")
            return {"ok": True, "teaching": self.teaching}
        except Exception as e:
            log(f"arm {self.side} teaching failed: {e!r}")
            return {"ok": False, "error": repr(e)}

    # ----------------------------------------------------------- master/slave
    def set_master_slave(self, on, role=None):
        """0xFA = teaching INPUT arm (leader), 0xFC = motion OUTPUT (follower).

        Offsets are 0: both arms keep their own CAN base IDs, which matches the
        current wiring (can_left / can_right on separate buses).
        """
        if not self.ok:
            return {"ok": False, "error": "arm not connected"}
        try:
            if on:
                cfg = 0xFA if role == "leader" else 0xFC
                self.iface.MasterSlaveConfig(linkage_config=cfg,
                                             feedback_offset=0x00,
                                             ctrl_offset=0x00,
                                             linkage_offset=0x00)
                self.master_slave = {"enabled": True, "role": role}
                # Neither end may also be commanded by us: the leader is moved
                # by the operator's hand and the follower mirrors it.
                with self.lock:
                    self.traj["playing"] = False
                log(f"arm {self.side} master/slave ON as {role} (0x{cfg:02X})")
            else:
                self.iface.MasterSlaveConfig(linkage_config=0x00,
                                             feedback_offset=0x00,
                                             ctrl_offset=0x00,
                                             linkage_offset=0x00)
                self.master_slave = {"enabled": False, "role": None}
                with self.lock:
                    self._resync_target_locked()
                log(f"arm {self.side} master/slave OFF")
            return {"ok": True, **self.master_slave}
        except Exception as e:
            log(f"arm {self.side} master/slave failed: {e!r}")
            return {"ok": False, "error": repr(e)}

    def set_target(self, joints_deg=None, joint=None, deg=None):
        """
        ABSOLUTE target control, as opposed to the incremental nudge().

        Clamped to the documented joint limits but NOT to the 2 deg step - a
        target is a destination, not a jog. The arm's own `move_spd_rate_ctrl`
        still bounds how fast it gets there, so a large target is not a large
        jerk.
        """
        if not self.ok:
            return None
        with self.lock:
            if joints_deg is not None:
                try:
                    vals = [float(v) for v in joints_deg][:6]
                except Exception:
                    return None
                if len(vals) < 6:
                    return None
                self.joints = [clamp(vals[i], self.limits[i][0], self.limits[i][1])
                               for i in range(6)]
            elif joint is not None and deg is not None:
                j = int(joint)
                if not (0 <= j <= 5):
                    return None
                self.joints[j] = clamp(float(deg), self.limits[j][0], self.limits[j][1])
            else:
                return None
            return list(self.joints)

    def nudge(self, joint, delta_deg):
        """
        Move the held target by a bounded step.

        THE RUNAWAY GUARD BELOW IS NOT OPTIONAL. This used to accumulate into the
        target with only the joint-limit clamp, so if a motor stopped following -
        which really happens, e.g. the Piper motors fault with
        motor_overheating/driver_error_status and stop tracking - the target kept
        climbing while the arm stood still. Observed on a real recording session:
        left J1 target reached 118 deg while the arm sat at 11 deg, a 107 deg gap
        that would have swung violently the moment the motor recovered.

        Two guards, both cheap:
          1. If the joint is currently faulted, do not accumulate at all - hold
             the target at the measured angle, because the arm cannot follow.
          2. Never let the target sit further than MAX_TARGET_LAG_DEG from the
             measured angle. If the arm cannot keep up, the target is pulled back
             rather than allowed to run away.
        """
        if not self.ok or not (0 <= joint <= 5):
            return None
        delta_deg = clamp(float(delta_deg), -MAX_JOINT_STEP_DEG, MAX_JOINT_STEP_DEG)
        lo, hi = self.limits[joint]
        with self.lock:
            measured = self._measured[joint]
            faulted = bool(self.faults[joint])
            if faulted:
                # Cannot follow: park the target on reality instead of drifting.
                self.joints[joint] = clamp(measured, lo, hi)
                return list(self.joints)

            want = self.joints[joint] + delta_deg
            lag = want - measured
            if abs(lag) > MAX_TARGET_LAG_DEG:
                # Pull back to the allowed envelope around where the arm actually is.
                want = measured + (MAX_TARGET_LAG_DEG if lag > 0 else -MAX_TARGET_LAG_DEG)
            self.joints[joint] = clamp(want, lo, hi)
            return list(self.joints)

    def nudge_gripper(self, delta_deg):
        if not self.ok:
            return None
        with self.lock:
            # Piper gripper: 0 = closed, ~70 deg = fully open
            self._grip_target = clamp(self._grip_target + float(delta_deg), 0.0, 70.0)
            return self._grip_target

    def state(self):
        with self.lock:
            target = [round(v, 3) for v in self.joints]
        return {
            "side": self.side,
            "can": self.channel,
            "connected": self.ok,
            # "enabled" = intent AND reality, so a motor that dropped out on its
            # own still shows as not-enabled rather than silently claiming ok.
            "enabled": bool(self.enabled and any(self.joints_enabled)),
            "firmware": self.firmware,
            "joints_deg": [round(v, 3) for v in self._measured],
            "targets_deg": target,
            "joints_enabled": list(self.joints_enabled),
            "faults": [list(f) for f in self.faults],
            "gripper_deg": round(self.gripper, 3),
            # The commanded gripper position. For imitation learning this - not
            # the measured angle - is the action the policy has to reproduce.
            "gripper_target_deg": round(self._grip_target, 3),
            "error_code": self.error_code,
            "home_deg": list(self.home_deg),
            # Exposed so a shaking arm can be told apart from a saturated CPU:
            # cmd_hz should sit near RATE_HZ, feedback_hz near FEEDBACK_HZ.
            "jitter": {"cmd_hz": self.cmd_hz, "feedback_hz": self.feedback_hz},
            # ---- operating mode ----
            "mode": self.mode,
            "mode_actual": self.mode_actual,
            "mode_verified": self.mode_verified,
            "teaching": self.teaching,
            "mit": {"kp": list(self.mit["kp"]), "kd": list(self.mit["kd"]),
                    "t_ref": list(self.mit["t_ref"])},
            "movec_active": bool(self.movec.get("active")),
            "traj": {"name": self.traj["name"],
                     "points": len(self.traj["points"]),
                     "dt": self.traj["dt"],
                     "idx": self.traj["idx"],
                     "playing": self.traj["playing"],
                     "loop": self.traj["loop"],
                     "speed": self.traj["speed"],
                     "plays": self.traj["plays"]},
            "master_slave": dict(self.master_slave),
        }

    def estop(self):
        try:
            self.enabled = False
            if self.iface:
                self.iface.DisableArm(motor_num=7)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# MJPEG streaming
# ----------------------------------------------------------------------------
def ffmpeg_cmd(dev):
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "v4l2", "-input_format", "mjpeg",
        "-video_size", f"{VIDEO_W}x{VIDEO_H}", "-i", dev,
        "-f", "mjpeg", "-q:v", "6", "-r", str(VIDEO_FPS), "-",
    ]


FRAME_TIMEOUT_S = 8.0
# How long /frame keeps a camera pump alive after the last request, so a
# polling policy client does not restart ffmpeg on every control tick.
FRAME_HOLD_S = float(os.environ.get("AGILEX_FRAME_HOLD", "30"))

# A camera that has just been opened can take much longer than FRAME_TIMEOUT_S
# to deliver its first frame. Measured on this robot: cam2 (right arm) needs
# around 9 seconds to cold-start, especially when the other camera is opening at
# the same moment on the same USB bus. Using the steady-state timeout for the
# FIRST frame made a fresh viewer give up before the camera was ready, which
# looked like "the right camera is broken". Give first frames a longer budget;
# a genuine stall later still trips at FRAME_TIMEOUT_S.
FIRST_FRAME_TIMEOUT_S = float(os.environ.get("AGILEX_FIRST_FRAME_TIMEOUT", "20"))


def release_device(dev):
    """
    Kill any *stale* ffmpeg still holding this V4L2 device.

    V4L2 allows exactly one reader. A leaked ffmpeg - from a previous run, or
    from a test whose SSH connection dropped mid-stream - leaves the camera
    permanently "Device or resource busy" until something kills it. This was a
    real, reproduced failure, not a hypothetical: two orphans held both cameras
    for 10 minutes. Recovering automatically beats making the user notice.
    """
    try:
        out = subprocess.run(["fuser", dev], capture_output=True, text=True, timeout=5)
        pids = [int(p) for p in out.stdout.split() if p.isdigit()]
    except Exception:
        return
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if comm == "ffmpeg":
                os.kill(pid, signal.SIGKILL)
                log(f"{dev}: killed stale ffmpeg pid {pid} that was holding the device")
        except Exception:
            pass


class MjpegSource:
    """
    A JPEG-frame source backed by one ffmpeg process.

    WHY THIS IS A CLASS AND NOT A GENERATOR: a generator's `finally` only runs
    when it is garbage-collected, which can be arbitrarily late. A client that
    disconnects mid-stream therefore left ffmpeg alive still holding /dev/videoN
    - observed live as "/dev/video2: Device or resource busy" and a camera that
    could not be reopened. Everything here is closed explicitly instead.

    ffmpeg is read on a helper thread with a timeout, because a blocking read()
    on a wedged ffmpeg never returns and would pin the device forever.
    """

    def __init__(self, dev):
        self.dev = dev
        self.proc = subprocess.Popen(ffmpeg_cmd(dev), stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
        self._q = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while not self._stop.is_set():
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break
                try:
                    self._q.put(chunk, timeout=2)
                except queue.Full:
                    pass  # client is slow: drop rather than block the device
        except Exception:
            pass
        finally:
            try:
                self._q.put_nowait(None)
            except Exception:
                pass

    def frames(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._q.get(timeout=FRAME_TIMEOUT_S)
            except queue.Empty:
                log(f"video {self.dev}: no data for {FRAME_TIMEOUT_S}s - giving up")
                return
            if chunk is None:
                return
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8")
                if s < 0:
                    buf = buf[-2:]
                    break
                e = buf.find(b"\xff\xd9", s + 2)
                if e < 0:
                    buf = buf[s:]
                    break
                yield buf[s:e + 2]
                buf = buf[e + 2:]

    def close(self):
        """Idempotent. The handler MUST call this however it exits."""
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            pass
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass
        log(f"video {self.dev}: released")


class FrameHub:
    """
    One ffmpeg per camera, shared by every consumer.

    WHY THIS EXISTS: V4L2 devices are EXCLUSIVE - a second opener gets EBUSY.
    Verified on this robot: with one ffmpeg holding /dev/video0, a second one
    failed with "Device or resource busy". So the live MJPEG view and the
    episode recorder CANNOT each open the camera; the second would always fail.

    Instead one source runs per camera and everyone reads the latest frame from
    here. Reference counted, so the device is released when the last consumer
    (a browser tab, the recorder, or both) goes away. This also fixes a latent
    bug: two simultaneous viewers used to fight over the device.
    """

    def __init__(self, dev):
        self.dev = dev
        self._cond = threading.Condition()
        self._latest = None
        self._seq = 0
        self._refs = 0
        self._stop = threading.Event()
        self._thread = None
        self.fps = 0.0
        self._err = None

    # -- consumer lifecycle ---------------------------------------------------
    def acquire(self):
        """
        Bump the refcount and make sure the single pump thread is alive.

        The pump thread is created ONCE and lives for the process lifetime; it
        holds the device only while refs > 0. That is deliberate. The first
        version started a new thread whenever the count went 0 -> 1, but the old
        thread needs up to FRAME_TIMEOUT_S to notice it should stop - so a quick
        release/re-acquire left TWO threads racing for a device only one can
        open. Observed live: after a stream closed and a recording began,
        `cameras_warm` came back {0: False, 2: False} and one camera produced
        zero frames for the whole episode.
        """
        with self._cond:
            self._refs += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._pump, daemon=True)
                self._thread.start()
            self._cond.notify_all()
        return True

    def release(self):
        with self._cond:
            self._refs = max(0, self._refs - 1)
            self._cond.notify_all()

    def wait(self, last_seq, timeout=FRAME_TIMEOUT_S):
        """Block until a frame newer than last_seq exists. Returns (seq, jpg)."""
        with self._cond:
            # Wait when there is nothing to hand out yet (a fresh acquire leaves
            # latest=None, seq=0) OR when the sequence has not moved since the
            # caller's last frame. Testing only `seq == last_seq` made a COLD
            # consumer return instantly with None and see zero frames - a real
            # regression, caught by testing the standalone /video path rather
            # than only the during-recording path.
            if (self._latest is None or self._seq == last_seq) \
                    and not self._stop.is_set():
                self._cond.wait(timeout)
            return self._seq, self._latest

    def snapshot(self):
        with self._cond:
            return self._latest

    def seq_now(self):
        """Frame counter only - lets a caller tell a fresh frame from a repeat."""
        with self._cond:
            return self._seq

    def stats(self):
        with self._cond:
            return {"dev": self.dev, "consumers": self._refs,
                    "seq": self._seq, "fps": round(self.fps, 1),
                    "error": self._err}

    # -- producer -------------------------------------------------------------
    def _pump(self):
        """
        One thread, for the life of the process. Idles while there are no
        consumers and holds the device only while refs > 0, so the device is
        never opened by two threads at once.
        """
        while not self._stop.is_set():
            # Wait until somebody wants frames.
            with self._cond:
                while self._refs == 0 and not self._stop.is_set():
                    self._cond.wait(0.5)
                if self._stop.is_set():
                    return
            src = None
            n = 0
            t0 = time.time()
            try:
                release_device(self.dev)
                src = MjpegSource(self.dev)
                for jpg in src.frames():
                    with self._cond:
                        if self._refs == 0 or self._stop.is_set():
                            break
                        n += 1
                        self._latest = jpg
                        self._seq += 1
                        if n % 10 == 0:
                            el = time.time() - t0
                            self.fps = n / el if el > 0 else 0.0
                        self._cond.notify_all()
            except Exception as e:
                self._err = repr(e)
                log(f"hub {self.dev} error: {e!r}")
            finally:
                if src is not None:
                    src.close()
                with self._cond:
                    # Consumers that come back must not inherit a stale frame.
                    if self._refs == 0:
                        self._latest = None
                        self._seq = 0
                    self._cond.notify_all()
            # Reopen if the stream died while still wanted (unplug, transient
            # EBUSY, ...). Short backoff so a genuine failure does not spin.
            time.sleep(0.4)
        log(f"hub {self.dev}: stopped")


# ----------------------------------------------------------------------------
# Shared control state
# ----------------------------------------------------------------------------
class Control:
    def __init__(self, ros, arms):
        self.ros = ros
        self.arms = list(arms)
        self.arm = self.arms[0] if self.arms else None   # backwards compat
        self.lock = threading.Lock()
        self.lin = 0.0
        self.ang = 0.0
        self.last_drive = 0.0
        self.estop = False
        self.started = time.time()
        threading.Thread(target=self._drive_loop, daemon=True).start()

    def _drive_loop(self):
        """20 Hz. This is where the watchdog actually enforces 'stop'."""
        while True:
            with self.lock:
                stale = (time.time() * 1000 - self.last_drive) > WATCHDOG_MS
                lin, ang = (0.0, 0.0) if (stale or self.estop) else (self.lin, self.ang)
            self.ros.publish(lin, ang)
            time.sleep(0.05)

    def drive(self, lin, ang):
        lin = clamp(float(lin), -MAX_LIN, MAX_LIN)
        ang = clamp(float(ang), -MAX_ANG, MAX_ANG)
        with self.lock:
            if self.estop:
                return (0.0, 0.0)
            self.lin, self.ang = lin, ang
            self.last_drive = time.time() * 1000
        return (lin, ang)

    def zero(self):
        with self.lock:
            self.lin = self.ang = 0.0
            self.last_drive = 0.0

    def set_estop(self, on):
        with self.lock:
            self.estop = bool(on)
            if on:
                self.lin = self.ang = 0.0
                self.last_drive = 0.0
        if on:
            for a in self.arms:      # E-STOP must silence BOTH arms
                a.estop()
        self.ros.publish(0.0, 0.0)

    def pick_arm(self, body):
        """Resolve the optional "arm" field: 0 = left (default), 1 = right."""
        try:
            i = int(body.get("arm", 0))
        except Exception:
            i = 0
        if not (0 <= i < len(self.arms)):
            i = 0
        return self.arms[i]

    def set_master_slave(self, on, leader=0, follower=1):
        """Link two arms: leader = 0xFA teaching input, follower = 0xFC output.

        Returns a per-arm result so the UI can show which end failed. Turning it
        OFF is always attempted on BOTH arms even if one errored, because a
        half-disabled link is worse than either state.
        """
        out = {}
        if on:
            if len(self.arms) < 2:
                return {"ok": False, "error": "need two arms"}
            li, fi = int(leader), int(follower)
            if li == fi or not (0 <= li < len(self.arms)) or \
               not (0 <= fi < len(self.arms)):
                return {"ok": False, "error": "bad leader/follower indices"}
            out["leader"] = self.arms[li].set_master_slave(True, "leader")
            time.sleep(0.2)
            out["follower"] = self.arms[fi].set_master_slave(True, "follower")
        else:
            for i, a in enumerate(self.arms):
                out[f"arm{i}"] = a.set_master_slave(False)
        ok = all(bool(v.get("ok")) for v in out.values())
        return {"ok": ok, "enabled": bool(on), "results": out}

    def status(self):
        with self.lock:
            lin, ang, es = self.lin, self.ang, self.estop
        ch = self.ros.state()
        ch["lin_x"] = round(lin, 4)
        ch["ang_z"] = round(ang, 4)
        arms = [a.state() for a in self.arms]
        return {
            "ok": True,
            "chassis": ch,
            # "arm" kept for backwards compatibility = the left arm.
            "arm": arms[0] if arms else {},
            "arms": arms,
            "limits": {"max_lin": MAX_LIN, "max_ang": MAX_ANG,
                       "max_joint_step_deg": MAX_JOINT_STEP_DEG,
                       # Where the slider should start, vs. what is allowed.
                       "default_lin": DEFAULT_LIN, "default_ang": DEFAULT_ANG,
                       # The hardware ceiling, so the UI can show what the
                       # chassis is actually capable of next to the safe default.
                       "hw_max_lin": HW_MAX_LIN, "hw_max_ang": HW_MAX_ANG,
                       "hw_max_source": HW_MAX_SOURCE},
            "estop": es,
            "watchdog_ms": WATCHDOG_MS,
        }


# ----------------------------------------------------------------------------
# Episode recorder
# ----------------------------------------------------------------------------
EPISODE_ROOT = os.environ.get("AGILEX_EPISODE_ROOT", "/home/agilex/agilex-data/episodes")
RECORD_HZ = float(os.environ.get("AGILEX_RECORD_HZ", "10"))
RECORD_MAX_S = float(os.environ.get("AGILEX_RECORD_MAX_S", "300"))


class EpisodeRecorder:
    """
    Records one demonstration as a self-contained episode directory.

    WHY IT LIVES HERE AND NOT IN ROSBAG: the ROS graph on this robot carries only
    chassis topics - no camera images, no arm joints (cameras are read straight
    from V4L2, arms are driven over CAN via piper_sdk). A rosbag would capture
    none of what a policy needs. This process already holds images, joint angles
    and commanded velocities at the same instant, so recording here is both
    simpler and better synchronised.

    Layout, one directory per episode:
        episode_0001/
            meta.json      task, notes, timing, outcome, counts
            cam0/000001.jpg ...   left arm camera
            cam2/000001.jpg ...   right arm camera
            timeline.csv   one row per tick: t, frame, joint angles, gripper,
                           commanded lin/ang, odometry
    File names are deliberately the same tick index, so a row in timeline.csv
    and the two images with that index are the same moment by construction.
    """

    def __init__(self, control, hubs, root=EPISODE_ROOT):
        self.control = control
        self.hubs = hubs
        self.root = root
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.active = False
        self.episode_dir = None
        self.meta = {}
        self._count = 0
        # Last frame seen per camera, so a tick can still emit an image when the
        # source has not advanced (keeps every episode rectangular).
        self._last_jpg = {}

    # -- lifecycle ------------------------------------------------------------
    def next_index(self):
        """
        Highest existing index + 1.

        NOT len(existing)+1, which is what this did at first: it collides as soon
        as the numbering has a gap or a higher index already exists. Observed
        live - episode_0005 was left over from an earlier run, so four fresh
        recordings in a row all computed index 5 and silently overwrote each
        other, losing three episodes' worth of demonstrations.
        """
        try:
            os.makedirs(self.root, exist_ok=True)
            names = os.listdir(self.root)
        except Exception:
            names = []
        highest = 0
        for d in names:
            m = re.match(r"episode_(\d+)$", d)
            if m:
                highest = max(highest, int(m.group(1)))
        return highest + 1

    def start(self, task="", notes=""):
        with self._lock:
            if self.active:
                return {"ok": False, "error": "already recording",
                        "episode": self.episode_dir}
            idx = self.next_index()
            ep = os.path.join(self.root, f"episode_{idx:04d}")
            if os.path.isdir(ep) and os.listdir(ep):
                # Belt and braces: never silently clobber an existing episode.
                return {"ok": False, "error": f"{ep} already exists and is not empty"}
            try:
                for sub in ("cam0", "cam2"):
                    os.makedirs(os.path.join(ep, sub), exist_ok=True)
            except Exception as e:
                return {"ok": False, "error": f"cannot create {ep}: {e}"}

            self.episode_dir = ep
            self.meta = {
                "index": idx,
                "task": task,
                "notes": notes,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "record_hz": RECORD_HZ,
                "cameras": {"cam0": "left arm", "cam2": "right arm"},
                "frames": 0,
                "duration_s": 0.0,
                "success": None,
            }
            self._count = 0
            self.active = True
            self._stop.clear()
            # Hold the cameras for the whole episode so one ffmpeg serves both
            # the recorder and any live viewer.
            for h in self.hubs.values():
                h.acquire()
            # Wait for each camera to deliver a first frame BEFORE the clock
            # starts. Without this the opening seconds of an episode have no
            # images at all - observed on a real recording: cam0's first jpg was
            # frame 7, and cam2 (cold-starting on the same USB bus) did not
            # produce until ~9 s in, so only 30 of 122 frames had a right-camera
            # image. The dataset has to be rectangular to be trainable.
            warmed = {}
            deadline = time.time() + 12.0
            for cam, hub in self.hubs.items():
                while time.time() < deadline:
                    if hub.snapshot() is not None:
                        warmed[cam] = True
                        break
                    time.sleep(0.15)
                warmed.setdefault(cam, False)
            self.meta["cameras_warm"] = warmed
            if not all(warmed.values()):
                log(f"recorder: cameras not all warm at start: {warmed}")

            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            log(f"recording started: {ep} (task={task!r}) warm={warmed}")
            return {"ok": True, "episode": ep, "index": idx, "cameras_warm": warmed}

    def stop(self, success=None):
        with self._lock:
            if not self.active:
                return {"ok": False, "error": "not recording"}
            self.active = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        for h in self.hubs.values():
            h.release()

        self.meta["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.meta["success"] = success
        try:
            with open(os.path.join(self.episode_dir, "meta.json"), "w") as f:
                json.dump(self.meta, f, indent=2)
        except Exception as e:
            log(f"recorder: could not write meta.json: {e!r}")
        log(f"recording stopped: {self.episode_dir} "
            f"({self._count} frames, {self.meta['duration_s']:.1f}s, success={success})")
        return {"ok": True, "episode": self.episode_dir,
                "frames": self._count, "duration_s": self.meta["duration_s"]}

    def status(self):
        return {
            "active": self.active,
            "episode": self.episode_dir,
            "frames": self._count,
            "duration_s": round(self.meta.get("duration_s", 0.0), 2),
            "root": self.root,
            "record_hz": RECORD_HZ,
            "hubs": [h.stats() for h in self.hubs.values()],
        }

    # -- capture loop ---------------------------------------------------------
    def _loop(self):
        ep = self.episode_dir
        csv_path = os.path.join(ep, "timeline.csv")
        t_start = time.time()
        period = 1.0 / RECORD_HZ if RECORD_HZ > 0 else 0.1
        try:
            f = open(csv_path, "w", newline="")
        except Exception as e:
            log(f"recorder: cannot open {csv_path}: {e!r}")
            self.active = False
            for h in self.hubs.values():
                h.release()
            return

        with f:
            w = csv.writer(f)
            header = ["t", "frame"]
            # observation.state - what the robot measured
            header += [f"L_j{i}" for i in range(6)] + ["L_grip"]
            header += [f"R_j{i}" for i in range(6)] + ["R_grip"]
            # action - what the operator COMMANDED. A behaviour-cloning policy
            # learns to reproduce these, not the measured angles, so recording
            # only the measured state would leave the dataset untrainable.
            header += [f"L_t{i}" for i in range(6)] + ["L_tgrip"]
            header += [f"R_t{i}" for i in range(6)] + ["R_tgrip"]
            header += ["cmd_lin", "cmd_ang", "odom_x", "odom_y", "battery_v"]
            # Source frame counters, so a consumer can tell a genuinely new
            # camera frame from a repeated one (see the image block below).
            header += ["cam0_seq", "cam2_seq"]
            w.writerow(header)

            nxt = t_start
            while not self._stop.is_set():
                now = time.time()
                if now - t_start > RECORD_MAX_S:
                    log(f"recorder: hit {RECORD_MAX_S}s cap, stopping")
                    break
                if now < nxt:
                    time.sleep(min(period, nxt - now))
                    continue
                nxt += period

                self._count += 1
                frame = self._count
                t = time.time() - t_start

                # Images: the hub holds the newest frame, which is fine at 10 Hz
                # against a 28 Hz source. ALWAYS write both cameras, repeating the
                # previous frame if the source has not advanced - otherwise the
                # episode is ragged (cam2 could miss 90 of 122 frames) and every
                # consumer downstream has to handle holes. The cam*_seq columns
                # record whether the frame was actually new.
                seqs = {}
                for cam, sub in ((0, "cam0"), (2, "cam2")):
                    hub = self.hubs.get(cam)
                    jpg = hub.snapshot() if hub else None
                    seq = hub.seq_now() if hub else 0
                    seqs[cam] = seq
                    if jpg:
                        self._last_jpg[cam] = jpg
                    jpg = self._last_jpg.get(cam)
                    if jpg:
                        try:
                            with open(os.path.join(ep, sub, f"{frame:06d}.jpg"), "wb") as im:
                                im.write(jpg)
                        except Exception as e:
                            log(f"recorder: image write failed: {e!r}")

                # State + action. Reading the cached state() keeps this off the
                # CAN path - it must never block the 50 Hz control thread.
                try:
                    st = self.control.status()
                except Exception:
                    st = {}
                arms = st.get("arms") or [{}, {}]
                row = [round(t, 4), frame]
                # observation.state
                for a in (arms[0], arms[1] if len(arms) > 1 else {}):
                    js = a.get("joints_deg") or [0.0] * 6
                    row += [round(v, 3) for v in (list(js) + [0.0] * 6)[:6]]
                    row.append(round(float(a.get("gripper_deg") or 0.0), 3))
                # action
                for a in (arms[0], arms[1] if len(arms) > 1 else {}):
                    tg = a.get("targets_deg") or [0.0] * 6
                    row += [round(v, 3) for v in (list(tg) + [0.0] * 6)[:6]]
                    row.append(round(float(a.get("gripper_target_deg") or 0.0), 3))
                ch = st.get("chassis") or {}
                row += [round(float(ch.get("lin_x") or 0.0), 4),
                        round(float(ch.get("ang_z") or 0.0), 4),
                        round(float(ch.get("odom_x") or 0.0), 4),
                        round(float(ch.get("odom_y") or 0.0), 4),
                        ch.get("battery_v"),
                        seqs.get(0, 0), seqs.get(2, 0)]
                try:
                    w.writerow(row)
                except Exception as e:
                    log(f"recorder: csv write failed: {e!r}")

                self.meta["frames"] = self._count
                self.meta["duration_s"] = round(t, 2)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    control = None
    hubs = {}
    recorder = None
    ui_bytes = b"<h1>remote-ui.html not installed</h1>"

    def log_message(self, *a):
        pass  # keep the journal clean; we log our own events

    def _from_loopback(self):
        try:
            return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
        except Exception:
            return False

    def _authorised(self):
        """Loopback is trusted (the Mac's SSH tunnel). Others must have signed in."""
        if ALLOW_ANON or self._from_loopback():
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME and v and secrets.compare_digest(v, ACCESS_TOKEN):
                return True
        # ?token= also works, for a bookmark or a debugging curl
        if "?" in self.path:
            m = re.search(r"token=([A-Za-z0-9_\-]+)", self.path.split("?", 1)[1])
            if m and secrets.compare_digest(m.group(1), ACCESS_TOKEN):
                return True
        return False

    def _deny(self):
        log(f"denied {self.command} {self.path} from {self.client_address[0]}")
        self._json({"ok": False, "error": "not signed in"}, 401)

    def _login_page(self):
        body = LOGIN_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            # Not signed in -> show the login form instead of the controller.
            # This is what makes the Android WebView work without any special
            # auth handling in the app.
            if not self._authorised():
                self._login_page()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(self.ui_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(self.ui_bytes)
            return
        if not self._authorised():
            self._deny()
            return
        if path == "/api/status":
            st = self.control.status()
            # Surface recording state alongside everything else, so a client can
            # show "recording..." without polling a second endpoint.
            if self.recorder is not None:
                st["recorder"] = self.recorder.status()
            self._json(st)
            return
        if path == "/frame":
            # Single JPEG, for policy clients. /video is a multipart stream meant
            # for an <img> tag; a client that only wants one frame per control
            # tick would otherwise open a new stream each time, which restarts the
            # ffmpeg consumer and costs ~1.7 s per grab - far too slow for a 5 Hz
            # control loop. Reading the hub's newest frame is immediate, and it
            # does not add a second consumer on an exclusive V4L2 device.
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            m = re.search(r"cam=(\d+)", q)
            cam = int(m.group(1)) if m else 0
            hub = self.hubs.get(cam)
            if not hub:
                self._json({"ok": False, "error": f"camera {cam} not configured"}, 503)
                return

            # The pump only runs while refs > 0, so the first /frame for a camera
            # has to acquire. We then HOLD that ref for FRAME_HOLD_S after the
            # last request: a policy client polls at a few Hz, and releasing on
            # every request would stop and restart ffmpeg each tick (cold start
            # is ~9 s on cam2). The ref is given back by the sweep below.
            now = time.time()
            with self.server.frame_lock:
                held = self.server.frame_held.get(cam)
                if held is None:
                    hub.acquire()
                    self.server.frame_held[cam] = now
                    log(f"/frame: acquired camera {cam} for a policy client")
                else:
                    self.server.frame_held[cam] = now
            # sweep: release cameras nobody has asked for in a while
            with self.server.frame_lock:
                for c, last in list(self.server.frame_held.items()):
                    if now - last > FRAME_HOLD_S:
                        h = self.hubs.get(c)
                        if h:
                            h.release()
                        del self.server.frame_held[c]
                        log(f"/frame: released camera {c} after {FRAME_HOLD_S}s idle")

            last_frame = hub.wait(None, timeout=FRAME_TIMEOUT_S)
            jpg = last_frame if isinstance(last_frame, (bytes, bytearray)) else hub.snapshot()
            if jpg is None:
                self._json({"ok": False, "error": f"camera {cam} has no frame yet"}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(jpg)
            return

        if path == "/video":
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            m = re.search(r"cam=(\d+)", q)
            cam = int(m.group(1)) if m else 0
            dev = VIDEO_DEV.get(cam)
            hub = self.hubs.get(cam)
            if not dev or not hub or not os.path.exists(dev):
                self._json({"ok": False, "error": f"camera {cam} ({dev}) not present"}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            # ONE ffmpeg per camera lives in the hub; this handler only reads the
            # newest frame. The old code opened the device here, which meant a
            # second viewer - or the episode recorder - always lost with
            # "Device or resource busy", because V4L2 is exclusive.
            hub.acquire()
            last = -1
            first = True
            try:
                while True:
                    # A cold camera needs longer for its very first frame; only
                    # after that does the steady-state timeout apply.
                    budget = FIRST_FRAME_TIMEOUT_S if first else FRAME_TIMEOUT_S
                    seq, jpg = hub.wait(last, timeout=budget)
                    if jpg is None or seq == last:
                        log(f"video {cam}: no new frame in {budget:g}s - ending")
                        break
                    first = False
                    last = seq
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass                      # client went away
            except Exception as e:
                log(f"video {cam} error: {e!r}")
            finally:
                hub.release()
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]

        # Login must be reachable while unauthenticated.
        if path == "/api/login":
            b = self._body()
            pw = str(b.get("password", ""))
            if ALLOW_ANON or self._from_loopback() or \
               (pw and secrets.compare_digest(pw, PASSWORD)):
                log(f"login OK from {self.client_address[0]}")
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie",
                                 f"{COOKIE_NAME}={ACCESS_TOKEN}; Path=/; SameSite=Lax")
                self.end_headers()
                self.wfile.write(body)
            else:
                log(f"login FAILED from {self.client_address[0]}")
                time.sleep(1.0)          # slow down guessing
                self._json({"ok": False, "error": "wrong password"}, 401)
            return

        if path == "/api/logout":
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie",
                             f"{COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax")
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._authorised():
            self._deny()
            return
        c = self.control
        try:
            if path == "/api/drive":
                b = self._body()
                lin, ang = c.drive(b.get("lin", 0.0), b.get("ang", 0.0))
                self._json({"ok": True, "applied": {"lin": round(lin, 4), "ang": round(ang, 4)}})
            elif path == "/api/arm":
                b = self._body()
                a = c.pick_arm(b)
                t = a.nudge(int(b.get("joint", 0)), b.get("delta_deg", 0.0))
                self._json({"ok": t is not None, "targets_deg": t})
            elif path == "/api/arm/target":
                # Absolute position control: joints_deg for a full pose, or
                # joint+deg for a single axis.
                b = self._body()
                a = c.pick_arm(b)
                t = a.set_target(joints_deg=b.get("joints_deg"),
                                 joint=b.get("joint"), deg=b.get("deg"))
                self._json({"ok": t is not None, "targets_deg": t})
            elif path == "/api/arm/reset":
                b = self._body()
                a = c.pick_arm(b)
                ok = a.reset()
                self._json({"ok": ok, "enabled": a.state()["enabled"],
                            "joints_deg": a.state()["joints_deg"],
                            "targets_deg": a.state()["targets_deg"]})
            elif path == "/api/arm/home":
                b = self._body()
                a = c.pick_arm(b)
                t = a.go_home()
                self._json({"ok": t is not None, "moving_to": t})
            elif path == "/api/arm/enable":
                b = self._body()
                a = c.pick_arm(b)
                a.set_enabled(bool(b.get("enable", True)))
                self._json({"ok": True, "enabled": a.state()["enabled"]})
            elif path == "/api/gripper":
                b = self._body()
                a = c.pick_arm(b)
                t = a.nudge_gripper(b.get("delta_deg", 0.0))
                self._json({"ok": t is not None, "gripper_target_deg": t})
            elif path == "/api/arm/mode":
                b = self._body()
                a = c.pick_arm(b)
                ok = a.set_mode(b.get("mode"))
                self._json({"ok": ok, "mode": a.mode,
                            "modes": list(a.MODES)})
            elif path == "/api/arm/mit":
                b = self._body()
                a = c.pick_arm(b)
                g = a.set_mit(kp=b.get("kp"), kd=b.get("kd"),
                              t_ref=b.get("t_ref"))
                self._json({"ok": True, "mit": g, "mode": a.mode})
            elif path == "/api/arm/movec":
                b = self._body()
                a = c.pick_arm(b)
                self._json(a.run_movec(b.get("points")))
            elif path == "/api/traj/load":
                b = self._body()
                a = c.pick_arm(b)
                self._json(a.load_trajectory(b.get("name"), b.get("points"),
                                             b.get("dt", 0.05)))
            elif path == "/api/traj/load-file":
                b = self._body()
                a = c.pick_arm(b)
                self._json(a.load_trajectory_file(b.get("path"), b.get("dt")))
            elif path == "/api/traj/list":
                self._json({"ok": True, "items": list_trajectories()})
            elif path == "/api/traj/play":
                b = self._body()
                a = c.pick_arm(b)
                self._json(a.play_trajectory(loop=b.get("loop", False),
                                             speed=b.get("speed", 1.0)))
            elif path == "/api/traj/pause":
                b = self._body()
                self._json(c.pick_arm(b).pause_trajectory())
            elif path == "/api/traj/stop":
                b = self._body()
                self._json(c.pick_arm(b).stop_trajectory())
            elif path == "/api/teach":
                b = self._body()
                a = c.pick_arm(b)
                self._json(a.set_teaching(bool(b.get("on", True)),
                                          execute=bool(b.get("execute", False))))
            elif path == "/api/master-slave":
                b = self._body()
                self._json(c.set_master_slave(bool(b.get("on", True)),
                                              leader=b.get("leader", 0),
                                              follower=b.get("follower", 1)))
            elif path == "/api/chassis/clear-fault":
                ok = c.ros.clear_fault()
                self._json({"ok": ok, "error_code": c.ros.error_code})
            elif path == "/api/record/start":
                b = self._body()
                rec = self.recorder
                r = rec.start(task=b.get("task", ""), notes=b.get("notes", ""))
                self._json(r)
            elif path == "/api/record/stop":
                b = self._body()
                rec = self.recorder
                # success may be true / false / null (unlabelled)
                self._json(rec.stop(success=b.get("success")))
            elif path == "/api/record/status":
                self._json(self.recorder.status())
            elif path == "/api/estop":
                c.set_estop(True)
                log("E-STOP engaged")
                self._json({"ok": True, "estop": True})
            elif path == "/api/estop/clear":
                c.set_estop(False)
                log("E-STOP cleared")
                self._json({"ok": True, "estop": False})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            log(f"POST {path} failed: {e!r}")
            self._json({"ok": False, "error": str(e)}, 500)


def main():
    global ACCESS_TOKEN
    log("starting agilex remote server")
    ACCESS_TOKEN = load_or_make_session()
    log(f"access control: {'DISABLED (AGILEX_ALLOW_ANON=1)' if ALLOW_ANON else 'cookie required off-loopback'}")
    ros = RosBridge()
    # Two Piper arms, six motors each. can_left is the left arm, can_right the
    # right. Order here defines the "arm" index used by the API (0 = left).
    arms = []
    for side, chan in (("left", CAN_CHANNEL), ("right", CAN_CHANNEL_RIGHT)):
        try:
            arms.append(ArmBridge(chan, JOINT_LIMITS_DEG, side=side))
        except Exception as e:
            log(f"arm {side} ({chan}) failed to initialise: {e!r}")
    if not arms:
        log("WARNING: no arms initialised - arm endpoints will report failure")
    ctl = Control(ros, arms)
    Handler.control = ctl
    # One frame hub per camera. Both the live MJPEG view and the episode
    # recorder read from these, because a V4L2 device cannot be opened twice -
    # verified: a second ffmpeg gets "Device or resource busy".
    hubs = {cam: FrameHub(dev) for cam, dev in VIDEO_DEV.items()}
    Handler.hubs = hubs
    Handler.recorder = EpisodeRecorder(ctl, hubs)
    log(f"recorder ready: root={EPISODE_ROOT} at {RECORD_HZ:g} Hz")
    # Clear a latched chassis fault at startup. Without this the chassis keeps
    # reporting error_code 4 and silently ignores every cmd_vel, which is
    # indistinguishable from "the controller is broken". Logged loudly so a
    # recurring fault is visible rather than hidden by the auto-clear.
    if os.environ.get("AGILEX_AUTO_CLEAR_FAULT", "1") == "1":
        ros.clear_fault()
    if os.path.exists(UI_PATH):
        with open(UI_PATH, "rb") as f:
            Handler.ui_bytes = f.read()
        log(f"UI loaded: {UI_PATH} ({len(Handler.ui_bytes)} bytes)")
    else:
        log(f"WARNING: UI not found at {UI_PATH}")

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    # Shared state for the /frame hold: which cameras a policy client has asked
    # for recently, and the lock guarding it. ThreadingHTTPServer instantiates a
    # new Handler per request, so this cannot live on the handler.
    srv.frame_lock = threading.Lock()
    srv.frame_held = {}
    srv.daemon_threads = True
    log(f"listening on http://{HOST}:{PORT}")

    def bye(*_):
        ctl.zero()
        ctl.ros.publish(0.0, 0.0)
        log("shutting down; velocity zeroed")
        os._exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    srv.serve_forever()


if __name__ == "__main__":
    main()
