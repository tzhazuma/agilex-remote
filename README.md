# AgileX Remote — a browser-based controller for an AgileX UGV + dual Piper arms

A self-hosted virtual remote controller for an **AgileX Tracer** chassis with **two
Piper arms**, two arm-mounted depth cameras, and ROS 2 Humble. It runs a small
Python server on the robot and serves a single-file web UI that also installs as
an Android app.

This is the code that came out of setting one specific robot up. It is published
because the non-obvious parts (below) took real debugging to find, and they apply
to any AgileX + Piper build.

> **拿到这个包，先看 [`INSTALL-CONNECT.md`](INSTALL-CONNECT.md)** — 安装、连接、自检、常见问题，
> 大约 10 分钟能让网页遥控器跑起来。
> Just want it running? Start with **[`INSTALL-CONNECT.md`](INSTALL-CONNECT.md)** (install + connect guide).
> One-command check afterwards: `./scripts/check-connection.sh`.

---

## What it does

| | |
|---|---|
| **Chassis drive** | Keyboard (WASD/arrows), mouse D-pad, on-screen joystick, or a physical gamepad |
| **Dual arms** | Per-arm panels: incremental nudge, **absolute target angles**, gripper, enable/disable, per-joint fault display |
| **3D view** | Hand-rolled forward-kinematics stick model of each arm, driven by the live joint angles, with the commanded target ghosted in amber |
| **Cameras** | Two live MJPEG streams (one per arm), streamed straight from V4L2 with no re-encode |
| **Safety** | Hard server-side clamps, a watchdog that forces a stop, E-STOP, and a fault-clearing reset |
| **Discovery** | The robot publishes its own DHCP address to an ntfy topic, so a client finds it after a reboot without scanning |
| **Android** | A thin WebView shell (`android/`) with a launcher icon, ntfy auto-discovery, and a configurable base URL |

## Architecture

```
   browser / Android app
            │  HTTP + MJPEG
            ▼
   ┌──────────────────────────┐        ┌─────────────────────────────┐
   │ agilex-remote-server.py  │  ROS   │ tracer_base  → can0 (500k)  │
   │  on the robot, 0.0.0.0   │───────▶│   /cmd_vel → chassis        │
   │  · /cmd_vel publisher    │        └─────────────────────────────┘
   │  · /tracer_status sub    │        ┌─────────────────────────────┐
   │  · piper_sdk (2 arms)    │  CAN   │ can_left / can_right (1M)   │
   │  · ffmpeg → MJPEG        │───────▶│   Piper arms, 6 motors each │
   └──────────────────────────┘        └─────────────────────────────┘
```

The server must run **on the robot**, because the robot sets
`ROS_LOCALHOST_ONLY=1` — DDS never leaves the box, so a remote rosbridge would
not see the topics.

---

## Recording demonstrations

The controller can record a demonstration end to end, because it already holds
the images, the joint states and the commanded targets in one process. That
matters here: the ROS graph on this robot carries **only chassis topics** — there
is no `/camera/*` and no `/joint_states`, so `rosbag record -a` captures nothing
a policy could learn from.

```
POST /api/record/start   {"task": "...", "notes": "..."}
POST /api/record/stop    {"success": true|false}
POST /api/record/status
```

Each episode becomes a self-contained directory:

```
episode_0007/
    meta.json          task, timing, frame count, outcome, camera warm-up state
    cam0/000001.jpg    left arm camera
    cam2/000001.jpg    right arm camera
    timeline.csv       one row per tick
```

`timeline.csv` holds **both** halves that exist in a demonstration:

| columns | meaning |
|---|---|
| `L_j0..5`, `L_grip`, `R_j0..5`, `R_grip` | `observation.state` — measured |
| `L_t0..5`, `L_tgrip`, `R_t0..5`, `R_tgrip` | `action` — **commanded target** |
| `cmd_lin`, `cmd_ang`, `odom_x`, `odom_y` | chassis command and odometry |
| `cam0_seq`, `cam2_seq` | source frame counter, so a repeated frame is detectable |

