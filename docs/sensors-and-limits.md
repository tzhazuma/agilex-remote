# AgileX Tracer — chassis speed limits & sensor inventory

**Task:** task-11 · **Author:** teammate `desktop` · **Date:** 2026-10-13 ~16:20 CST
**Robot:** `agilex@agilex.local` → resolved to **<robot-ip>** (DHCP moved again; it was <robot-ip> on Oct 10 — `agilex.local` mDNS handled it)
**Method:** strictly read-only. Passive `candump`/`tcpdump`, `lsusb`, sysfs, `ros2 topic echo --once`, PDF text extraction. Nothing installed, no config changed, no `cmd_vel` published, no fault-clear sent.

---

## 1. Answers

### (A) Maximum speed

| | Value | Status |
|---|---|---|
| **Max linear speed (hardware)** | **≤ 2.3 m/s** (unloaded) | **DOCUMENTED** — vendor manual, `TRACER_UserManual_v1.2.pdf` §1.2 性能参数 table |
| **Max angular speed (hardware)** | **not documented anywhere** | **UNKNOWN** — no mechanical angular limit exists in the code or the manual |
| Angular reference value | 0.7853 rad/s | **DERIVED** — this is the v1 protocol's "100%" reference, not a stated hardware limit |
| Protocol-v2 wire ceiling | ±32.767 m/s / ±32.767 rad/s | **DERIVED** from CAN scaling (int16 × 1000) — a protocol bound, *not* a mechanical one |
| Code-level clamp (v2, in use) | **none** | **VERIFIED** — nothing clamps |

> **The number to display as "hardware maximum" is 2.3 m/s.** There is **no angular equivalent** — if the UI must show one, show `0.7853 rad/s` but label it as the protocol reference, not a hardware rating.

### (B) Sensor inventory — 6 confirmed sensors + 2 present-without-driver

| # | Sensor | Interface | Vendor:product | Driver installed? | Hardware present? |
|---|---|---|---|---|---|
| 1 | **Hipnuc IMU** | `/dev/ttyUSB0` @115200 (CP2102N USB-UART) | `10c4:ea60` | ✅ `hipnuc_imu` built | ✅ **present** (port enumerated) |
| 2 | **Orbbec camera ×2** — colour (UVC) | `/dev/video0-1`, `/dev/video2-3` | `2bc5:0557` "Dabai DC1" | ⚠️ **UVC only — no ROS driver** | ✅ **present** (2 units) |
| 3 | **Orbbec depth ×2** | vendor-specific USB, no driver bound | `2bc5:0657` "ORBBEC Depth Sensor" | ❌ **no driver** (OpenNI libs only) | ✅ **present** (2 units) |
| 4 | **Chassis BMS** | CAN `can0`, frames `0x361`/`0x362` | n/a (internal) | ✅ via `tracer_base` | ✅ **present & reporting** |
| 5 | **Chassis motors ×2** | CAN `can0`, frames `0x251/0x252/0x261/0x262` | n/a (internal) | ✅ via `tracer_base` | ✅ **present** — but see §4 fault |
| 6 | **Logitech K400 Plus** (kbd/mouse) | USB HID | `046d:c52b` | n/a (kernel) | ✅ present |
| — | **2× Piper arm CAN buses** | `can_left`, `can_right` @1 Mbps | `1d50:606f` candleLight ×2 | ✅ `piper_ros` sourced | ✅ **both streaming** `0x2A1–0x2A5` |
| — | **RealSense** | — | — | ✅ `ros-humble-realsense2-camera` (apt) | ❌ **NO hardware** |
| — | **LiDAR (Livox / RoboSense / YDLidar)** | — | — | ✅ livox + rslidar built; ❌ ydlidar not built | ❌ **NO hardware** |
| — | Bluetooth (Atheros AR3012) | USB | `0930:021c` | kernel | ✅ present |
| — | Built-in mic (Intel ALC269VB) | HDA | n/a | kernel/ALSA | ✅ present |

**Count: 6 sensor subsystems confirmed present** (IMU, 2× Orbbec camera, BMS, motors, HID input), **plus** 2 CAN adapters driving the arm buses. **2 things have drivers but no hardware** (RealSense, LiDAR). **2 things have hardware but no ROS driver** (the Orbbec depth interfaces, and the Orbbec colour streams beyond raw UVC).

---

## 2. (A) Chassis maximum speed — full evidence chain

### 2.1 The documented number (authoritative)

The vendor ships the Tracer manual inside the SDK at
`~/agilex_ws/src/ugv_sdk/docs/protocol_v2/TRACER_UserManual_v1.2.pdf`.
`pdftotext -layout` extracts §1.2 性能参数 (performance parameters):

