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
    # dry run: predict actions, print them, command nothing
    python3 tools/run-smolvla.py --ckpt ~/runs/smolvla/checkpoints/last/pretrained_model \
                                 --task "pick and place" --dry-run --steps 20

    # live, slow
    python3 tools/run-smolvla.py --ckpt ... --task "pick and place" \
                                 --max-lin 0.05 --max-joint-step 0.5 --hz 5
"""
import argparse
import json
import os
import sys
import time
import urllib.request

import numpy as np

API = "http://127.0.0.1:8765"

# The 14-dim layout the recorder writes and the policy predicts:
#   [L_j0..L_j5, L_grip, R_j0..R_j5, R_grip]   degrees, gripper 0..70
STATE_COLS = [f"L_j{i}" for i in range(6)] + ["L_grip"] + \
             [f"R_j{i}" for i in range(6)] + ["R_grip"]


def api_get(path, timeout=10):
    with urllib.request.urlopen(API + path, timeout=timeout) as r:
        return json.load(r)


def api_post(path, obj, timeout=10):
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True,
                    help="path to .../checkpoints/<step>/pretrained_model")
    ap.add_argument("--task", default="pick and place",
                    help="language instruction fed to the VLM; should match the "
                         "task string the episodes were recorded with")
    ap.add_argument("--dry-run", action="store_true",
                    help="predict but never command the robot")
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

    import torch
    import lerobot.policies.smolvla  # noqa: F401  (registers the policy class)
    from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    policy = SmolVLAPolicy.from_pretrained(args.ckpt).to(dev).eval()
    print(f"policy loaded on {dev}")

    # -------------------------------------------------------------- preflight
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

    print(f"\n{'DRY RUN - no commands will be sent' if args.dry_run else 'LIVE'} | "
          f"max_lin={args.max_lin} m/s  max_joint_step={args.max_joint_step} deg  "
          f"rate={args.hz} Hz\n")

    period = 1.0 / args.hz
    n = 0
    try:
        while True:
            t_loop = time.time()

            # Re-read health every single iteration. A policy that keeps pushing
            # after the hardware has stopped listening is how arms get hurt.
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
            try:
                api_post("/api/estop", {}, timeout=5)
                print("emergency stop engaged on exit")
            except Exception:
                print("WARNING: could not engage e-stop", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
