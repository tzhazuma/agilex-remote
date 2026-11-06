#!/usr/bin/env python3
"""Replay a REAL recorded episode inside the Piper simulation.

WHY THIS IS USEFUL
    The simulator has so far only run trajectories we invented. This runs the
    arm through what a human actually did on the real robot, and reports the
    things a static inspection cannot:

      * joint angles outside the URDF limits (the real arm sits a degree or two
        outside its datasheet range, which is fine, but a replay shows when a
        commanded target would be far outside),
      * self-collision / ground penetration during the motion,
      * how a real demonstration's poses map onto the model.

WHY IT PROBES FIRST
    The SDK reports degrees with its own zero convention; the URDF has its own.
    If those differ, a naive replay produces a plausible-looking but wrong pose,
    and every conclusion drawn from it is wrong. So --probe prints the resulting
    tool position for the first frames and the joint ranges, which lets the
    convention be checked before trusting anything else.

Usage
    .venv-sim/bin/python tools/sim_replay.py --probe <timeline.csv>
    .venv-sim/bin/python tools/sim_replay.py <timeline.csv> [--render]
"""
import argparse
import math
import os
import sys

import numpy as np

os.environ.setdefault("MUJOCO_GL", "cgl")
import mujoco  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCENE = os.path.join(ROOT, "assets", "sim", "models", "piper", "piper_pick_scene.xml")
OUT_DIR = os.path.join(ROOT, "assets", "sim", "replay")

TCP_OFFSET = np.array([0.0, 0.0, 0.115])


