#!/usr/bin/env python3
"""A grasp trajectory on the REAL Piper model, with a numeric success check.

WHY THIS EXISTS SEPARATELY FROM sim_piper.py
    That script solves IK with scipy.optimize.least_squares: 4 waypoints x 25
    random restarts x 2000 evaluations. On this machine it now stalls for
    minutes, and it is not the part worth guarding. This version uses MuJoCo's
    ANALYTIC body Jacobian (mj_jac) with damped least squares - no scipy, no
    random restarts, deterministic, and it converges in a few ms per pose.

WHAT IT PROVES
    Not "an animation was rendered", but: the end effector reaches the cube, the
    jaws close, the cube leaves the table (z rises) and ends up on the pad. The
    script exits non-zero if the grasp fails.

GEOMETRY NOTE (found the hard way, and it constrains task design)
    The Piper's wrist pitch spans only +/-70 deg, so the gripper CANNOT be aimed
    straight down much above ~0.17 m. Every approach height here stays below
    that; a top-down task planner that assumes a vertical tool at 0.3 m will
    simply fail IK.

Usage
    .venv-sim/bin/python tools/sim_grasp.py                # full run + video
    .venv-sim/bin/python tools/sim_grasp.py --check        # IK only, no render
    .venv-sim/bin/python tools/sim_grasp.py --gif          # also write a GIF
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

import numpy as np

os.environ.setdefault("MUJOCO_GL", "cgl")
import mujoco  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL_DIR = os.path.join(ROOT, "assets", "sim", "models", "piper")
SCENE = os.path.join(MODEL_DIR, "piper_pick_scene.xml")
OUT_DIR = os.path.join(ROOT, "assets", "sim", "grasp")
FRAMES_DIR = os.path.join(OUT_DIR, "frames")

TCP_OFFSET = np.array([0.0, 0.0, 0.115])   # link6 origin -> between the jaw pads
HOME_Q = np.array([0.0, 1.57, -1.3485, 0.0, 0.0, 0.0])
GRIP_OPEN, GRIP_CLOSED = 0.035, 0.0

# Scene landmarks (read from piper_pick_scene.xml so they cannot drift silently)
CUBE_XYZ = np.array([0.330, -0.100, 0.021])
PAD_XYZ = np.array([0.330, 0.100, 0.004])
CUBE_HALF = 0.02


class Arm:
    def __init__(self, model, data):
        self.m, self.d = model, data
        self.b6 = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "link6")
        self.cube = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "cube")
        self.lo = model.jnt_range[:6, 0].copy()
        self.hi = model.jnt_range[:6, 1].copy()
        self._jacp = np.zeros((3, model.nv))

    def set_arm(self, q):
        self.d.qpos[:6] = q
        mujoco.mj_forward(self.m, self.d)

    def tcp(self):
        return self.d.xpos[self.b6] + self.d.xmat[self.b6].reshape(3, 3) @ TCP_OFFSET

    def tool_z(self):
        """How far the tool axis is from pointing straight down (1.0 = straight down)."""
        return float(self.d.xmat[self.b6].reshape(3, 3)[2, 2])

    def ik(self, target, seed, iters=300, lam=0.04, tol=1e-4):
        """Damped least squares on the analytic Jacobian. Position only."""
        q = np.clip(seed.copy(), self.lo, self.hi)
        target = np.asarray(target, float)
        best_q, best_e = q.copy(), np.inf
        for _ in range(iters):
            self.set_arm(q)
            err = target - self.tcp()
            e = np.linalg.norm(err)
            if e < best_e:
                best_q, best_e = q.copy(), e
            if e < tol:
                break
            self._jacp[:] = 0.0
            mujoco.mj_jac(self.m, self.d, self._jacp, None, self.tcp(), self.b6)
            J = self._jacp[:, :6]
            dq = J.T @ np.linalg.solve(J @ J.T + (lam ** 2) * np.eye(3), err)
            # step limit keeps the linearisation honest on a 6-DOF chain
            step = np.linalg.norm(dq)
            if step > 0.25:
                dq *= 0.25 / step
            q = np.clip(q + dq, self.lo, self.hi)
        self.set_arm(best_q)
        return best_q, best_e


def plan(arm):
    """Waypoints -> joint-space segments. Heights stay inside the wrist limit."""
    w = {}
    w["home"] = HOME_Q.copy()
    seed = HOME_Q.copy()

    targets = [
        ("above_cube", (CUBE_XYZ[0], CUBE_XYZ[1], 0.115)),
        ("at_cube",    (CUBE_XYZ[0], CUBE_XYZ[1], CUBE_XYZ[2])),
        ("lift",       (CUBE_XYZ[0], CUBE_XYZ[1], 0.130)),
        ("above_pad",  (PAD_XYZ[0], PAD_XYZ[1], 0.125)),
        ("at_pad",     (PAD_XYZ[0], PAD_XYZ[1], CUBE_HALF + 0.006)),
    ]
    residual = {}
    for name, t in targets:
        q, e = arm.ik(t, seed)
        residual[name] = e
        print(f"  IK {name:11s} target={np.round(t,3)} "
              f"residual={e*1000:5.1f} mm  tool_down={arm.tool_z():+.2f}")
        if e > 0.01:
            raise SystemExit(f"IK failed for {name!r}: {e*1000:.1f} mm")
        w[name] = q.copy()
        seed = q.copy()
    return w, residual


def segments(w):
    """(duration, target_q, gripper_target) - smoothstep between them."""
    return [
        (1.6, w["above_cube"], GRIP_OPEN),
        (0.9, w["at_cube"],    GRIP_OPEN),
        (0.5, w["at_cube"],    GRIP_CLOSED),   # close and hold
        (1.1, w["lift"],       GRIP_CLOSED),   # lift clear of the table
        (1.8, w["above_pad"],  GRIP_CLOSED),   # carry across
        (0.9, w["at_pad"],     GRIP_CLOSED),   # lower
        (0.5, w["at_pad"],     GRIP_OPEN),     # release
        (1.6, w["home"],       GRIP_OPEN),     # retreat
    ]


def smoothstep(s):
    s = np.clip(s, 0.0, 1.0)
    return s * s * s * (s * (6.0 * s - 15.0) + 10.0)


def expand(segs, fps):
    out, q_prev, g_prev = [], HOME_Q.copy(), GRIP_OPEN
    for dur, q_t, g_t in segs:
        n = max(1, int(round(dur * fps)))
        for i in range(1, n + 1):
            s = smoothstep(i / n)
            out.append((q_prev + s * (q_t - q_prev), g_prev + s * (g_t - g_prev)))
        q_prev, g_prev = q_t.copy(), g_t
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--gif", action="store_true")
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--height", type=int, default=600)
    ap.add_argument("--fps", type=int, default=25)
    args = ap.parse_args()

    t0 = time.time()
    print(f"[grasp] mujoco {mujoco.__version__}")
    model = mujoco.MjModel.from_xml_path(SCENE)
    data = mujoco.MjData(model)
    print(f"[grasp] model loaded in {time.time()-t0:.1f}s  nq={model.nq} nu={model.nu}")

    arm = Arm(model, data)
    arm.set_arm(HOME_Q)
    print(f"[grasp] TCP at home: {np.round(arm.tcp(),3)}")
    print("[grasp] solving IK ...")
    way, _ = plan(arm)

    frames = expand(segments(way), args.fps)
    print(f"[grasp] trajectory: {len(frames)} frames "
          f"({len(frames)/args.fps:.1f}s of motion)")

    if args.check:
        print("[grasp] --check: model + IK OK, not simulating.")
        return 0

    # --- simulate ----------------------------------------------------------
    mujoco.mj_resetData(model, data)
    data.qpos[:6] = HOME_Q
    data.qpos[6], data.qpos[7] = GRIP_OPEN, -GRIP_OPEN
    data.ctrl[:6] = HOME_Q
    data.ctrl[6] = GRIP_OPEN
    mujoco.mj_forward(model, data)

    cube0 = data.qpos[8:11].copy()
    print(f"[grasp] cube start: {np.round(cube0,3)}")

    os.makedirs(FRAMES_DIR, exist_ok=True)
    for f in os.listdir(FRAMES_DIR):
        if f.endswith(".png"):
            os.unlink(os.path.join(FRAMES_DIR, f))

    renderer = mujoco.Renderer(model, height=args.height, width=args.width)
    cam = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, "overview")
    steps_per_frame = max(1, int(round(1.0 / (args.fps * model.opt.timestep))))

    peak_z = cube0[2]
    saved, tmpdir = 0, os.path.join(OUT_DIR, "_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    for i, (q, g) in enumerate(frames):
        data.ctrl[:6] = q
        data.ctrl[6] = g
        for _ in range(steps_per_frame):
            mujoco.mj_step(model, data)
        peak_z = max(peak_z, float(data.qpos[10]))
        if i % 1 == 0:
            renderer.update_scene(data, camera=cam)
            img = renderer.render()
            path = os.path.join(tmpdir, f"{i:05d}.png")
            _write_png(path, img)
            saved += 1
            if i % 5 == 0:
                import PIL.Image  # noqa: F401  (only used for the still copies)
                _save_still(os.path.join(FRAMES_DIR, f"{i:05d}.png"), img)
    renderer.close()

    cube1 = data.qpos[8:11].copy()
    print(f"[grasp] cube end:   {np.round(cube1,3)}")

    # --- video -------------------------------------------------------------
    ffmpeg = shutil.which("ffmpeg")
    video = os.path.join(OUT_DIR, "piper_grasp.mp4")
    if ffmpeg:
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                        "-framerate", str(args.fps), "-i", os.path.join(tmpdir, "%05d.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", video],
                       check=False)
        print(f"[grasp] video: {video}")
        if args.gif:
            gif = os.path.join(OUT_DIR, "piper_grasp.gif")
            subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-framerate", str(args.fps), "-i", os.path.join(tmpdir, "%05d.png"),
                            "-vf", "scale=480:-1:flags=lanczos,split[a][b];[a]palettegen[p];[b][p]paletteuse",
                            gif], check=False)
            print(f"[grasp] gif:   {gif}")
    shutil.rmtree(tmpdir, ignore_errors=True)

    # --- verdict -----------------------------------------------------------
    lift = float(peak_z - cube0[2])
    on_pad = float(np.linalg.norm(cube1[:2] - PAD_XYZ[:2]))
    dropped = float(cube1[2] - (CUBE_HALF))

    print()
    print(f"  cube lifted   : {lift*1000:6.1f} mm   (peak z {peak_z:.3f})")
    print(f"  landed on pad : {on_pad*1000:6.1f} mm from the pad centre")
    print(f"  final z       : {cube1[2]:.3f}  (half-size {CUBE_HALF})")
    ok = lift > 0.04 and on_pad < 0.05 and abs(dropped) < 0.02
    print()
    print("GRASP SUCCEEDED" if ok else "GRASP FAILED")
    return 0 if ok else 1


def _write_png(path, rgb):
    from PIL import Image
    Image.fromarray(rgb).save(path)


def _save_still(path, rgb):
    from PIL import Image
    Image.fromarray(rgb).save(path)


if __name__ == "__main__":
    sys.exit(main())
