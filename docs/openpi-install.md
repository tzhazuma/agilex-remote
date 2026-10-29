# openpi (JAX) install on gpu-a + Piper π0-FAST checkpoint load

**Host:** `gpu-a` = <gpu-host> (4× RTX 5090 32 GB, driver 570.211.01, compute capability **12.0 = sm_120**)
**Date:** 2026-10-22 01:20–01:56 CST
**Verdict:** ✅ **JAX runs native sm_120 kernels** · ✅ **Piper checkpoint restores and loads into a stock openpi `pi0_fast` architecture** (better than the predicted "config missing" outcome)

---

## 1. TL;DR

| Question | Answer |
|---|---|
| Does JAX work on the 5090 (sm_120)? | **Yes.** jax/jaxlib 0.5.3 + CUDA 12.9 ptxas; matmul + reductions run on `CudaDevice` RTX 5090; XLA-generated PTX is `.target sm_120a` |
| Does the Piper checkpoint restore? | **Yes.** Orbax restores `params/` (42 leaves, **2,951,204,592 params = 2.95 B**) and `train_state/` (69 leaves, step 1001) |
| Does it load into openpi? | **Yes** — `Pi0FASTConfig(action_dim=7, action_horizon=10, max_token_len=180, paligemma_variant="gemma_2b_lora")` → `cfg.load(params)` passes exact shape validation, and a real forward pass runs (CPU and **on the 5090**) |
| State / action dims | **state = 7**, **actions = 7** (`[j1..j6 rad, gripper 0..1]`), action chunk 10 @ 20 Hz |
| What's still missing for inference | No `piper*` config exists in openpi (`grep -c piper config.py` = **0**): the data/policy config (input keys, prompt, `asset_id="piper_ppv4ppv5"`) and the FAST/PaliGemma tokenizer assets must be authored/fetched |

Everything lives on the NAS; **nothing was installed on `/`** (still 98 %, 18 GB free) and the existing `/mnt/nas_datasets/<user>/agilex/venv` was never touched.

---

## 2. Exact commands that worked

### 2.1 Clone (HTTPS worked — SSH-over-443 fallback never needed)

```bash
mkdir -p /mnt/nas_datasets/<user>/agilex
cd /mnt/nas_datasets/<user>/agilex
git clone https://github.com/Physical-Intelligence/openpi.git openpi      # worked, no hang
git -C openpi submodule update --init --recursive                         # aloha + LIBERO, also over HTTPS
```

> **Necessary fix:** the NAS squashes ownership (files show `uid 1024 users`, we are `uid 1014(<user>)`), so git refused to operate on uv's *own* git cache with
> `fatal: detected dubious ownership in repository at '…/.uvcache/git-v1/db/…'`.
> Fixed with `git config --global --add safe.directory "*"` (only affects user `<user>`).
> Symptom if not fixed: `uv sync` fails with `cause: Git operation failed`.

### 2.2 Environment (mirrors + caches on NAS)

`/mnt/nas_datasets/<user>/agilex/openpi-logs/env.sh`:

```bash
export UV_CACHE_DIR=/mnt/nas_datasets/<user>/agilex/.uvcache
export HF_HOME=/mnt/nas_datasets/<user>/agilex/.hf
export XDG_CACHE_HOME=/mnt/nas_datasets/<user>/agilex/.cache
export PIP_CACHE_DIR=/mnt/nas_datasets/<user>/agilex/.pipcache
export HF_ENDPOINT=https://hf-mirror.com
export UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
export UV_HTTP_TIMEOUT=180
export GIT_LFS_SKIP_SMUDGE=1
export OPENPI_DATA_HOME=/mnt/nas_datasets/<user>/agilex/.openpi
```

### 2.3 Install (openpi's documented `uv` flow, run detached)

```bash
source /mnt/nas_datasets/<user>/agilex/openpi-logs/env.sh
cd /mnt/nas_datasets/<user>/agilex/openpi
uv python install 3.11          # repo pins .python-version = 3.11
uv sync                         # creates .venv, pulls lerobot + dlimp from git
uv pip install -e .
```

Log: `/mnt/nas_datasets/<user>/agilex/openpi-logs/sync3.log` → `=== EDIT_EXIT=0 ===`, `.venv` = **7.7 GB**, Python 3.11.16.
`uv sync` took ~13 min wall (≈1 min of that was self-inflicted: a leftover `uv pip install` from a first attempt held the venv lock — see §6).

