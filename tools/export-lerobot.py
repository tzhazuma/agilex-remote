#!/usr/bin/env python3
"""
Convert recorded episodes into a LeRobot dataset (v2.1 layout).

The controller writes one directory per demonstration:

    episode_0005/
        meta.json
        cam0/000001.jpg ...
        cam2/000001.jpg ...
        timeline.csv

This script turns a set of those into the layout `lerobot` expects:

    <out>/meta/info.json
    <out>/meta/tasks.jsonl
    <out>/meta/episodes.jsonl
    <out>/meta/stats.json
    <out>/data/chunk-000/episode_000000.parquet
    <out>/videos/chunk-000/observation.images.cam_left/episode_000000.mp4
    <out>/videos/chunk-000/observation.images.cam_right/episode_000000.mp4

WHAT IS STATE AND WHAT IS ACTION
    observation.state = the measured joint angles + measured gripper (14 values)
    action            = the COMMANDED targets          + target gripper (14 values)

Recording only the measured angles would produce a dataset you cannot train on:
behaviour cloning learns the mapping from observation to the command the operator
issued, not from the observation to itself.

PARQUET NEEDS PYARROW. On this robot system python does not have it (and must not
get it - ROS Humble depends on numpy 1.21.5). Use the isolated venv:

    /home/agilex/agilex-data/.venv-export/bin/python3 export-lerobot.py ...
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

STATE_COLS = [f"L_j{i}" for i in range(6)] + ["L_grip"] + \
             [f"R_j{i}" for i in range(6)] + ["R_grip"]
ACTION_COLS = [f"L_t{i}" for i in range(6)] + ["L_tgrip"] + \
              [f"R_t{i}" for i in range(6)] + ["R_tgrip"]
DIM = len(STATE_COLS)

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAVE_PYARROW = True
except Exception:
    HAVE_PYARROW = False


def log(msg):
    print(msg, flush=True)


def find_episodes(root):
    """Episode directories that have both a timeline and at least one image."""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not (os.path.isdir(d) and name.startswith("episode_")):
            continue
        tl = os.path.join(d, "timeline.csv")
        if not os.path.isfile(tl):
            continue
        cams = [c for c in ("cam0", "cam2") if os.path.isdir(os.path.join(d, c))]
        if not cams:
            continue
        out.append((name, d, cams))
    return out


def read_timeline(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def as_vec(row, cols):
    out = []
    for c in cols:
        try:
            out.append(float(row.get(c) or 0.0))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def make_video(ep_dir, cam, out_path, fps):
    """Encode the episode's JPEG sequence into an h264 mp4."""
    src = os.path.join(ep_dir, cam, "%06d.jpg")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(fps),
        "-start_number", "1",
        "-i", src,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
        "-movflags", "+faststart",
        out_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    ok = r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    if not ok:
        log(f"    ! video {cam} failed: {r.stderr.strip()[:200]}")
    return ok


def image_size(ep_dir, cam):
    """Read width/height from a JPEG header without extra dependencies."""
    d = os.path.join(ep_dir, cam)
    names = sorted(os.listdir(d))
    if not names:
        return None
    with open(os.path.join(d, names[0]), "rb") as f:
        data = f.read()
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return w, h
        seglen = (data[i + 2] << 8) | data[i + 3]
        i += 2 + seglen
    return None


def stats_for(episodes):
    """mean / std / min / max / count for state and action, as LeRobot expects."""
    out = {}
    for key, cols in (("observation.state", STATE_COLS), ("action", ACTION_COLS)):
        n = 0
        s = [0.0] * DIM
        sq = [0.0] * DIM
        mn = [float("inf")] * DIM
        mx = [float("-inf")] * DIM
        for ep in episodes:
            for row in ep["rows"]:
                v = as_vec(row, cols)
                n += 1
                for i, x in enumerate(v):
                    s[i] += x
                    sq[i] += x * x
                    mn[i] = min(mn[i], x)
                    mx[i] = max(mx[i], x)
        if n:
            mean = [v / n for v in s]
            std = [max(0.0, (sq[i] / n) - mean[i] ** 2) ** 0.5 for i in range(DIM)]
        else:
            mean = [0.0] * DIM
            std = [1.0] * DIM
            mn = [0.0] * DIM
            mx = [0.0] * DIM
        out[key] = {
            "mean": [round(v, 6) for v in mean],
            "std": [round(v, 6) for v in std],
            "min": [round(v, 6) for v in mn],
            "max": [round(v, 6) for v in mx],
            "count": [n],
        }
    return out