Recording only the measured angles produces a dataset you cannot train on:
behaviour cloning learns observation → *command*, not observation → itself.
Image file names and the `frame` column share the same index, so row N and the
two `00000N.jpg` files are the same instant by construction.

### One camera, many consumers

V4L2 devices are exclusive — a second `ffmpeg` on the same node fails with
`Device or resource busy`. So each camera is owned by a single `FrameHub` that
publishes the newest frame and is reference-counted; the live MJPEG view and the
recorder both read from it. Without this, watching the camera and recording at
the same time was impossible.

## Exporting to LeRobot

```bash
tools/export-lerobot.py --episodes <episode dir> --out <dataset dir> --fps 10
```

Writes the LeRobot v2.1 layout: `meta/{info,tasks,episodes,stats}.json`,
`data/chunk-000/*.parquet`, and one h264 mp4 per camera per episode.
Requires `pyarrow`. Do **not** install it into system Python — ROS Humble depends
on numpy 1.21.5, which a modern `pyarrow`/`torch` install will replace. Use a venv:

```bash
python3 -m venv ~/agilex-data/.venv-export
~/agilex-data/.venv-export/bin/pip install pyarrow
```

## Baseline results

`tools/bc-baseline.py` trains the smallest model that can prove the pipeline is
sound: a small CNN over one camera plus an MLP over joint state, predicting the
14 commanded targets.

On 8 episodes / 643 frames, split by episode:

| | |
|---|---|
| device | CUDA, RTX 4060, torch 2.14.1+cu130 |
| params | 0.09 M |
| train MSE | **0.0089** (mean error 0.11°) |
| val MSE | **0.046** (mean error 0.64°) |

The gate that matters is *can it fit at all*: if a tiny model cannot drive the
training loss down, something upstream is wrong (misaligned frames, wrong
columns, bad normalisation) and ACT would not help either. It passes, so the
data path is sound.

## A note on the odometry

`/odom` and the reported `odom_x`/`odom_y` stay at zero on this robot even while
it drives. The chassis **does** move — verified by camera frame differencing:
2.6 mean-abs-diff while stationary versus 45 after driving for two seconds, with
the scene visibly displaced. Do not treat zero odometry as "not moving"; use
vision if you need a displacement signal.

## The five things that are easy to get wrong

These are the reason this repo exists. All five were reproduced, not theorised.

### 1. A latched chassis fault silently ignores every motion command

The Tracer latches certain faults. While one is set, the vendor manual states the
drive output is limited and *"motion control commands have no effect… the host
must send a fault-clear command; only after clearing can motion commands
execute."*

The symptom is nasty: `/cmd_vel` is published, the CAN motion frame goes out, and
nothing happens — indistinguishable from a broken controller. Check
`/tracer_status.error_code`, and clear it with **CAN `0x441`, payload `0x00`**:

```bash
cansend can0 441#00        # 0x00 = clear all, 0x01/0x02 = clear motor 1/2
```

The server does this at startup (and exposes `POST /api/chassis/clear-fault`).

### 2. Enabling a Piper arm needs `EmergencyStop(0x02)` first

`MotionCtrl_1(0x02)` is the documented **resume**. Without it, an arm left in
`REACH_TARGET_POS_FAILED` accepts `EnableArm`, reports only some motors enabled,
and then ignores every `JointCtrl`.

Note the argument: `EmergencyStop(0x00)` is documented as **invalid / no-op**, so
"clearing" with `0` does nothing at all.

### 3. `EnableArm` is not enough — the mode must be re-asserted

An arm that was *already* enabled never received `MotionCtrl_2(ctrl_mode=0x01)`,
so it stayed in its previous mode and moved a fraction of a degree before
stalling short of the target. The server re-asserts the mode periodically.

### 4. A single faulted motor must not mark the whole arm disabled

`bool(all(joints_enabled))` reads as "disabled" when five of six motors are live
and usable. Report per-joint truth and treat the arm as controllable if any motor
is up.

### 5. The control loop must never block on feedback

