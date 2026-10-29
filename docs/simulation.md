# Piper 6-DOF arm — working headless MuJoCo simulation

**Status: working.** A real-geometry AgileX Piper (6 revolute joints + parallel
2-finger gripper) loads in MuJoCo, a scripted pick-and-place runs through the
physics engine, and 7 PNG stills + a 11.7 s MP4 were rendered offscreen on this
Mac. Nothing touched the robot, no sudo, no system Python.

| Question | Answer |
|---|---|
| Real Piper geometry or stand-in? | **Real.** MuJoCo Menagerie `agilex_piper` (MIT), derived from the official AgileX `piper_description` URDF. No stand-in was needed. |
| Did rendering work? | **Yes** — `mujoco.Renderer` offscreen with `MUJOCO_GL=cgl` (the macOS default). No EGL, no OSMesa, no X server. |
| Frames | `assets/sim/frames/*.png` (7 stills, 800×600) |
| Video | `assets/sim/piper_pick_and_place.mp4` (H.264, 800×600, 25 fps, 292 frames, 11.68 s, 1.1 MB) |
| Script | `tools/sim_piper.py` |
| Measured result | cube lifted **+0.102 m** (rest 0.021 m → 0.123 m) and set down **4.0 mm** from the pad centre |

---

## 1. What model was used — and is it the real robot?

**Real geometry, not a stand-in.** Two independent sources both carry Piper
description files; the simulation uses the Menagerie one because it is a
cleaned-up, MIT-licensed, ready-to-render MJCF:

```
assets/sim/models/piper/piper.xml    <- google-deepmind/mujoco_menagerie/agilex_piper
assets/sim/models/piper/assets/*     <- 84 OBJ/STL meshes from the official URDF
```

Provenance chain (verified in the Menagerie README):

> "This package contains a simplified robot description (MJCF) of the AgileX
> PiPER … derived from the publicly available model
> `agilexrobotics/Piper_ros/tree/ros-noetic-no-aloha/src/piper_description/urdf`."

and the official `piper_ros` repo, which I also cloned, ships the same URDF
(`src/piper_description/urdf/piper_description.urdf`) plus a hand-written MJCF
(`src/piper_description/mujoco_model/piper_description.xml`). Both were
inspected; the Menagerie MJCF was chosen (see §3).

### Degrees of freedom (as compiled by MuJoCo)

| idx | joint | type | range (rad) | range (deg) | note |
|---|---|---|---|---|---|
| 0 | `joint1` | revolute | −2.618 … 2.618 | −150 … 150 | base yaw |
| 1 | `joint2` | revolute | 0 … 3.14 | 0 … 180 | shoulder pitch |
| 2 | `joint3` | revolute | −2.697 … 0 | −154.5 … 0 | elbow |
| 3 | `joint4` | revolute | −1.832 … 1.832 | −105 … 105 | forearm roll |
| 4 | `joint5` | revolute | −1.22 … 1.22 | −70 … 70 | wrist pitch |
| 5 | `joint6` | revolute | −3.14 … 3.14 | −180 … 180 | tool roll |
| 6 | `joint7` | **prismatic** | 0 … 0.035 m | — | left finger |
| 7 | `joint8` | **prismatic** | −0.035 … 0 m | — | right finger (mimics 7 via an equality constraint) |

`nq=15, nv=14, nu=7` with the cube's free joint added by the scene.
Actuators are position servos (`kp` 80/80/80/40/10/10 for j1–j6, `kp=40` for the
gripper), timestep 2 ms, `integrator=implicitfast`.

> **Correction to the task brief.** The brief says "joint limits are roughly
> ±150 deg". That is only true for joint1. The real per-joint limits are the
> table above — notably the **wrist pitch is only ±70°** and the elbow is
> one-sided. This matters: see §6.

---

## 2. Model acquisition log — every attempt, in order

### 2.1 `pip download piper_sdk` — no URDF inside

