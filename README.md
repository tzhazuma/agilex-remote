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
| **Cameras** | Two wrist MJPEG streams plus optional global BRIO (`cam=4`) and RealSense D455 (`cam=12`, YUYV) |
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
    cam0/000001.jpg    right wrist camera
    cam2/000001.jpg    left wrist camera
    cam4/000001.jpg    optional global Logitech BRIO
    cam12/000001.jpg   optional global RealSense D455
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

A single `/api/drive` POST is not a duration-controlled move: it only refreshes the target until the watchdog expires. For a sustained move, continuously re-post the command (and lease heartbeat) faster than `WATCHDOG_MS`, then explicitly send zero. At `0.03 m/s`, one isolated request can expire after roughly 9 mm even if the client sleeps for 0.2 s; this made earlier tests look like the chassis had not moved. A successful HTTP response alone is not motion evidence—verify fresh images or calibrated wheel counts.

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

# from the checkout, on the robot:
sudo ./install.sh
# or, equivalently:
sudo ./server/install-server.sh
```

`install-server.sh` installs both server files itself; you do not need to copy
them to `/usr/local/share` or `/tmp` first. If the files live elsewhere, point
`AGILEX_PAYLOAD` at the directory that contains `agilex-remote-server.py` and
`remote-ui.html`. The web password is prompted (or read from
`AGILEX_PASSWORD` / `AGILEX_PASSWORD_FILE`); there is no default.

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

Open `http://<robot-ip>:8765/`. Off-loopback clients get a password sign-in page;
the password is chosen at install time (there is no default, see `AGILEX_PASSWORD`).

Loopback is exempt, so `ssh -L 8765:127.0.0.1:8765 robot` skips the login.

### Control lease (single controller)

Motion is single-controller by design. All non-GET **control** POSTs
(`/api/arm*`, `/api/gripper`, `/api/arm/mode|mit|movec`, `/api/traj/load|play|pause|stop`,
`/api/teach`, `/api/master-slave`, `/api/record/start|stop`,
`/api/chassis/clear-fault`, and non-zero `/api/drive`) require a short-lived lease.
`GET /api/status`, `/frame`, `/video` and `GET /api/lease/status` stay open.
**E-STOP itself is exempt**; a zero-velocity `/api/drive` is also exempt.
`/api/estop/clear` is **not** a panic action: it requires a valid lease and
body `{"confirm":"CLEAR"}`, so one tab cannot clear another operator's latch.
The UI's clear button must acquire the lease and send that confirm.

```bash
# 1. acquire once; keep the secret in memory (the shell helper uses a 0600
#    curl config file, never a log)
curl -s -X POST http://127.0.0.1:8765/api/lease/acquire \
  -H 'Content-Type: application/json' -d '{"controller":"laptop-1"}'
# => {"ok":true,"lease":"<random>","controller":"laptop-1","expires_in":20.0,...}

# 2. every control POST carries the secret in this header
curl -s -X POST http://127.0.0.1:8765/api/arm \
  -H 'Content-Type: application/json' \
  -H "X-Control-Lease: $LEASE" \
  -d '{"arm":0,"joint":0,"delta_deg":0.1}'

# 3. extend before the 20 s TTL expires
curl -s -X POST http://127.0.0.1:8765/api/lease/heartbeat \
  -H "X-Control-Lease: $LEASE" -d '{}'

# 4. release (only the holder can)
curl -s -X POST http://127.0.0.1:8765/api/lease/release \
  -H "X-Control-Lease: $LEASE" -d '{"controller":"laptop-1"}'

# 5. explicit takeover; requires the exact confirm token and is audited
curl -s -X POST http://127.0.0.1:8765/api/lease/acquire \
  -H 'Content-Type: application/json' \
  -d '{"controller":"laptop-2","takeover":true,"confirm":"TAKEOVER"}'
```

For shell scripts, source `scripts/agilex-lease.sh` instead of hand-rolling the
header. It acquires, writes the secret to a 0600 curl config file (never argv or
logs), auto-releases on EXIT, and can heartbeat a long run:

```bash
. scripts/agilex-lease.sh
lease_acquire "my-script" || exit $?
lease_start_heartbeat 5
lease_curl /api/arm '{"arm":0,"joint":0,"delta_deg":0.1}'
lease_clear_estop        # lease + {"confirm":"CLEAR"}; fails without both
lease_release
```