def main():
    ap = argparse.ArgumentParser(description="Export episodes to a LeRobot dataset")
    ap.add_argument("--episodes", default="/home/agilex/agilex-data/episodes")
    ap.add_argument("--out", default="/home/agilex/agilex-data/lerobot/agilex_dual_arm")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--task", default="", help="override task string for all episodes")
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not HAVE_PYARROW:
        log("FATAL: pyarrow is required to write the parquet data files.")
        log("       Use the isolated venv:")
        log("       /home/agilex/agilex-data/.venv-export/bin/python3 this-script")
        return 2

    found = find_episodes(args.episodes)
    if args.limit:
        found = found[:args.limit]
    if not found:
        log(f"No episodes found under {args.episodes}")
        return 1

    log(f"Found {len(found)} episode(s) under {args.episodes}")
    if os.path.isdir(args.out):
        log(f"Output {args.out} exists - replacing")
        shutil.rmtree(args.out)
    for sub in ("meta", "data/chunk-000", "videos/chunk-000"):
        os.makedirs(os.path.join(args.out, sub), exist_ok=True)

    # ---- load ----
    episodes = []
    for name, d, cams in found:
        rows = read_timeline(os.path.join(d, "timeline.csv"))
        try:
            meta = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
        except Exception:
            meta = {}
        if not rows:
            log(f"  {name}: no rows, skipping")
            continue
        episodes.append({"src": name, "dir": d, "rows": rows,
                         "meta": meta, "cams": cams})
    if not episodes:
        log("Nothing usable.")
        return 1

    # ---- tasks ----
    tasks, task_index = [], {}
    for ep in episodes:
        t = args.task or (ep["meta"].get("task") or "").strip() or "unspecified task"
        ep["task"] = t
        if t not in task_index:
            task_index[t] = len(tasks)
            tasks.append(t)
    with open(os.path.join(args.out, "meta/tasks.jsonl"), "w", encoding="utf-8") as f:
        for i, t in enumerate(tasks):
            f.write(json.dumps({"task_index": i, "task": t}, ensure_ascii=False) + "\n")

    # ---- per episode: video + parquet ----
    size = None
    for _, d, cams in found:
        for c in cams:
            size = image_size(d, c)
            if size:
                break
        if size:
            break
    width, height = size if size else (1280, 720)

    ep_lines = []
    global_index = 0
    total_frames = 0
    for ei, ep in enumerate(episodes):
        rows = ep["rows"]
        log(f"  [{ei + 1}/{len(episodes)}] {ep['src']}: {len(rows)} frames, task={ep['task']!r}")

        state = [as_vec(r, STATE_COLS) for r in rows]
        action = [as_vec(r, ACTION_COLS) for r in rows]
        n = len(rows)

        table = pa.table({
            "observation.state": pa.array(state, type=pa.list_(pa.float32())),
            "action": pa.array(action, type=pa.list_(pa.float32())),
            "timestamp": pa.array([round(i / args.fps, 6) for i in range(n)], type=pa.float32()),
            "frame_index": pa.array(list(range(n)), type=pa.int64()),
            "episode_index": pa.array([ei] * n, type=pa.int64()),
            "index": pa.array(list(range(global_index, global_index + n)), type=pa.int64()),
            "task_index": pa.array([task_index[ep["task"]]] * n, type=pa.int64()),
        })
        pq.write_table(table,
                       os.path.join(args.out, f"data/chunk-000/episode_{ei:06d}.parquet"))
        global_index += n
        total_frames += n

        if not args.no_video:
            for cam, key in (("cam0", "observation.images.cam_left"),
                             ("cam2", "observation.images.cam_right")):
                if cam in ep["cams"]:
                    make_video(ep["dir"], cam,
                               os.path.join(args.out, f"videos/chunk-000/{key}/episode_{ei:06d}.mp4"),
                               args.fps)

        ep_lines.append({"episode_index": ei, "tasks": [ep["task"]], "length": n})

    with open(os.path.join(args.out, "meta/episodes.jsonl"), "w", encoding="utf-8") as f:
        for line in ep_lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ---- stats ----
    st = stats_for(episodes)
    st["timestamp"] = {"mean": [0.0], "std": [1.0], "min": [0.0], "max": [0.0],
                       "count": [total_frames]}
    with open(os.path.join(args.out, "meta/stats.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)

    # ---- info.json ----
    names = STATE_COLS
    info = {
        "codebase_version": "v2.1",
        "robot_type": "agilex_tracer_piper_dual",
        "total_episodes": len(episodes),
        "total_frames": total_frames,
        "total_tasks": len(tasks),
        "total_videos": 0 if args.no_video else 2 * len(episodes),
        "total_chunks": 1,
        "chunks_size": 1000,
        "fps": args.fps,
        "splits": {"train": f"0:{len(episodes)}"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": {
            "observation.images.cam_left": {
                "dtype": "video", "shape": [height, width, 3],
                "names": ["height", "width", "channel"],
                "video_info": {"video.fps": args.fps, "video.codec": "h264",
                               "video.pix_fmt": "yuv420p", "video.is_depth_map": False,
                               "has_audio": False},
            },
            "observation.images.cam_right": {
                "dtype": "video", "shape": [height, width, 3],
                "names": ["height", "width", "channel"],
                "video_info": {"video.fps": args.fps, "video.codec": "h264",
                               "video.pix_fmt": "yuv420p", "video.is_depth_map": False,
                               "has_audio": False},
            },
            "observation.state": {"dtype": "float32", "shape": [DIM], "names": names},
            "action": {"dtype": "float32", "shape": [DIM], "names": names},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
        },
    }
    with open(os.path.join(args.out, "meta/info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)

    log("")
    log(f"Wrote {args.out}")
    log(f"  episodes : {len(episodes)}")
    log(f"  frames   : {total_frames}")
    log(f"  tasks    : {len(tasks)}")
    log(f"  image    : {width}x{height} @ {args.fps:g} fps")
    log(f"  state/action dim: {DIM} ({names[0]} ... {names[-1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
