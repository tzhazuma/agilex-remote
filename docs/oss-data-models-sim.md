# OSS data, models and simulators for AgileX Tracer + 2×PiPER

Target: Tracer differential-drive UGV, 2× PiPER 6-DOF + parallel gripper, 2 wrist cameras, Ubuntu 22.04 / ROS 2 Humble.
Researched 2026-10-29. Every claim has a URL. Confidence: **VERIFIED** = I fetched the page/API/file and saw it; **LIKELY** = search-result or README-summary only; **NOT FOUND** = searched, came up empty (not "probably doesn't exist").

Method notes:
- `huggingface.co` is unreachable from this host (HTTP 000); every HF check below was done against **hf-mirror.com**, the same API/路径. Canonical URL = identical path on `huggingface.co`.
- GitHub REST was rate-limited partway through; repo trees were read with `git clone --filter=blob:none --no-checkout` + `git ls-tree`, which is authoritative for file existence.
- "Piper" collides with the Piper TTS engine on HF. Naive `search=piper` returns mostly speech models; robotics hits came from `agilex`, `piper+arm`, `dual+piper`, `piper+bimanual`, `aloha+agilex`.

---

## 1. Open-source datasets containing PiPER data

### 1a. LeRobot-format datasets that actually contain PiPER (or dual PiPER) data

| Dataset | URL | Format | Episodes / frames | Arms | Action dim | Licence | Conf |
|---|---|---|---|---|---|---|---|
| `HITdongdong/piper_dual_tube` | https://hf-mirror.com/datasets/HITdongdong/piper_dual_tube | LeRobot v2.1 | 200 / 131,269 @30 fps | **dual** (`robot_type=piper_dual`) | **14** (state 14) | none stated | VERIFIED |
| `gauravpradeep/t01_piper_pick_and_place_bimanual_lerobot` | https://hf-mirror.com/datasets/gauravpradeep/t01_piper_pick_and_place_bimanual_lerobot | LeRobot v2.1 | 128 / 51,983 @50 fps | **dual** | **14** | Apache-2.0 | VERIFIED |
| `HCHoongChing/bimanual-piper-dataset` | https://hf-mirror.com/datasets/HCHoongChing/bimanual-piper-dataset | LeRobot v2.1 | 1 / 154 @30 fps | **dual** (`bi_piper`) | **14** | Apache-2.0 | VERIFIED |
| `ISdept/piper_arm` | https://hf-mirror.com/datasets/ISdept/piper_arm | LeRobot v3.0 | 229 / 24,814 @10 fps | single | 7 | none stated | VERIFIED |
| `KeWangRobotics/piper_test` | https://hf-mirror.com/datasets/KeWangRobotics/piper_test | LeRobot v2.0 | 54 / 20,646 @30 fps | single (`piper`) | 7 | none stated | VERIFIED |
| `axiboai/piper_umi_place_mug_v2_right_arm` | https://hf-mirror.com/datasets/axiboai/piper_umi_place_mug_v2_right_arm | LeRobot v2.1 | 25 / 23,771 @30 fps | single | **10** (EE pose 6D + grip) | none stated | VERIFIED |
| `moonyh1230/piper_wooden_block_dual_realsense_0922` | https://hf-mirror.com/datasets/moonyh1230/piper_wooden_block_dual_realsense_0922 | LeRobot v2.1 | 100 / 92,797 @30 fps | single (`piper_follower`) | 7 | none stated | VERIFIED |
| `mazhipeng/piper_dataset` | https://hf-mirror.com/datasets/mazhipeng/piper_dataset | LeRobot v2.1 | 100 / 13,618 @10 fps | single (`piper`) | not exposed in `meta/info.json` | none stated | VERIFIED (meta) |
| `tianxiwei/piper_blackbox` | https://hf-mirror.com/datasets/tianxiwei/piper_blackbox | LeRobot v2.1 | 52 / 11,996 @20 fps | single (`piper_standard`) | not exposed in `meta/info.json` | none stated | VERIFIED (meta) |

