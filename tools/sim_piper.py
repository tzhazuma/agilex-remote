#!/usr/bin/env python3
"""
sim_piper.py -- headless MuJoCo pick-and-place demo for the AgileX PiPER arm.

Model: google-deepmind/mujoco_menagerie -> agilex_piper (MIT), which is itself
derived from the official AgileX piper_description URDF. This is the REAL Piper
geometry (6 revolute joints + a parallel 2-finger gripper), not a stand-in.

What it does
  1. builds a pick-and-place scene around the upstream robot MJCF
     (floor, skybox, lights, a graspable cube, a green drop pad, a camera);
  2. solves Cartesian waypoints to joint angles with damped least squares IK
     (scipy.optimize.least_squares, warm-started + random restarts);
  3. runs the trajectory through the physics engine (position actuators);
  4. renders PNG frames offscreen with mujoco.Renderer and, if ffmpeg exists,
     encodes an MP4.

Usage
  .venv-sim/bin/python tools/sim_piper.py                 # full demo
  .venv-sim/bin/python tools/sim_piper.py --width 960 --height 720
  .venv-sim/bin/python tools/sim_piper.py --no-video --fps 15
  .venv-sim/bin/python tools/sim_piper.py --check          # model/IK sanity only

Render backend on macOS: MUJOCO_GL=cgl (MuJoCo's default on macOS) works
headless -- no X server, no osmesa needed. The script pins it if unset.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("MUJOCO_GL", "cgl")  # macOS offscreen GL

import numpy as np  # noqa: E402
import mujoco  # noqa: E402
from PIL import Image  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODEL_DIR = ROOT / "assets" / "sim" / "models" / "piper"
ROBOT_XML = MODEL_DIR / "piper.xml"
SCENE_XML = MODEL_DIR / "piper_pick_scene.xml"
FRAMES_DIR = ROOT / "assets" / "sim" / "frames"
VIDEO_PATH = ROOT / "assets" / "sim" / "piper_pick_and_place.mp4"

# ---------------------------------------------------------------- scene build

SCENE_TEMPLATE = """<mujoco model="piper_pick_scene">
  <include file="piper.xml"/>

  <visual>
    <headlight diffuse="0.55 0.55 0.55" ambient="0.28 0.28 0.28" specular="0.1 0.1 0.1"/>
    <rgba haze="0.15 0.25 0.35 1"/>
    <global azimuth="120" elevation="-20" offwidth="{width}" offheight="{height}"/>
  </visual>

  <asset>
    <texture type="skybox" builtin="gradient" rgb1="0.3 0.5 0.7" rgb2="0 0 0" width="512" height="3072"/>
    <texture type="2d" name="groundplane" builtin="checker" mark="edge" rgb1="0.22 0.31 0.40"
             rgb2="0.11 0.19 0.27" markrgb="0.75 0.78 0.80" width="300" height="300"/>
    <material name="groundplane" texture="groundplane" texuniform="true" texrepeat="4 4" reflectance="0.08"/>
    <material name="pad_mat" rgba="0.09 0.52 0.22 0.9"/>
  </asset>

  <worldbody>
    <light pos="0.6 -0.6 1.4" dir="-0.4 0.4 -1" directional="true" diffuse="0.7 0.7 0.7"/>
    <light pos="-0.8 0.4 1.0" dir="0.6 -0.3 -1" directional="true" diffuse="0.35 0.35 0.4"/>
    <geom name="floor" size="0 0 0.05" type="plane" material="groundplane"/>

    <!-- green drop pad: the place target -->
    <geom name="pad" type="box" pos="{pad_pos}" size="0.09 0.09 0.004" material="pad_mat"/>

    <!-- graspable cube -->
    <body name="cube" pos="{cube_pos}">
      <freejoint name="cube_free"/>
      <geom name="cube_geom" type="box" size="0.02 0.02 0.02" mass="0.05"
            rgba="0.92 0.42 0.10 1" friction="1.4 0.03 0.001" condim="4"/>
    </body>

    <camera name="overview" pos="{cam_pos}" xyaxes="{cam_xyaxes}"/>
  </worldbody>