### 2.4 Installed versions (as locked by openpi)

```
jax 0.5.3 · jaxlib 0.5.3 · jax-cuda12-plugin 0.5.3 · jax-cuda12-pjrt 0.5.3
nvidia-cuda-nvcc-cu12 12.9.41   <-- ptxas 12.9, knows sm_120
nvidia-cuda-runtime-cu12 12.6.77 (torch's) · nvidia-cudnn-cu12 9.5.1.17
flax 0.10.2 · orbax-checkpoint 0.11.13 · torch 2.7.1 · transformers 4.53.2 · numpy 1.26.4
```

### 2.5 Checkpoint download (hf-mirror)

```bash
export HF_ENDPOINT=https://hf-mirror.com
uvx --from huggingface_hub hf download Kavin60606/pi0-fast-piper-h-plate-pick-ft-v2 \
    --local-dir /mnt/nas_datasets/<user>/agilex/models/Kavin60606/pi0-fast-piper-h-plate-pick-ft-v2
```

**Size reality check:** the repo is much bigger than the 2–4 GB expected — it ships **four** Orbax checkpoints (`1000`, `1500`, `2000`, `2500`), each ≈8 GB (`params/` 5.2 GB + `train_state/` 2.8 GB) → **~31 GB total** on disk. `huggingface.co` is DNS-blocked; the mirror served everything without auth.

---

## 3. JAX on sm_120 — proof, not just `jax.devices()`

Probe (`/mnt/nas_datasets/<user>/agilex/openpi-logs/jax_probe.py`), run as
`CUDA_VISIBLE_DEVICES=3 .venv/bin/python jax_probe.py`:

```
python 3.11.16
jax 0.5.3
jaxlib 0.5.3
backend gpu
devices [CudaDevice(id=0)]
  device: gpu NVIDIA GeForce RTX 5090
MATMUL_OK value= 1073741824.0        # (1024x1024 @ 1024x1024).sum() == 1024^3 exactly
ELEMWISE_OK value= 0.4658278226852417
```

Real-kernel evidence for the **exact** architecture (the point of the exercise):

```bash
XLA_FLAGS="--xla_dump_to=/tmp/xd --xla_gpu_dump_llvmir" \
  CUDA_VISIBLE_DEVICES=3 .venv/bin/python -c "import jax,jax.numpy as jnp; x=jnp.ones((512,512)); print(float((x@x).sum().block_until_ready()))"
grep -rh "target sm_120a" /tmp/xd | sort -u
# .target sm_120a
```

→ XLA emitted PTX/code for **`sm_120a`** (Blackwell). Copy kept at
`/mnt/nas_datasets/<user>/agilex/openpi-logs/evidence/module_0005.jit_matmul.ptx`.

So: **no blocker.** openpi's pinned `jax[cuda12]==0.5.3` resolves to the CUDA **12.9** nvcc/runtime wheels, which is exactly what Blackwell needs; the driver JIT/compiles for sm_120 and the kernels execute. No newer JAX build was required, and no fallback path was needed.

---

## 4. Piper checkpoint: restore + load

### 4.1 Raw Orbax restore (config-free)

```
### step 1000: .../checkpoints/ppv4ppv5_run1/1000/params
  orbax restore OK: leaves=42 params=2,951,204,592 (2.9512 B)
  dtypes: {'bfloat16': 9, 'float32': 33}
  lora leaves: 10
### step 2500: .../2500/params
  orbax restore OK: leaves=42 params=2,951,204,592 (2.9512 B)
```

`train_state/` (step 1000) also restores:

```
metadata top keys: ema_params, model_def, opt_state, params, step
TRAIN_STATE_RESTORE_OK leaves: 69
  step: 1001
```