`lease_curl` returns 0 only for HTTP 2xx. HTTP 409 returns 9, and any other
non-2xx (including 500) returns 1, so a failed E-STOP/zero-speed request is
never reported as success.

`tools/run-smolvla.py` does the same in Python: it acquires before the first
command, heartbeats in a daemon thread, turns a 409 into `LeaseLost`, and
releases in `finally`. `--dry-run` is a truly offline check: no policy load,
no server request, no lease.
`verify-modes.sh` and `mode-smoke.sh` acquire automatically; their explicit
`AGILEX_ALLOW_MODE_TESTS=1` safety gate is unchanged.

> `tools/feedback_control.py` is a pure offline decoder/controller library: it
> opens no socket and sends no HTTP, so it neither needs nor receives a lease.
> A future online adapter that POSTs to the server must acquire the lease and
> send `X-Control-Lease` itself.

The server stores only `sha256(lease)` plus an expiry, never the secret. A missing,
expired, or wrong secret gets HTTP 409 with the current owner in `lease`.
The web UI does **not** acquire on page load or while watching cameras; it
acquires on the first active action, shows `CONTROL: YOU` / `CONTROL: <id>` /
`CONTROL: free`, and provides **Take control** (with confirm) and **Release**.
E-STOP remains available without the lease.

Every control POST is audited as one JSON line in the service journal (stderr):
UTC time, client address, controller id, path, a whitelist-summarized body, and
result. The controller is the **lease holder** when a valid lease is presented;
lease-exempt requests are marked `controller_verified:false`. Password, cookie,
token, and lease values are never written. Successful non-zero `/api/drive`
audits are sampled (default 1/s); zero drive and errors are always recorded.

### Feedback safety and stop behavior

Joint feedback is re-validated on **every** 5 Hz read, not just at startup: all
six values must be finite, and an all-zero pose is rejected by default because a
silent CAN bus reports exactly that. A genuine arm parked at zero is legal but
indistinguishable, so it needs an explicit opt-in:

```bash
AGILEX_ALLOW_ZERO_POSE=1   # only for an arm you have confirmed is parked at 0°
```

Do not set this to paper over a dead bus: the startup probe still requires the
CAN RX counter to advance. While feedback is stale or invalid, every motion
entry point (nudge, target, home, gripper, trajectory, teach, master/slave)
returns failure, and `connected` is false.

What a stop does depends on what owns the arm:

| Active source | Stop action |
|---|---|
| Plain MOVE J | Stop sending `JointCtrl` and hold the measured pose. **No `DisableArm`**: dropping torque on a raised arm is worse than stopping the stream. |
| Drag teach | `MotionCtrl_1(grag_teach_ctrl=0x02)` (documented "end record"). |
| Master/slave | `MasterSlaveConfig(linkage_config=0x00)` (unlink). |
| MOVE C | `MotionCtrl_1(emergency_stop=0x01)` (0x02 is resume). If that frame itself fails, `DisableArm` is the permitted last resort. |

The same table applies to E-STOP, lease expiry/release/takeover, and a latched
feedback fault. E-STOP additionally calls `DisableArm` because it is a panic
action.

The first lease acquire (`free → held`) deliberately does **not** cancel
motion: the usual sequence is "take the lease, then start the trajectory".
Only release, expiry, takeover, or a missed release+re-acquire cancels.

### MIT gain bounds

`/api/arm/mit` and `ArmBridge.set_mit` enforce the same conservative box before
anything reaches the motors: `kp` 0–500, `kd` 0–50, `t_ref` −20–20. NaN, Inf,
negative gains, empty lists, and out-of-box values return HTTP 400 and leave the
previous gains untouched. Bench-specific ceilings can be raised with
`AGILEX_MIT_KP_MAX` / `AGILEX_MIT_KD_MAX`; `t_ref` is fixed.