</mujoco>
"""

CUBE_XY = (0.33, -0.10)
PAD_XY = (0.33, 0.10)
CUBE_HALF = 0.02
PAD_TOP = 0.008


def _camera_xyaxes(pos, target):
    """Convert (camera pos, look-at point) into MuJoCo's xyaxes string."""
    pos = np.asarray(pos, float)
    fwd = np.asarray(target, float) - pos
    fwd /= np.linalg.norm(fwd)
    up = np.array([0.0, 0.0, 1.0])
    right = np.cross(fwd, up)
    if np.linalg.norm(right) < 1e-6:
        right = np.array([1.0, 0.0, 0.0])
    right /= np.linalg.norm(right)
    up2 = np.cross(right, fwd)
    return " ".join(f"{v:.6f}" for v in np.concatenate([right, up2]))


def build_scene(width=800, height=600, cam_pos=(0.78, -0.64, 0.42),
                cam_target=(0.26, 0.0, 0.17)):
    """Write a scene MJCF next to piper.xml (same dir, so meshdir='assets' holds)."""
    xml = SCENE_TEMPLATE.format(
        width=width,
        height=height,
        cube_pos=f"{CUBE_XY[0]:.4f} {CUBE_XY[1]:.4f} {CUBE_HALF + 0.001:.4f}",
        pad_pos=f"{PAD_XY[0]:.4f} {PAD_XY[1]:.4f} {PAD_TOP / 2:.4f}",
        cam_pos=" ".join(f"{v:.4f}" for v in cam_pos),
        cam_xyaxes=_camera_xyaxes(cam_pos, cam_target),
    )
    SCENE_XML.write_text(xml)
    return SCENE_XML


# ------------------------------------------------------------------ kinematics

class PiperKinematics:
    """FK/IK helpers around the link6 frame of the Piper."""

    TCP_OFFSET = np.array([0.0, 0.0, 0.115])  # link6 origin -> middle of the jaw pads

    def __init__(self, model, data):
        self.m = model
        self.d = data
        self.b6 = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "link6")
        self.lo = model.jnt_range[:6, 0].copy()
        self.hi = model.jnt_range[:6, 1].copy()
        self._jp = np.zeros((3, model.nv))
        self._jr = np.zeros((3, model.nv))

    def set_arm(self, q):
        self.d.qpos[:6] = q
        self.d.qpos[6:8] = 0.0  # fingers closed; do NOT touch the free-joint qpos
        mujoco.mj_forward(self.m, self.d)

    def fk(self, q):
        self.set_arm(q)
        p = self.d.xpos[self.b6] + self.d.xmat[self.b6].reshape(3, 3) @ self.TCP_OFFSET
        return p.copy(), self.d.xmat[self.b6].reshape(3, 3).copy()

    def _residual(self, q, target, R_target):
        p, R = self.fk(q)
        if R_target is None:  # position-only task
            return (target - p) * 10.0
        eM = R_target @ R.T
        quat = np.zeros(4)
        mujoco.mju_mat2Quat(quat, eM.reshape(9))
        er = np.zeros(3)
        mujoco.mju_quat2Vel(er, quat, 1.0)
        return np.concatenate([(target - p) * 10.0, er * 2.0])

    def ik(self, target, R_target, seed, restarts=24, rng=None, tol=2e-3):
        """Damped-least-squares IK; returns (q, residual_norm)."""
        from scipy.optimize import least_squares

        target = np.asarray(target, float)
        rng = rng or np.random.default_rng(0)
        seeds = [np.asarray(seed, float)]
        seeds += [rng.uniform(self.lo * 0.75, self.hi * 0.75) for _ in range(restarts)]
        best_q, best_c = None, np.inf
        for s in seeds:
            sol = least_squares(
                self._residual, s, args=(target, R_target), bounds=(self.lo, self.hi),
                xtol=1e-10, ftol=1e-10, gtol=1e-10, max_nfev=2000,
            )
            c = float(np.linalg.norm(self._residual(sol.x, target, R_target)))
            if c < best_c:
                best_q, best_c = sol.x.copy(), c
            if best_c < tol:
                break
        return best_q, best_c


