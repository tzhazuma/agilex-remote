# Hardware test report — arm / cameras / chassis (task-7)

**Robot:** `agilex` (<robot-ip>) · Ubuntu 22.04.5 · ROS 2 Humble · `ROS_DOMAIN_ID=222`, `ROS_LOCALHOST_ONLY=1`
**Date:** 2026-10-13 00:25–01:10 (+08:00)
**Method:** everything executed on the robot via `bin/rrun.sh` (user) / `bin/rsudo.sh` (root). No hand-rolled ssh/sudo.

---

## 0. Verdicts (read this first)

| Subsystem | Verdict | One-line evidence |
|---|---|---|
| **CAN buses** | ✅ **WORKS** | `can0` UP @500 k, `can_left`/`can_right` UP @1 M, all `ERROR-ACTIVE` |
| **Arm (Piper ×2)** | ✅ **WORKS** | Both arms stream 0x2A1–0x2A8 feedback at **200 Hz**; joints, enable state, gripper, end pose, firmware `S-V1.9-1` all read successfully |
| **Cameras (Orbbec)** | ⚠️ **PARTIALLY WORKS — 2 of 3 present** | Two cameras produce real colour **and** depth images; the third ("front", `CC1W56101WC`) **is not detected on USB at all** |
| **Chassis** | ✅ **WORKS (state readable)** | `can0` streams AgileX-V2 state at ~50 Hz: standby mode, `error_code=0`, BMS SOC 100 %, current 0.0 A, all velocities & odometry **0** (stationary). One unresolved scale question on battery voltage — see §4.3 |

**Deliverable the user asked for: real camera pictures are on the Mac** in `evidence/` — 6 JPEGs + 2 depth PNGs + 2 MP4 videos, all verified to contain real imagery (see §3.4).

### What I deliberately did NOT do (safety)

* **No arm motion.** Not one motion command was sent. Arm proof is read-only state over CAN.
* **No chassis motion.** `cmd_vel` was never published and `tracer_base` was **not** launched (see §4.4 — it transmits a mode-change frame on connect).
* **Total frames this session ever transmitted:** `can0` = **0**, `can_left` = **4**, `can_right` = **4**. The 8 arm frames were firmware-version *query* frames (`0x4AF`, payload `01 00 00 00 00 00 00 00`) — see §2.5. Everything else was passive listening.

---

## 1. CAN buses — ✅ WORKS

`can0`/`can1`/`can2` were all **DOWN** on arrival (as recon predicted). `ethtool` and `can-utils` were **already installed** — no apt needed.

Command (as root, via `bin/rsudo.sh`):

```bash
cd /home/agilex/piper_ros && bash ./can_config.sh
```

Raw output (trimmed to the per-interface result lines):

```
检测到 USB 硬件地址参数: 1-13:1.0
找到与 USB 硬件地址 1-13:1.0 对应的接口: can2
接口 can2 已重新设置为比特率 1000000 并激活。
将接口 can2 重命名为 can_left
接口已重命名为 can_left，并重新激活。
-------------------OVER------------------------
检测到 USB 硬件地址参数: 1-12:1.0
找到与 USB 硬件地址 1-12:1.0 对应的接口: can1
接口 can1 已重新设置为比特率 1000000 并激活。
将接口 can1 重命名为 can_right
接口已重命名为 can_right，并重新激活。
-------------------OVER------------------------
检测到 USB 硬件地址参数: 1-4:1.0
找到与 USB 硬件地址 1-4:1.0 对应的接口: can0
接口 can0 未激活或未设置比特率。
接口 can0 已重新设置为比特率 500000 并激活。
-------------------OVER------------------------
=== EXIT=0 ===
```

`ip -details link show type can` (settled state):

