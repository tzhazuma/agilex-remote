# ACT training run — completion, loss analysis, and checkpoint validation

**Date:** 2026-10-22 (Asia/Shanghai)
**Host:** 5090a / <gpu-host> (4× RTX 5090 32 GB)
**Run dir:** `/mnt/nas_datasets/<user>/agilex/runs/act_pickplace`
**Job:** `lerobot-train` 0.4.4, ACT, dataset `local/agilex_pickplace` (episodes 11–40)

---

## 1. Verdict (short version)

| Question | Answer |
|---|---|
| Did training finish? | **Yes** — all 20,000 steps, four checkpoints saved (005000, 010000, 015000, 020000). |
| Does the process exit cleanly? | **No** — it crashes *after* the final checkpoint, in `push_model_to_hub` (HF Hub push attempted with `HF_HUB_OFFLINE=1`). Training output is unaffected. |
| Did loss go down? | **Yes.** Total loss on a fixed held-out batch: **0.0893 → 0.0432 → 0.0340 → 0.0250** at 5K/10K/15K/20K. |
| Does the checkpoint load? | **Yes.** `ACTPolicy.from_pretrained` → 51,613,582 params, no errors. |
| Does `select_action` work? | **Yes.** `(1, 14)`, all finite, action queue behaves correctly. |
| Predicted vs ground truth | **Close.** MAE 0.19–0.32 (dataset action units), max error 2.0–2.8 on gripper/L_j0/L_j4. |
| Is the policy actually useful? | **Uncertain — the training data is largely degenerate (see §7).** Low loss here does not prove a working manipulation policy. |

---

## 2. Training timeline and checkpoints

- Header in log: `===== ACT training Thu Oct 22 12:39:01 AM CST 2026 =====`
- Last log line: `End of training` at `2026-10-22 01:17:21`
- Wall clock: **38 min 20 s** for 20,000 steps (~9.7 step/s, batch 32).
- 640K samples seen, **109.42 epochs** over 5,849 training frames, lr 1e-5, final `grdn` 2.932.

Checkpoints observed (all present, 592 MB each, 2.4 GB total):

```
checkpoints/005000
checkpoints/010000
checkpoints/015000
checkpoints/020000
checkpoints/last        # copy of 020000
```

`training_state/training_step.json` in 020000 reads `{"step": 20000}`.

**Final checkpoint:**
`/mnt/nas_datasets/<user>/agilex/runs/act_pickplace/checkpoints/020000/pretrained_model/`
(`model.safetensors` 206 MB, `config.json`, `train_config.json`, `policy_preprocessor.json`,
`policy_postprocessor.json`, plus the normalizer/unnormalizer `*.safetensors`).

---

## 3. Why the job exits with a traceback (not a training failure)

After `End of training`, `lerobot_train.py:536` calls `unwrapped_policy.push_model_to_hub(cfg)`,
which tries `huggingface.co/api/repos/create` and dies because the environment forces offline mode:

```
huggingface_hub.errors.OfflineModeIsEnabled: Cannot reach https://huggingface.co/api/repos/create:
offline mode is enabled. To disable it, please unset the `HF_HUB_OFFLINE` environment variable.
```

Consequences:
- All 20,000 steps and all checkpoints were written **before** this error — nothing was lost.
- Exit status is non-zero, so a supervisor script would report "failed" even though training succeeded.
- If this matters, set `--policy.push_to_hub=false` (the config currently has `"push_to_hub": true`).

**Checkpoints are fine; the run just cannot publish.**

---

## 4. Loss — what the log does and does not contain

⚠️ **`/mnt/nas_datasets/<user>/agilex/train-act.log` is truncated.** It is only 215 KB and begins at
step 16,701 — the first ~33 minutes of the run were trimmed/rotated away. Only **33 loss lines**
survive (steps 17K–20K):

```
step:17K ... loss:0.024
step:18K ... loss:0.023
step:19K ... loss:0.021
step:20K ... loss:0.020   (final, 2026-10-22 01:17:14)
```

There is **no wandb directory and no metrics JSON** anywhere in the run dir — only checkpoints.
So the early-training loss history is simply not recoverable from the log.

To get an honest loss *trend* I re-evaluated all four checkpoints on one identical fixed batch
(12 frames from episodes 11/17/23/29/35/40, run through the saved preprocessor, `chunk_size=100`,
masked by `action_is_pad`). Same data, same metric, four model snapshots:

| checkpoint | total loss (train-mode) | L1 | KLD | eval L1 (deployment path) | unnormalized MAE |
|---|---|---|---|---|---|
| 005000 | 0.08926 | 0.05195 | 0.00373 | 0.04886 | 0.5452 |
| 010000 | 0.04316 | 0.03403 | 0.00091 | 0.03436 | 0.4042 |
| 015000 | 0.03401 | 0.02901 | 0.00050 | 0.02671 | 0.2821 |
| 020000 | **0.02498** | **0.02216** | **0.00028** | **0.02096** | **0.2398** |