Key observation for us: **`HITdongdong/piper_dual_tube` is the closest public match to this robot** — dual PiPER, 14-dim joint action, and exactly the camera trio `cam_high` + `cam_left_wrist` + `cam_right_wrist` (https://hf-mirror.com/datasets/HITdongdong/piper_dual_tube/resolve/main/meta/info.json, VERIFIED). `gauravpradeep/t01_*` is the second usable one (14-dim, `left`/`middle`/`right` cams, Apache-2.0, so redistributable).

### 1b. HDF5 / RoboTwin-export datasets (dual arm)

| Dataset | URL | Format | Size / episodes | Arms | Action dim | Licence | Conf |
|---|---|---|---|---|---|---|---|
| `Godrlic/piper-dual-arm` | https://hf-mirror.com/datasets/Godrlic/piper-dual-arm | HDF5 (`plug_insertion.hdf5`, `screw_insertion.hdf5`) | `plug_insertion.hdf5` = **11,374,877,672 B ≈ 11.4 GB** | dual (name + task) | not verified (did not download 11 GB) | none stated | VERIFIED (files) |
| `hongxiaoy/SoftFold_20260108_Full_DualPiperArm_WhiteCloth(_HDF5)` | https://hf-mirror.com/datasets/hongxiaoy/SoftFold_20260108_Full_DualPiperArm_WhiteCloth_HDF5 | HDF5, one file per episode (`episode_N.hdf5`) | episodes 0…N | dual PiPER (name) | not verified | none stated | VERIFIED (files) |
| `DorayakiLin/aloha-agilex_clean_50` | https://hf-mirror.com/datasets/DorayakiLin/aloha-agilex_clean_50 | HDF5 per episode + `instructions.json` (RoboTwin export) | 50-episode sim set | dual (aloha-agilex) | 14 (RoboTwin convention) | none stated | VERIFIED (files), dim LIKELY |
| `yuffish/aloha-agilex_clean-50_flow` | https://hf-mirror.com/datasets/yuffish/aloha-agilex_clean-50_flow | LeRobot + openpi `norm_stats.json` | 50 eps | dual | **14 real dims inside a 32-dim padded vector** (norm_stats has 32 entries, only first 14 non-zero) | none stated | VERIFIED |

### 1c. AgileX official / large collections

| Item | URL | What it is | Format | Licence | Conf |
|---|---|---|---|---|---|
| `agilexsupport/ARIO` (AgileX official HF account) | https://hf-mirror.com/datasets/agilexsupport/ARIO | "All Robots In One" (Pengcheng Lab + AgileX), multi-modal, `data/taskN/episodeM.tar.gz` | tar.gz per episode | **CC-BY-4.0** | VERIFIED (listing + card); arm type per task **not documented in card** |
| ARIO project repo | https://github.com/agilexrobotics/ARIO | Dataset description/collection code | — | — | VERIFIED (README) |
| `RoboCOIN/Agilex_Cobot_Magic_*` (several dozen tasks) | https://hf-mirror.com/datasets/RoboCOIN/Agilex_Cobot_Magic_wipe_table | AgileX Cobot Magic bimanual demos | HF dataset | gated | **VERIFIED gated** — "Access … is restricted and you are not in the authorized list" |
| `villekuosmanen/agilex_*` (≈40 task sets) | https://hf-mirror.com/datasets/villekuosmanen/agilex_pour_water_cup_half | LeRobot v2.1, 19 eps / 10,048 fr, action 14 | LeRobot | none stated | VERIFIED — but `robot_type = **arx5_bimanual**`, i.e. **ARX5 arms, not PiPER**. Do not count these as Piper data. |

### 1d. Empty results (searched, nothing found)

| Search | Result | Conf |
|---|---|---|
| **Open X-Embodiment** — grep of the OXE dataset registry used by OpenVLA (`configs.py`, **70** top-level dataset keys) for `piper|agilex|tracer` | **0 hits** | NOT FOUND (verified against https://github.com/openvla/openvla/blob/main/prismatic/vla/datasets/rlds/oxe/configs.py) |
| **RLDS-format PiPER data** (any host) | none found | NOT FOUND |
| **AgileX official GitHub dataset repo** (beyond `data_tools` / `ARIO`) | none | NOT FOUND (https://github.com/agilexrobotics?tab=repositories listing reviewed) |
| `piper` + `rlds` / `piper` + `tfds` on HF | none | NOT FOUND |

---

## 2. Open-source models / checkpoints

### 2a. Base checkpoints and their action width

| Model | Checkpoint URL | Action dim | 14 dims (2 arms) representable? | Licence | Conf |
|---|---|---|---|---|---|
| **π0 / π0.5** (openpi) | https://github.com/agilexrobotics/openpi-agilex/blob/master/src/openpi/models/pi0_config.py (`action_dim: int = 32`, `action_horizon: int = 50`) | **32 (padded)** | **Yes, structurally** — 14 fills 14 of 32 slots | Apache-2.0 (upstream openpi) | VERIFIED |
| **π0-FAST** | https://github.com/agilexrobotics/openpi-agilex/blob/master/src/openpi/models/pi0_fast.py (`action_dim: int = 32`) | **32 (padded)** | Yes, structurally | Apache-2.0 | VERIFIED |
| **SmolVLA** (LeRobot) | https://github.com/huggingface/lerobot/blob/main/src/lerobot/policies/smolvla/configuration_smolvla.py (`max_state_dim=32`, `max_action_dim=32`); weights https://hf-mirror.com/lerobot/smolvla_base | **32 (padded)** | Yes, structurally | Apache-2.0 | VERIFIED |
| **OpenVLA** | https://github.com/openvla/openvla (README: "Predict Action (**7-DoF**; un-normalize for BridgeData V2)") | **7** | **No** | MIT (repo) | VERIFIED |
| **ACT** (LeRobot) | https://github.com/huggingface/lerobot/blob/main/src/lerobot/policies/act/modeling_act.py (action head built from dataset `output_features`; raises if no ACTION feature) | data-driven (7 or 14 or N) | **Yes, if trained from scratch at 14** | Apache-2.0 | VERIFIED |
| **Diffusion Policy** (LeRobot) | https://github.com/huggingface/lerobot/blob/main/src/lerobot/policies/diffusion/configuration_diffusion.py (`horizon=64`, `n_action_steps=32`) | data-driven | Yes, if trained at 14 | Apache-2.0 | VERIFIED |
| **RT-1-X / OXE** | https://github.com/google-deepmind/open_x_embodiment (README: "seven variables for the gripper movement") | **7** | No | Apache-2.0 | VERIFIED |

**The 7-dim → 14-dim statement, stated precisely:** a checkpoint whose action head has width 7 has *no parameter and no information path* to a second arm. There is no feature, slot, or token the second arm's 7 joints could be read from — the head is a fixed-width regression/decoder over 7 outputs. Widening it requires either (a) retraining from scratch with a 14-wide head (ACT/DP, cheap), or (b) using a *padded* head (π0/π0.5/π0-FAST/SmolVLA: 32 slots) where the extra 18 dims exist but were trained masked/zero — so they carry no learned semantics and must be fine-tuned on real 14-dim data. `yuffish/aloha-agilex_clean-50_flow` is a live example of (b): 32-entry `norm_stats` with only the first 14 non-zero.

### 2b. PiPER/AgileX-specific checkpoints found

| Checkpoint | URL | Base policy | Action dim | Notes | Conf |
|---|---|---|---|---|---|
| `LBST/t16_pick_and_place_agilex_smolvla_18k` (+ 4k/10_4k/18_4k) | https://hf-mirror.com/LBST/t16_pick_and_place_agilex_smolvla_18k | SmolVLA | **14** (train_config `shape: [14]`), `max_action_dim=32`, 3 cams (`left/middle/right`) | real AgileX bimanual, closest published 14-dim SmolVLA | VERIFIED |
| `yuffish/robotwin-pi0_aloha-agilex_clean-50` | https://hf-mirror.com/yuffish/robotwin-pi0_aloha-agilex_clean-50 | π0 (RoboTwin sim, 50 demos) | 14-in-32 | sim-only; good sanity baseline for "50 demos" | VERIFIED |
| `Xianzhe-Fan/pi05_cup_agilex_39999`, `pi05_plug_agilex_49999`, `pi05_spray_cap_agilex_49999` | https://hf-mirror.com/Xianzhe-Fan/pi05_cup_agilex_39999 | π0.5 fine-tunes on AgileX data | 14-in-32 (Trossen-style `norm_stats`) | MIT tag on repo | VERIFIED (files), dim LIKELY |
| `axiboai/pi05_piperx_umi_place_mug_v2_right_arm_augsuite` | https://hf-mirror.com/axiboai/pi05_piperx_umi_place_mug_v2_right_arm_augsuite | π0.5, PiPER-X single arm | 10 (EE pose) | single-arm only | VERIFIED (exists), dim LIKELY |
| `Avada11/RoboTwin-Model-AgileX-ACT`, `…-DP`, `…-Diverse-DP` | https://hf-mirror.com/Avada11/RoboTwin-Model-AgileX-ACT | ACT / DP on RoboTwin aloha-agilex | 14 | sim | LIKELY (search result only) |
| `kywanghyun/discoverse-piper-arm` | https://hf-mirror.com/kywanghyun/discoverse-piper-arm | — | — | **not a policy**: contains only `assets/mjcf/manipulator/agilex_piper/piper*.xml` (MuJoCo) | VERIFIED |

Gaps, stated explicitly: **no official AgileX-published π0/π0.5 checkpoint for PiPER.** `agilexrobotics/openpi-agilex` ships the openpi fork plus `scripts/piper_FK-ros2.py` and `scripts/piper_IK-ros2.py`, and its `training/config.py` contains **only ALOHA-style `LeRobotAlohaDataConfig` / `pi0_aloha_1` / `pi05_aloha_1` configs — no PiPER `TrainConfig`** (https://github.com/agilexrobotics/openpi-agilex/blob/master/src/openpi/training/config.py, VERIFIED).

---

## 3. Simulation environments

| Simulator | PiPER? | Tracer? | Where | Install difficulty (macOS/Linux, CPU, no GPU) | Conf |
|---|---|---|---|---|---|
| **MuJoCo / mujoco_menagerie** | **YES** — `agilex_piper/` with `piper.xml` + `scene.xml` | no | https://github.com/google-deepmind/mujoco_menagerie/tree/main/agilex_piper | **Easiest.** `pip install mujoco` (≥2.3.4 per its README); MJCF is pure XML, runs on macOS CPU | VERIFIED |
| MuJoCo (3rd-party PiPER MJCF + controllers) | YES | no | https://github.com/soulde/Piper_mujoco (MJCF + gravity comp + joint-impedance/Cartesian-IK), https://github.com/SkylineZ123/Piper_Mujoco_Sim | easy (pip requirements, MuJoCo) | VERIFIED (README) |
| **Isaac Sim** | **YES (USD)** — `USD/piper_v1.usd`, `piper_v2.usd`, `piper_h/l/x_v1.usd` | no | https://github.com/agilexrobotics/piper_isaac_sim | **Hard**: Isaac Sim + NVIDIA GPU + Ubuntu; the repo's own test config is "ubuntu24.04 + RTX 5090" | VERIFIED (tree + README) |
| **Isaac Lab** | **no built-in** — none of the 32 `isaaclab_assets/.../robots/*.py` modules is AgileX/PiPER | no | https://github.com/isaac-sim/IsaacLab | — | **NOT FOUND** (verified absence) |
| Isaac Lab (community fork) | **YES** — `data/Robots/Agilex/PIPER/*.usd` + `tasks/manipulation/cabinet/config/piper/{joint_pos,ik_abs}_env_cfg.py` | no | https://github.com/agilexrobotics/robot_lab ("piper on Isaac Lab", needs IsaacLab ≥2.0.0 / IsaacSim 4.5.0, Apache-2.0) | **Hard** (GPU, Linux) | VERIFIED |
| **Gazebo classic** (PiPER) | YES — `piper_description_gazebo.xacro` in `piper_ros` humble branch | — | https://github.com/agilexrobotics/piper_ros/tree/humble/src/piper_description/urdf | medium: ROS 2 Humble + Gazebo classic; Linux-only | VERIFIED |
| **Gazebo classic** (Tracer) | — | YES — `tracer/tracer_description` + `tracer_gazebo_sim` | https://github.com/agilexrobotics/ugv_gazebo_sim/tree/master/tracer | medium; **ROS Melodic / Ubuntu 18.04** per its README → needs porting to Humble | VERIFIED |
| **gz / Ignition** (Tracer) | no | YES (Tracer 2) — ROS 2 Humble + Ignition Fortress | https://github.com/Narcis-Abella/hospital-agv-sim | medium; Linux + Fortress | VERIFIED (README), runtime LIKELY |
| **Gazebo Harmonic** (Tracer + 2×PiPER + lift) | YES | YES — diff-drive chassis + column/lift + two PiPER + D435i + VLP-16, self-contained URDF/xacro | https://github.com/Sut1313/ag_robot_description | medium; closest single asset to our exact robot | VERIFIED (README) |
| **PyBullet** | **NO** — GitHub repo search `piper pybullet` returned `total_count: 0` | no | — | — | **NOT FOUND** |
| **SAPIEN / ManiSkill3** | **YES** — `mani_skill/agents/robots/piper/piper.py`, `assets/robots/piper/meshes/*`, PPO baselines (`train_piper_pickcube_state.py`, `…pushcube…`, `ppo_pick_anything.py`) | no | https://github.com/fwliu1243/ManiSkill-Piper | `pip install mani_skill` (3.0.1) + `sapien` (3.0.3) — **sapien wheels are `Operating System :: POSIX :: Linux` only** → not macOS | VERIFIED |
| **Genesis** | via URDF/MJCF import (menagerie MJCF works) | **YES** — Tracer URDF demo | https://github.com/TimeEscaper/genesis-tracer-demo (URDF derived from `tracer_ros`); `pip install genesis-world` (1.4.5) | easy pip, but GPU strongly recommended | VERIFIED (README + PyPI) |

### URDF / xacro sources (the question "does `piper_sdk` / `piper_ros` ship one?")

| Source | Ships URDF/xacro? | Detail | Conf |
|---|---|---|---|
| **`agilexrobotics/piper_ros`, branch `humble`** | **YES** | `src/piper_description/urdf/`: `piper_description.urdf`, `piper_description.xacro`, `piper_description_gazebo.xacro`, `piper_description_old.urdf`, `piper_no_gripper_*`; plus `src/piper_moveit/*/config/piper.urdf.xacro` + `piper.ros2_control.xacro`. Repo README states Ubuntu 22.04 / ROS 2 Humble, `pip3 install piper_sdk`, **MIT**. Branches: `foxy`, `humble`, `humble_beta1`, `noetic`. | VERIFIED |
| **`agilexrobotics/agx_arm_urdf`** | **YES** | `piper/`, `piper_h/`, `piper_l/`, `piper_x/`, `nero/`, `agx_gripper/`, `revo2/` — each with `*_description.urdf` + `*_with_gripper_description.xacro` + left/right Revo2 hand xacro | VERIFIED |
| **`agilexrobotics/piper_isaac_sim`** | **YES** | bundles a full `piper_description/` ROS 2 package (meshes STL+DAE, launch files) next to the USDs | VERIFIED |
| **`agilexrobotics/piper_sdk`** | **NO** | 0 files matching `*.urdf|*.xacro|*.stl|*.dae`; it is a Python/CAN driver (`piper_sdk/`, `pyproject.toml`, MIT) | VERIFIED |
| `smalleha/piper_urdf`, `wudijuaner/piper_description`, `resolveccc/dual_piper_description`, `LinBornRain/dual_piper_ros` (dual PiPER + IsaacSim USD) | YES | community mirrors / dual-arm variants | LIKELY (search results) |

Practical takeaway: **for a pip-only, no-GPU box, MuJoCo + `mujoco_menagerie/agilex_piper` is the only frictionless option.** Everything else needs ROS 2/Gazebo (Linux), Isaac (GPU+Linux), SAPIEN (Linux), or Genesis (GPU recommended).

---

## 4. Realistic task ideas for Tracer + 2×PiPER + 2 wrist cams

Assumed interface for all rows (this is what the two wrist cams + 14 joints buy you):
- **Observation**: `images`: 2× wrist RGB (e.g. 256×256 or 480×640) + optional head RGB; `state`: 14 = [L: q1..q6, grip, R: q1..q6, grip]; optionally +2 base (v, ω) +1 lift = 17.
- **Action**: 14 absolute joint-position targets (or 14 deltas), at 10–30 Hz; base commands kept out of the policy and handled by Nav2.
- π0/π0.5/SmolVLA pad this to 32 dims natively; ACT/DP take 14 directly.

| Task | Obs / action space | Bimanual? | Trainable with ~50 demos? | Why / caveat | Conf |
|---|---|---|---|---|---|
| **Pick-and-place** (one arm picks, other holds receptacle) | 2 wrist RGB + state 14 → action 14 | optional | **Yes — best case.** ACT or DP, 50 demos ≈ 40–80 % success on fixed object set | Public proof: RoboTwin π0 trained on exactly 50 aloha-agilex demos (https://hf-mirror.com/yuffish/robotwin-pi0_aloha-agilex_clean-50); dual-PiPER 14-dim data exists (https://hf-mirror.com/datasets/HITdongdong/piper_dual_tube) | VERIFIED (existence), success rate LIKELY |
| **Drawer opening** (single arm, prismatic joint) | 1–2 wrist RGB + state 14 → action 14 | no | **Yes**, 50 demos, if drawer pose is fixed and the handle is visible in the wrist cam | Isaac Lab Piper has a ready `cabinet` task to pretrain in sim: https://github.com/agilexrobotics/robot_lab/tree/master/source/robot_lab/robot_lab/tasks/manipulation/cabinet/config/piper | VERIFIED (task exists) |
| **Button pressing** | wrist RGB + state 14 → action 14 | no | **Yes**, 50 demos (simple, short horizon, contact-rich but low-dim) | Needs force/contact cue or the wrist cam must see the button; ACT suffices | LIKELY |
| **Bin picking** (cluttered bin, grasp selection) | 2 wrist RGB (+ head) + state 14 → action 14 | one arm at a time | **Marginal at 50** — expect ~20–40 %; 150–300 demos or sim pretrain + 50 real fine-tune | Clutter ⇒ multi-modal grasp distribution; ACT averages modes, DP handles it better | LIKELY |
| **Handover between the two arms** | 2 wrist RGB + head RGB + state 14 → action 14 | **yes, mandatory** | **No at 50** — needs 150–300+ demos; each arm must see the *other* arm, so a head/3rd camera is effectively required | Coordination + occlusion; the partner arm is outside both wrist FOVs for much of the trajectory | LIKELY |
| **Cable routing / deformable** | 2 wrist RGB (+ head) + state 14 → action 14 | yes | **No at 50** — 200+ demos, or sim + RL; needs visual servoing at higher rate | Deformable state is unobservable from 2 wrist cams alone | LIKELY |
| **Mobile manipulation** (drive to shelf, then pick) | +2 base dims → action 16 | — | **No** — decouple: Nav2 for the base, 50 demos for the arm skill only | No public checkpoint has base+2-arm; π0's 32 slots can hold 17 dims but nothing is pretrained on it | LIKELY |

Recommendation for a first milestone: **pick-and-place with the right arm + drawer opening**, ACT at 14 dims trained from scratch (or SmolVLA/π0.5 fine-tuned at 32-padded), 50 demos, evaluated in `mujoco_menagerie/agilex_piper` before touching hardware.

---

## 5. One-paragraph bottom line

PiPER data and models exist but are **thin and unlicensed-by-default**: the usable public sets are `HITdongdong/piper_dual_tube` (dual, 14-dim, 200 eps, wrist cams — best match), `gauravpradeep/t01_*` (dual, 14-dim, Apache-2.0), and the HDF5 sets `Godrlic/piper-dual-arm` / `hongxiaoy/SoftFold_*`; Open X-Embodiment and RLDS contain **no** PiPER data, and I found no AgileX-official π0/PiPER policy checkpoint. Simulation is the strong suit: **MuJoCo `agilex_piper` (MIT) is the one no-GPU, pip-installable PiPER model**, with Isaac (USD via `piper_isaac_sim` / `robot_lab`), Gazebo (`piper_description_gazebo.xacro`, `ugv_gazebo_sim/tracer`, Gazebo-Harmonic dual-PiPER `ag_robot_description`), ManiSkill/SAPIEN (`ManiSkill-Piper`) and Genesis (`genesis-tracer-demo`) as heavier options. PyBullet has nothing. A 7-dim single-arm checkpoint cannot be widened to 14 without retraining, but π0/π0.5/π0-FAST/SmolVLA already carry a 32-dim padded action head that *structurally* holds 14 dims and only needs fine-tuning.
