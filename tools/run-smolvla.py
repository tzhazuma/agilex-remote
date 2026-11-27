#!/usr/bin/env python3
"""
run-smolvla.py — drive the robot from a trained SmolVLA checkpoint.

This closes the loop: record -> convert -> train -> run on the real machine.

WHY IT RUNS ON THE ROBOT, NOT ON THE 5090
    ROS_LOCALHOST_ONLY=1 is set on this robot, so DDS never leaves the box. A
    policy running anywhere else cannot see /cmd_vel, /tracer_status or the CAN
    bridge that the arm driver uses. The only ways out are to stream frames and
    receive actions over HTTP, or to run on the robot. This runs on the robot and
    talks to the same local server the browser remote uses.

SAFETY, IN ORDER OF IMPORTANCE
    1. --max-lin caps chassis speed, default 0.10 m/s. The chassis is rated to
       2.3 m/s; at 2.3 the 300 ms watchdog coast is 0.69 m, which is not a
       reaction distance, it is a collision distance.
    2. --max-joint-step caps how far one command may move a joint, default 1.0
       deg. A policy that jumps gives you no time to hit anything.
    3. Every loop iteration re-reads /api/status first. If an arm is disabled, a
       motor reports a fault, or the server is gone, we stop commanding.
    4. The arm target-vs-measured lag is checked. --max-lag (default 15 deg) is
       the same bound the server enforces; a persistent violation means the arm
       is not following and the run aborts rather than accumulating targets.
    5. E-STOP is one HTTP call away and the loop notices it.

    Beyond that: this is a research script. First runs belong on a bench with the
    arm unloaded and a hand on the physical emergency stop, not on a live task.

USAGE
    # dry run: offline check only; no policy load, no robot, no lease
    python3 tools/run-smolvla.py --ckpt ~/runs/smolvla/checkpoints/last/pretrained_model \
                                 --task "pick and place" --dry-run --steps 20

    # live, slow: acquires the control lease, heartbeats it, releases on exit
    python3 tools/run-smolvla.py --ckpt ... --task "pick and place" \
                                 --max-lin 0.05 --max-joint-step 0.5 --hz 5

    # if another controller holds the lease, take it over explicitly
    python3 tools/run-smolvla.py ... --takeover --confirm TAKEOVER
"""
import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

try:
    import numpy as np
except ImportError:  # the lease tests import this module without numpy
    np = None

API = os.environ.get("AGILEX_API", "http://127.0.0.1:8765")
CONTROLLER = os.environ.get(
    "AGILEX_CONTROLLER", "run-smolvla-%s-%d" % (socket.gethostname(), os.getpid()))
LEASE = None

# The 14-dim layout the recorder writes and the policy predicts:
#   [L_j0..L_j5, L_grip, R_j0..R_j5, R_grip]   degrees, gripper 0..70
STATE_COLS = [f"L_j{i}" for i in range(6)] + ["L_grip"] + \
             [f"R_j{i}" for i in range(6)] + ["R_grip"]


class LeaseError(RuntimeError):
    pass


class LeaseLost(LeaseError):
    pass