```
1.2 性能参数
     参数类型                   项目                                指标
                      长 x 宽 x 高（mm ）                      700 X 500X 163
                        轴距 (mm)                                350
                      车体重量（Kg）                                25~30
                         电池类型                            锂电池 24V 30aH
     机械参数                   电机                           直流无刷 2 X 150W
                         驱动形式                               独立驱动
                            悬架                             非独立悬架
                            转向                              差速转向
                         安全装备                            伺服刹车/防撞管
                      空载最高车速（m/s）                             ≤2.3      <-- line 160
                       最小转弯半径                              可原地转弯
    性能参数指针
                     最小离地间隙（mm）                                36
                       最大爬坡能力                                 ≤10
```

- **`空载最高车速（m/s）` = "maximum vehicle speed, unloaded" = `≤2.3`**
- Extracted text **line 160**; PDF is `~/agilex_ws/src/ugv_sdk/docs/protocol_v2/TRACER_UserManual_v1.2.pdf`, section **1.2 性能参数**.
- Other useful specs from the same table: mass 25–30 kg, payload limit **100 kg** (manual line 51), 2 × 150 W brushless DC motors, differential steering (can turn in place), ground clearance 36 mm, max climb ≤10°, battery 24 V 30 Ah, comms CAN/RS232.

**This is the only real hardware speed rating in the entire repo, and it is a PDF — not code.** That is why a code search alone would have missed it.

### 2.2 The code contains NO Tracer limit

Searched `ugv_sdk/include`, `ugv_sdk/src`, and all of `tracer_ros2`:

```
$ grep -rniE 'max[_ ]?(linear|angular|speed|vel)|MAX_(LINEAR|ANGULAR|SPEED)|max_speed' ~/agilex_ws/src/ugv_sdk/
include/ugv_sdk/details/protocol_v1/robot_limits.hpp:15:  static constexpr double max_linear = 1.5;  // m/s
include/ugv_sdk/details/protocol_v1/robot_limits.hpp:17:  static constexpr double max_angular = 0.5235;  // rad/s
include/ugv_sdk/details/protocol_v1/robot_limits.hpp:25:  static constexpr double max_linear = 3.0;  // m/s
include/ugv_sdk/details/protocol_v1/robot_limits.hpp:27:  static constexpr double max_angular = 2.5235;  // rad/s
include/ugv_sdk/details/protocol_v1/robot_limits.hpp:35:  static constexpr double max_linear = 3.0;  // m/s
include/ugv_sdk/details/protocol_v1/robot_limits.hpp:37:  static constexpr double max_angular = 2.5235;  // rad/s
include/ugv_sdk/details/protocol_v1/robot_limits.hpp:45:  static constexpr double max_linear = 1.5;  // m/s
include/ugv_sdk/details/protocol_v1/robot_limits.hpp:47:  static constexpr double max_angular = 0.444;  // rad
```

`robot_limits.hpp` defines exactly four structs — **`ScoutV2Limits`, `ScoutMiniLimits`, `BunkerLimits`, `HunterV1Limits`. There is no `TracerLimits`.**

And `tracer_ros2/tracer_base/` contains **zero** limit/clamp code:

```
$ grep -rniE 'max_linear|max_angular|max_speed|clamp|limit|1\.5|2\.0|0\.5' ~/agilex_ws/src/tracer_ros2/tracer_base/
(no output)
```

### 2.3 The BunkerLimits trap (important)

```
include/ugv_sdk/details/protocol_v1/protocol_v1_parser.hpp:79:
using TracerProtocolV1Parser = ProtocolV1Parser<BunkerLimits>;
```

The Tracer's **V1** parser reuses **`BunkerLimits`** → `max_linear = 3.0 m/s`, `max_angular = 2.5235 rad/s` (`robot_limits.hpp:35,37`).

Two consequences worth telling the user:
1. **3.0 m/s is above the 2.3 m/s hardware maximum.** If the robot ever runs in V1 mode, the SDK's clamp would happily pass a command 30 % beyond what the chassis can do. The clamp is not a safety net.
2. `ProtocolV1Parser::EncodeMessage` rescales to a percentage: `linear / max_linear * 100.0` (`protocol_v1_parser.hpp:49`). So in V1 the wire value is a percentage of **3.0**, not of 2.3 — the scaling reference is simply wrong for a Tracer.

### 2.4 Protocol V2 (the one actually in use) has no clamp at all

`ProtocolV2Parser` never clamps. The CAN scaling is a pure ×1000 conversion, in `src/protocol_v2/agilex_msg_parser_v2.c`:

```c
// encode motion command (frame 0x111) — lines 386-397
int16_t linear_cmd  = (int16_t)(msg->body.motion_command_msg.linear_velocity  * 1000);
int16_t angular_cmd = (int16_t)(msg->body.motion_command_msg.angular_velocity * 1000);
frame.linear_velocity.high_byte  = (uint8_t)(linear_cmd >> 8);
frame.linear_velocity.low_byte   = (uint8_t)(linear_cmd & 0x00ff);
...
// decode motion state (frame 0x221) — lines 81-88
msg->body.motion_state_msg.linear_velocity  = (int16_t)(...) / 1000.0;
msg->body.motion_state_msg.angular_velocity = (int16_t)(...) / 1000.0;
```

