# Deploying the open-source data and model: what actually happened

The survey (`oss-data-models-sim.md`) lists candidates. This file records which
ones were **actually downloaded and inspected**, because "the repo exists" and
"the artifact is usable" are different claims.

Network note: `huggingface.co` is unreachable from this machine, so everything
went through `hf-mirror.com`. The mirror needs a different URL shape for models
than for datasets:

| | URL shape |
|---|---|
| dataset file | `https://hf-mirror.com/datasets/<user>/<repo>/resolve/main/<path>` |
| model file | `https://hf-mirror.com/<user>/<repo>/resolve/main/<file>`  ← **no `/models`** |
| metadata | `https://hf-mirror.com/api/datasets/<user>/<repo>` |

Getting that wrong returns a 404 HTML page, not an error — easy to misread as
"the repo is gone".

## 1. Dataset — `HITdongdong/piper_dual_tube` — DEPLOYED (one episode)

First attempt hit `HTTP 429` ("quota of 100000 resolvers requests per 5 minutes").
A 20 s backoff was enough.

```
curl -sL -o ep0.parquet \
  https://hf-mirror.com/datasets/HITdongdong/piper_dual_tube/resolve/main/data/chunk-000/episode_000000.parquet
# 148 KB, Apache Parquet
```

Schema, read with pyarrow:

```
rows: 1   cols: 10
  observation.state                      fixed_size_list<float>[14]
  action                                 fixed_size_list<float>[14]
  observation.images.cam_high            struct<bytes, path>
  observation.images.cam_left_wrist      struct<bytes, path>
  observation.images.cam_right_wrist     struct<bytes, path>
  timestamp, frame_index, episode_index, index, task_index
```

Values are **radians** (abs max 1.421, not 80+). Each frame carries three real
JPEGs: 49 KB / 55 KB / 41 KB.

Verdict: **usable and structurally identical to this robot's layout** — 14-dim
state and action, two wrist cameras plus a head camera. The only thing this robot
lacks is the head camera, which is exactly the gap the task survey flagged for
handover tasks.

## 2. Model — `LBST/t16_pick_and_place_agilex_smolvla_18k` — VERIFIED, not loaded

`config.json` (2,333 B) fetched:

```json
{"type": "smolvla", "n_obs_steps": 1, "chunk_size": 50, "n_action_steps": 50,
 "input_features": {"observation.images.middle": [3,256,256],
                    "observation.images.right":  [3,256,256],
                    "observation.images.left":   [3,256,256],
                    "observation.state":         [14]},
 "output_features": {"action": [14]}}
```

`model.safetensors` is 906,713,496 B (LFS). Rather than pull 906 MB, the
safetensors header was read with a **200 KB ranged request** — the header is a
length-prefixed JSON block at the start of the file:

```
header length: 73696        tensors: 506
  model.state_proj.weight        F32 [960, 32]
  model.action_in_proj.weight    F32 [720, 32]
  model.action_out_proj.weight   F32 [32, 720]
```

That `[960, 32]` / `[32, 720]` is the decisive number: the action head is
**32-dim padded**, and the 14 real dims live inside it. This confirms from the
actual weights what the survey inferred from configs — a 14-dim dual-arm layout
fits the standard VLA head structurally, and needs fine-tuning rather than
zero-shot use.

## What is still not done

* **No fine-tuning run.** The checkpoint is verified as loadable-shaped, not as
  accurate on this robot. That needs the 5090 and a dataset in LeRobot v3.0.
* **Only 1 of 200 episodes** was downloaded. Pulling all of them is ~30× that,
  which is fine but was not needed to answer "is it usable".
* The dataset is **LeRobot v2.1**; the local LeRobot 0.4.4 refuses v2.1, so a
  conversion step is required before training (see `act-training-result.md`).

## Reproducing

```bash
# one episode
curl -sL -o ep0.parquet https://hf-mirror.com/datasets/HITdongdong/piper_dual_tube/resolve/main/data/chunk-000/episode_000000.parquet
python -c "import pyarrow.parquet as pq; print(pq.read_table('ep0.parquet').schema)"

# model config + weight header only
curl -sL -o cfg.json https://hf-mirror.com/LBST/t16_pick_and_place_agilex_smolvla_18k/resolve/main/config.json
curl -sL -r 0-200000 -o head.bin https://hf-mirror.com/LBST/t16_pick_and_place_agilex_smolvla_18k/resolve/main/model.safetensors
```