```
4: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 10
    link/can  promiscuity 0 minmtu 0 maxmtu 0
    can state ERROR-ACTIVE restart-ms 0
	  bitrate 500000 sample-point 0.875
	  tq 125 prop-seg 6 phase-seg1 7 phase-seg2 2 sjw 1
	  gs_usb: tseg1 1..16 tseg2 1..8 sjw 1..4 brp 1..1024 brp-inc 1
	  clock 48000000 numtxqueues 1 numrxqueues 1 gso_max_size 65536 gso_max_segs 65535 parentbus usb parentdev 1-4:1.0
5: can_right: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 10
    link/can  promiscuity 0 minmtu 0 maxmtu 0
    can state ERROR-ACTIVE restart-ms 0
	  bitrate 1000000 sample-point 0.750
	  tq 62 prop-seg 5 phase-seg1 6 phase-seg2 4 sjw 2
	  gs_usb: tseg1 1..16 tseg2 1..8 sjw 1..4 brp 1..1024 brp-inc 1
	  clock 48000000 numtxqueues 1 numrxqueues 1 gso_max_size 65536 gso_max_segs 65535 parentbus usb parentdev 1-12:1.0
6: can_left: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 10
    link/can  promiscuity 0 minmtu 0 maxmtu 0
    can state ERROR-ACTIVE restart-ms 0
	  bitrate 1000000 sample-point 0.750
	  tq 62 prop-seg 5 phase-seg1 6 phase-seg2 4 sjw 2
	  gs_usb: tseg1 1..16 tseg2 1..8 sjw 1..4 brp 1..1024 brp-inc 1
	  clock 48000000 numtxqueues 1 numrxqueues 1 gso_max_size 65536 gso_max_segs 65535 parentbus usb parentdev 1-13:1.0
```

**Note on a transient:** the very first `ip -details -brief` immediately after the script showed `can0 DOWN <NO-CARRIER,...>`. Re-reading a few seconds later showed `UP` with `carrier=1`. This was a settle race during the down→rename→up sequence, not a fault. All three buses are UP and were stable for the rest of the session.

`/sys/class/net/*/carrier` = `1` for all three; `operstate` = `up`.

---

## 2. Arm (Piper) — ✅ WORKS

### 2.1 Passive proof the arms are alive (zero bytes transmitted)

`candump` on both arm buses, 6 s each:

```
=== can_left (6s): 14494 frames ===
   1197 2A8     1197 2A7     1197 2A6     1197 2A5
   1197 2A4     1197 2A3     1197 2A2     1197 2A1     <- 0x2A1..0x2A8 = arm feedback, ~200 Hz each
    599 256      599 255      599 254      599 253      599 252      599 251
    239 1C1      120 266      120 265      120 263      120 262      120 261
raw:
 (000.000000)  can_left  2A1   [8]  01 00 01 00 01 00 00 00
 (000.000104)  can_left  2A2   [8]  00 00 4F 7C FF FF 39 47
 (000.000130)  can_left  2A3   [8]  00 02 C0 26 00 01 C8 2D
 (000.000160)  can_left  2A4   [8]  00 00 D5 E5 00 00 CD 8E
 (000.000083)  can_left  2A5   [8]  FF FF 4A D6 FF FF FF 27
 (000.000104)  can_left  2A6   [8]  00 00 01 27 FF FF 7D 99

=== can_right (6s): 14524 frames ===  (same 8 IDs, 1200 each)
=== can0 chassis (6s): 2093 frames ===
    300 313   300 311   295 251   294 252   294 221   287 241
     60 371    60 261    60 240    54 262    30 361    30 211    29 362
```

CAN ID map confirmed from the installed SDK (`piper_sdk/piper_msgs/msg_v2/can_id.py`):
`0x2A1`=status · `0x2A2/3/4`=end pose · `0x2A5`=joint 1‑2 · `0x2A6`=joint 3‑4 · `0x2A7`=joint 5‑6 · `0x2A8`=gripper.

### 2.2 Read-only SDK query

Exact command:

```bash
/home/agilex/miniforge3/envs/aloha/bin/python /tmp/hw_arm_read.py
# script body:
#   from piper_sdk import C_PiperInterface_V3
#   p = C_PiperInterface_V3("can_left")
#   p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
#   ... only Get*() calls ...
```

`piper_init=False` is important: the SDK's `PiperInit()` (guarded at `piper_interface.py:627`) would otherwise send `SearchAllMotorMaxAngleSpd`, `SearchAllMotorMaxAccLimit` and `SearchPiperFirmwareVersion`. With `piper_init=False` **none of those are sent**.

Raw output — **can_left**:

```
GetArmJointMsgs: Hz:200.0
Joint 1:-46378  Joint 2:-217  Joint 3:295  Joint 4:-33383  Joint 5:24000  Joint 6:66834
  -> -46.378°, -0.217°, 0.295°, -33.383°, 24.000°, 66.834°

GetArmStatus: Hz:200.0
Control Mode: CAN_CTRL(0x1)
Arm Status: NORMAL(0x0)
Mode Feed: MOVE_J(0x1)
Teach Status: DISABLED(0x0)
Motion Status: REACH_TARGET_POS_FAILED(0x1)
Trajectory Num: 0
Error Code: 0
Error Status:
 Joint 1..6 Angle Limit Status: False (all)
 Joint 1..6 Communication Status: False (all)

GetArmEnableStatus: [True, True, True, True, True, True]
GetArmGripperMsgs: Hz:200.0  grippers_angle: 6300 (6.300°)  grippers_effort: 56 (0.056)
  driver_enable_status: True   (all fault flags False)
GetArmEndPoseMsgs: Hz:200.0
 X_axis : 20348   Y_axis : -50873  Z_axis : 180262
 RX_axis : 116781  RY_axis : 54757  RZ_axis : 52622
GetCanFps: 2340.0
GetCurrentInterfaceVersion: INTERFACE_V3 (0x3)
GetCurrentSDKVersion: PIPER_SDK_VERSION_1_0_0 (1.0.0)
GetCurrentProtocolVersion: ARM_PROROCOL_V3 (0x3)
```

Raw output — **can_right**:

```
GetArmJointMsgs: Hz:200.0
Joint 1:42750  Joint 2:210  Joint 3:-77  Joint 4:538  Joint 5:2591  Joint 6:-31706
  -> 42.750°, 0.210°, -0.077°, 0.538°, 2.591°, -31.706°

GetArmStatus: identical flags to can_left (CAN_CTRL, NORMAL, MOVE_J, DISABLED, REACH_TARGET_POS_FAILED, Error Code 0)
GetArmEnableStatus: [True, True, True, True, True, True]
GetArmGripperMsgs: grippers_angle: -1500 (-1.500°)  grippers_effort: -403
GetArmEndPoseMsgs: X_axis : 41570  Y_axis : 38480  Z_axis : 209410
                    RX_axis : -85609  RY_axis : 58756  RZ_axis : -43469
```

### 2.3 Stationarity check (safety)

Sampled every 2.5 s for 10 s on both arms:

```
STATIONARITY CHECK: can_left
  t= 0.0s joints(0.001deg)=(-46378, -217, 295, -33383, 24000, 66834) gripper=6300
  t= 2.5s joints(0.001deg)=(-46378, -217, 295, -33383, 24000, 66834) gripper=6300  delta=(0, 0, 0, 0, 0, 0)
  t= 5.0s ... delta=(0, 0, 0, 0, 0, 0)
  t= 7.5s ... delta=(0, 0, 0, 0, 0, 0)
  t=10.0s ... delta=(0, 0, 0, 0, 0, 0)
STATIONARITY CHECK: can_right
  t= 0.0s joints(0.001deg)=(42750, 210, -77, 538, 2591, -31706) gripper=-1500
  t= 2.5s..10.0s  delta=(0, 0, 0, 0, 0, 0) every sample
```

**Both arms are rock-steady — zero counts of drift over 10 s.** They are holding position under power (all 6 joints report `enabled=True`).

### 2.4 Firmware version

`GetPiperFirmwareVersion()` returns `-0x4AF` (= `-1199`) unless the SDK has *sent* the version query — the value is assembled from the arm's reply, it is not passively broadcast. I sent the query frame only:

> CAN ID **`0x4AF`**, data `01 00 00 00 00 00 00 00` (source: `piper_sdk/interface/piper_interface.py`, `SearchPiperFirmwareVersion`). This is a **query**, not a motion command.

Result, both arms:

```
attempt 1: 'S-V1\x00\x00\x00\x00'
attempt 2: 'S-V1\x00\x00\x00\x00'
attempt 3: 'S-V1.9-1'          <- firmware version: S-V1.9-1
  joint delta after all queries: (0, 0, 0, 0, 0, 0)   <- no motion
```

The string assembles over several replies; the first query returns a partial string. **Firmware = `S-V1.9-1` on both arms**, which is consistent with the SDK's V3 interface (`V3` ↔ firmware V1.8-8+).

### 2.5 Transmit accounting for the arm (independent proof)

Kernel TX counters, not my own claim:

```
                 before   after first query   after 3-query retry
can_left  tx       0            1                    4
can_right tx       0            1                    4
```