- `linear_velocity` — **m/s × 1000 → int16** (i.e. mm/s), **file:line `agilex_msg_parser_v2.c:387`** (encode), `:84` (decode)
- `angular_velocity` — **rad/s × 1000 → int16** (i.e. 0.001 rad/s), **file:line `agilex_msg_parser_v2.c:389`** (encode), `:88` (decode)

**Derived protocol ceiling (not a hardware limit):** int16 max = 32767 → **±32.767 m/s and ±32.767 rad/s**. Anything within that range is transmitted verbatim; the chassis firmware is the only thing that would reject or saturate it.

### 2.5 The robot is confirmed on protocol V2

Passive `candump can0` shows V2 frame IDs only:

```
$ timeout 13 candump can0 | awk '{print $2}' | sort | uniq -c | sort -rn
    649 311     <- CAN_MSG_ODOMETRY_ID          (0x311)
    641 313     <- (not defined in this SDK revision)
    637 221     <- CAN_MSG_MOTION_STATE_ID       (0x221)
    636 252     <- CAN_MSG_ACTUATOR2_HS_STATE_ID (0x252)
    636 251     <- CAN_MSG_ACTUATOR1_HS_STATE_ID (0x251)
    624 241     <- CAN_MSG_RC_STATE_ID           (0x241)
    260 111     <- CAN_MSG_MOTION_COMMAND_ID     (0x111)
    130 371     <- (not defined in this SDK revision)
    130 261     <- CAN_MSG_ACTUATOR1_LS_STATE_ID (0x261)
    130 240     <- (not defined in this SDK revision)
    117 262     <- CAN_MSG_ACTUATOR2_LS_STATE_ID (0x262)
     65 361     <- CAN_MSG_BMS_BASIC_ID          (0x361)
     65 211     <- CAN_MSG_SYSTEM_STATE_ID       (0x211)
     63 362     <- CAN_MSG_BMS_EXTENDED_STATE_ID (0x362)

 can0@500000   364   49140  23296   9%
```

`0x111/0x211/0x221/0x241/0x251/0x252/0x261/0x262/0x311` are all V2 IDs from `agilex_protocol_v2.h` (lines 42–99). So `tracer_base_ros.cpp`'s `ProtocolDetector` (`tracer_base_ros.cpp:67-71`, prints `Detected protocol: AGX_V2`) is running the **V2** path → **no clamping anywhere in the chain.**

`0x240`, `0x313`, `0x371` are **not defined** in this SDK's `agilex_protocol_v2.h` — the chassis firmware emits frames newer than the vendored SDK. Harmless, but it means the SDK is behind the firmware.

### 2.6 What this means for the controller

- **Display: 2.3 m/s** as the hardware maximum (source: vendor manual).
- **The clamp must live in the controller.** Nothing downstream enforces it — not `tracer_base`, not `ugv_sdk`'s V2 path, and not the V1 path either (it clamps at the wrong value, 3.0).
- **Default to whatever you like below 2.3** — there is no lower bound imposed by the code either. A sensible default for a 25–30 kg tracked chassis indoors is in the 0.3–0.6 m/s range, but that is an operational choice, not a hardware constraint.
- **For angular:** no hardware figure exists. If you must ship a number, `0.7853 rad/s` is the only one the vendor ever wrote down (as the v1 percentage reference), so treat it as a soft ceiling and verify empirically. **Do not present it as a datasheet rating.**
- Consider adding a rate/acceleration limit too — the manual notes 伺服刹车 (servo brake) and a 100 kg payload rating, but gives no acceleration spec.

---

## 3. (B) Sensor inventory — evidence

### 3.1 USB devices (full `lsusb`)

```
$ lsusb
Bus 002 Device 003: ID 0bda:0411 Realtek Semiconductor Corp. Hub
Bus 002 Device 002: ID 0bda:0411 Realtek Semiconductor Corp. Hub
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 001 Device 010: ID 1a40:0101 Terminus Technology Inc. Hub
Bus 001 Device 014: ID 2bc5:0657 Orbbec 3D Technology International, Inc ORBBEC Depth Sensor
Bus 001 Device 011: ID 2bc5:0557 Orbbec 3D Technology International, Inc Dabai DC1
Bus 001 Device 007: ID 05e3:0610 Genesys Logic, Inc. Hub
Bus 001 Device 012: ID 2bc5:0657 Orbbec 3D Technology International, Inc ORBBEC Depth Sensor
Bus 001 Device 009: ID 2bc5:0557 Orbbec 3D Technology International, Inc Dabai DC1
Bus 001 Device 005: ID 05e3:0610 Genesys Logic, Inc. Hub
Bus 001 Device 004: ID 1d50:606f OpenMoko, Inc. Geschwister Schneider CAN adapter
Bus 001 Device 008: ID 10c4:ea60 Silicon Labs CP210x UART Bridge
Bus 001 Device 006: ID 0bda:5411 Realtek Semiconductor Corp. RTS5411 Hub
Bus 001 Device 003: ID 0bda:5411 Realtek Semiconductor Corp. RTS5411 Hub
Bus 001 Device 017: ID 0930:021c Toshiba Corp. Atheros AR3012 Bluetooth
Bus 001 Device 015: ID 1d50:606f OpenMoko, Inc. Geschwister Schneider CAN adapter
Bus 001 Device 013: ID 1d50:606f OpenMoko, Inc. Geschwister Schneider CAN adapter
Bus 001 Device 002: ID 046d:c52b Logitech, Inc. Unifying Receiver
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
```

