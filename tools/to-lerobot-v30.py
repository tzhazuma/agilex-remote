#!/usr/bin/env python3
"""
Build a LeRobot v3.0 dataset directly from our episode directories.

WHY THIS EXISTS
    Our own exporter writes the v2.1 layout, which LeRobot 0.4.4 refuses to load
    ("The dataset you requested is in 2.1 format ... not backward compatible").
    LeRobot ships a converter, but it insists on a dataset living in the HF hub
    cache and calls snapshot_download, which cannot work on an offline/blocked
    network.

    So build v3.0 directly through LeRobot's own writer API instead of hand
    rolling the format. That keeps the on-disk layout exactly what the library
    expects, and it is version-proof in a way that hand-writing JSON is not.

STATE / ACTION LAYOUT
    7 values per arm, 14 total: six joint angles in degrees + the gripper.
    observation.state = measured, action = commanded target. Same convention as
    piper-aio's inference/task_configs.yaml (left0/right0 are 7-element vectors),
    so checkpoints trained elsewhere on a Piper are dimensionally compatible.

USAGE
    /home/agilex/agilex-data/.venv-lerobot/bin/python tools/to-lerobot-v30.py \
        --episodes ~/agilex-data/episodes \
        --out      ~/agilex-data/lerobot/agilex_pickplace_v30 \
        --repo-id  local/agilex_pickplace
"""
import argparse
import csv
import os
import sys

import numpy as np
from PIL import Image

STATE_COLS = [f"L_j{i}" for i in range(6)] + ["L_grip"] + \
             [f"R_j{i}" for i in range(6)] + ["R_grip"]
ACTION_COLS = [f"L_t{i}" for i in range(6)] + ["L_tgrip"] + \
              [f"R_t{i}" for i in range(6)] + ["R_tgrip"]


def load_episode(d, img_size):
    """Return (frames, states, actions, task) for one episode directory."""
    tl = os.path.join(d, "timeline.csv")
    if not os.path.isfile(tl):
        return None
    with open(tl, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None

    task = "unspecified task"
    mp = os.path.join(d, "meta.json")
    if os.path.isfile(mp):
        try:
            import json
            task = (json.load(open(mp, encoding="utf-8")).get("task") or task).strip() or task
        except Exception:
            pass

    cams = {}
    for cam in ("cam0", "cam2"):
        sub = os.path.join(d, cam)
        if os.path.isdir(sub):
            cams[cam] = sub
    if not cams:
        return None

    frames, states, actions = [], [], []
    for r in rows:
        fno = int(r.get("frame") or 0)
        imgs = {}
        ok = True
        for cam, sub in cams.items():
            p = os.path.join(sub, "%06d.jpg" % fno)
            if not os.path.isfile(p):
                ok = False
                break
            imgs[cam] = np.asarray(
                Image.open(p).convert("RGB").resize((img_size, img_size)),
                dtype=np.uint8)
        if not ok:
            continue
        frames.append(imgs)
        states.append([float(r.get(c) or 0.0) for c in STATE_COLS])
        actions.append([float(r.get(c) or 0.0) for c in ACTION_COLS])
    if not frames:
        return None
    return frames, np.asarray(states, np.float32), np.asarray(actions, np.float32), task


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", default=os.path.expanduser("~/agilex-data/episodes"))
    ap.add_argument("--out", default=os.path.expanduser("~/agilex-data/lerobot/agilex_pickplace_v30"))
    ap.add_argument("--repo-id", default="local/agilex_pickplace")
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--img-size", type=int, default=256,
                    help="square resize; ACT downsamples to ~224 anyway")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    eps = sorted(d for d in os.listdir(args.episodes)
                 if d.startswith("episode_") and
                 os.path.isdir(os.path.join(args.episodes, d)))
    if args.limit:
        eps = eps[:args.limit]
    print(f"episodes found: {len(eps)}", flush=True)

    feats = {
        "observation.images.cam_left": {
            "dtype": "video", "shape": (args.img_size, args.img_size, 3),
            "names": ["height", "width", "channels"]},
        "observation.images.cam_right": {
            "dtype": "video", "shape": (args.img_size, args.img_size, 3),
            "names": ["height", "width", "channels"]},
        "observation.state": {"dtype": "float32", "shape": (14,), "names": STATE_COLS},
        "action": {"dtype": "float32", "shape": (14,), "names": ACTION_COLS},
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    ds = LeRobotDataset.create(
        repo_id=args.repo_id, fps=args.fps, features=feats,
        root=args.out, robot_type="agilex_tracer_piper_dual",
        use_videos=True, vcodec="libsvtav1",
    )

    written, skipped, total = 0, 0, 0
    for i, name in enumerate(eps):
        got = load_episode(os.path.join(args.episodes, name), args.img_size)
        if got is None:
            skipped += 1
            print(f"  [{i+1}/{len(eps)}] {name}: SKIP", flush=True)
            continue
        frames, states, actions, task = got
        for k in range(len(frames)):
            ds.add_frame({
                "observation.images.cam_left": frames[k].get("cam0",
                    np.zeros((args.img_size, args.img_size, 3), np.uint8)),
                "observation.images.cam_right": frames[k].get("cam2",
                    np.zeros((args.img_size, args.img_size, 3), np.uint8)),
                "observation.state": states[k],
                "action": actions[k],
                "task": task,
            })
        ds.save_episode()
        written += 1
        total += len(frames)
        print(f"  [{i+1}/{len(eps)}] {name}: {len(frames)} frames  task={task!r}", flush=True)

    ds.finalize()
    print(f"\nwrote {written} episodes, {total} frames ({skipped} skipped)", flush=True)
    print(f"root: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