```bash
python3.12 -m pip download piper_sdk --no-deps -d .
# piper_sdk-0.6.2-py3-none-any.whl  (194 kB)
unzip -l piper_sdk-0.6.2-py3-none-any.whl | grep -iE "urdf|mesh|stl|dae|xml|mjcf"
# -> NONE
```

The wheel contains only the CAN protocol layer (piper_ctrl_*, piper_read_*,
can_activate.sh). **Result: no model. Moved on.**

### 2.2 GitHub search API — works, but rate-limited

```bash
curl -s "https://api.github.com/search/repositories?q=piper+urdf&per_page=10"        # 10 hits
curl -s "https://api.github.com/search/repositories?q=piper_description"             # 17 hits
curl -s "https://api.github.com/search/repositories?q=agilex+piper"
```

Search API worked (separate 10 req/min bucket). The **core** API did not:

```bash
curl -s -i https://api.github.com/repos/wudijuaner/piper_description
# HTTP/2 403  x-ratelimit-remaining: 0   (60 req/h unauthenticated)
```

Workaround: skip the API and `git clone` / fetch `raw.githubusercontent.com`
directly, which are not subject to that bucket.

### 2.3 `mujoco_menagerie` — **this is the hit**

```bash
curl -s https://raw.githubusercontent.com/google-deepmind/mujoco_menagerie/main/README.md \
  | grep -i piper
# | AgileX PiPER | 8 | [MIT](agilex_piper/LICENSE) |
```

A full `git clone --depth 1` of Menagerie **timed out at 180 s** (large repo,
only `.git` was left, `git ls-tree HEAD` -> `fatal: Not a valid object name
HEAD`). A blobless sparse clone of just the model directory succeeded:

```bash
git clone --depth 1 --filter=blob:none --sparse \
    https://github.com/google-deepmind/mujoco_menagerie.git
cd mujoco_menagerie && git sparse-checkout set agilex_piper   # 33 MB
```

### 2.4 Official AgileX repo — also a hit (provenance + alternative MJCF)

```bash
git clone --depth 1 https://github.com/agilexrobotics/piper_ros.git
# src/piper_description/urdf/piper_description.urdf        (13 kB, real URDF)
# src/piper_description/mujoco_model/piper_description.xml (hand-written MJCF)
# src/piper_description/meshes/*.STL + *.dae               (60 MB)
```

The bundled official MJCF was inspected: 6 revolute + 2 slide joints with the
real inertials, but it has **no floor, no lights, no camera, no scene**, very
stiff damping (500 on joint1) and no gripper mimic constraint. The Menagerie
MJCF is a cleaned-up derivative with collision geoms, position actuators, a
finger-mimic equality and a ready scene, so it is the one used here. (The
official `src/piper_sim/piper_mujoco` ROS node is **not** usable today: it
imports the abandoned `mujoco_py`, which does not build on current macOS/Python.)

### 2.5 AgileX docs site — not needed

```bash
curl -s -o /dev/null -w "%{http_code}" https://global.agilex.ai/   # 200
curl -s -o /dev/null -w "%{http_code}" https://docs.agilex.ai/     # curl exit 35
```

`docs.agilex.ai` fails the TLS handshake from this machine (exit 35). The model
was already in hand, so this fallback was not pursued further.

---

## 3. Environment (exact commands that worked)

Everything lives under `agilex-setup/`; the venv is `.venv-sim`. System Python
was never touched. Base interpreter: Homebrew `python3.12` (3.12.16, arm64).

```bash
cd <repo>

/opt/homebrew/bin/python3.12 -m venv .venv-sim
.venv-sim/bin/python -m pip install --upgrade pip
.venv-sim/bin/pip install mujoco numpy pillow scipy
```

Installed (verified with `pip list`):

```
mujoco  3.17.0
numpy   2.5.4
pillow  13.0.0
scipy   1.18.1
```

`scipy` is used only by the IK solver (`least_squares`). If you want a
dependency-free run, the script still needs it — install it.

---

## 4. The render path that worked

**Offscreen `mujoco.Renderer` with the CGL backend. Nothing else was needed.**