**The loss decreased monotonically, by ~3.6× from 5K to 20K.** The 020000 value (0.0250) is
consistent with the logged 0.020 (the logger reports a running average over the interval and
training uses dropout; the two are not expected to match exactly).

Note the log "loss" is `L1 + kl_weight(10) × KLD`, which is why KLD is reported separately above.

---

## 5. Policy loads and runs

Loader used: the real deployment path — `PreTrainedConfig.from_pretrained` →
`ACTPolicy.from_pretrained` → saved `policy_preprocessor` → `select_action` → saved
`policy_postprocessor`.

```
ACT loaded: 51,613,582 params
config: chunk_size=100, n_action_steps=100, n_obs_steps=1, use_vae=True,
        vision_backbone=resnet18, dim_model=512, 4 enc / 1 dec layers
inputs:  observation.images.cam_left (3,256,256), observation.images.cam_right (3,256,256),
         observation.state (14)
output:  action (14)
```

Checks (sample: episode 11, frame 140):

| check | result |
|---|---|
| `predict_action_chunk(batch)` shape | `(1, 100, 14)` ✅ |
| `select_action(batch)` shape | `(1, 14)` ✅ |
| all values finite | `True` ✅ (no NaNs/Infs anywhere) |
| output not constant | `True` — per-joint spread within a chunk is non-zero for every joint |
| action-queue semantics | 2nd `select_action` == `chunk[:, 1]` ✅ |
| depends on state | `max|f(s) − f(s+0.1)| = 0.033` ✅ |
| depends on vision | `max|f(img) − f(img+0.1)| = 0.0105` ✅ |

Both modalities influence the output, so it is not silently ignoring the cameras or the state.

---

## 6. Predicted vs ground-truth action, per joint

Action space, 14 dims: `L_j0..L_j5, L_grip, R_j0..R_j5, R_grip`.
Values below are in **dataset action units**. The `*_deg` columns are a conversion applied only to
dims whose recorded range is within ±2.5 (L_j2, L_j3, L_j5, R_j1, R_j2, R_grip) — those are almost
certainly radians; the rest look like degrees (see §8 for the caveat).

### Episode 11, frame 140 — healthy episode, arm and gripper moving

| joint | state | gt_action | predicted | err | gt (deg) | pred (deg) |
|---|---|---|---|---|---|---|
| L_j0 | 11.950 | 11.956 | 10.463 | 1.493 | 11.96 | 10.46 |
| L_j1 | 11.934 | 11.958 | 11.902 | 0.056 | 11.96 | 11.90 |
| L_j2 | -2.388 | -2.400 | -2.381 | 0.019 | -137.51 | -136.41 |
| L_j3 | -2.106 | -2.110 | -2.110 | 0.000 | -120.89 | -120.91 |
| L_j4 | 14.910 | 14.946 | 12.952 | 1.994 | 14.95 | 12.95 |
| L_j5 | -1.490 | -1.511 | -1.511 | 0.000 | -86.57 | -86.57 |
| L_grip | 8.500 | 9.000 | 9.845 | 0.845 | 9.00 | 9.85 |
| R_j0 | -1.689 | 13.537 | 13.534 | 0.003 | 13.54 | 13.53 |
| R_j1 | -0.208 | -0.208 | -0.208 | 0.000 | -11.92 | -11.92 |
| R_j2 | 0.202 | 0.202 | 0.202 | 0.000 | 11.57 | 11.57 |
| R_j3 | 38.370 | 38.478 | 38.470 | 0.008 | 38.48 | 38.47 |
| R_j4 | 11.925 | 20.607 | 20.602 | 0.005 | 20.61 | 20.60 |
| R_j5 | -31.152 | -31.152 | -31.146 | 0.006 | -31.15 | -31.15 |
| R_grip | -0.300 | 0.000 | 0.000 | 0.000 | 0.00 | 0.01 |

**MAE 0.3164, max error 1.99.**

### Episode 20, frame 60 — healthy, mid-trajectory

MAE **0.1943**, max error 2.15 (gripper: state 15.2, gt 12.0, pred 14.15 — the model lags the
gripper closing rather than jumping with it). Largest joint errors: `L_j2` 3.12° in degree terms,
`L_grip` 2.15 units.

### Episode 40, frame 60 — degraded episode (`observation.state` frozen)

| joint | state | gt_action | predicted | err |
|---|---|---|---|---|
| L_j0 | 11.582 | 114.556 | 117.321 | 2.765 |
| L_j4 | 38.314 | 70.000 | 70.247 | 0.247 |
| L_j1 | 0.065 | 11.158 | 11.107 | 0.051 |
| **MAE** | | | | **0.2217** (max 2.76) |