def read_episode(path, side="left", use_targets=False):
    """Returns (frames, dt, columns_used). Angles are converted deg -> rad."""
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    head = [h.strip() for h in rows[0]]
    pref = "L_" if side == "left" else "R_"
    kind = "t" if use_targets else "j"
    cols = [f"{pref}{kind}{i}" for i in range(6)]
    if not all(c in head for c in cols):
        raise SystemExit(f"{path}: missing columns {cols}")
    idx = [head.index(c) for c in cols]
    ti = head.index("t") if "t" in head else None
    grip_i = head.index(f"{pref}grip") if f"{pref}grip" in head else None
    frames, grips, times = [], [], []
    for r in rows[1:]:
        if len(r) < 2:
            continue
        try:
            frames.append([math.radians(float(r[i])) for i in idx])
        except ValueError:
            continue
        g = 0.0
        if grip_i is not None:
            try:
                # recorded in degrees 0..70 -> model finger travel 0..0.035 m
                g = max(0.0, min(float(r[grip_i]), 70.0)) / 70.0 * 0.035
            except ValueError:
                g = 0.0
        grips.append(g)
        if ti is not None:
            try:
                times.append(float(r[ti]))
            except ValueError:
                pass
    dt = 0.1
    if len(times) >= 2:
        d = sorted(times[i + 1] - times[i] for i in range(len(times) - 1))
        med = d[len(d) // 2]
        if 0.001 <= med <= 1.0:
            dt = med
    return np.array(frames), np.array(grips), dt, cols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode", help="path to timeline.csv or an episode dir")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--side", default="left", choices=["left", "right"])
    ap.add_argument("--targets", action="store_true",
                    help="replay the commanded targets (L_t*) instead of measured (L_j*)")
    args = ap.parse_args()

    p = args.episode
    if os.path.isdir(p):
        p = os.path.join(p, "timeline.csv")
    frames, grips, dt, cols = read_episode(p, args.side, args.targets)
    print(f"[replay] {p}")
    print(f"[replay] {len(frames)} frames @ {dt:.3f}s  ({len(frames)*dt:.1f}s)  cols={cols}")

    model = mujoco.MjModel.from_xml_path(SCENE)
    data = mujoco.MjData(model)
    b6 = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "link6")
    lo = model.jnt_range[:6, 0]
    hi = model.jnt_range[:6, 1]

    print(f"[replay] model joint limits (rad): lo={np.round(lo,2)}  hi={np.round(hi,2)}")
    print(f"[replay] episode range     (rad): lo={np.round(frames.min(0),2)}  "
          f"hi={np.round(frames.max(0),2)}")

    def tcp_of(q, g=0.0):
        data.qpos[:6] = q
        data.qpos[6], data.qpos[7] = g, -g
        mujoco.mj_forward(model, data)
        return (data.xpos[b6] + data.xmat[b6].reshape(3, 3) @ TCP_OFFSET).copy()

    if args.probe:
        print("\n[probe] tool position for a few frames (world z is height):")
        for i in (0, len(frames) // 2, len(frames) - 1):
            tcp = tcp_of(frames[i])
            print(f"  frame {i:4d}  q={np.round(frames[i],2)}  tcp={np.round(tcp,3)}")
        print("\n[probe] interpretation: with the base at z=0 and the tool aimed")
        print("        down, tcp z should be small positive (~0.1-0.3). A tcp far")
        print("        above the base means the joint zero differs from the URDF.")
        return 0

    # ---- full replay ------------------------------------------------------
    outside, selfcol, ground = [], [], []
    min_z = np.inf
    arm_geoms = set()
    for b in range(model.nbody):
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, b) or ""
        if name.startswith("link") or name == "base_link":
            for g in range(model.body_geomadr[b],
                           model.body_geomadr[b] + model.body_geomnum[b]):
                arm_geoms.add(g)

    for i, q in enumerate(frames):
        excess = np.maximum(lo - q, q - hi)
        # 0.5 deg tolerance: the real arm rests a fraction of a degree outside
        # its datasheet range (left J2/J3 do exactly this at rest), so counting
        # that as a violation would flag every episode.
        if np.any(excess > math.radians(0.5)):
            # Only POSITIVE excess is a violation: negative means the joint is
            # inside its range, and printing those made a 0.06 deg excursion look
            # like six broken joints.
            worst = int(np.argmax(excess))
            outside.append((i, worst + 1, float(excess[worst])))
        data.qpos[:6] = q
        # Replay the RECORDED gripper opening. Forcing it closed made the two
        # jaws (link7/link8) register as a "self-collision" on every frame.
        data.qpos[6], data.qpos[7] = grips[i], -grips[i]
        mujoco.mj_forward(model, data)
        for b in range(model.nbody):
            nm = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, b) or ""
            if nm.startswith("link"):
                min_z = min(min_z, float(data.xpos[b][2]))
        for c in range(data.ncon):
            con = data.contact[c]
            g1, g2 = con.geom1, con.geom2
            if g1 in arm_geoms and g2 in arm_geoms:
                b1 = model.geom_bodyid[g1]
                b2 = model.geom_bodyid[g2]
                # Adjacent links share a joint and their collision geoms always
                # intersect there. That is a modelling artefact (the MJCF has no
                # <contact><exclude> entries), not a real self-collision, so it
                # must not be counted as one or every pose "collides".
                n1 = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, b1) or ""
                n2 = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, b2) or ""
                # The two jaws meeting is the gripper closing, not a collision.
                jaws = {n1, n2} == {"link7", "link8"}
                adjacent = jaws or (model.body_parentid[b1] == b2
                                    or model.body_parentid[b2] == b1)
                # A contact at dist ~= 0 is a graze; the coarse collision geoms
                # touch in several poses. Only penetration deeper than 1 mm is
                # worth calling a collision.
                if float(con.dist) < -0.001:
                    selfcol.append((i, g1, g2, round(float(con.dist), 4), adjacent))
            elif (g1 in arm_geoms) ^ (g2 in arm_geoms):
                other = g2 if g1 in arm_geoms else g1
                nm = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, other) or ""
                if nm == "floor" and con.dist < 0:
                    ground.append((i, nm, round(float(con.dist), 4)))

    print("\n[replay] results")
    print(f"  joint-limit violations : {len(outside)} frame(s)")
    if outside:
        worst = max(outside, key=lambda r: r[2])
        print(f"      worst: joint {worst[1]} by {np.degrees(worst[2]):.2f} deg "
              f"at frame {worst[0]}")
    for row in outside[:3]:
        print(f"      frame {row[0]}: joint {row[1]} +{np.degrees(row[2]):.2f} deg")
    real = [r for r in selfcol if not r[4]]
    adj = len(selfcol) - len(real)
    print(f"  self-collisions        : {len(real)} real"
          + (f" (+{adj} adjacent-link joint contacts ignored)" if adj else ""))
    for row in real[:5]:
        n1 = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[row[1]])
        n2 = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[row[2]])
        print(f"      frame {row[0]}: {n1} <-> {n2} dist {row[3]}")
    print(f"  floor penetration      : {len(ground)}")
    print(f"  lowest arm body z      : {min_z:.3f} m (model floor is z=0)")
    if len(grips):
        print(f"  gripper             : {grips.min():.4f} .. {grips.max():.4f} m travel")

    ok = not outside and not real and not ground
    print()
    print("REPLAY CLEAN" if ok else "REPLAY FOUND ISSUES")

    if args.render:
        os.makedirs(OUT_DIR, exist_ok=True)
        renderer = mujoco.Renderer(model, height=600, width=800)
        cam = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, "overview")
        picks = [0, len(frames) // 4, len(frames) // 2, (3 * len(frames)) // 4, len(frames) - 1]
        from PIL import Image
        for i in picks:
            data.qpos[:6] = frames[i]
            mujoco.mj_forward(model, data)
            renderer.update_scene(data, camera=cam)
            out = os.path.join(OUT_DIR, f"replay_{i:04d}.png")
            Image.fromarray(renderer.render()).save(out)
            print("  wrote", out)
        renderer.close()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