So exactly **4 frames per arm bus** were ever transmitted in this whole session, all of them the firmware query. Every other operation read only (`rx` climbed by ~2.7 M frames per bus).

### 2.6 Observation worth flagging (not a fault)

Both arms report `Motion Status: REACH_TARGET_POS_FAILED(0x1)` and `Mode Feed: MOVE_J(0x1)` while sitting perfectly still, with `Arm Status: NORMAL` and `Error Code: 0`. This is a *status flag* the arm is holding, most likely left over from a previous session/target. It is **not** causing motion (proved in §2.3). Worth knowing if anyone later commands the arm: the controller believes its last target was not reached.

---

## 3. Cameras (Orbbec) — ⚠️ PARTIALLY WORKS: **2 of the 3 configured cameras are present**

### 3.1 The headline finding

Recon recorded three cameras with serials `CC1W56101WC` (front), `CC1V56100JZ` (left), `CC1V56100DR` (right). Live enumeration says **only two are physically connected**:

```
$ ros2 run orbbec_camera list_devices_node
[info][DeviceManager.cpp:15] Current found device(s): (2)
[info][DeviceManager.cpp:24] 	- Name: DaBai DC1, PID: 0x0657, SN/ID: , Connection: USB2.0
[info][DeviceManager.cpp:24] 	- Name: DaBai DC1, PID: 0x0657, SN/ID: , Connection: USB2.0
[INFO] [list_device_node]: serial: CC1V56100DR     usb port: 1-5.4
[INFO] [list_device_node]: serial: CC1V56100JZ     usb port: 1-6.4
```

Starting a node for the front serial fails and retries forever:

```
[camera_f.camera_f]: Connecting to device with serial number: CC1W56101WC
[camera_f.camera_f]: [WARN] Device with serial number CC1W56101WC not found
```