This row is the most informative one: the state says 11.58, the ground-truth action is 114.56, and
the policy outputs 117.32. It is clearly **not** copying the state — it reproduces the recorded
action-space values, including the (suspicious) large offsets. That is genuine learned behaviour,
but of the dataset's quirk, not of manipulation.

---

## 7. Data-quality findings (important)

While validating I found the training data is far from a normal demonstration set. Evidence:

**(a) `observation.state` is frozen for episodes 22–40.** Within those episodes every frame carries
the *same* state vector (19–21 unique rows out of ~170 frames). Example, episode 40, every frame:

```
[11.582, 0.065, -0.958, -17.964, 38.314, 29.407, 17.5,
 -1.689, -0.208, 0.202, 38.370, 11.925, -31.152, -0.300]
```

Episodes 11–21 do have a live state stream (49–72 unique rows); episodes 22+ do not.

**(b) `action` drifts away from `state`, episode by episode.** Mean `|action − state|` for joint
`L_j0` grows monotonically: ep22 21.3 → ep25 48.6 → ep30 67.2 → ep35 86.0 → ep40 104.5, while the
state stays pinned at 11.58. Over episodes 11–40 the per-joint mean `|action − state|` is:

```
L:  39.8, 6.8,  0.7,  8.8, 16.5, 17.1, 0.6
R:  15.2, 0.0,  0.0,  0.1,  8.7,  0.0, 0.3
```

**(c) The data is mostly static, and the right arm never moves.** 76–90 % of consecutive frame
pairs have an identical action vector. Per-joint maximum change in a single 100 ms step, across all
of episodes 11–40:

```
L_j0 0.900 | L_j1 0.800 | L_j2 0.700 | L_j3 0.000 | L_j4 0.700 | L_j5 0.000 | L_grip 3.000
R_j0 0.000 | R_j1 0.000 | R_j2 0.000 | R_j3 0.000 | R_j4 0.000 | R_j5 0.000 | R_grip 0.000
```

Every right-arm joint — **including the right gripper** — is bit-for-bit constant *within* every
episode (values differ between episodes, but nothing moves during one). The largest single step
anywhere is 3.0, and it is the left gripper at episode 11, frame 130. So a 14-DoF bimanual
"pick and place" dataset contains, per episode, at most a fraction of a degree of left-arm motion
plus a gripper open/close.

**(d) State and action live in different spaces.** For most joints `action` and `state` are not the
same quantity: e.g. `L_j0` action range 0–118 while state range 8–48. If this dataset came from a
leader/follower recording, the two streams were never reconciled.

**Implication:** the model fits this data extremely well because most of the mapping is
"predict an almost-constant action vector", with episode-level offsets. **A low loss and a good
predicted-vs-GT match here do not demonstrate that the policy can drive the real robot.** Before
using `020000` on hardware, the data collection path (why state freezes from ep22, why action drifts)
should be understood and the dataset re-recorded.

---

## 8. Units caveat

The 14 action dims are **not in one consistent unit**; their recorded ranges (from `meta/stats.json`,
which matches the saved normalizer exactly) split into two groups:

| looks like radians (range within ±2.5) | looks like degrees (range ≫) |
|---|---|
| `L_j2` −2.40–0, `L_j3` −2.12–0, `L_j5` −1.51–0, `R_j1` −0.21–0, `R_j2` 0–0.20, `R_grip` −0.80–0 | `L_j0` 0–118, `L_j1` 0–20, `L_j4` 0–70, `L_grip` 0–18, `R_j0` −1.9–26, `R_j3` 0–41.5, `R_j4` 0–34, `R_j5` −31–0 |

That mixed-unit pattern inside a single 14-dim vector is itself suspicious, so treat the `*_deg`
columns as an interpretation, not as ground truth. Raw action-unit numbers are the authoritative
ones above.

---

## 9. What was *not* done

- Training was **not** touched: no kill, no restart, no new training job, dataset not modified.
- All validation ran on **GPU 1** (`CUDA_VISIBLE_DEVICES=1`), needing ~1.2 GB. Other users'
  processes (21–29 GB per GPU) were left alone. GPU 0's training job had already exited by 01:17.
- No held-out evaluation: every episode in this checkpoint's config (11–40) was used for training,
  so the numbers in §6 are in-distribution fits, not generalisation.

---

## 10. Reproduce

Scripts live in `agilex-setup/` and are copied to `/mnt/nas_datasets/<user>/agilex/` on 5090a:

```bash
cd /mnt/nas_datasets/<user>/agilex
export HF_HOME=/mnt/nas_datasets/<user>/agilex/.hf
export HF_HUB_OFFLINE=1
export XDG_CACHE_HOME=/mnt/nas_datasets/<user>/agilex/.cache
export CUDA_VISIBLE_DEVICES=1
./venv/bin/python final_validation.py     # loads 020000, prints §5/§6 tables
./venv/bin/python loss_trend.py           # 4-checkpoint loss comparison (§4)
```

`units3.py` / `units4.py` reproduce the data-quality evidence in §7.