```python
os.environ.setdefault("MUJOCO_GL", "cgl")   # MuJoCo's macOS default
renderer = mujoco.Renderer(model, height=600, width=800)
renderer.update_scene(data, camera="overview")
img = renderer.render()                      # (600, 800, 3) uint8
```

* `MUJOCO_GL=cgl` — **worked**, first frame ~1.4 s, then ~25 ms/frame. macOS
  creates an offscreen CGL context, so a headless SSH-style session is fine.
* `MUJOCO_GL=egl` / `osmesa` — **not attempted**, and not needed: EGL is a
  Linux/ANGLE path and OSMesa is the software fallback. Keeping the platform
  default avoided the usual macOS "no GL context" rabbit hole.
* `mujoco.viewer` — not used. It needs a display/GLFW window; the point of this
  run was a headless, reproducible render.

Video encoding used the system ffmpeg (`/usr/local/bin/ffmpeg`, 8.0):

```bash
ffmpeg -y -framerate 25 -i frames/%05d.png -c:v libx264 -pix_fmt yuv420p -crf 20 out.mp4
```

`ffprobe` confirms: `h264, 800x600, 25/1 fps, 292 frames, 11.68 s`.

---

## 5. What the demo trajectory does

The scene adds to the robot: a checkered floor, skybox, two directional lights,
an orange **4 cm cube** with a free joint (0.05 kg, μ=1.4) at
`(0.33, −0.10)`, a green **drop pad** at `(0.33, +0.10)`, and a fixed
`overview` camera.

Cartesian waypoints are converted to joint angles by damped-least-squares IK
(`scipy.optimize.least_squares`, 6-D pose task: position + tool pointing straight
down, warm-started from the previous waypoint with random restarts). Joint 6 is
pinned to 0 after each solve — it is a pure roll about the tool axis and does not
move the TCP, so pinning it prevents the wrist unwinding ~360° between the pick
and place poses.

| # | segment | duration | gripper |
|---|---|---|---|
| 0 | start at the model's `home` keyframe `(0, 1.57, −1.3485, 0, 0, 0)` | — | open (35 mm) |
| 1 | move to pre-grasp, TCP `(0.33, −0.10, 0.13)`, tool vertical | 2.0 s | open |
| 2 | descend to the cube, TCP `(0.33, −0.10, 0.021)` = cube centre | 1.1 s | open |
| 3 | close the jaws (35 mm → 0) | 0.6 s | close |
| 4 | lift back to `z = 0.13` | 1.1 s | closed |
| 5 | transfer over the pad, `(0.33, +0.10, 0.13)` | 2.0 s | closed |
| 6 | lower to `z = 0.030` (pad top + cube half-height + 2 mm) | 1.1 s | closed |
| 7 | open the jaws — cube drops 2 mm onto the pad | 0.6 s | open |
| 8 | retreat to `z = 0.13` | 1.1 s | open |
| 9 | return to `home` | 2.0 s | open |

Each segment is interpolated with a quintic smoothstep in joint space, so there
are no velocity jumps. The grasp is genuine physics: the position-controlled
fingers squeeze the cube, friction holds it during the 0.10 m lift and the
0.20 m lateral transfer, and it is released by opening the jaws.

Measured at the end of the run (printed by the script):

```
cube start=[0.33 -0.10 0.021]  end=[0.326 0.10 0.028]
max_z=0.123  dist_to_pad_center=4.0 mm   -> pick-and-place SUCCEEDED
```

The script exits non-zero if the cube is not lifted >5 cm and delivered within
6 cm of the pad centre, so this is a self-checking demo, not just an animation.

---

## 6. The one thing that surprised me (and constrains task planning)

**The Piper cannot point its gripper straight down above ≈0.17 m.** With the
wrist pitch limited to ±70° and the elbow one-sided, a vertical tool pose is only
reachable in a low, close-in band. A random scan of 300 000 joint
configurations finds the highest TCP with the tool axis within 10° of vertical
at **z ≈ 0.167 m**; the practical top of the top-down grasp envelope is
~0.13–0.17 m.

Consequences, all of which shaped this demo:

