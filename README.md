# AgileX Remote — a browser-based controller for an AgileX UGV + dual Piper arms

A self-hosted virtual remote controller for an **AgileX Tracer** chassis with **two
Piper arms**, two arm-mounted depth cameras, and ROS 2 Humble. It runs a small
Python server on the robot and serves a single-file web UI that also installs as
an Android app.

This is the code that came out of setting one specific robot up. It is published
because the non-obvious parts (below) took real debugging to find, and they apply
to any AgileX + Piper build.

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