Reading `GetDriverStates()` / `GetArmJointMsgs()` costs a CAN round-trip. Doing it
on the 50 Hz command thread stalls the stream and the arm visibly shakes. Keep
commanding and reading on separate threads.

### Bonus: two vendor scaling bugs

- `battery_voltage` is reported **10× too high** (275 V for a 24 V pack). The raw
  CAN value is in 10 mV units.
- There is **no velocity clamp for the Tracer anywhere in `ugv_sdk`** —
  `robot_limits.hpp` has structs for four other chassis but not this one, and the
  V1 parser aliases `BunkerLimits`, which clamps *above* the Tracer's real maximum.
  **The clamp has to live in your controller.**

---

## Speed limits

The chassis's documented maximum is **2.3 m/s unloaded**
(`ugv_sdk/docs/protocol_v2/TRACER_UserManual_v1.2.pdf` §1.2). No maximum angular
rate is documented; `0.7853 rad/s` is only the V1 protocol's "100 %" reference,
not a hardware rating.

The controller therefore clamps server-side and starts the UI slider well below
the ceiling:

| | default | ceiling |
|---|---|---|
| linear | `0.5 m/s` | `2.3 m/s` |
| angular | `0.3 rad/s` | `0.7853 rad/s` |

**The watchdog is a braking distance, not a timeout.** The robot coasts for the
whole watchdog window before a stop is even commanded:

```
coast = watchdog_ms / 1000 * speed
```

At the original 0.08 m/s cap a 700 ms window was 5.6 cm and irrelevant. At
2.3 m/s it would be **1.6 m**. The default is therefore 300 ms (0.69 m at full
speed). Tune it with `AGILEX_WATCHDOG_MS`.

All limits are environment variables — see the table below.

---

## Install

### Robot

```bash
# x11vnc is optional but recommended for a real desktop view
sudo apt-get install -y x11vnc can-utils ethtool ffmpeg

sudo install -d /usr/local/share/agilex-remote
sudo install -m 0755 server/agilex-remote-server.py /usr/local/share/agilex-remote/
sudo install -m 0644 server/remote-ui.html          /usr/local/share/agilex-remote/
sudo install -m 0644 server/install-server.sh       /tmp/
sudo bash /tmp/install-server.sh      # or just copy the unit from that script
```

The chassis needs `tracer_base` running to bridge `/cmd_vel` onto CAN:

```bash
source /opt/ros/humble/setup.bash
source ~/agilex_ws/install/setup.bash
ros2 launch tracer_base tracer_base.launch.py
```

> ⚠️ `tracer_base` calls `EnableCommandedMode()` on connect, which takes the
> chassis **out of RC-handset mode** — the physical remote stops driving it until
> the chassis is power-cycled. That is inherent to ROS control.

### Client

Open `http://<robot-ip>:8765/`. Off-loopback clients get a password sign-in page
(default password `changeme` — **change it**, see `AGILEX_PASSWORD`).

Loopback is exempt, so `ssh -L 8765:127.0.0.1:8765 robot` skips the login.

### Android

```bash
cd android && ./build.sh          # produces agilex-remote.apk
adb install -r agilex-remote.apk
```

The app is a WebView shell. It defaults to `http://ROBOT_IP:8765/` and, when that
fails, queries the ntfy topic to discover the robot's current address. The base
URL and topic are editable in Settings.

---

## Automatic IP discovery

The robot's address is DHCP and changes on reboot, so the robot announces itself:
`scripts/agilex-report-ip.sh` publishes the current IP to an **ntfy.sh** topic on
boot and every 5 minutes (via a systemd timer). Clients read the latest message
from that topic.

> Set your own topic. This repo ships with `YOUR-NTFY-TOPIC-HERE`; a public topic
> means anyone who guesses the name can read your robot's address (it is an
> RFC1918 address, but still).

```bash
curl -s 'https://ntfy.sh/YOUR-NTFY-TOPIC-HERE/json?poll=1' | tail -1
```

⚠️ Parse the `"message"` **key**, not the string `message` — a naive scan matches
the value of `"event":"message"` first and returns `"topic"` instead.