* Objects must be grasped **at table/floor level**, not at chest height. That is
  why the cube sits on the ground plane instead of on a 0.4 m table — the first
  version of this demo targeted a table at z = 0.32 and IK could not converge
  (position error 4–12 cm, wrist pinned at its limit).
* The pre-grasp pose is at `z = 0.13`, and even there the solver leaves a
  **3.2 mm** position residual; the grasp pose itself is exact (0.0 mm).
* For real work you would mount the arm on a pedestal or tilt the wrist and use a
  side grasp. Worth knowing before writing a task planner against this arm.

---

## 7. Reproducing

```bash
cd <repo>

# validate model + IK only (fast, no simulation)
.venv-sim/bin/python tools/sim_piper.py --check

# full demo: frames + mp4
.venv-sim/bin/python tools/sim_piper.py

# options
.venv-sim/bin/python tools/sim_piper.py --width 1280 --height 960
.venv-sim/bin/python tools/sim_piper.py --fps 30 --no-video
.venv-sim/bin/python tools/sim_piper.py --save-all      # dump all 292 frames
```

The script is location-independent (all paths derive from its own location) and
regenerates `models/piper/piper_pick_scene.xml` on every run from a template +
the untouched upstream `piper.xml`, so the vendored model is never modified.
A full run takes ~22 s on this machine (2 s IK, 6 s physics, 13 s render/encode).

---

## 8. Files produced

```
agilex-setup/
├── .venv-sim/                                   # local venv (mujoco 3.17.0, numpy, pillow, scipy)
├── tools/sim_piper.py                           # reusable demo: model -> IK -> sim -> frames -> mp4
├── reports/simulation.md                        # this report
└── assets/sim/
    ├── piper_pick_and_place.mp4                 # 800x600, 25 fps, 11.68 s, 292 frames, 1.1 MB
    ├── frames/
    │   ├── 00_start_home.png                    # arm at home, cube on the left
    │   ├── 01_approach_cube.png                 # descending onto the cube
    │   ├── 02_grasp_closed.png                  # jaws closed on the cube
    │   ├── 03_lift.png                          # cube lifted clear
    │   ├── 04_transfer.png                      # carrying over the pad
    │   ├── 05_place_release.png                 # released on the green pad
    │   └── 06_return_home.png                   # back at home
    ├── models/piper/
    │   ├── piper.xml                            # upstream Menagerie MJCF (unmodified)
    │   ├── scene.xml, LICENSE, README.md, CHANGELOG.md
    │   ├── assets/                              # 84 OBJ/STL meshes
    │   └── piper_pick_scene.xml                 # generated scene (robot + floor + cube + pad + camera)
    └── _src/piper_ros/src/piper_description/    # official AgileX URDF + MJCF + meshes (provenance)
```

---

## 9. Limitations

1. **No Tracer base.** The arm is bolted to the world at the origin. There is no
   chassis, no wheels, no suspension, no mobile-base dynamics — the demo is a
   fixed manipulator only.
2. **Single arm.** The real robot has two Pipers on a Tracer; this sim has one.
3. **No motion planning.** Waypoints are hand-specified Cartesian poses solved by
   IK. There is no RRT/OMPL, no collision-aware planning, no self-collision
   avoidance beyond whatever MuJoCo's contact solver does at run time. A bad
   waypoint will collide.
4. **Simple grasp model.** Position-controlled jaws + Coulomb friction. No
   contact-compliance tuning, no force control, no slip detection, no soft
   fingers. The cube is 4 cm and 50 g — this would not generalise to heavy or
   deformable objects.
5. **Simplified collision geometry.** The Menagerie model uses capsules/boxes for
   collision and the detailed OBJ meshes only for visuals (`contype=0`), so
   contact is cheaper but less faithful than mesh-mesh.
6. **No sensors.** No RGB-D camera, no wrist camera, no force/torque sensor in
   the model, no ROS bridge. Rendering is for humans, not perception.
7. **Kinematic envelope.** As described in §6, a top-down grasp only works below
   ~0.17 m. The demo is shaped around that limit rather than overcoming it.