USB topology confirms exactly two physical cameras, each of which appears as **two** USB devices (a UVC colour interface + a vendor-specific depth interface behind the camera's own internal hub):

```
1-5.2  2bc5:0557  "Dabai DC1" (Sonix)          serial=CC1V56100DR   uvcvideo  -> /dev/video0,1
1-5.4  2bc5:0657  "ORBBEC Depth Sensor"        serial=              (no driver) <- OrbbecSDK/libobsensor
1-6.2  2bc5:0557  "Dabai DC1" (Sonix)          serial=CC1V56100JZ   uvcvideo  -> /dev/video2,3
1-6.4  2bc5:0657  "ORBBEC Depth Sensor"        serial=              (no driver) <- OrbbecSDK/libobsensor
```

The OrbbecSDK reads the camera serial from the `0x0657` depth head; it matches the UVC sibling's sysfs serial. **There is no third camera on the bus.** `CC1W56101WC` is either unplugged, powered off, or dead — that needs a physical check, I cannot distinguish those from software.

Permissions are *not* the problem: `agilex` is not in the `video` group, but `/dev/video0` carries an ACL `user:agilex:rw-` and the udev rules `99-obsensor-libusb.rules` / `56-orbbec-usb.rules` are installed.

### 3.2 Independent capture path A — raw UVC via ffmpeg (hardware-level)

This bypasses ROS and the OrbbecSDK entirely, proving the camera hardware + kernel driver work:

```bash
ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x720 -i /dev/video0 -frames:v 10 -update 1 -y /tmp/hwshots/v0.jpg
# /dev/video0 -> 1-5.2 -> serial CC1V56100DR
# /dev/video2 -> 1-6.2 -> serial CC1V56100JZ
```

Formats offered (both cameras identical):

```
Compressed: mjpeg : Motion-JPEG : 640x480 1632x1224 1280x960 1280x720 640x360 320x240 320x180 640x480
Raw       : yuyv422 : YUYV 4:2:2 : 640x480 1632x1224 1280x960 1280x720 640x360 320x240 320x180 640x480
```

Result:

```
/tmp/hwshots/v0.jpg: JPEG image data, baseline, precision 8, 1280x720, components 3   (36350 bytes)
/tmp/hwshots/v2.jpg: JPEG image data, baseline, precision 8, 1280x720, components 3   (25274 bytes)
```

### 3.3 Independent capture path B — full ROS/OrbbecSDK pipeline, including depth

```bash
source /home/agilex/camera_ros/install/setup.bash
ros2 launch orbbec_camera dabai.launch.py camera_name:=camera_r serial_number:=CC1V56100DR \
    enable_point_cloud:=false enable_colored_point_cloud:=false
```

The driver connects (this is the *vendor SDK* path, so the depth pipeline is genuinely working):

```
[camera_r.camera_r]: SDK version: 1.10.35
[camera_r.camera_r]: device unique id: 1-5.4-12
[camera_r.camera_r]: usb connect type: USB2.0
[camera_r.camera_r]: Start device cost 729 ms
[camera_r.camera_r]: Initialize device cost 497 ms
[camera_r.camera_r]: Publishing static transform from color to depth
```

Topics (per camera namespace):

```
/camera_r/color/image_raw        /camera_r/color/image_raw/compressed
/camera_r/depth/image_raw        /camera_r/depth/image_raw/compressed
/camera_r/color/camera_info      /camera_r/depth/camera_info
/camera_r/depth_to_color         /camera_r/depth_filter_status
/tf  /tf_static
```

Measured rate and frame metadata (captured with a small `rclpy` + `cv_bridge` node):

```
ros2 topic hz /camera_r/color/image_raw
  average rate: 29.948 / 28.435 / 27.929  window: 31 / 58 / 85      -> ~28-30 fps

camera_r COLOR: 640x480 encoding=rgb8  step=1920 frame_id=camera_r_color_optical_frame
camera_r DEPTH: 640x400 encoding=16UC1 step=1280 frame_id=camera_r_depth_optical_frame
                depth raw min=0 max=3195   (mm -> up to 3.195 m)

camera_l COLOR: 640x480 encoding=rgb8  step=1920 frame_id=camera_l_color_optical_frame
camera_l DEPTH: 640x400 encoding=16UC1  depth raw min=0 max=4699  (mm -> up to 4.699 m)
```

Both cameras were driven through this full pipeline successfully (`camera_r` = `CC1V56100DR`, `camera_l` = `CC1V56100JZ`).

### 3.4 Image / video files delivered to the Mac

All in `<home>/Documents/deepseek-harness/default-workspace/agilex-setup/evidence/`:

| File | Size | What it is |
|---|---|---|
| `camera-right-CC1V56100DR.jpg` | 36 350 B | ffmpeg UVC still, **1280×720** MJPEG |
| `camera-right-CC1V56100DR-1632x1224.jpg` | 86 764 B | ffmpeg UVC still, **1632×1224** (max res) |
| `camera-right-CC1V56100DR-ros-640x480.jpg` | 103 284 B | ROS pipeline, **640×480 rgb8** |
| `camera-right-CC1V56100DR-depth-normalized.png` | 30 860 B | ROS depth, **640×400 16UC1**, 0–3195 mm, normalised for viewing |
| `camera-right-CC1V56100DR.mp4` | 171 978 B | **6.0 s, 1280×720, h264, 180 frames (30 fps)** |
| `camera-left-CC1V56100JZ.jpg` | 25 274 B | ffmpeg UVC still, **1280×720** |
| `camera-left-CC1V56100JZ-1632x1224.jpg` | 62 265 B | ffmpeg UVC still, **1632×1224** |
| `camera-left-CC1V56100JZ-ros-640x480.jpg` | 68 266 B | ROS pipeline, **640×480 rgb8** |
| `camera-left-CC1V56100JZ-depth-normalized.png` | 34 785 B | ROS depth, **640×400 16UC1**, 0–4699 mm |
| `camera-left-CC1V56100JZ.mp4` | 13 501 B | **6.0 s, 1280×720, h264, 180 frames (30 fps)** |

`ffprobe` on the video: `codec_name=h264 width=1280 height=720 duration=6.000000 nb_frames=180`.

Content was visually verified (not just "a file exists"):
* **`CC1V56100DR`** looks across a desk — the **Piper arm itself is clearly visible** with its carbon links and "Piper" branding, plus a monitor, a PC and cabling.
* **`CC1V56100JZ`** looks down at the floor — chair legs, a power supply brick and cables. Depth map matches the same scene.

Both MP4s are well under the 20 MB budget. The left video is small (13 KB) simply because the scene is static.

---

## 4. Chassis — ✅ WORKS (state read passively, zero transmission)

### 4.1 What I did instead of launching `tracer_base`

The brief suggested `ros2 launch tracer_base tracer_base.launch.py`. I read the source first, and it **transmits** on connect:

```cpp
// ~/agilex_ws/src/tracer_ros2/tracer_base/src/tracer_base_ros.cpp:103
if (robot_->Connect(port_name_)) {
  robot_->EnableCommandedMode();      // <-- sends a control-mode config frame (0x421)
}
```

`EnableCommandedMode()` puts the chassis into CAN-commanded mode, i.e. it **takes the chassis out of whatever mode it is in** (and would stop it responding to the RC handset). That is a state change on real hardware which is **not required** to answer "does the chassis work", so I did not do it, and read the chassis state passively instead — which the brief's safety section explicitly prefers ("Prove it works by reading state only … with the robot stationary").

### 4.2 Passive capture and decode

`candump -t d can0` for 8 s → 3134 frames → decoded with a script built from the vendor's own `agilex_msg_parser_v2.c` rules (all multi-byte fields big-endian on this host; `USE_LITTLE_ENDIAN` is defined in `agilex_protocol_v1.h`).

CAN IDs seen on `can0` in 8 s:

```
0x211:45  0x221:441  0x240:90  0x241:430  0x251:440  0x252:440  0x261:90
0x262:81  0x311:450  0x313:449  0x361:45  0x362:44  0x371:89
```

That is exactly the AgileX **V2** state group for a **two-motor differential chassis** (`0x251`/`0x252` = the two drive motors, `0x261`/`0x262` = their low-speed/driver states, `0x311` = odometry, `0x361`/`0x362` = BMS, `0x241` = RC state, `0x211`/`0x221` = system/motion state) — consistent with a Tracer.

Decoded output:

```
0x211 SYSTEM STATE  raw=['0x0','0x0','0xa','0xc8','0x0','0x0','0x0','0x0']
   vehicle_state   = 0 (0x00)
   control_mode    = 0 (0x00)      [0 = standby, 1 = CAN command]
   battery_voltage = 276.0 V       (raw int16 BE = 2760, vendor scale x0.1)   <-- see 4.3
   error_code      = 0 (0x0000)
   battery over 45 frames: min=276.0 max=277.0 V

0x221 MOTION STATE  raw = all zero
   linear_velocity  = 0.000 m/s
   angular_velocity = 0.000 rad/s
   lateral_velocity = 0.000 m/s
   steering_angle   = 0.000 rad

0x311 ODOMETRY  raw = all zero  (n=450 frames)
   left_wheel  = 0
   right_wheel = 0
   delta over capture: left=0 right=0     <-- wheels did not turn at all

0x251 ACTUATOR1_HS (motor 0)  raw=['0x0','0x0','0x0','0xc','0xff','0xff','0xff','0xe7']
   rpm=0   current=1.20 A   pulse_count=-25
0x252 ACTUATOR2_HS (motor 1)  raw=['0x0','0x0','0x0','0x8','0xff','0xff','0xff','0xec']
   rpm=0   current=0.80 A   pulse_count=-20

0x361 BMS BASIC  raw=['0x64','0x63','0xa','0xd1','0x0','0x0','0x1','0x34']
   battery_soc = 100 %   battery_soh = 99 %
   voltage     = 276.9 V (raw 2769, scale x0.1)
   current     = 0.0 A
   temperature = 30.8 °C

0x241 RC STATE  raw=['0xaa','0x0','0x0','0x0','0x0','0x0','0x0','0xf8']
   sws=0xAA (swa=0x02 = UP)   sticks all 0   var_a=0
```

**Stationarity is proved independently by the chassis itself:** odometry counts and all three velocity fields are exactly **0** across the whole capture, and both drive motors report **rpm = 0**.

Transmit accounting:

```
can0 tx BEFORE = 0
can0 tx AFTER  = 0        <- I never transmitted a single frame on the chassis bus
```

### 4.3 Open question — battery voltage scale

Both the `0x211` system state and the `0x361` BMS report the **same** raw number (`2760` / `2769`), and the vendor parser in `agilex_msg_parser_v2.c` multiplies by **0.1** in both places — giving **~276.9 V**. That is not a plausible pack voltage for a mobile base.

If the field were instead in **0.01 V** units it reads **27.69 V**, which is a textbook 24 V-class pack sitting at 100 % SOC — and the BMS's *other* scaled fields (temperature 30.8 °C at ×0.1, SOC 100 %, SOH 99 %, current 0.0 A) are all entirely sane at ×0.1.

So: **raw value certain (2769), true engineering value unresolved.** I did not send a version-request frame (`0x411`) to the chassis to identify its exact model/firmware, because that would mean transmitting on a bus the brief told me to treat as read-only. If the Lead wants it nailed down, one `0x411` query would likely resolve the model, and the pack label would settle the scale.

Everything else about the chassis is unambiguous: it is alive, it is healthy (`error_code = 0`), it is in **standby** (`control_mode = 0`), its BMS is happy, and it is **not moving**.

---

## 5. Session transmit ledger (full disclosure)

| Bus | Frames transmitted | What |
|---|---|---|
| `can0` (chassis) | **0** | nothing, ever |
| `can_left` | **4** | firmware version query `0x4AF` (`01 00 00 00 00 00 00 00`), ×4 attempts |
| `can_right` | **4** | same |
| USB cameras | 0 commands | read-only capture (ffmpeg / OrbbecSDK) |

No motion command of any kind was issued to any actuator. No `cmd_vel` was published. `tracer_base` was not launched.

## 6. State I left behind on the robot

* **CAN interfaces are UP** — `can0` @500 k, `can_left`/`can_right` @1 M. This is a change from arrival (all were DOWN) and is the documented "ready to use" state. Bringing them down again would have been a second unrequested state change; I left them enabled. Say the word if you want them down.
* **All ROS nodes I started are stopped** (verified: no `component_container`, no `ros2 launch` processes remain).
* Captures remain in the robot's `/tmp/hwshots/` (harmless, ~750 KB); helper scripts in `/tmp/hw_*.py`, `/tmp/hw_*.sh`, `/tmp/hw_can*_dump.txt`.
* No file outside `/tmp` was created or modified on the robot. No `colcon build`, no config edits, no `~/.bashrc`/`~/.profile`/systemd/VNC changes.

## 7. Remaining unknowns

1. **Where is the third camera?** `CC1W56101WC` ("front") is absent from USB. Unplugged / unpowered / dead — needs eyes on the hardware.
2. **Chassis battery voltage scale** (§4.3) — raw 2769, vendor scale says 276.9 V, physics suggests 27.69 V.
3. **Chassis model/firmware** — would need a `0x411` version request (transmit), deliberately not sent.
4. **Which camera is physically "front/left/right"** — I have named files by **serial number**, not by mounting position, because recon's mapping (`CC1W56101WC`=front) does not survive contact with reality. `CC1V56100DR` looks over a desk at the arm; `CC1V56100JZ` looks down at the floor.
5. **Arm actuation was never commanded** — I can prove the arm *communicates, senses and holds position under power*, but I have not proven it can *move on command*. That test is the Lead's call (see §2.6 for the `REACH_TARGET_POS_FAILED` flag before attempting it).
6. **Depth point clouds / IR streams** were not exercised (point cloud publishing was disabled for these runs).

## 8. Reproduce

```bash
cd <home>/Documents/deepseek-harness/default-workspace/agilex-setup

# CAN
bin/rsudo.sh -c 'cd /home/agilex/piper_ros && bash ./can_config.sh'
bin/rrun.sh  -c 'ip -details -brief link show type can'

# Arm (read-only; piper_init=False)
bin/rrun.sh -f <script running /home/agilex/miniforge3/envs/aloha/bin/python /tmp/hw_arm_read.py>

# Cameras — hardware level
bin/rrun.sh -c 'ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x720 -i /dev/video0 -frames:v 10 -update 1 -y /tmp/x.jpg'
# Cameras — ROS level
bin/rrun.sh -c 'source /home/agilex/camera_ros/install/setup.bash && ros2 launch orbbec_camera dabai.launch.py camera_name:=camera_r serial_number:=CC1V56100DR'

# Chassis — passive only
bin/rrun.sh -c 'timeout 8 candump -t d can0 > /tmp/d.txt; python3 /tmp/hw_chassis_decode.py /tmp/d.txt'
```

> **Gotcha for future agents:** `bin/rrun.sh -f` base64-decodes the script and passes it as `bash -c "<script text>"`, so the script text **is** the shell's own argv. Any `pkill -f`/`pgrep -f` pattern that appears literally in the script will match and kill your own shell. Either build the pattern from concatenated fragments, or (cleanest) write the script to a file on the robot first and run `bash /path/to/script.sh`.