On the Mac side, `scripts/agilex-proxy.sh` is an SSH `ProxyCommand` that resolves
the address (cache → ntfy → known addresses → mDNS → sweep) and hands the socket
to `ssh`, so `ssh robot` keeps working across reboots.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AGILEX_REMOTE_HOST` | `0.0.0.0` | Bind address |
| `AGILEX_REMOTE_PORT` | `8765` | Port |
| `AGILEX_PASSWORD` | `changeme` | Sign-in password for off-loopback clients |
| `AGILEX_MAX_LIN` / `AGILEX_MAX_ANG` | `2.3` / `0.7853` | Hard clamp |
| `AGILEX_DEFAULT_LIN` / `AGILEX_DEFAULT_ANG` | `0.5` / `0.3` | UI slider start |
| `AGILEX_WATCHDOG_MS` | `300` | Stop-if-silent window (= braking distance) |
| `AGILEX_MAX_JOINT_STEP` | `2.0` | Max degrees per nudge |
| `AGILEX_ARM_SPEED_PCT` | `25` | Piper `move_spd_rate_ctrl` |
| `AGILEX_ARM_CAN` / `AGILEX_ARM_CAN_RIGHT` | `can_left` / `can_right` | Arm buses |
| `AGILEX_CHASSIS_CAN` | `can0` | Chassis bus |
| `AGILEX_ALLOW_ANON` | `0` | `1` disables the login entirely |

---

## Sensors this robot actually had

Worth knowing, because the drivers installed do **not** match the hardware present:

- **Present:** hipnuc IMU (`/dev/ttyUSB0`), 2× Orbbec depth cameras, chassis BMS,
  2× drive motors, 3× gs_usb CAN adapters
- **Driver installed, hardware absent:** RealSense, and *all* LiDAR
  (Livox / RoboSense / YDLidar were all built; none was connected)
- The Orbbec cameras had **no ROS driver** installed — the UI reads them straight
  from V4L2 instead

---

## Caveats

- The Android APK here is **debug-signed** — fine for sideloading, not for
  distribution.
- `DisableArm` does not de-energise the motors; they hold position. Do **not**
  reach for `ResetPiper()` to force a power-down: its own documentation says the
  arm "will immediately lose power and fall down".
- **Never run `apt autoremove` on a box like this.** On the robot this came from,
  it would have removed the working NVIDIA driver stack *and* `dkms` itself.
- The Java package id is `com.azuma.agilexremote`; rename it if you fork this.

## Licence

MIT — see `LICENSE`.

---

## Operating modes

Each arm can be driven six ways. The default is joint-space position; the rest
exist because position control alone cannot do compliant contact, Cartesian arcs,
hand-guided demos, or arm-to-arm mirroring.

| Mode | SDK call | What it is for |
|---|---|---|
| `pos_j` MOVE J *(default)* | `MotionCtrl_2(move_mode=0x01)` + `JointCtrl` | Joint-space position. The action space a policy emits. |
| `pos_p` MOVE P | `MotionCtrl_2(move_mode=0x00)` + `JointCtrl` **on change only** | Arm plans its own path to a point. |
| `mit` MIT impedance | `MotionCtrl_2(is_mit_mode=0xAD)` + `JointMitCtrl` | Compliance: per-motor `kp` / `kd` / `t_ref`. Needed for anything that touches. |
| `movec` MOVE C | `move_mode=0x03` + `EndPoseCtrl` + `MoveCAxisUpdateCtrl` | Cartesian arc through start → via → end. |
| Trajectory stream | targets fed from a list at `dt` | Replay a recorded / downloaded joint trajectory. |
| Drag teach | `MotionCtrl_1(grag_teach_ctrl=0x01/0x02/0x03)` | Record by hand, then replay. |
| Master/slave | `MasterSlaveConfig(0xFA leader / 0xFC follower)` | Left arm hand-guided, right arm mirrors it. |

API: `POST /api/arm/mode`, `/api/arm/mit`, `/api/arm/movec`,
`/api/traj/load|load-file|list|play|pause|stop`, `/api/teach`, `/api/master-slave`.
All take `{"arm": 0|1, ...}`.

`/api/traj/load-file` replays a trajectory **from disk**: a `.json`
(`{"name","dt","points"}`, or a bare list), an episode `timeline.csv` written by
this recorder (it uses the commanded-target columns `L_t*` / `R_t*`, not the
measured ones), or a directory containing one. `/api/traj/list` returns what is
loadable and the UI offers it as a per-arm picker.

Two things are **verified against the hardware, not assumed**:

* `set_mode` reads `GetArmStatus()` back and refuses to pretend. If the arm did not
  change mode, the server reverts its own bookkeeping and returns failure. This
  exists because a mode switch silently failing is what made an arm shake on
  2026-10-29 — see `docs/incident-2026-10-29-shaking.md`.
* An arm whose CAN bus is silent is marked **not connected** at startup, and no
  target or home pose is adopted. Otherwise a dead bus reads as `[0,0,0,0,0,0]`
  and that becomes the "home" pose.

## Finding the robot

The robot is on DHCP, so its address changes. `ssh agilex` keeps working because
`~/.ssh/config` points `ProxyCommand` at `scripts/agilex-proxy.sh`, which tries,
in order, first hit wins:

1. **Cache** `~/.cache/agilex/current-ip`, validated by SSH banner *and* host key
   (an old address can be handed to a different machine).
2. **ntfy** — the robot publishes its own IP on boot and every 5 minutes
   (`scripts/agilex-report-ip.sh` + `.timer`). The client polls
   `https://ntfy.sh/<topic>/json?poll=1` and takes the last `message`. No scanning,
   instant, survives any address change. The Android app uses the same channel.
