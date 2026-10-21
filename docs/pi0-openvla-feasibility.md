# π0 / π0-FAST / OpenVLA on AgileX Piper — bounded feasibility check

**Date:** 2026-10-22 (Asia/Shanghai)
**Host inspected:** `5090a` (GPU_HOST) over SSH
**Venv inspected:** `/mnt/data/agilex/venv` — LeRobot **0.4.4**, torch **2.10.0+cu128**, Python **3.10.21**
**Budget:** read/inspect only + one small isolated install (transformers into a `--target` dir, never into the shared venv) + one VRAM probe on GPU 3. Nothing was written into the venv, the ACT job's environment, or GPU 0.

**Verdict up front: π0/π0-FAST is *not* a drop-in for a 14-dim dual Piper arm, and OpenVLA is a worse fit than π0. 32 GB of VRAM is not the blocker — software packaging, checkpoint format, and action-space semantics are.**

---

## 0. Environment facts measured at the time of the check

```
nvidia-smi (5090a):
  0, 21664 MiB used / 32607 MiB   <- ACT job (pid 959219; same pid also holds GPU 2 and 3)
  1, 30337 MiB used / 32607 MiB   <- leftover openpi serve_policy.py (pid 1682255, etime 33d)
  2, 23872 MiB used / 32607 MiB   <- ACT job
  3, 20320 MiB used / 32607 MiB   <- ACT job
free: GPU0 10.9 GB | GPU1 2.3 GB | GPU2 8.7 GB | GPU3 12.3 GB
/mnt/nas_datasets 70T, 29T avail   |   / has 18G free
```

Two facts worth carrying forward:

1. **An openpi π0 server is already running on this box** (`scripts/serve_policy.py --policy.config pi0_libero_low_mem_finetune`, 33 days uptime, **29.5 GB** resident). That 29.5 GB is mostly JAX's default preallocation (75 % of the card), *not* the model's real need — but it does show that a JAX π0 server is a real, already-demonstrated thing here, and that it monopolises a card.
2. GPU 1/2/3 are **not** free; "use CUDA_VISIBLE_DEVICES=1 or 2" currently leaves 2.3 GB / 8.7 GB. Only GPU 3 (12.3 GB) had room for the probe below.

---

## 1. What LeRobot 0.4.4 actually needs to run `pi0` / `pi0_fast`

### 1.1 Code path (read from the installed wheel)

`/mnt/data/agilex/venv/lib/python3.10/site-packages/lerobot/policies/`

| file | what it is |
|---|---|
| `pi0/configuration_pi0.py` (168 L) | `PI0Config`, registered as `pi0`. `paligemma_variant="gemma_2b"`, `action_expert_variant="gemma_300m"`, `dtype="float32"` **by default**, `chunk_size=50`, `n_action_steps=50`, `num_inference_steps=10`, `max_state_dim=32`, `max_action_dim=32`, `image_resolution=(224,224)`, `empty_cameras=0`, `tokenizer_max_length=48` |
| `pi0/modeling_pi0.py` (1310 L) | `PaliGemmaWithExpertModel` + `PI0Pytorch` + `PI0Policy` |
| `pi0/processor_pi0.py` (166 L) | pre/post pipeline |
| `pi0_fast/configuration_pi0_fast.py` (162 L) | `PI0FastConfig`, registered as `pi0_fast`. Same backbone; adds `action_tokenizer_name="physical-intelligence/fast"`, `text_tokenizer_name="google/paligemma-3b-pt-224"`, `max_action_tokens=256`, `fast_skip_tokens=128`, `use_kv_cache=True`, autoregressive decode (`max_decoding_steps=256`) |
| `pi0_fast/modeling_pi0_fast.py` (1353 L) | FAST tokenizer in / tokens out |

`PI0Policy.from_pretrained` (modeling_pi0.py:958) does **only** this: pull `config.json` via `PreTrainedConfig.from_pretrained`, build the model, then `transformers.utils.cached_file(repo, "model.safetensors")` + `safetensors.load_file`, apply `_fix_pytorch_state_dict_keys` (handles `time_mlp_*` → `action_time_mlp_*`, adaRMS mismatches) and prepend `model.`.

**There is no Orbax/JAX loader and no conversion script in the installed package.** `find .../lerobot -name "*convert*"` returns only dataset converters (`v30/convert_dataset_v21_to_v30.py`) and `processor/converters.py`. LeRobot's openpi→LeRobot converter lives in the GitHub repo, not in the wheel.