A request body that is valid JSON but not an object (a list, string, number) gets
HTTP 400 before any gate runs, instead of closing the connection.

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
| `AGILEX_PASSWORD` | *(unset)* | Sign-in password for off-loopback clients; the installer requires one (min 8 chars) |
| `AGILEX_MAX_LIN` / `AGILEX_MAX_ANG` | `2.3` / `0.7853` | Hard clamp |
| `AGILEX_DEFAULT_LIN` / `AGILEX_DEFAULT_ANG` | `0.5` / `0.3` | UI slider start |
| `AGILEX_WATCHDOG_MS` | `300` | Stop-if-silent window (= braking distance) |
| `AGILEX_BRIO_FRAME_URL` | *(unset)* | Optional shared global JPEG endpoint; enables `/frame|video?cam=4` and `cam4` recording without reopening V4L2 |
| `AGILEX_REALSENSE_RGB_DEV` | *(unset)* | Optional D455 RGB device path; enables `cam12` |
| `AGILEX_CAM12_INPUT_FORMAT` | `mjpeg` | Set `yuyv422` for the D455 RGB node |
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
   `export AGILEX_SCAN_RANGE=<ip>/16` — the shipped default is a placeholder.

Set the topic once: `echo '<your-topic>' > ~/.config/agilex/ntfy-topic`.

## Install

```bash
# from a laptop, over SSH (stages, installs, starts, verifies)
./install.sh agilex@agilex.local

# or on the robot itself
sudo ./install.sh
```

Then open `http://<robot-ip>:8765/`. The web UI password is chosen at install
time and stored in `/etc/agilex-remote.env` (`AGILEX_PASSWORD=...`, mode 0600).
There is **no default**; the installer refuses to run without a real password
(8+ chars, not `changeme`). Rotate it with:

```bash
sudo AGILEX_PASSWORD='<new-password>' ./server/install-server.sh --env-only
sudo systemctl restart agilex-remote
```

## Configuration

| Setting | Where | Default |
|---|---|---|
| Web UI password | `/etc/agilex-remote.env` → `AGILEX_PASSWORD` | *(unset; installer requires one)* |
| Listen address / port | unit env `AGILEX_REMOTE_HOST`, code `PORT` | `0.0.0.0:8765` |
| Chassis speed cap | `AGILEX_MAX_LIN` / `AGILEX_MAX_ANG` | 2.3 m/s, 0.7853 rad/s |
| Arm speed | `AGILEX_ARM_SPEED_PCT` | 25 % |
| Max target lag | `AGILEX_MAX_TARGET_LAG` | 15° |
| Resync tolerance | `AGILEX_RESYNC_MARGIN` | 5° |
| Feedback staleness | `AGILEX_FEEDBACK_STALE_S` | 2 s |
| Accept an all-zero pose | `AGILEX_ALLOW_ZERO_POSE` | `0` (reject) |
| MIT kp / kd ceiling | `AGILEX_MIT_KP_MAX` / `AGILEX_MIT_KD_MAX` | 500 / 50 |
| ntfy topic | `~/.config/agilex/ntfy-topic` | none |

## Tests

```bash
python3 tools/test-modes-offline.py     # 41 assertions, fake CAN, no hardware
python3 scripts/test-lease-offline.py   # fake lease API: acquire/heartbeat/409/release
./scripts/verify-modes.sh               # hardware read-back; NOT motion-free (see gate)
./scripts/mode-smoke.sh                 # MOVE P + trajectory replay; NOT motion-free
./scripts/verify-bundle.sh <zip>        # prove the SHIPPED tree works, from a clean extract
```

On Windows use `python` instead of `python3` (the Store `python3` stub may not run);
`verify-bundle.sh` probes both and falls back automatically.

The two on-robot scripts change arm modes and are **not** "no motion" tests:
`verify-modes.sh` can switch to MOVE P (and to MIT with a second opt-in), and
`mode-smoke.sh` replays a trajectory. Both refuse to start unless you clear the
workspace and set `AGILEX_ALLOW_MODE_TESTS=1`. MIT additionally needs
`AGILEX_ALLOW_MIT_TEST=1` and is refused while an arm is enabled unless
`AGILEX_ALLOW_MIT_WHILE_ENABLED=1` is set.

`verify-bundle.sh` exists because a stale copy of the test file was once copied
over the fixed one, silently dropping the regression tests while the bundle
still built and shipped. It extracts the zip to a throwaway directory, runs all
four shipped suites (mode 41+, server 370+, feedback 272+, lease 23+) with PASS
floors, checks every shell script parses, and scans for credentials and
real addresses. Run it before publishing.
