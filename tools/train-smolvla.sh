#!/usr/bin/env bash
# ==============================================================================
# train-smolvla.sh — fine-tune SmolVLA on this robot's own demonstrations
#
# WHY SMOLVLA AND NOT ACT OR pi0
#   ACT trains from scratch and works, but it only sees what we teach it.
#   pi0 and OpenVLA cannot be used here: LeRobot's pi0 needs its patched
#   transformers fork, the PaliGemma tokenizer is gated, and every pretrained
#   checkpoint is single-arm 7-dim while this robot is dual-arm 14-dim.
#   SmolVLA is 450M params, takes state[14] -> action[14] natively, uses only
#   ~0.93 GB of VRAM, and starts from the SmolVLM2-500M vision-language
#   backbone, so the visual encoder already knows what objects look like.
#
# WHAT IT EXPECTS
#   A LeRobot **v3.0** dataset. Our own exporter writes v2.1 and LeRobot 0.4.4
#   refuses to load it, so run tools/to-lerobot-v30.py first (or pass --dataset
#   pointing at an already-converted one). The dataset must have exactly:
#     observation.images.cam_left   (3,256,256)
#     observation.images.cam_right  (3,256,256)
#     observation.state             (14,)
#     action                        (14,)
#   Two cameras are fine — verified that SmolVLA builds with empty_cameras=0.
#
# USAGE
#   ./train-smolvla.sh                    # newest dataset, fresh run
#   ./train-smolvla.sh --resume           # continue the last run
#   ./train-smolvla.sh --steps 30000      # longer
#   ./train-smolvla.sh --episodes 5-60    # only these episodes
#   ./train-smolvla.sh --dry-run          # validate only, do not train
# ==============================================================================
set -uo pipefail

BASE=${AGILEX_BASE:-/mnt/data/agilex}
VENV=$BASE/venv
PY=$VENV/bin/python
TRAIN=$VENV/bin/lerobot-train
HF_HOME_D=$BASE/.hf
DATA_ROOT=${AGILEX_DATA:-$BASE/data}
RUN_ROOT=${AGILEX_RUNS:-$BASE/runs}
STEPS=30000
BATCH=64
EPISODES=""
RESUME=0
DRY=0
DATASET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --resume)   RESUME=1; shift ;;
    --dry-run)  DRY=1; shift ;;
    --steps)    STEPS="$2"; shift 2 ;;
    --batch)    BATCH="$2"; shift 2 ;;
    --episodes) EPISODES="$2"; shift 2 ;;
    --dataset)  DATASET="$2"; shift 2 ;;
    -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Caches must live on the NAS. / on this box is 98% full and filling it breaks
# everyone else's jobs, not just ours.
export HF_HOME=$HF_HOME_D
export HF_ENDPOINT=https://hf-mirror.com
export XDG_CACHE_HOME=$BASE/.cache
export TORCH_HOME=$BASE/.cache/torch
export HF_HUB_OFFLINE=0
export WANDB_MODE=disabled
export TOKENIZERS_PARALLELISM=false

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- pick a dataset
if [ -z "$DATASET" ]; then
  DATASET=$(find "$DATA_ROOT" -maxdepth 1 -type d -name 'agilex_*' -printf '%T@ %p\n' 2>/dev/null \
            | sort -rn | head -1 | cut -d' ' -f2-)
fi
[ -n "$DATASET" ] && [ -f "$DATASET/meta/info.json" ] || {
  echo "no dataset with meta/info.json found under $DATA_ROOT" >&2
  echo "run tools/to-lerobot-v30.py first" >&2
  exit 1
}

say "dataset: $DATASET"

# ------------------------------------------------------------- validate it
"$PY" - "$DATASET" "$EPISODES" <<'PYEOF' || exit 1
import json, os, sys
root, ep_filter = sys.argv[1], sys.argv[2]
info = json.load(open(os.path.join(root, "meta", "info.json")))
ver = info.get("codebase_version")
if ver != "v3.0":
    sys.exit(f"dataset is {ver}; SmolVLA path expects v3.0. Convert with tools/to-lerobot-v30.py")

want = {"observation.images.cam_left", "observation.images.cam_right",
        "observation.state", "action"}
have = set(info["features"])
missing = want - have
if missing:
    sys.exit(f"missing features: {sorted(missing)}; have {sorted(have)}")

shape_state = info["features"]["observation.state"]["shape"]
shape_act = info["features"]["action"]["shape"]
if list(shape_state) != [14] or list(shape_act) != [14]:
    sys.exit(f"expected 14-dim state/action, got {shape_state}/{shape_act}")

n_ep, n_fr = info["total_episodes"], info["total_frames"]
print(f"  episodes={n_ep} frames={n_fr} fps={info['fps']}")
print(f"  state={shape_state} action={shape_act}")

# A dataset of 30 near-identical scripted episodes trains a policy that outputs
# one almost-constant vector. Warn rather than refuse - the user may know better.
if n_ep < 40:
    print(f"  WARNING: only {n_ep} episodes. 50-100 with varied object positions is "
          f"the range where ACT/SmolVLA start generalising.")
if ep_filter:
    print(f"  restricting to episodes {ep_filter}")
PYEOF

if [ "$DRY" = "1" ]; then
  say "dry run: dataset is valid, not training"
  exit 0
fi

# ------------------------------------------------------------------ run dir
NAME=$(basename "$DATASET")
OUT=$RUN_ROOT/smolvla_$NAME
# LeRobot refuses to write into an existing dir when resume=false, so we must
# NOT pre-create $OUT - it makes the directory itself. The log goes alongside.
if [ "$RESUME" = "1" ] && [ -d "$OUT" ]; then
  say "resuming from $OUT"
  RESUME_ARGS="--resume=true"
else
  rm -rf "$OUT"
  RESUME_ARGS="--resume=false"
fi
mkdir -p "$RUN_ROOT"
LOG=$RUN_ROOT/smolvla_$NAME.log
: > "$LOG"

say "training SmolVLA -> $OUT"
echo "  steps=$STEPS batch=$BATCH"

EP_ARGS=""
[ -n "$EPISODES" ] && EP_ARGS="--dataset.episodes=$(python3 -c "
import sys
s='$EPISODES'
out=[]
for part in s.split(','):
    if '-' in part:
        a,b=part.split('-'); out += list(range(int(a), int(b)+1))
    else:
        out.append(int(part))
print('['+','.join(map(str,out))+']')
")"

# push_to_hub MUST be false: HF is blocked here, and with HF_HUB_OFFLINE=1 the
# trainer otherwise finishes the run and then dies on an OfflineModeIsEnabled
# traceback after the last checkpoint. Harmless but alarming.
"$TRAIN" \
  --dataset.repo_id="local/$(basename "$DATASET" | tr '_' '-')" \
  --dataset.root="$DATASET" \
  $EP_ARGS \
  --policy.type=smolvla \
  --policy.device=cuda \
  --policy.load_vlm_weights=true \
  --policy.freeze_vision_encoder=true \
  --policy.train_expert_only=true \
  --policy.n_action_steps=50 \
  --policy.chunk_size=50 \
  --policy.push_to_hub=false \
  --output_dir="$OUT" \
  --batch_size="$BATCH" \
  --steps="$STEPS" \
  --save_freq=5000 \
  --log_freq=50 \
  --num_workers=8 \
  --seed=0 \
  $RESUME_ARGS \
  2>&1 | tee "$LOG"

say "done — checkpoints in $OUT/checkpoints"
ls -1 "$OUT/checkpoints" 2>/dev/null | sed 's/^/  /'