Resolved to physical ports via `/sys/bus/usb/devices`:

```
  046d:c52b  USB Receiver                          1-1     <- Logitech K400 Plus (kbd + mouse)
  1d50:606f  candleLight USB to CAN adapter       1-12    <- can_right
  1d50:606f  candleLight USB to CAN adapter       1-13    <- can_left
  0930:021c                                       1-14    <- Atheros AR3012 Bluetooth
  0bda:5411  USB2.1 Hub                           1-2 / 1-2.1
  10c4:ea60  CP2102N USB to UART Bridge           1-2.4   <- /dev/ttyUSB0  (hipnuc IMU)
  1d50:606f  candleLight USB to CAN adapter       1-4     <- can0 (chassis)
  05e3:0610  USB2.0 Hub                           1-5 / 1-6
  2bc5:0557  Dabai DC1                            1-5.2   <- Orbbec camera A, UVC colour
  2bc5:0657  ORBBEC Depth Sensor                  1-5.4   <- Orbbec camera A, depth
  2bc5:0557  Dabai DC1                            1-6.2   <- Orbbec camera B, UVC colour
  2bc5:0657  ORBBEC Depth Sensor                  1-6.4   <- Orbbec camera B, depth
  1a40:0101  USB 2.0 Hub [MTT]                    1-9
  0bda:0411  USB3.2 Hub                           2-2 / 2-2.1
```

**There is no LiDAR, no RealSense, no GPS, no UWB dongle, no USB microphone, and no USB-SSD in `lsusb`.**

### 3.2 Cameras — 2× Orbbec, each a colour + depth pair

`dmesg` is decisive about the pairing:

```
[    4.295135] usb 1-5.4: New USB device found, idVendor=2bc5, idProduct=0657
[    4.372261] usb 1-5.4: Product: ORBBEC Depth Sensor
[    4.372263] usb 1-5.4: Manufacturer: Orbbec(R)
[    4.676138] usb 1-6.4: New USB device found, idVendor=2bc5, idProduct=0657
[    4.753330] usb 1-6.4: Product: ORBBEC Depth Sensor
[    6.536377] usb 1-5.2: Found UVC 1.00 device Dabai DC1 (2bc5:0557)
[    6.540596] usb 1-6.2: Found UVC 1.00 device Dabai DC1 (2bc5:0557)
```

Two physical cameras, each presenting a **UVC colour interface** (`2bc5:0557`, bound to `uvcvideo`) and a **proprietary depth interface** (`2bc5:0657`, no driver bound — `Driver=` empty in `lsusb -t`).

Video nodes, with serials (the two serials prove two distinct units):

```
/dev/v4l/by-id/:
  usb-Sonix_Technology_Co.__Ltd._Dabai_DC1_CC1V56100DR-video-index0 -> ../../video0
  usb-Sonix_Technology_Co.__Ltd._Dabai_DC1_CC1V56100DR-video-index1 -> ../../video1
  usb-Sonix_Technology_Co.__Ltd._Dabai_DC1_CC1V56100JZ-video-index0 -> ../../video2
  usb-Sonix_Technology_Co.__Ltd._Dabai_DC1_CC1V56100JZ-video-index1 -> ../../video3

/dev/v4l/by-path/:
  pci-0000:00:14.0-usb-0:5.2:1.0-video-index0 -> video0     <- camera A
  pci-0000:00:14.0-usb-0:5.2:1.0-video-index1 -> video1
  pci-0000:00:14.0-usb-0:6.2:1.0-video-index0 -> video2     <- camera B
  pci-0000:00:14.0-usb-0:6.2:1.0-video-index1 -> video3
```

`/dev/media0` and `/dev/media1` are the UVC media controllers for those same two paths (`ID_PATH=...usb-0:5.2:1.0` and `...usb-0:6.2:1.0`).