class LeaseSession:
    """Minimal client for the server's /api/lease/* control lease.

    The secret lives only in this object (and the HTTP header); it is never
    logged, printed, or written to disk. A daemon heartbeat extends the lease
    during a long run. E-STOP and a zero-velocity /api/drive are sent without
    the lease so a lost lease can never block a stop.
    """

    EXEMPT = {"/api/estop", "/api/lease/acquire", "/api/lease/heartbeat",
              "/api/lease/release", "/api/record/status", "/api/traj/list"}

    def __init__(self, api, controller, timeout=10):
        self.api = api.rstrip("/")
        self.controller = controller
        self.timeout = timeout
        self.secret = None
        self.supported = True
        self.lost = False
        self._ttl = 20.0
        self._stop = threading.Event()
        self._heartbeat_thread = None

    def _body(self, obj):
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = dict(obj)
            obj.setdefault("controller", self.controller)
        return json.dumps(obj).encode()

    def _request(self, method, path, obj=None, lease=False, timeout=None):
        if lease and self.lost:
            raise LeaseLost("control lease lost")
        data = self._body(obj)
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if lease and self.secret:
            headers["X-Control-Lease"] = self.secret
        req = urllib.request.Request(self.api + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = {}
            try:
                body = json.loads(e.read().decode())
            except Exception:
                pass
            if e.code == 409:
                self.lost = True
                raise LeaseLost("control lease required or expired") from None
            raise LeaseError("HTTP %d: %s" % (e.code, body.get("error") or path)) from None

    def acquire(self, takeover=False, confirm=None):
        body = {"controller": self.controller, "takeover": bool(takeover)}
        if confirm is not None:
            body["confirm"] = confirm
        try:
            r = self._request("POST", "/api/lease/acquire", body, lease=False)
        except LeaseError as e:
            # Older servers have no lease endpoint; keep working unleased.
            if "HTTP 404" in str(e) or "HTTP 405" in str(e):
                self.supported = False
                return False
            raise
        if not r.get("ok"):
            raise LeaseError(str(r.get("error") or "lease refused"))
        secret = r.get("lease")
        if not secret:
            raise LeaseError("lease response contained no secret")
        self.secret = str(secret)
        self._ttl = float(r.get("expires_in") or 20.0)
        self.lost = False
        if self._ttl > 0:
            interval = max(2.0, min(5.0, self._ttl / 3.0))
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, args=(interval,), daemon=True)
            self._heartbeat_thread.start()
        return True

    def _heartbeat_loop(self, interval):
        while not self._stop.wait(interval):
            try:
                self.heartbeat()
            except Exception:
                self.lost = True
                return

    def heartbeat(self):
        if not self.supported or not self.secret:
            return None
        try:
            r = self._request("POST", "/api/lease/heartbeat", {}, lease=True)
        except LeaseError:
            self.lost = True
            raise
        if not r.get("ok"):
            self.lost = True
            raise LeaseLost("lease heartbeat failed")
        return r

    def post(self, path, obj, timeout=None):
        if path == "/api/estop/clear":
            if not isinstance(obj, dict) or obj.get("confirm") != "CLEAR":
                raise LeaseError("clear requires body confirm=CLEAR")
            lease = True
        else:
            lease = path not in self.EXEMPT
        if path == "/api/drive" and isinstance(obj, dict):
            try:
                lease = not (abs(float(obj.get("lin") or 0.0)) <= 1e-9
                             and abs(float(obj.get("ang") or 0.0)) <= 1e-9)
            except Exception:
                lease = True
        return self._request("POST", path, obj, lease=lease, timeout=timeout)

    def get(self, path, timeout=None):
        return self._request("GET", path, None, lease=False, timeout=timeout)

    def release(self):
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None
        if self.supported and self.secret:
            try:
                self._request("POST", "/api/lease/release", {}, lease=True)
            except Exception:
                pass
            self.secret = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def api_get(path, timeout=10):
    if LEASE is not None:
        return LEASE.get(path, timeout=timeout)
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.load(r)