(`train_state.params` is empty by design — openpi's `save_state` splits params out into the separate `params/` item and keeps `ema_params` in the train state; the 1000-step dir stores step 1001.)

Parameter tree is `params/PaliGemma/{img,llm}` with **scanned** layer stacks and **LoRA** adapters, matching the model card ("PaliGemma 2B + SigLIP So400m, ~50 M trainable LoRA params"):

```
PaliGemma/img/Transformer/encoderblock/{LayerNorm_*,MlpBlock_*,MultiHeadDotProductAttention_*}   [27, …]
PaliGemma/llm/layers/{pre_attention_norm,pre_ffw_norm}                                          [18, 2048]
PaliGemma/llm/layers/attn/{q,kv,attn_vec}_einsum/{w,lora_a,lora_b}                              [18, …]
PaliGemma/llm/layers/mlp/{gating_einsum,linear,gating_einsum_lora_a,…_lora_b,linear_lora_a,…}   [18, …]
PaliGemma/llm/embedder/input_embedding                                                          [257152, 2048]
```

### 4.2 Loading into openpi's own model (this is the surprising part)

```python
from openpi.models import model as _model, pi0_fast
params = _model.restore_params("…/1000/params", restore_type=np.ndarray)
cfg = pi0_fast.Pi0FASTConfig(action_dim=7, action_horizon=10, max_token_len=180,
                             paligemma_variant="gemma_2b_lora")
model = cfg.load(params)          # <-- BaseModelConfig.load, not model.load
```

```
cfg.load(params) -> MODEL_LOAD_OK  (exact shape check passed)
forward: img tokens (1, 256, 2048) mean -0.02942 | llm embed (1, 3, 2048) mean -0.01453
WEIGHTS_ARE_LIVE_OK
```

`BaseModelConfig.load()` runs `at.check_pytree_equality(check_shapes=True)`, so this is a **strict structural match** with openpi's `gemma_2b_lora` π0-FAST architecture — not a lenient merge. Same result for step 1000 and step 2500.

### 4.3 Forward pass on the 5090 (real weights, sm_120)

```bash
CUDA_VISIBLE_DEVICES=3 XLA_PYTHON_CLIENT_PREALLOCATE=false XLA_PYTHON_CLIENT_MEM_FRACTION=0.35 \
  .venv/bin/python gpu_fwd.py
```

```
devices: [CudaDevice(id=0)]
params as bf16: 2,951,204,592
GPU_FORWARD_OK img tokens (1, 256, 2048) bfloat16 mean -0.02865
device of output: {CudaDevice(id=0)}
```

(SigLIP encoder forward with the checkpoint's real weights on the RTX 5090. bf16 was used to fit the ~11.8 GB free on GPU 3; the fp32 model build OOM'd first — see §5.)

### 4.4 Norm stats — state/action dimensions

`…/1000/assets/local/piper_ppv4ppv5/norm_stats.json` (asset id `piper_ppv4ppv5`):

| | dim | mean (first 3) | std (first 3) | q01 (first 3) | q99 (first 3) |
|---|---|---|---|---|---|
| `state` | **7** | 1.4576, 2.2535, −2.0664 | 0.1293, 0.3047, 0.3330 | 1.2073, 0.1652, −2.4968 | 1.7085, 2.4854, −0.0876 |
| `actions` | **7** | −0.00262, −0.00271, 0.00031 | 0.0371, 0.0310, 0.0402 | −0.2016, −0.1579, −0.0615 | 0.0578, 0.0339, 0.2161 |

Action means ≈0 with small std ⇒ these are **delta joint actions** (not absolute), 7-DoF `[j1..j6 rad, gripper∈[0,1]]`, chunk 10 @ 20 Hz — consistent with the model card.

---

## 5. What failed / is still missing (real errors)

1. **GPU memory is the real constraint, not JAX.** At run time all four 5090s were occupied:

   | GPU | used / free | occupant |
   |---|---|---|
   | 0 | 21.7 / **10.5 GB** | `llama-server` (PID 959219, 21.6 GB) |
   | 1 | 29.5 / **2.6 GB** | `scripts/serve_policy.py --policy.config pi0_libero_low_mem_finetune` (PID 1682255, up 33 days, 29.5 GB) |
   | 2 | 23.9 / **8.2 GB** | `llama-server` (23.9 GB) |
   | 3 | 20.3 / **11.8 GB** | `llama-server` (20.3 GB) |

   The task brief said "a training job is using GPU 0, use GPU 1/2/3" — that was **not** accurate: **no `lerobot-train` process exists** on the box, GPU 1 holds a long-running pi0-LIBERO *policy server* and has only 2.6 GB free, and GPUs 0/2/3 run `llama-server`. I used **GPU 3** (most free) throughout. *(Separately: `/mnt/nas_datasets/<user>/agilex/train-act.log` ends at 01:17:23 with `huggingface_hub.errors.OfflineModeIsEnabled: Cannot reach https://huggingface.co/api/repos/create: offline mode is enabled` — i.e. the ACT job appears to have already died on an HF offline-mode error before I started. I did not touch it or its venv.)*

2. **Model build OOM on GPU 3** (first attempt, fp32, JAX preallocating 75 % of a 32 GB card that only had 11.8 GB free):
   ```
   jaxlib.xla_extension.XlaRuntimeError: RESOURCE_EXHAUSTED: Out of memory while trying to allocate 2106589184 bytes.
     at gemma_fast.py:121  jnp.dot(x, self.input_embedding_table.T)
   ```
   Worked around with `JAX_PLATFORMS=cpu` (structural load) and with bf16 + `XLA_PYTHON_CLIENT_PREALLOCATE=false` (GPU forward). **A full π0-FAST inference would not fit in the currently free GPU memory** (needs ≥ ~12 GB for weights + activations); it will fit once a 5090 is freed.

3. **`model.load(...)` is not the API** — `AttributeError: 'Pi0FAST' object has no attribute 'load'`; `load()` lives on `BaseModelConfig` (`openpi/models/model.py:233`), so the correct call is `cfg.load(params)`.

4. **No `piper*` training config exists in openpi** (`grep -rc piper src/openpi/training/config.py` = **0**). The *weights* are architecture-compatible with the stock π0-FAST LoRA config, but running this policy still needs, authored by hand:
   - a data/policy config with the Piper observation keys, prompt, and `asset_id="piper_ppv4ppv5"`;
   - the **FAST action tokenizer** + PaliGemma tokenizer assets (`openpi/models/tokenizer.py` downloads `gs://big_vision/paligemma_tokenizer.model`; `pi0_fast` also needs `gs://openpi-assets/checkpoints/pi0_fast_base/assets/…`). The checkpoint itself only ships `norm_stats.json` — **no tokenizer assets**;
   - the action-space mapping (7-D delta joint + gripper) — norm stats alone don't define it.

5. Non-blocking noise: `cudnn` autotuning printed `slow_operation_alarm` warnings (6–19 s) while benchmarking the SigLIP conv on a busy GPU — cosmetic.

---

## 6. Gotchas worth remembering

- **Two `uv` processes cannot share the venv**: my first `uv sync` was killed (`pkill -f sync.sh` also killed the SSH shell — exit 255) but its child `uv pip install -e .` survived, held the `.venv` lock, and made the next `uv sync` hang silently for ~10 min. Kill by PID, not by pattern; check `ps --ppid`.
- `export GIT_CONFIG_VALUE_0=*` gets **glob-expanded** by bash — quote it (`"*"`) or use `git config --global`.
- `hf download --local-dir` temporarily doubles space (staging under `<dest>/.cache/huggingface/download`); peak ~15 GB while writing, settles afterwards.
- `uv python install 3.11` works fine through the campus network (python-build-standalone from GitHub); no mirror override needed.
- The `raw.githubusercontent.com` / `hf-mirror.com` / Tsinghua PyPI endpoints were all reachable; **github.com git-over-HTTPS also worked** (both the openpi clone and uv's `lerobot`/`dlimp` git deps), so the SSH-443 fallback was never used.

## 7. Artifacts

| Path (on gpu-a) | What |
|---|---|
| `/mnt/nas_datasets/<user>/agilex/openpi` | openpi checkout + `.venv` (Python 3.11.16, 7.7 GB) |
| `/mnt/nas_datasets/<user>/agilex/openpi-logs/env.sh` | env with mirrors + NAS caches |
| `…/openpi-logs/sync3.log`, `dl.log` | install + download logs |
| `…/openpi-logs/jax_probe.py`, `restore.py`, `restore2.py`, `gpu_fwd.py` | probe / restore / forward scripts |
| `…/openpi-logs/evidence/module_0005.jit_matmul.ptx` | XLA PTX, `.target sm_120a` |
| `/mnt/nas_datasets/<user>/agilex/models/Kavin60606/pi0-fast-piper-h-plate-pick-ft-v2` | checkpoint (31 GB, steps 1000/1500/2000/2500) |