> ⚠️ **Hardware present, ROS driver NOT installed.** There is **no `orbbec_camera` package** in `~/agilex_ws/install/` and none in `/opt/ros/humble/share/`. What exists is only the generic **OpenNI** stack from apt (`libopenni2-0`, `libopenni-dev`, `openni-utils`) — libraries, not a ROS node. The colour streams are reachable as plain V4L2 UVC, but **no `usb_cam` / `v4l2_camera` node is installed either**. So today the cameras cannot be published into ROS without installing a driver.
>
> Also unexplained and worth a look: `dmesg` shows a ROS process touching camera B —
> `[28293.327077] usb 1-6.2: usbfs: process 24597 (component_conta) did not claim interface 1 before use`
> `[28293.355525] usb 1-6.2: No streaming interface found for terminal 4.`
> i.e. a `component_container` tried to stream from it at some point and mis-claimed the interface. I did not identify which launch file did this.

### 3.3 IMU — Hipnuc, present, correctly configured

```
$ ls -l /dev/ttyUSB*
crw-rw---- 1 root dialout 188, 0 10月 13 06:21 /dev/ttyUSB0

$ udevadm info -q property -n /dev/ttyUSB0
ID_VENDOR_ID=10c4
ID_MODEL_ID=ea60
ID_VENDOR=Silicon_Labs
ID_MODEL=CP2102N_USB_to_UART_Bridge_Controller
DEVPATH=/devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2.4/1-2.4:1.0/ttyUSB0/tty/ttyUSB0
```

The driver's **source default is the wrong port**, but the shipped YAML overrides it correctly:

```
src/imu/hipnuc_imu/src/serial_port.cpp:34:  declare_parameter<std::string>("serial_port", "/dev/ttyUSB1");
src/imu/hipnuc_imu/src/serial_port.cpp:35:  declare_parameter<int>("baud_rate", 460800);

config/hipnuc_config.yaml:
    IMU_publisher:
        ros__parameters:
            serial_port: "/dev/ttyUSB0"     <-- matches the enumerated port
            baud_rate: 115200
            frame_id: "base_link"
            imu_topic: "/IMU_data"
```

- Driver installed: `~/agilex_ws/install/hipnuc_imu` ✅ (also `src/imu/hipnuc_imu`)
- Port present: `/dev/ttyUSB0` ✅
- Config matches: ✅
- **Caveat:** `agilex` is **not in the `dialout` group** (`groups` → `adm cdrom sudo dip plugdev lpadmin lxd sambashare`), and `/dev/ttyUSB0` is `root:dialout 0660`. The node will need to run as root, or `agilex` needs `dialout`. **I did not verify that the IMU itself is powered and streaming** — that would require opening the serial port, which I treated as out of scope for a read-only task. The node is **not currently running** (see §3.6).

### 3.4 CAN buses — 3 adapters, all live

```
$ ip -details link show type can
4: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 ... can state ERROR-ACTIVE
	  bitrate 500000 sample-point 0.875 ... parentbus usb parentdev 1-4:1.0
5: can_right: <NOARP,UP,LOWER_UP,ECHO> mtu 16 ... can state ERROR-ACTIVE
	  bitrate 1000000 sample-point 0.750 ... parentbus usb parentdev 1-12:1.0
6: can_left: <NOARP,UP,LOWER_UP,ECHO> mtu 16 ... can state ERROR-PASSIVE
	  bitrate 1000000 sample-point 0.750 ... parentbus usb parentdev 1-13:1.0
```

| Bus | Bitrate | Role | State at first probe | Passive listen |
|---|---|---|---|---|
| `can0` | 500 kbps | **chassis** (Tracer) | ERROR-ACTIVE ✅ | V2 frames at ~9 % bus load |
| `can_right` | 1 Mbps | arm bus | ERROR-ACTIVE ✅ | `0x2A1–0x2A5` streaming |
| `can_left` | 1 Mbps | arm bus | **ERROR-PASSIVE** ⚠️ | `0x2A1–0x2A5` streaming |

`can_left` being ERROR-PASSIVE is a *transient* bus-error state (the node has counted >127 errors); by the time of the passive capture it was receiving normally again, so it recovered on its own. Worth monitoring but not currently broken.

Chassis bus (`can0`) frame census — see §2.5. **Notably absent** from the census: `0x321/0x322/0x323` (chassis IMU accel/gyro/euler), `0x331` (safety bumper), `0x341–0x348` (ultrasonic ×8), `0x351–0x354` (UWB). So **this Tracer has no ultrasonic array, no bumper sensor, no UWB tag, and does not report an onboard IMU over CAN** — despite the protocol defining all of them.

Arm buses carry `0x2A1–0x2A5`, which are **not** AgileX V2 IDs:

```
can_left   2A1 [8] 01 00 01 00 01 00 00 00
can_left   2A2 [8] 00 00 BD EF FF FF F5 1A
can_left   2A3 [8] 00 02 98 BE 00 02 23 8B
can_right  2A1 [8] 01 00 01 00 01 00 00 00
can_right  2A2 [8] 00 00 99 56 00 00 9E C3
```

Two independent 1 Mbps buses named `can_left`/`can_right`, both streaming an identical frame layout — consistent with **two Piper arm controllers**. I could **not** confirm the arm model from the bus alone (see §5).