8. **Gripper units.** On hardware the gripper is commanded 0–70° (SDK); here
   `joint7` is 0–0.035 m of finger travel. A real task stack needs that mapping.
9. **Timing.** 2 ms timestep with no actuator dynamics/limits modelled; real
   Piper joint acceleration limits are not enforced.

---

## 10. Extending to a Tracer base + two arms

Concrete path, in the order I would do it:

1. **Chassis.** Add a `<body name="tracer">` with a `<freejoint>`, a box geom
   ~0.9 × 0.6 × 0.3 m and 4 wheel geoms (cylinders), mass ~30 kg, plus a
   differential-drive actuator pair (two velocity-controlled joints) — or model
   the Tracer as a planar `slide x`, `slide y`, `hinge z` triple if you only care
   about base motion and not wheel slip. MuJoCo's Menagerie has no Tracer model,
   so this is hand-built.
2. **Two arms.** MuJoCo has no name scoping. The clean way is to *generate* the
   second arm: parse `piper.xml` with `ElementTree`, prefix every
   `body/joint/actuator/geom/mesh/material` name with `left_`/`right_`, rewrite
   `meshdir` to a per-arm asset dir, and offset each arm's base body onto the
   chassis (`pos="±0.28 0 0.42"`, `quat` for a ±15° inward cant). Keep the
   equality constraint's `joint1`/`joint2` references prefixed too. The official
   repo already ships `piper_description_left.xacro` / `_right.xacro` if you
   prefer starting from URDF.
3. **Scene.** One `<include>` per generated arm inside a chassis scene, a shared
   floor/room, and a camera per arm plus an overview camera.
4. **Control.** Extend `sim_piper.py`: the `PiperKinematics` class already takes a
   `model/data` pair and looks bodies up by name — instantiate it twice with the
   prefixed names. Drive both arms from one trajectory loop (the two
   `data.ctrl` slices are independent) and add base `ctrl` for the chassis.
5. **Task layer.** With a base, the reach problem in §6 largely disappears: drive
   the Tracer to the table, then use the arm. Add a simple state machine
   (navigate → align → grasp → lift → place) and, if you need obstacle avoidance,
   swap the hand-written IK for `mink` or MoveIt2 against the same URDF.
6. **Sensors/ROS (optional).** Add `<camera>` elements and
   `mujoco.Renderer`-based RGB-D capture, or bridge to ROS 2 with
   `mujoco_ros2_control`; the official repo's `piper_mujoco` node is a
   `mujoco_py` dead end, do not start there.

---

## Appendix — exact command transcript

```bash
# 1. model discovery
/opt/homebrew/bin/python3.12 -m pip download piper_sdk --no-deps -d /tmp/piper_sdk_dl
unzip -l piper_sdk-0.6.2-py3-none-any.whl | grep -iE "urdf|mesh|stl|dae|xml|mjcf"   # NONE
curl -s "https://api.github.com/search/repositories?q=piper+urdf&per_page=10"         # 10 hits
curl -s "https://api.github.com/search/repositories?q=piper_description"              # 17 hits
curl -s https://raw.githubusercontent.com/google-deepmind/mujoco_menagerie/main/README.md | grep -i piper
git clone --depth 1 https://github.com/agilexrobotics/piper_ros.git
git clone --depth 1 --filter=blob:none --sparse https://github.com/google-deepmind/mujoco_menagerie.git
cd mujoco_menagerie && git sparse-checkout set agilex_piper

# 2. environment
/opt/homebrew/bin/python3.12 -m venv .venv-sim
.venv-sim/bin/python -m pip install --upgrade pip
.venv-sim/bin/pip install mujoco numpy pillow scipy

# 3. run
.venv-sim/bin/python tools/sim_piper.py --check
.venv-sim/bin/python tools/sim_piper.py

# 4. verify
ffprobe -v error -show_entries stream=width,height,nb_frames,r_frame_rate \
        -of default=noprint_wrappers=1 assets/sim/piper_pick_and_place.mp4
```