# ---------------------------------------------------------------- the trajectory

DOWN_ROT = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])

HOME_Q = np.array([0.0, 1.57, -1.3485, 0.0, 0.0, 0.0])
GRIP_OPEN = 0.035
GRIP_CLOSED = 0.0

# (name, TCP target, tool must point straight down?)
CARTESIAN_TARGETS = [
    ("pre_pick", (CUBE_XY[0], CUBE_XY[1], 0.13), True),
    ("pick", (CUBE_XY[0], CUBE_XY[1], CUBE_HALF + 0.001), True),
    ("pre_place", (PAD_XY[0], PAD_XY[1], 0.13), True),
    ("place", (PAD_XY[0], PAD_XY[1], PAD_TOP + CUBE_HALF + 0.002), True),
]


def solve_waypoints(kin, verbose=True):
    rng = np.random.default_rng(11)
    way = {"home": HOME_Q.copy()}
    seed = HOME_Q.copy()
    for name, target, down in CARTESIAN_TARGETS:
        q, cost = kin.ik(target, DOWN_ROT if down else None, seed, rng=rng)
        if verbose:
            p, R = kin.fk(q)
            print(f"  IK {name:10s} target={np.round(target,3)} "
                  f"residual={cost:.4f} poserr={np.linalg.norm(p - target)*1000:5.1f} mm")
        if cost > 0.5:
            raise RuntimeError(f"IK failed for waypoint {name!r} (residual {cost:.3f})")
        # joint6 is a pure roll about the tool axis -> it does not move the TCP.
        # Pin it so the wrist does not have to unwind ~360 deg between waypoints.
        q = q.copy()
        q[5] = 0.0
        way[name] = q
        seed = q
    return way


def make_segments(way):
    """(duration_s, q_target, gripper_target) sequence of the demo."""
    return [
        (2.0, way["pre_pick"], GRIP_OPEN),
        (1.1, way["pick"], GRIP_OPEN),
        (0.6, way["pick"], GRIP_CLOSED),      # close on the cube
        (1.1, way["pre_pick"], GRIP_CLOSED),  # lift
        (2.0, way["pre_place"], GRIP_CLOSED), # transfer over the pad
        (1.1, way["place"], GRIP_CLOSED),     # lower
        (0.6, way["place"], GRIP_OPEN),       # release
        (1.1, way["pre_place"], GRIP_OPEN),   # retreat
        (2.0, way["home"], GRIP_OPEN),
    ]


def smoothstep(s):
    s = np.clip(s, 0.0, 1.0)
    return s * s * s * (s * (6.0 * s - 15.0) + 10.0)


def trajectory(segments, fps):
    """Expand the waypoint segments into per-frame (q_arm, gripper) samples."""
    frames = []
    q_prev = segments[0][1] * 0.0 + HOME_Q
    g_prev = GRIP_OPEN
    for dur, q_tgt, g_tgt in segments:
        n = max(1, int(round(dur * fps)))
        for i in range(1, n + 1):
            s = smoothstep(i / n)
            frames.append((q_prev + s * (q_tgt - q_prev), g_prev + s * (g_tgt - g_prev)))
        q_prev, g_prev = q_tgt.copy(), g_tgt
    return frames


# ------------------------------------------------------------------- main loop