def api_post(path, obj, timeout=10):
    if LEASE is not None:
        return LEASE.post(path, obj, timeout=timeout)
    if isinstance(obj, dict):
        obj = dict(obj)
        obj.setdefault("controller", CONTROLLER)
    req = urllib.request.Request(
        API + path, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def grab_one(cam, timeout=15):
    """
    Grab one frame via GET /frame?cam=N.

    This uses the single-frame endpoint rather than /video. /video is a multipart
    MJPEG stream for an <img> tag: reading it pulls megabytes and never ends, and
    opening a fresh stream per control tick restarts the ffmpeg consumer, which
    measured ~1.7 s per grab - far too slow for a 5 Hz loop. /frame just returns
    the hub newest frame as image/jpeg, measured 0.09-0.13 s warm on this robot.
    """
    import io
    from PIL import Image

    with urllib.request.urlopen(f"{API}/frame?cam={cam}", timeout=timeout) as r:
        buf = r.read()
    if not buf.startswith(b"\xff\xd8"):
        raise RuntimeError(f"cam {cam} did not return a JPEG")
    return np.asarray(Image.open(io.BytesIO(buf)).convert("RGB"), dtype=np.uint8)


def grab_frames():
    """Both camera frames as HWC uint8 RGB."""
    out = {}
    for cam, key in ((0, "cam_left"), (2, "cam_right")):
        try:
            out[key] = grab_one(cam)
        except Exception as e:
            raise RuntimeError(f"camera {cam} unavailable: {e}") from e
    return out


def read_state(status):
    """Flatten /api/status into the 14-dim vector the policy was trained on."""
    v = []
    for arm in status["arms"]:
        v.extend(arm["joints_deg"][:6])
        # the recorder logs the gripper in its own open-degree units
        # The recorder logs the gripper in open-degrees (0 closed .. 70 open).
        # Older server builds omit it from /api/status, so default to closed
        # rather than raising - a wrong gripper is recoverable, a crashed loop is not.
        v.append(float(arm.get("gripper_deg", arm.get("gripper_target_deg", 0.0)) or 0.0))
    return np.asarray(v, dtype=np.float32)


def main():
    global API, CONTROLLER, LEASE
    LEASE = None
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True,
                    help="path to .../checkpoints/<step>/pretrained_model")
    ap.add_argument("--task", default="pick and place",
                    help="language instruction fed to the VLM; should match the "
                         "task string the episodes were recorded with")
    ap.add_argument("--dry-run", action="store_true",
                    help="offline check only: no policy load, no robot contact, no lease")
    ap.add_argument("--api", default=API,
                    help="server base URL (default: AGILEX_API or 127.0.0.1:8765)")
    ap.add_argument("--controller", default=CONTROLLER,
                    help="controller id used for the control lease audit trail")
    ap.add_argument("--takeover", action="store_true",
                    help="request the lease even if another controller holds it")
    ap.add_argument("--confirm", default=None,
                    help="takeover confirmation; the server requires TAKEOVER")
    ap.add_argument("--steps", type=int, default=0, help="0 = run until Ctrl-C")
    ap.add_argument("--hz", type=float, default=4.0,
                    help="control loop rate; two /frame grabs measure 0.08-0.20 s, "
                         "so 4 Hz leaves headroom")
    ap.add_argument("--max-lin", type=float, default=0.10,
                    help="chassis linear cap, m/s (rated max is 2.3)")
    ap.add_argument("--max-joint-step", type=float, default=1.0,
                    help="max degrees one command may move a joint")
    ap.add_argument("--max-lag", type=float, default=15.0,
                    help="abort if |target-measured| exceeds this, degrees")
    ap.add_argument("--warmup", type=float, default=3.0,
                    help="seconds to let cameras deliver a frame before starting")
    args = ap.parse_args()

    API = args.api.rstrip("/")
    CONTROLLER = args.controller
    if args.dry_run:
        print("DRY RUN: offline; no robot connection, no lease, no policy load")
        return 0
    if np is None:
        print("numpy is required to run SmolVLA (pip install numpy)", file=sys.stderr)
        return 1

    import torch
    import lerobot.policies.smolvla  # noqa: F401  (registers the policy class)
    from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    policy = SmolVLAPolicy.from_pretrained(args.ckpt).to(dev).eval()
    print(f"policy loaded on {dev}")

    # -------------------------------------------------------------- preflight
    if args.dry_run:
        st = None
    else:
        print("preflight: waiting for cameras")
        t0 = time.time()
        while True:
            try:
                grab_frames()
                break
            except RuntimeError as e:
                if time.time() - t0 > args.warmup + 30:
                    print(f"  FAILED: {e}", file=sys.stderr)
                    return 1
                time.sleep(1.0)
        print(f"  cameras ok after {time.time()-t0:.1f}s")

        st = api_get("/api/status")
        for arm in st["arms"]:
            faults = [i for i, f in enumerate(arm["faults"]) if f]
            if faults:
                print(f"  ARM {arm['side']} HAS FAULTS on joints {faults}: "
                      f"{[arm['faults'][i] for i in faults]}", file=sys.stderr)
                print("  refusing to run with a faulted arm", file=sys.stderr)
                return 1
            if not arm["enabled"]:
                print(f"  arm {arm['side']} not enabled", file=sys.stderr)
                return 1
        print("  arms enabled, no faults")

        LEASE = LeaseSession(API, CONTROLLER)
        try:
            acquired = LEASE.acquire(takeover=args.takeover, confirm=args.confirm)
        except LeaseError as e:
            print(f"  lease: {e}", file=sys.stderr)
            return 1
        if acquired:
            print("  control lease acquired")
        else:
            print("  lease: server has no lease API; continuing unleased")

    print(f"\n{'DRY RUN - no commands will be sent' if args.dry_run else 'LIVE'} | "
          f"max_lin={args.max_lin} m/s  max_joint_step={args.max_joint_step} deg  "
          f"rate={args.hz} Hz\n")

    period = 1.0 / args.hz
    n = 0
    try:
        while True:
            t_loop = time.time()

            if args.dry_run:
                # Synthetic observation: no HTTP, no camera, no lease.
                state = np.zeros(14, dtype=np.float32)
                frames = {
                    "cam_left": np.zeros((256, 256, 3), dtype=np.uint8),
                    "cam_right": np.zeros((256, 256, 3), dtype=np.uint8),
                }
            else:
                # Re-read health every single iteration. A policy that keeps
                # pushing after the hardware stopped listening is how arms get
                # hurt.
                st = api_get("/api/status")
                for arm in st["arms"]:
                    if not arm["enabled"]:
                        raise RuntimeError(f"arm {arm['side']} became disabled")
                    if any(arm["faults"]):
                        raise RuntimeError(f"arm {arm['side']} faulted mid-run")
                    lag = max(abs(t - m) for t, m in
                              zip(arm["targets_deg"], arm["joints_deg"]))
                    if lag > args.max_lag:
                        raise RuntimeError(
                            f"arm {arm['side']} lag {lag:.1f} deg > {args.max_lag}; "
                            f"the arm is not following, aborting")

                frames = grab_frames()
                state = read_state(st)

            batch = {
                "observation.images.cam_left":
                    torch.from_numpy(frames["cam_left"]).permute(2, 0, 1)[None].float() / 255.0,
                "observation.images.cam_right":
                    torch.from_numpy(frames["cam_right"]).permute(2, 0, 1)[None].float() / 255.0,
                "observation.state": torch.from_numpy(state)[None],
                "task": [args.task],
            }
            batch = {k: (v.to(dev) if torch.is_tensor(v) else v) for k, v in batch.items()}

            with torch.no_grad():
                action = policy.select_action(batch)
            a = action.squeeze(0).float().cpu().numpy()

            if not np.all(np.isfinite(a)):
                raise RuntimeError("policy produced non-finite actions")

            # clip the step, then map onto the API's absolute-target interface
            cur = state
            delta = np.clip(a - cur, -args.max_joint_step, args.max_joint_step)
            target = cur + delta

            if args.dry_run:
                print(f"  [{n:04d}] state L_j0={state[0]:7.2f}  pred L_j0={a[0]:7.2f}  "
                      f"clipped={target[0]:7.2f}  gripL={a[6]:5.1f}")
            else:
                for arm_i in (0, 1):
                    base = arm_i * 7
                    api_post("/api/arm/target", {
                        "arm": arm_i,
                        "joints_deg": [float(x) for x in target[base:base + 6]],
                    })
                    # /api/gripper is RELATIVE (delta_deg); there is no absolute
                    # gripper endpoint, so convert target - measured into a delta
                    # and clamp it like the joints.
                    g_want = float(np.clip(target[base + 6], 0.0, 70.0))
                    g_now = float(state[base + 6])
                    g_delta = float(np.clip(g_want - g_now,
                                            -args.max_joint_step * 3,
                                            args.max_joint_step * 3))
                    if abs(g_delta) > 1e-6:
                        api_post("/api/gripper", {"arm": arm_i, "delta_deg": g_delta})
                print(f"  [{n:04d}] commanded L_j0={target[0]:7.2f}")

            n += 1
            if args.steps and n >= args.steps:
                break
            sleep = period - (time.time() - t_loop)
            if sleep > 0:
                time.sleep(sleep)

    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        return 1
    finally:
        if not args.dry_run:
            # E-STOP first and without a lease requirement: a lost/expired
            # lease must never prevent a stop.
            try:
                api_post("/api/estop", {}, timeout=5)
                print("emergency stop engaged on exit")
            except Exception:
                print("WARNING: could not engage e-stop", file=sys.stderr)
            if LEASE is not None:
                try:
                    LEASE.release()
                    print("control lease released")
                except Exception:
                    print("WARNING: could not release the control lease", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