### 3.5 LiDAR — drivers installed, **no hardware**

This was the most important thing to get right, and the answer is negative.

**Drivers built** (`~/agilex_ws/install/`): `livox_ros_driver2`, `livox_sdk2`, `rslidar_sdk`, `rslidar_msg` ✅
**Not built:** `ydlidar_ros2_driver` ❌ — and `tracer_bringup/launch/open_ydlidar.launch.py` does `get_package_share_directory("ydlidar_ros2_driver")`, so **that launch file will fail outright**.

**Hardware evidence — none.** Three independent checks:

1. **No USB LiDAR.** `lsusb` contains no Livox, RoboSense, or YDLidar device.
2. **The Ethernet port is not talking to a LiDAR.** `enp3s0` is up at 1000 Mb/s with carrier, and has received 4 MB — but a passive `tcpdump` shows the *only* traffic is **spanning-tree BPDUs from a network switch**:

```
$ tcpdump -i enp3s0 -n -e -c 8
16:05:28.686398 0c:11:7f:06:1a:c0 > 01:80:c2:00:00:00, 802.3, length 38:
   LLC, dsap STP (0x42) ... STP 802.1d, Config, Flags [none],
   bridge-id 8000.0c:11:7f:06:1a:c0.8001, length 35
   ... (repeats every 2 s)
```

   `tx_packets` = **0** — the robot has never transmitted a single frame on this interface. `rx_packets` = 67 943 over 22.8 h uptime with a mean frame size of **60 bytes** (4 045 560 / 67 426). That is **not** a point-cloud stream: a RoboSense MSOP packet is 1248 bytes, and 67 943 such packets would be 85 MB, not 4 MB. It is small-frame chatter — consistent with STP hellos plus broadcast noise from the switch.

   Worth noting precisely, because it explains an apparent contradiction: in the 20 s window where I re-read the counter the delta was **0**, yet `tcpdump` saw BPDUs throughout. STP frames go to the reserved multicast `01:80:c2:00:00:00`, which the `igc` NIC **filters in hardware** unless the interface is in promiscuous mode. So the BPDUs are real and continuous but are normally not counted in `rx_packets`; `tcpdump` only sees them because it puts the interface into promiscuous mode (`dmesg`: `enp3s0: entered promiscuous mode`). The counters and the capture are therefore not in conflict.

3. **No LiDAR is publishing.** The live topic list contains no `/rslidar_points`, `/livox/lidar`, or `/scan`.

Also note `enp3s0` has **no IP address configured**, so even if a LiDAR were attached it would be unreachable. The Livox configs expect the host at `192.168.1.5` (`livox_ros_driver2-master/config/*.json` → `"host_ip": "192.168.1.5"`), and the RoboSense config targets an **RSHELIOS** at MSOP 6699 / DIFOP 7788 (`rslidar_sdk/config/config.yaml`). Neither is set up right now.

**Verdict: driver-but-no-hardware, for both Livox and RoboSense. YDLidar has neither.** If a LiDAR is ever attached, it must be an Ethernet type, and the interface needs an IP.

### 3.6 What is actually running right now

```
$ timeout 20 bash -ic 'ros2 node list'      # ROS_DOMAIN_ID=222, ROS_LOCALHOST_ONLY=1
/agilex_remote
/tracer

$ timeout 20 bash -ic 'ros2 topic list'
/cmd_vel
/light_control
/odom
/parameter_events
/rosout
/tf
/tracer_rc_status
/tracer_status
```

**Only the chassis base node and the remote-control bridge are up.** No IMU node, no camera node, no LiDAR node. So of the six confirmed sensors, only the chassis (motors + BMS) is currently publishing.