3. **Previously seen** addresses (`~/.cache/agilex/seen-ips`, last 20).
4. **mDNS** `agilex.local`.
5. **Sequential sweep**, known /24s first, 600 s budget, one sweep at a time,
   every candidate validated by SSH banner + host key. **Set your own subnet:**
   `export AGILEX_SCAN_RANGE=10.0.0.0/16` — the shipped default is a placeholder.

Set the topic once: `echo '<your-topic>' > ~/.config/agilex/ntfy-topic`.

## Install

```bash
# from a laptop, over SSH (stages, installs, starts, verifies)
./install.sh agilex@agilex.local

# or on the robot itself
sudo ./install.sh
```

Then open `http://<robot-ip>:8765/`. The web UI password comes from
`/etc/agilex-remote.env` (`AGILEX_PASSWORD=...`, mode 0600). **The default is
`changeme` — change it.**

## Configuration

| Setting | Where | Default |
|---|---|---|
| Web UI password | `/etc/agilex-remote.env` → `AGILEX_PASSWORD` | `changeme` |
| Listen address / port | unit env `AGILEX_REMOTE_HOST`, code `PORT` | `0.0.0.0:8765` |
| Chassis speed cap | `AGILEX_MAX_LIN` / `AGILEX_MAX_ANG` | 2.3 m/s, 1.2 rad/s |
| Arm speed | `AGILEX_ARM_SPEED_PCT` | 25 % |
| Max target lag | `AGILEX_MAX_TARGET_LAG` | 15° |
| Resync tolerance | `AGILEX_RESYNC_MARGIN` | 5° |
| ntfy topic | `~/.config/agilex/ntfy-topic` | none |

## Tests

```bash
python3 tools/test-modes-offline.py     # 41 assertions, fake CAN, no hardware
./scripts/verify-modes.sh               # 12 checks on the robot, no motion
./scripts/mode-smoke.sh                 # 23 checks on the robot, no motion
./scripts/verify-bundle.sh <zip>        # prove the SHIPPED tree works, from a clean extract
```

`verify-bundle.sh` exists because a stale copy of the test file was once copied
over the fixed one, silently dropping the regression tests while the bundle
still built and shipped. It extracts the zip to a throwaway directory, runs the
shipped tests, checks every shell script parses, and scans for credentials and
real addresses. Run it before publishing.