def run(args):
    t0 = time.time()
    print(f"[sim] mujoco {mujoco.__version__} | GL backend: {os.environ.get('MUJOCO_GL')}")
    print(f"[sim] robot: {ROBOT_XML}")

    scene = build_scene(args.width, args.height)
    print(f"[sim] scene written: {scene}")
    model = mujoco.MjModel.from_xml_path(str(scene))
    data = mujoco.MjData(model)
    print(f"[sim] nq={model.nq} nv={model.nv} nu={model.nu} "
          f"timestep={model.opt.timestep} bodies={model.nbody}")

    kin = PiperKinematics(model, data)
    print("[sim] solving IK waypoints ...")
    way = solve_waypoints(kin)
    segments = make_segments(way)
    frames = trajectory(segments, args.fps)
    print(f"[sim] trajectory: {len(frames)} frames @ {args.fps} fps "
          f"({len(frames)/args.fps:.1f} s of motion)")

    if args.check:
        print("[sim] --check: IK + model OK, not simulating.")
        return 0

    # initial state
    mujoco.mj_resetData(model, data)
    data.qpos[:6] = HOME_Q
    data.qpos[6], data.qpos[7] = GRIP_OPEN, -GRIP_OPEN
    data.ctrl[:6] = HOME_Q
    data.ctrl[6] = GRIP_OPEN
    mujoco.mj_forward(model, data)

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    for old in FRAMES_DIR.glob("*.png"):
        old.unlink()

    renderer = mujoco.Renderer(model, height=args.height, width=args.width)
    steps_per_frame = max(1, int(round(1.0 / (args.fps * model.opt.timestep))))

    # frames worth keeping as stills (index -> label)
    keep = {
        0: "00_start_home",
        int(len(frames) * 0.24): "01_approach_cube",
        int(len(frames) * 0.36): "02_grasp_closed",
        int(len(frames) * 0.50): "03_lift",
        int(len(frames) * 0.68): "04_transfer",
        int(len(frames) * 0.80): "05_place_release",
        len(frames) - 1: "06_return_home",
    }

    video_frames = []
    cube_start = data.body("cube").xpos.copy()
    max_cube_z = 0.0
    for i, (q_arm, grip) in enumerate(frames):
        data.ctrl[:6] = q_arm
        data.ctrl[6] = grip
        for _ in range(steps_per_frame):
            mujoco.mj_step(model, data)
        cube_z = float(data.body("cube").xpos[2])
        max_cube_z = max(max_cube_z, cube_z)

        renderer.update_scene(data, camera="overview")
        img = renderer.render()
        if args.save_all:
            Image.fromarray(img).save(FRAMES_DIR / f"f{i:04d}.png")
        if i in keep:
            Image.fromarray(img).save(FRAMES_DIR / f"{keep[i]}.png")
        if args.video:
            video_frames.append(img.copy())
        if i % 25 == 0:
            print(f"  frame {i:4d}/{len(frames)}  cube z={cube_z:.3f}  "
                  f"elapsed {time.time()-t0:.1f}s")

    renderer.close()

    cube_end = data.body("cube").xpos.copy()
    drift = np.linalg.norm(cube_end[:2] - np.array(PAD_XY))
    print(f"[sim] cube start={np.round(cube_start,3)} end={np.round(cube_end,3)} "
          f"max_z={max_cube_z:.3f} dist_to_pad_center={drift*1000:.1f} mm")
    ok = drift < 0.06 and max_cube_z > 0.05
    print(f"[sim] pick-and-place {'SUCCEEDED' if ok else 'FAILED'} "
          f"(lifted above 5 cm and landed within 6 cm of the pad centre)")

    if args.video and video_frames:
        ffmpeg = shutil.which("ffmpeg") or "/usr/local/bin/ffmpeg"
        if not Path(ffmpeg).exists():
            print("[sim] ffmpeg not found -- skipping MP4")
        else:
            tmp = FRAMES_DIR / "_video_tmp"
            tmp.mkdir(exist_ok=True)
            for j, img in enumerate(video_frames):
                Image.fromarray(img).save(tmp / f"{j:05d}.png")
            cmd = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(args.fps),
                   "-i", str(tmp / "%05d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                   "-crf", "20", str(VIDEO_PATH)]
            print("[sim] encoding:", " ".join(cmd))
            subprocess.run(cmd, check=True)
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"[sim] video: {VIDEO_PATH} ({VIDEO_PATH.stat().st_size/1e6:.2f} MB)")

    print(f"[sim] done in {time.time()-t0:.1f}s")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--height", type=int, default=600)
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--save-all", action="store_true", help="dump every frame to PNG")
    ap.add_argument("--check", action="store_true", help="validate model+IK only")
    args = ap.parse_args()
    args.video = not args.no_video
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