`systemctl` also shows a non-sensor service worth knowing about: `agilex-remote.service` — *"AgileX virtual remote controller server (HTTP + MJPEG + ROS/CAN bridge)"* (that's `/agilex_remote`), plus `agilex-report-ip.timer` which beacons the robot's IP to ntfy.sh.

### 3.7 Other buses / interfaces (nothing else hiding)

```
/dev:  ttyUSB0 (IMU) · video0-3 (2 cameras) · media0-1 (their UVC controllers)
       hidraw0-1 · input/event0-9 · gpiochip0 · i2c-0..5 · tty0-63
       NO /dev/iio*  NO /dev/spidev*  NO /dev/ttyACM*

lsmod (iio/hid/can/video/usb-serial): gs_usb, cp210x, uvcvideo, can_dev, btusb, usbhid
       NO industrial-IO (iio) drivers at all
```

- **No IIO devices** → no industrial ADC/accelerometer on SPI/I2C.
- **No `/dev/ttyACM*`** → only the single CP2102N serial bridge.
- **Input devices:** `event3` = "Logitech K400 Plus" (the wireless keyboard/trackpad), plus `Sleep Button`, `Power Button` ×2, and HDA audio jack-sense nodes. **No gamepad/joystick** — the optional 2.4 GHz RC remote (manual §1.2) is **not** connected as an input device.
- **Audio capture:** only `card 0: PCH [HDA Intel PCH], device 0: ALC269VB Analog` — the laptop's built-in mic. **No USB microphone.**
- **Network:** `wlp2s0` (Qualcomm Atheros AR9462, eduroam) · `enp3s0` (Intel I225-V, link up, switch only) · `eno1` down · 3× CAN.
- **BMS:** present, reporting over CAN `0x361`/`0x362` — see §4.

---

## 4. Bonus findings (safety-relevant, outside the two questions)

These surfaced while probing and the user should know about them.

### 4.1 The chassis is reporting a latched motor fault

`/tracer_status` reports `error_code: 4`, stable across repeated samples. The raw `0x211` frame decodes it directly:

```
can0  211  [8]  00 01 0A BE 00 04 00 00
                │  │  └──┬──┘ └──┬──┘
                │  │     │       └── error_code: high byte 0x00, low byte 0x04  ->  0x0004
                │  │     └── battery raw = 0x0ABE = 2750
                │  └── control_mode = 0x01 (CAN command mode)
                └── vehicle_state = 0x00
```

`error_code = 0x0004` → **bit[2] of the fault word**. The manual's fault table (`TRACER_UserManual_v1.2.pdf`, 表格 3.12 故障信息说明表) reads:

| bit | Meaning |
|---|---|
| 0 | 电池欠压故障 — battery undervoltage (protection at 22 V) |
| 1 | 电池过压故障 — battery overvoltage |
| **2** | **电机 1 通讯故障 — Motor 1 communication fault** |
| 3 | 电机 2 通讯故障 — Motor 2 communication fault |
| 4 | 电机 3 通讯故障 |
| 5 | 电机 4 通讯故障 |
| 6 | 电机驱动过温保护 — driver over-temperature (65 °C) |
| 7 | 电机过流保护 — motor over-current (20 A rms) |

So the chassis believes **motor 1 has a communication fault**, and it is **latched** (identical frame every sample, not flickering).

**Uncertainty I want to be explicit about:** the manual's fault table is *generic across AgileX chassis* and enumerates four motors, while the Tracer has **two** (`TracerActuatorState[2]`, "直流无刷 2 × 150W"). Which physical motor "电机 1" maps to on a 2-motor Tracer is **not determinable from the docs I have**. The manual also states that after a fault the host must send an explicit **fault-clear command** (`故障清除指令`, manual line 1112: *"需要上位机发送清除故障保护指令，清除指令后才可以正常执行运动控制指令内容"*) before motion commands are honoured.

**I did not send a fault-clear** — that is a write. The user should check this before driving. It may be a latched historical event, or it may be live; either way it is visible to the controller and worth surfacing in the UI.

### 4.2 The reported battery voltage is 10× too high

The parser scales battery as `raw * 0.1` (`agilex_msg_parser_v2.c:69-72`), and `/tracer_status` therefore reports:

```
battery_voltage: 275.0
```

**275 V is impossible for this chassis** — the manual specifies a **24 V 30 Ah** lithium pack. The raw CAN value is `2750`, and the BMS basic frame independently reports the same magnitude:

```
can0  361  [8]  64 63 0A C4 00 00 01 32
                    └──┬──┘
              0x0AC4 = 2756
```

Reading those as **10 mV units gives 27.50 V / 27.56 V** — exactly right for a fully-charged 24 V (7S Li-ion) pack, and consistent with the BMS frame's `byte[0] = 0x64 = 100` (≈100 % SOC). So:

> **The true pack voltage is ≈27.5 V. `ugv_sdk`'s `* 0.1` scaling over-reports by 10×** (raw is in 10 mV, not 100 mV). Treat `battery_voltage` from `tracer_status` as **decivolts mislabelled as volts**, i.e. divide by 10 — or better, read the BMS frame directly.
>
> This is an **inference from the raw bytes**, not something I confirmed against a multimeter. Flagged because a controller displaying "275 V" would be alarming and wrong.

---

## 5. What I could NOT determine

Stated plainly, per the task's request for honest unknowns:

1. **Whether the Hipnuc IMU is actually powered and streaming.** `/dev/ttyUSB0` exists and matches the config, but I did not open the port (that would have meant changing port settings, and I kept the task strictly read-only). The node is not running, so there is no topic to check either.
2. **The chassis maximum angular velocity.** No hardware figure exists in the code or the manual. `0.7853 rad/s` is a protocol reference value, not a rating. Would need bench measurement.
3. **Which physical motor "电机 1" is** on a 2-motor Tracer, and whether the `error_code: 4` fault is live or a latched historical event. Would need a fault-clear (a write) or vendor firmware documentation.
4. **The identity of the two Piper arms.** `can_left`/`can_right` stream `0x2A1–0x2A5` and `piper_ros` is sourced in `~/.bashrc`, which is strong circumstantial evidence, but I did not confirm the model from the bus or from a running node.
5. **Whether a LiDAR was ever fitted** to this robot. The drivers and launch files exist and `enp3s0` is cabled to a switch, but no LiDAR is present or reachable now. The switch suggests the Ethernet port may have been intended for one.
6. **Which launch file spawned the `component_container`** that mis-claimed camera B's UVC interface at dmesg `[28293]`.
7. **Whether the 2.3 m/s figure is achievable in practice** — it is the vendor's *unloaded* rating. With the arms and cameras fitted, the real ceiling will be lower. Not measurable without driving the robot (out of scope).
8. **Whether `enp3s0`'s 4 MB of received traffic** was purely STP or included something else earlier. The capture window showed STP only, and the counters are cumulative since boot, so I cannot fully rule out earlier non-STP traffic.

---

## 6. Raw evidence index

| Evidence | Section |
|---|---|
| Vendor spec table (`空载最高车速 ≤2.3`) with PDF path | §2.1 |
| `grep` for max-velocity constants; `robot_limits.hpp` full listing; `TracerProtocolV1Parser = ProtocolV1Parser<BunkerLimits>` | §2.2, §2.3 |
| `agilex_msg_parser_v2.c` encode/decode scaling with line numbers | §2.4 |
| `candump can0` frame census + `canbusload` | §2.5 |
| Full `lsusb` + `/sys/bus/usb/devices` port map | §3.1 |
| `dmesg` UVC/Orbbec pairing; `/dev/v4l/by-id` serials; `media0/1` paths | §3.2 |
| `udevadm` on `/dev/ttyUSB0`; hipnuc YAML vs source defaults | §3.3 |
| `ip -details link show type can`; passive `candump` on all 3 buses | §3.4 |
| `tcpdump -i enp3s0`; rx/tx counters; Livox/RoboSense configs | §3.5 |
| `ros2 node list` / `ros2 topic list` | §3.6 |
| `/dev` scan, `lsmod`, input devices, `arecord -l` | §3.7 |
| Raw `0x211` and `0x361` frames decoded | §4.1, §4.2 |

**Probe hygiene:** every robot-side action was read-only — `lsusb`, `udevadm info`, `ip`, `cat /sys/...`, `dmesg`, `ethtool`, `tcpdump` (passive), `candump` (passive, no `cansend`), `ros2 topic echo --once` under `timeout`, `pdftotext` into `/tmp`. Nothing installed, no service touched, no config edited, no `cmd_vel` published, no fault-clear sent. The only artifact left on the robot is `/tmp/tracer_manual.txt` and `/tmp/tracer_v11.txt` (extracted PDF text, harmless).

## 7. Sources

- **`~/agilex_ws/src/ugv_sdk/docs/protocol_v2/TRACER_UserManual_v1.2.pdf`** — §1.2 性能参数 (max speed ≤2.3 m/s, mass, motors, battery), 表格 3.12 故障信息说明表 (fault bits), protocol v1 percentage table (1.5 m/s / 0.7853 rad/s reference). *Primary source for the speed answer.*
- `~/agilex_ws/src/ugv_sdk/include/ugv_sdk/details/protocol_v1/robot_limits.hpp` — the four limit structs (no Tracer).
- `~/agilex_ws/src/ugv_sdk/include/ugv_sdk/details/protocol_v1/protocol_v1_parser.hpp:35-53,79` — V1 clamping and the `BunkerLimits` alias.
- `~/agilex_ws/src/ugv_sdk/src/protocol_v2/agilex_msg_parser_v2.c:64-88,386-397` — V2 decode/encode, ×1000 scaling, no clamping.
- `~/agilex_ws/src/ugv_sdk/src/protocol_v2/agilex_protocol_v2.h:42-110` — CAN frame ID definitions.
- `~/agilex_ws/src/tracer_ros2/tracer_base/src/tracer_base_ros.cpp:67-77` — runtime protocol auto-detection.
- `~/agilex_ws/src/tracer_ros2/tracer_base/include/tracer_base/tracer_messenger.hpp:82-92` — `error_code`/`battery_voltage` population.
- `~/agilex_ws/src/tracer_ros2/tracer_msgs/msg/TracerStatus.msg` — message fields, 2-element actuator array.
- `~/agilex_ws/src/imu/hipnuc_imu/config/hipnuc_config.yaml` + `src/serial_port.cpp:34-35` — IMU port/baud.
- `~/agilex_ws/src/rslidar_sdk/config/config.yaml`, `~/agilex_ws/src/livox_ros_driver2-master/config/*.json` — LiDAR network expectations.
- [AgileX Tracer 2.0 product page (Generation Robots)](https://www.generationrobots.com/en/404310-autonomous-mobile-robot-tracer-20-agv.html) · [AgileX Tracer user manual (Manualzz)](https://manualzz.com/doc/71694596/tracer-agilex-robotics-team-autonomous-mobile-robot-user-...) — secondary corroboration of the vendor spec family.