### 1.2 Four concrete blockers (all verified, in order of severity)

**(a) `transformers` is not installed at all.** `lerobot.utils.import_utils._transformers_available` is `False`; `importlib.util.find_spec("transformers")` → `False` (`safetensors`, `accelerate`, `einops` are present; `tokenizers`, `sentencepiece`, `timm` absent). Instantiating the policy fails immediately with a misleading error:

```
File ".../pi0/modeling_pi0.py", line 351, in __init__
    vlm_config_hf = CONFIG_MAPPING["paligemma"]()
TypeError: 'NoneType' object is not subscriptable
```

The metadata says `lerobot[transformers-dep]` → `transformers>=4.57.1,<5.0.0`, so the documented install is `pip install "lerobot[transformers-dep]"` (no `pi0` extra exists in 0.4.4's METADATA; `Provides-Extra` has `transformers-dep`, `smolvla`, `groot`, `sarm`, `xvla`, `peft`, `libero`, … but no `pi0`).

**(b) Stock `transformers` is not enough — LeRobot needs its *patched fork*.** `PI0Pytorch.__init__` ends with:

```python
from transformers.models.siglip import check
if not check.check_whether_transformers_replace_is_installed_correctly():
    raise ValueError("An incorrect transformer version is used, ...")
```

`transformers==4.57.1` has no `transformers/models/siglip/check.py` → `ImportError` → the guard raises. I verified this directly. And when I stubbed the guard to proceed, the forward pass died in the *unpatched* attention path:

```
File ".../transformers/models/gemma/modeling_gemma.py", line 180, in eager_attention_forward
    attn_weights = attn_weights + causal_mask
RuntimeError: The size of tensor a (918) must match the size of tensor b (867) at non-singleton dimension 3
```

i.e. LeRobot's π0 port feeds a stacked prefix+suffix **2-D** mask (768 image tokens + 48 lang + 51 state/action = 867 prefix, +51 suffix) and relies on its fork's SigLIP/Gemma attention replacement to accept it. **You cannot run LeRobot π0/π0_fast on a stock PyPI `transformers`.** This is a hard packaging dependency, not a config tweak.

**(c) The PaliGemma tokenizer is gated.** Both policies build a `TokenizerProcessorStep(tokenizer_name="google/paligemma-3b-pt-224")` at *every* inference call (and pi0_fast additionally pulls `physical-intelligence/fast`). On hf-mirror with no token:

```
google/paligemma-3b-pt-224 tokenizer.json -> HTTP 403   (gated: "manual", 11.72 GB repo)
physical-intelligence/fast  tokenizer.json -> HTTP 200  (ungated, ~10 KB of files)
lerobot/pi0_base            config.json    -> HTTP 200
```

No `HF_TOKEN`/`HUGGING*` variable is set in the shell environment. The model weights themselves are **not** downloaded from `google/paligemma-*` — `PaliGemmaWithExpertModel.__init__` builds PaliGemma from a `CONFIG_MAPPING["paligemma"]()` config dict (lines 351–385), so only the **tokenizer** is affected. Workaround exists: ungated community republishes of the same tokenizer are on hf-mirror (e.g. `moaazelmarakby/paligemma-3b-pt-224`, `leo009/paligemma-3b-pt-224`), and `PI0FastConfig.text_tokenizer_name` is a config field. So (c) is a nuisance, not a wall.

**(d) What checkpoints genuinely exist in LeRobot format on hf-mirror.** `GET /api/models?author=lerobot&search=pi0` → **16 repos** (full list, with sizes from `?blobs=true`):

| repo | params | `model.safetensors` | notes |
|---|---|---|---|
| `lerobot/pi0_base` | 3.501 B | **14.01 GB** (fp32) | only `config.json` + `model.safetensors` + `policy_pre/postprocessor.json` — **no tokenizer bundled** |
| `lerobot/pi0fast-base` | 2.923 B | **11.69 GB** | |
| `lerobot/pi0_libero_finetuned_v044` | 3.501 B | **7.01 GB** (bf16) | action `[7]`, state `[8]`, 2 cameras + `empty_camera_0`, `dtype="bfloat16"`, `empty_cameras=1` |
| `lerobot/pi0fast-libero` | — | 7.73 GB | same shape: 7-dim action |
| plus `pi05_base`, `pi0_libero_base`, `pi05_*`, `pi0_old`, `pi052_*` | | | none is Piper |

Community LeRobot-format π0-FAST fine-tunes also exist for other robots (`iantc104/pi0_fast_*_ur5_*`, `rgarreta/pi0fast_pick_lego_*`, `PLB/pi0-fast-so100-*`, `fireman5379/pi0fast_stack_block`, …) — **all single-arm 7-dim**.

**Answer to "is there a Piper-compatible pretrained checkpoint in LeRobot format?": No.** The only Piper π0-FAST checkpoints are the two Orbax/JAX ones, and their file trees confirm it:

```
MODEL_AUTHOR/pi0-fast-piper-h-plate-pick-ft-v2 : 245 files, 42.58 GB
  checkpoints/ppv4ppv5_run1/500/params/ocdbt.process_0/d/<hash>      <- Orbax params
  checkpoints/ppv4ppv5_run1/500/train_state/ocdbt.process_0/d/<hash> <- Orbax optimizer state
MODEL_AUTHOR/piper-pi0fast-multistage : 69 files, 8.56 GB, same layout
```

The `params/ocdbt...` + `train_state/` layout is Orbax/JAX. (Also note 42.58 GB is mostly duplicated optimizer state — the inference-only payload is far smaller.)

---

## 2. VRAM and time for a π0-class forward pass

I measured what could be measured without the patched fork, on **GPU 3** (12.3 GB free), with random-init weights (PaliGemma is constructed from a config dict, so **no checkpoint download was needed** — this is why the probe was cheap):

```
params = 3.501 B          (matches lerobot/pi0_base's 3,501,372,176)
build on CPU       49.7 s
.to("cuda")         1.2 s
weights resident    7.01 GB   (bf16)   <- identical to lerobot/pi0_libero_finetuned_v044/model.safetensors = 7.012 GB
then: RuntimeError in unpatched eager attention (see 1.2b) -> peak-activation number NOT obtained
```

Probe script kept at `/mnt/data/agilex/pi0_vram_probe.py` (plus a dtype-consistent copy of the model at `.../probe/pi0_modeling_patched.py`, and `transformers==4.57.1` + `tokenizers==0.22.1` installed **only** into `/mnt/data/agilex/piextra` via `uv pip install --target`, never into the venv).

**Numbers for the answer (weights measured, activations bounded analytically and marked as estimates):**

| dtype | weights | activations, batch 1, 3×224² images, chunk 50, 10 denoise steps | total (est.) |
|---|---|---|---|
| bf16 | **7.0 GB** (measured) | ~0.3–1.0 GB (est.) — KV cache for 867 prefix tokens on an 18-layer / 1-KV-head / head_dim-256 Gemma is only ≈16 MB; the bulk is eager attention score matrices and MLP buffers | **≈ 8–9 GB** |
| fp32 (`dtype="float32"` is the **default** in `PI0Config`!) | **14.0 GB** (measured from `lerobot/pi0_base`) | ~0.6–2.0 GB (est.) | **≈ 15–17 GB** |

So: **32 GB per card is comfortably sufficient at batch 1 — confirmed for the weights, strongly bounded for activations.** The practical caveat is that on the current box the *free* memory is 2.3–12.3 GB per card, because three of four cards are shared with the ACT job and one with a 33-day-old openpi server. `dtype="bfloat16"` must be set explicitly (the default is fp32) and is the difference between fitting on a shared card and not.

Latency was not measured (blocked by 1.2b). For reference, the checkpoint family it comes from reports chunk-10 @ 20 Hz for the Piper fine-tune; LeRobot's default here is chunk-50 @ whatever your control rate is.

---

## 3. OpenVLA specifically

From the hf-mirror API (`?blobs=true`), which is the honest way to answer "how many GB" without downloading:

```
openvla/openvla-7b            gated=False   params=7,541,237,184 (BF16)
  model-00001-of-00003.safetensors   6.949 GB
  model-00002-of-00003.safetensors   6.971 GB
  model-00003-of-00003.safetensors   1.162 GB
  tokenizer.json                     0.002 GB
  TOTAL 15.09 GB
```

- **Smallest usable variant: `openvla/openvla-7b` itself.** The `openvla` org on hf-mirror has 7 repos (`openvla-7b`, `openvla-v01-7b`, `openvla-7b-prismatic`, and four `openvla-7b-finetuned-libero-*`). The LIBERO fine-tune `openvla/openvla-7b-finetuned-libero-10` is *also* `params=7,541,237,184`, **15.09 GB** — i.e. those are full fine-tunes, not small LoRA deltas. There is **no sub-7B OpenVLA**; the "OFT" variant I probed (`openvla/openvla-7b-oft-finetuned-libero-10`) returned an empty repo listing, so it is not a usable mirror target.
- **Is it on hf-mirror?** Yes — API metadata and blob sizes resolve, `gated=False`. (I did not download it; 15 GB is out of scope for a feasibility check.)
- **Is 7 B plausible for single-image inference on 32 GB? Yes, with room to spare.** BF16 weights = **15.09 GB**. One 224² image → 256 SigLIP/DINOv2 patch tokens + a ~30-token prompt → KV cache and activations are on the order of **1–2 GB** (a 7B Llama-2 with ~300 tokens of context has a trivially small KV cache: 32 layers × 2 × 32 heads × 128 head_dim × 300 tokens × 2 B ≈ 0.16 GB). **Peak ≈ 16–18 GB, i.e. ~14 GB of headroom on a 32 GB 5090.** The real cost is 15 GB of download and the fact that OpenVLA's action head is **7-dim single-arm end-effector deltas**, so it hits exactly the same dimensional problem as the Piper π0-FAST — with a worse architectural fit for a 14-dim joint-space dual-arm rig.

---

## 4. The dimensional mismatch problem — what would actually have to change

Two *independent* mismatches, and only the first is a wrapper problem.

**(i) Format (mechanical, solvable).** Orbax/JAX → LeRobot needs a converter that (a) walks the Orbax `params/` tree, (b) maps keys to LeRobot's `model.paligemma_with_expert.*` / `action_in_proj.*` naming (LeRobot already has `_fix_pytorch_state_dict_keys` for the `time_mlp_*`→`action_time_mlp_*` and adaRMS cases), (c) emits `model.safetensors` + a `config.json` + normalizer stats. Note the Piper checkpoint is a **LoRA** fine-tune (~50 M trainable params per its own README) — the merged weights are what you need, or you must add PEFT to the loading path.

**(ii) Semantics (not a wrapper problem).** The checkpoint is, in its own words: *"Action: 7-DoF — `[j1..j6 radians, gripper in [0,1]]` for the right arm only. Chunk size: 10 steps at 20 Hz."* Our robot is 14-dim (7 per arm) in **degrees**.

What a pure wrapper can and cannot do:

- **Unit conversion: free.** Normalization is affine (`NormalizationMode.MEAN_STD` for STATE and ACTION) and the stats travel with the checkpoint. Degrees↔radians is a per-dimension linear rescale, so you can fold `×180/π` into the mean/std. The model's *input* state must be converted the same way. No retraining needed for this alone.
- **Right arm, 7 dims: usable as-is** (after format conversion), in joint space, radians, gripper 0–1.
- **Left arm: no information path.** `max_action_dim=32` means the tensors are *padded*, so the shapes will load — but dims 7–31 of `action_out_proj` were never supervised (padded zeros with zero-variance stats), and `observation.state` for the left arm was never an input. Padding the left arm into the batch produces **zeros/garbage, not coordination**. Your options are: script the left arm (hold pose / mirror the right arm), or retrain. A wrapper cannot invent left-arm behaviour, and it cannot fix bimanual coordination (which objects to hand to which arm).
- **π0-FAST adds a tokenizer twist.** The FAST action tokenizer *is* dimension-agnostic — its own README example tokenizes `np.random.rand(256, 50, 14)`, and `physical-intelligence/fast` is ungated (HTTP 200, ~10 KB). So the tokenizer will happily encode 14 dims. But the checkpoint's VLM learned to *emit* the token sequence for 7-dim chunks; 14 dims roughly doubles the token count (~50 steps × 14 dims vs × 7) against `max_action_tokens=256`, and the emitted sequences for the extra dims were never trained. **For π0-FAST, changing the action dimension means retraining** (LoRA-scale retraining would suffice, which is exactly what the checkpoint authors did: ~50 M trainable params).
- **Empirical proof that "just adapt it" is not enough, from the checkpoint's own README:** ft-v1 was retrained into ft-v2 because *"camera mount / visual alignment"* and *"robot arm home position"* changed between data collection and deployment — a SigLIP embedding shift and a state-distribution mismatch. Fixing that cost **121 episodes / 111 train episodes / 48,043 frames at 20 Hz** for a *single-arm plate pick on the same robot*. Your setup differs by: second arm, degrees vs radians, different cameras, different task. That is a from-scratch data collection + LoRA fine-tune, not an adapter.

---

## 5. Recommendation

**Stay with (a) — keep fine-tuning ACT on your own data, and do not invest in π0/π0-FAST or OpenVLA right now.** The blockers are not VRAM (7 GB bf16 weights measured for π0; ~16–18 GB for OpenVLA, both fine on 32 GB) — they are: no LeRobot-format Piper checkpoint exists at all (the only two Piper checkpoints are 42.6 GB / 8.6 GB Orbax dumps requiring a converter that isn't shipped in the wheel), LeRobot 0.4.4's π0 port refuses to run without its *patched transformers fork* (stock `transformers` fails both the version guard and the 2-D attention mask — I reproduced both failures), the PaliGemma tokenizer is gated (403 without a token), and the checkpoint's 7-dim right-arm-radians space has no information path to your 14-dim dual-arm-degrees action space, so the left arm and all bimanual coordination would still have to be learned from new data. If you want a π0-class result soon, the cheap experiment is to let the *other* agent's openpi install serve `MODEL_AUTHOR/pi0-fast-piper-h-plate-pick-ft-v2` natively on GPU 1 (openpi reads Orbax directly — an openpi π0 server is literally already running on that box) and drive **only the right arm** from it while scripting the left; that gets you a real π0 policy on your hardware for the cost of an install that is already happening. If what you actually want is a *14-dim pretrained* starting point in LeRobot format, the only dimension match I found on hf-mirror is `LBST/t16_pick_and_place_agilex_smolvla_18k` — SmolVLA, **450 M params, 0.91 GB**, `observation.state [14]` → `action [14]`, 3 cameras at 256², trained on an AgileX 14-dim dataset — which is 8× smaller than π0-FAST and fits your action space exactly (verify the camera names and that it is the same Piper platform before trusting it). Chasing OpenVLA is strictly worse than π0 here: 15 GB download, 7-dim single-arm end-effector deltas, same gated-tokenizer class of problem, and no LoRA-scale fine-tune path documented for a 14-dim rig.

---

## Appendix — what I could NOT verify (explicitly)

| item | status |
|---|---|
| Peak activation VRAM for a *complete* π0 forward pass | **Not measured.** Blocked by the patched-transformers requirement. Weights measured (7.01 GB bf16); activations bounded analytically (~0.3–1.0 GB bf16) and marked as an estimate. |
| π0 inference latency on a 5090 | **Not measured** (same blocker). |
| Whether `lerobot[transformers-dep]`'s pinned `transformers>=4.57.1` is silently the fork on PyPI | **Not verified.** What I proved is that the *stock* 4.57.1 wheel lacks `transformers.models.siglip.check` and fails the guard, and that upstream eager attention cannot consume LeRobot's 2-D mask. The fork's provenance (git URL / private index) is unresolved and is the first thing to pin down before any π0 work. |
| OpenVLA actually loading/serving on this box | **Not attempted** (15 GB download; out of scope by the rules). All numbers are from the hf-mirror API + standard 7B KV-cache arithmetic. |
| `LBST/t16_pick_and_place_agilex_smolvla_*` being the same 2×Piper platform | **Not verified** — config/metadata only (`action [14]`, state `[14]`, 3 cams 256², dataset `gauravpradeep/bottle_task_aug_29_lerobot_diffusion`). |
| openpi being able to serve the Piper Orbax checkpoint | **Not verified by me** — that is the other agent's task. Corroborating evidence: an openpi `serve_policy.py` π0 process has been resident on GPU 1 for 33 days. |

**Artifacts left on the NAS (nothing in the shared venv):**
`/mnt/data/agilex/pi0_vram_probe.py`,
`/mnt/data/agilex/probe/pi0_modeling_patched.py`,
`/mnt/data/agilex/piextra/` (transformers 4.57.1 + tokenizers 0.22.1, `--target` install, plus a one-line SigLIP `check.py` stub used only for the probe).
