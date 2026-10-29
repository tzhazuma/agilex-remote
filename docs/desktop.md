# Reaching the AgileX robot's desktop from an Apple Silicon Mac

**Task:** task-4 · **Author:** teammate `desktop` · **Date:** 2026-10-10 22:15 CST
**Robot:** `agilex@<robot-ip>` (Ubuntu 22.04.5 LTS) · **Mac:** M3 Pro, macOS 27.2

---

## 1. Bottom line

| | |
|---|---|
| **PRIMARY** | **NoMachine 9.0.188 — already installed, enabled and running on the robot.** |
| **One-line reason** | It is already serving an **Available `physical-desktop` session on the robot's real X11 display `:1`**, and its NX protocol (per-tile adaptive codec selection + client-side frame buffering) is the most latency/jitter-tolerant option available — which is decisive because the link is **37–170 ms RTT with ~40 ms jitter and carries no UDP at all**. |
| **Did I install anything?** | **No.** The primary was already installed by whoever provisioned the robot, so there was nothing to install. Installing the fallback would have broken the "one solution" rule, so I documented it instead (60-second enable, §7). |
| **Connect command (Mac)** | `ssh -N -L 4000:127.0.0.1:4000 agilex@<robot-ip>` then point the NoMachine client at **`localhost:4000`, protocol NX**. Full recipe in §6. |
| **FALLBACK** | **x11vnc 0.9.16** mirroring `:1` (works *because* the robot is X11, not Wayland) — driven from the **RealVNC VNC Viewer already installed on the Mac**. |
| **Headline risk** | The robot's **NVENC/CUDA are broken** (kernel module 570.195.03 vs userspace libs 580.178.04) and stay broken until a reboot. This does **not** block the recommendation (OpenGL/GLX is healthy, so RViz renders) but it does kill Sunshine/Moonlight outright. |

Two findings changed the shape of the answer versus the original brief:

1. **The robot is NOT on Wayland — it is on X11.** The classic "x11vnc doesn't work on Ubuntu 22.04 GNOME" trap does not apply here. `loginctl` reports `Type=x11`; Xorg 1.21.1.4 runs as user `agilex` on `:1`.
2. **NoMachine is already installed and has a live session waiting.** `nxserver --list` shows `1  agilex  -  D61934265482ACB59265D83FB6ADE608  localhost:4000` — an **Available `physical-desktop` session on display 1**.

---

## 2. The network reality (this drives everything)

Measured from the Mac, 2026-10-10 21:50–22:15 CST.

### 2.1 Path

```
Mac (M3 Pro, macOS 27.2)
  └── utun8   POINTOPOINT, inet 2.0.0.1 --> 2.0.0.1, netmask 0xffffff00, mtu 1500
        └── <robot-ip>  (robot, wlp2s0, Wi-Fi)
```

The tunnel is a **userspace proxy**, not a plain routed VPN — evidence below. This matters because it changes what is reachable and what is not.

### 2.2 Latency, jitter, loss

```
$ ping -c 30 -i 0.2 <robot-ip>
--- <robot-ip> ping statistics ---
30 packets transmitted, 30 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 36.501/66.834/170.583/40.046 ms
```

**RTT 36.5–170.6 ms, average 66.8 ms, jitter (stddev) 40.0 ms, 0% loss** in this window. An earlier 8-packet sample gave `37.801/60.707/120.710/26.939`. The brief's "66–350 ms, ~108 ms jitter" was a worse window — consistent, and the conclusion is the same: **latency and jitter are the enemy; loss is currently zero.**

### 2.3 Bandwidth: strongly asymmetric — ~9.5 MB/s down, ~0.11 MB/s up

```
# DOWNLINK (robot -> Mac) — reproducible
$ ssh agilex@<robot-ip> "dd if=/dev/zero bs=1M count=60" | dd of=/dev/null
62914560 bytes transferred in 6.503602 secs (9673802 bytes/sec)      -> ~9.2 MB/s
$ ssh agilex@<robot-ip> "dd if=/dev/zero bs=1M count=20" | dd of=/dev/null
20971520 bytes transferred in 2.076623 secs (10098858 bytes/sec)     -> ~9.5 MB/s

# UPLINK (Mac -> robot) — reproducible, ~86x slower
  uplink 1MB: elapsed 9.01s  -> 0.11 MB/s
  uplink 3MB: elapsed 26.92s -> 0.11 MB/s
  (an earlier 30MB attempt took 641s -> 0.05 MB/s)
```

**The tunnel is ~86:1 asymmetric: ~9.5 MB/s (≈76 Mbit/s) down, ~0.11 MB/s (≈0.9 Mbit/s) up.**

For a remote desktop this asymmetry lands in the *favourable* direction:

- ✅ **Screen pixels flow robot → Mac** — that is the 9.5 MB/s direction. Plenty.
- ✅ **Mouse/keyboard input flows Mac → robot** — a mouse-move event is tens of bytes. 0.11 MB/s is ample.
- ⚠️ **But anything you push *from* the Mac is slow**: pasting a 1 MB block of text into the remote session takes ~9 s, and uploading a file through the session is impractical. Use `scp`/`rsync` for bulk transfers in that direction, and expect clipboard sync to be fine for text but painful for images.

Also measured — full SSH round-trip *including* the crypto handshake:

```
ssh round-trip (incl. handshake): n=10 min=506 p50=702 max=941 ms
```

So establishing a new SSH connection costs ~0.5–0.9 s (several handshake round trips over a 37–170 ms link). That is a **one-off cost for the tunnel in §6**, not a per-interaction cost — but it is why opening new SSH sessions to this robot feels sluggish.

**Bottom line: do not optimise for bandwidth; optimise for jitter tolerance.**

### 2.4 UDP does NOT traverse the tunnel — proven

This is the single most important network finding.

```
$ # real UDP echo listener bound on the robot at 0.0.0.0:39999
$ # then, from the Mac:
  attempt 0: no reply (5s timeout)
  attempt 1: no reply (5s timeout)
  attempt 2: no reply (5s timeout)
RESULT: UDP DOES NOT TRAVERSE (or is not relayed) -> NoMachine must use TCP
```

Cross-checked with a unicast mDNS query to the robot's avahi (`udp/5353`) — also no reply.

**Consequences:**
- NoMachine's `NXUDP` transport (`EnableUPnP NXTCP,NXUDP`, UDP `0.0.0.0:4000` is listening on the robot) is **unusable**; NoMachine will fall back to `NXTCP`. That is fine — NX-on-TCP still does full adaptive tile encoding.
- **Chrome Remote Desktop / WebRTC** and **Sunshine+Moonlight** (UDP-only transports) are structurally hobbled or impossible here.
- Any protocol that assumes UDP for its low-latency path loses that path.

### 2.5 Bare "port open" tests are meaningless on this link

```
$ python3 (socket.create_connection to each port)
  22     OPEN    connect=   146ms  first_bytes=b'SSH-2.0-OpenSSH_8.9p1 Ubuntu-3ubuntu0.17\r\n'
  4000   OPEN    connect=  2519ms  first_bytes=b'<no data / timeout>'
  5900   OPEN    connect=    82ms  first_bytes=b''
  3389   OPEN    connect=   118ms  first_bytes=b''
  5901   OPEN    connect=   133ms  first_bytes=b''
  59999  OPEN    connect=   137ms  first_bytes=b''      <-- nothing listens here
  12345  OPEN    connect=   663ms  first_bytes=b''      <-- nothing listens here
```

Ports **59999 and 12345 "connect" too.** The tunnel accepts every TCP connection locally and then fails to relay it. Only `:22` returned a genuine protocol banner. (This also looks like a Clash-style TUN: the Mac has *Clash Verge* and *ClashX Pro* installed, and `2.0.0.1/24` point-to-point is its signature.)

A 30-sample distribution confirms it — these are *local proxy accepts*, not network round trips:

```
latency distribution (30 TCP connects to <robot-ip>:4000):
  n=30 min=2 p50=3 mean=3 max=15 stdev=2 ms      <-- impossible over a 37-170 ms link
```

**Therefore:** the only transport I can *prove* carries real traffic to the robot is **SSH**. The recipe in §6 leads with an SSH tunnel for that reason.

---

## 3. The robot reality (probe evidence)

### 3.1 Session type: X11 — the Wayland trap does not apply

```
$ loginctl list-sessions
SESSION  UID USER   SEAT  TTY
     12 1000 agilex seat0 tty2
$ loginctl show-session 12 -p Type -p Class -p Active -p State
Type=x11
Class=user
Active=yes
State=active

$ ps -eo pid,user,cmd | grep Xorg
   6136 agilex  /usr/lib/xorg/Xorg vt2 -displayfd 3 -auth /run/user/1000/gdm/Xauthority \
                -nolisten tcp -background none -noreset -keeptty -novtswitch -verbose 3
   6303 agilex  /usr/bin/gnome-shell
```

- `XDG_SESSION_TYPE=x11`, `XDG_CURRENT_DESKTOP=ubuntu:GNOME`, GNOME Shell **42.9**, `systemctl get-default` → `graphical.target`, GDM 3.44 active.
- `xhost` → `access control enabled, only authorized clients can connect` / `SI:localuser:agilex`; `XAUTHORITY=/run/user/1000/gdm/Xauthority` (owned by `agilex`, mode 600).
- **Why X11 and not Wayland:** the NVIDIA proprietary driver makes Ubuntu's `/usr/lib/udev/rules.d/61-gdm.rules` disable Wayland (`KERNEL!="nvidia" ... GOTO="gdm_disable_wayland"`; `/etc/gdm3/custom.conf` has `#WaylandEnable=false` commented out). So **X11 is the stable, expected state on this robot** — it will not silently flip to Wayland. `x11vnc` is therefore a durable option.

### 3.2 GPU — two different stories, and the distinction matters

```
$ lspci | grep -iE 'vga|3d|display'
01:00.0 VGA compatible controller: NVIDIA Corporation Device 2882 (rev a1)     # AD107 / RTX 4060 class
$ nvidia-smi
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 580.178
$ modinfo nvidia | grep ^version
version:        580.178.04
$ cat /proc/driver/nvidia/version
NVRM version: NVIDIA UNIX x86_64 Kernel Module  570.195.03  Sat Sep 20 01:10:00 UTC 2025
```

**Driver mismatch.** The *loaded kernel module is 570.195.03*; the *userspace libraries were upgraded to 580.178.04*. dpkg shows both generations installed side by side (`nvidia-driver-570` **and** `nvidia-driver-580`, `libnvidia-*-570` **and** `libnvidia-*-580`). The NVIDIA userspace landed at ~20:16 today, **after** the X session started at 20:03.

**Hardware video encoding is dead right now:**

```
$ ffmpeg -f lavfi -i testsrc=size=640x480:rate=30 -t 1 -c:v h264_nvenc -f null -
[h264_nvenc] dl_fn->cuda_dl->cuInit(0) failed -> CUDA_ERROR_COMPAT_NOT_SUPPORTED_ON_DEVICE:
             forward compatibility was attempted on non supported HW
Error initializing output stream 0:0 -- Error while opening encoder
```

**…but the running X session's GL is healthy:**

```
$ grep -E 'glx|GLX' ~/.local/share/xorg/Xorg.1.log
(II) LoadModule: "glx"            -> /usr/lib/xorg/modules/extensions/libglx.so
(II) Loading sub module "glxserver_nvidia"
(II) Module glxserver_nvidia: vendor="NVIDIA Corporation"
(II) NVIDIA GLX Module  570.195.03  Sat Sep 20 00:38:46 UTC 2025
(II) Initializing extension GLX
(II) Initializing extension NV-GLX
(II) NVIDIA(0): [DRI2] Setup complete

$ xdpyinfo (on :1) — extension list includes:
    DRI2  DRI3  GLX  NV-CONTROL  NV-GLX  Present  RANDR  ...
```

Xorg loaded the **570** GLX module into memory at 20:03, before the 580 userspace arrived — so the in-memory X/GL stack is internally consistent. A fresh GLX client can still select `GLX_RGBA` + `GLX_DOUBLEBUFFER` visuals:

```
glXQueryVersion -> 1 (GLX 1.4)
glXChooseVisual(RGBA8888, properly terminated) -> OK
glXChooseVisual(DOUBLEBUFFER+RGBA8888)         -> OK
```

**Interpretation:**
- ✅ **OpenGL works → RViz will render** (RViz is pure OpenGL/OGRE; it does not need CUDA).
- ❌ **NVENC / CUDA / NVML do not work → Sunshine+Moonlight is ruled out**, and NoMachine cannot use hardware encode (it will software-encode; an i7-13700 with 24 threads is more than adequate for one 1024x768–1440p stream).
- The mismatch is resolved by a **reboot**, which was out of scope and forbidden here. This is a pre-existing condition that also affects the robot *locally* — worth telling the user regardless of remote desktop.

### 3.3 Display: a monitor is (was) attached — and the resolution changed mid-probe

At 21:52 the display was a **scaled 2560x1440** on a real HDMI monitor:

```
$ DISPLAY=:1 xrandr
Screen 0: minimum 8 x 8, current 2560 x 1440, maximum 32767 x 32767
HDMI-0 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 575mm x 323mm
```

At ~21:53 the monitor stopped presenting an EDID and X fell back:

```
$ DISPLAY=:1 xrandr
Screen 0: minimum 8 x 8, current 1024 x 768, maximum 32767 x 32767
HDMI-0 connected primary 1024x768+0+0 (normal left inverted right x axis y axis) 0mm x 0mm
   1024x768      60.00*+

$ for d in /sys/class/drm/card*-*; do echo "$(basename $d)=$(cat $d/status)"; done
card0-DP-1=disconnected card0-DP-2=disconnected card0-DP-3=disconnected card0-HDMI-A-1=disconnected
$ stat -c %s /sys/class/drm/card0-HDMI-A-1/edid
0                                    # no EDID at all
```

Xorg log, timestamps converted using the log's own anchor (`[17510.633] == Sat Oct 10 20:04:49`, i.e. the log clock is seconds-since-boot):

```
[17511.725] NVIDIA(0): Setting mode "HDMI-0: nvidia-auto-select @2560x1440 +0+0
              {Transform=(1.333328,...), ViewPortIn=2560x1440, ViewPortOut=1920x1080+0+0}"
[24015.428] NVIDIA(0): Setting mode "NULL"                                  # ~21:53:14
[24015.504] NVIDIA(0): Setting mode "HDMI-0: nvidia-auto-select @1024x768 +0+0 ..."
```

**This is a physical change, not something I did.** Every display command I issued was read-only (`xrandr` with no args, `xdpyinfo`, `xset q`, DRM sysfs reads, and 1×1 unmapped GLX probe windows that I destroyed). Nothing in that set can unplug a cable or change a mode. The kernel saw all connectors `disconnected` on my *first* probe at 21:52 — before Xorg's re-probe at 21:53:14 — so the monitor was already gone when I started.

**Implications:**
- The X session (`:1`) is **alive and healthy** (gnome-shell up 2h10m, session `Active=yes`, `IdleHint=no`) and keeps running with no attached display — which is exactly the situation NoMachine's physical-desktop shadowing handles.
- What you will see remotely is currently a **1024x768 fallback desktop**, not 2560x1440. For a 37–170 ms jittery link that is *helpful* (4× fewer pixels than 1440p). The `2560x1440` mode is no longer offered, so getting it back requires the monitor to be reconnected/awake with a valid EDID.
- ⚠️ **If the X server is ever restarted while no monitor is attached**, GNOME may come up headless or at a fallback size, and NoMachine would have to create a *virtual* display instead of shadowing the physical one. Not an issue today (no reboots), but it is the main structural fragility.

### 3.4 What is already installed — the surprise

```
$ systemctl status nxserver --no-pager
● nxserver.service - NoMachine Server daemon
     Loaded: loaded (/lib/systemd/system/nxserver.service; enabled; vendor preset: enabled)
     Active: active (running) since Sat 2026-10-10 15:13:44 CST; 6h ago
     Main PID: 1550 (nxserver.bin)

$ /usr/NX/bin/nxserver --version
NoMachine - Version 9.0.188

$ /usr/NX/bin/nxserver --status
NX> 111 New connections to NoMachine server are enabled.
NX> 161 Enabled service: nxserver.
NX> 161 Enabled service: nxnode.
NX> 161 Enabled service: nxd.

$ /usr/NX/bin/nxserver --list
NX> 127 Available sessions:

Display Username Remote IP       Session ID                       Node
------- -------- --------------- -------------------------------- --------------
1       agilex   -               D61934265482ACB59265D83FB6ADE608 localhost:4000

$ /usr/NX/bin/nxserver --history          (last lines)
Display Username Remote IP Session ID                       Date                Status    Node Type
1002    agilex   10.92.0.118 E76AE93B...                    2026-09-14 09:56:13 Failed    physical-shadow
1       agilex   N/A         9C002A5E...                    2026-09-14 10:23:11 Finished  physical-desktop
0       gdm      N/A         F6D359C5...                    2026-09-21 14:41:58 Finished  physical-desktop
1       agilex   N/A         D6193426...                    2026-10-10 20:04:52 Available physical-desktop

$ ss -tlnp | grep 4000
LISTEN 0 100 0.0.0.0:4000 0.0.0.0:* users:(("nxd",pid=1614,fd=5))
LISTEN 0 100    [::]:4000    [::]:* users:(("nxd",pid=1614,fd=6))
$ ss -ulnp | grep 4000
UNCONN 0 0 0.0.0.0:4000 0.0.0.0:*
```

This is a strong signal: **an `Available` `physical-desktop` session on display `1` for user `agilex` already exists**, and the history shows this has *worked repeatedly* on this exact robot, including from a remote client (`10.92.0.118`) in September. Relevant config:

```
/usr/NX/etc/server.cfg
  AvailableSessionTypes unix-remote,unix-console,unix-default,unix-application,
                         physical-desktop,shadow,unix-xsession-default,unix-gnome,unix-xdm,vpn
  ClientConnectionMethods NX,SSH
  Port 4000
  EnableUPnP NXTCP,NXUDP
```

`physical-desktop` is explicitly allowed, so NoMachine will attach to the **existing** `:1` session rather than spawn a virtual one.

**Also installed and running (not mine, worth knowing):**

```
ii  sunloginclient  15.2.0.63062   SunloginClient-15.2.0.63062 for Linux   (向日葵 / Oray)
    /usr/local/sunlogin/bin/oray_rundaemon -m server          (root, since boot)
    /usr/local/sunlogin/bin/sunloginclient --cmd=autorun      (agilex)
    runsunloginclient.service  loaded active running
    TCP 127.0.0.1:16067, UDP 127.0.0.1:35564/35587/5657, UDP 0.0.0.0:33546
```

A Chinese commercial remote-desktop product, already running with unattended access, relaying through Oray's cloud. See §5.8.

**Not installed:** `x11vnc`, `tigervnc`, `xrdp`, `x2go`, `xpra`, `wayvnc`, `sunshine`, `wayland-proxy-virtwl`, `krfb`, `remmina`, `vinagre`. `gnome-remote-desktop` 42.9 is installed as a package but its service is `disabled`/`inactive`, `grdctl status` reports RDP **and** VNC **disabled** and **view-only: yes**, and `/usr/libexec/gnome-remote-desktop` is absent.

### 3.5 Firewall: nothing is blocked

```
$ sudo ufw status verbose
Status: inactive
$ sudo iptables -S
-P INPUT ACCEPT
-P FORWARD ACCEPT
-P OUTPUT ACCEPT
```

`ufw` is `inactive` (its unit is "active" but the firewall is off) and all iptables policies are `ACCEPT`. Nothing blocks `:4000`. See the risk note in §9.

### 3.6 Other host facts

```
CPU: 13th Gen Intel Core i7-13700, 24 threads (16C/32T-class), 5.2 GHz max
RAM: 31 GiB total, 28 GiB available;  Swap: 124 GiB
Kernel: 6.8.0-90-generic (HWE) on Ubuntu 22.04.5 LTS
Network: wlp2s0 UP <robot-ip>/23   <-- Wi-Fi only
         eno1 DOWN, enp3s0 DOWN, can0/can1 DOWN
Default route: via <robot-ip> dev wlp2s0
apt repos: mirrors.ustc.edu.cn (jammy + updates + backports + security),
           packages.microsoft.com (VS Code), mirrors.ustc.edu.cn/ros2 (Humble),
           mirrors.bwbot.org
```

⚠️ **The robot is on Wi-Fi**, and the Wi-Fi link is *inside* the VPN path. That is a second, uncontrolled source of jitter and loss on top of the tunnel. `eno1` and `enp3s0` are both down — if the robot can be plugged into Ethernet, latency and jitter would improve materially. This is probably the single highest-leverage fix available.

---

## 4. The ROS2 gotcha: `ROS_LOCALHOST_ONLY=1` (and a second trap I found)

From `~/.bashrc`:

```bash
120: source /opt/ros/humble/setup.bash
132:        export PATH="/home/agilex/miniforge3/bin:$PATH"
138: export PATH=$PATH:/usr/local/cuda-11.8/bin
141: export ROS_DOMAIN_ID=222
142: export ROS_LOCALHOST_ONLY=1
144: source /home/agilex/data_ros/install/setup.bash
145: source /home/agilex/camera_ros/install/setup.bash
146: source /home/agilex/piper_ros/install/setup.bash
147: source /home/agilex/agilex_ws/install/setup.bash
```

### 4.1 What `ROS_LOCALHOST_ONLY=1` means for running RViz remotely

In ROS 2 Humble, `ROS_LOCALHOST_ONLY=1` makes the RMW restrict DDS discovery **and** data traffic to the loopback interface. With `rmw_fastrtps_cpp` (installed: `ros-humble-rmw-fastrtps-cpp 6.2.10`; `RMW_IMPLEMENTATION` is unset, so Fast DDS is the default) the participant binds only to `127.0.0.1`. Nothing is advertised off-host, on any interface.

**Consequence: you cannot run RViz on the Mac and see the robot's topics.** Matching `ROS_DOMAIN_ID=222` on the Mac is *not* sufficient — DDS traffic never leaves the robot. (And even if you unset it, DDS discovery is chatty and loss-intolerant; over a 37–170 ms TCP-only tunnel it would be miserable.)

**So the remote-desktop-to-the-robot architecture is not merely convenient — it is the correct architecture.** RViz must run *on the robot*, inside the remote session, where it reaches the robot's nodes over loopback. That is precisely what `ROS_LOCALHOST_ONLY=1` is designed for.

**My recommendation does not change `ROS_LOCALHOST_ONLY` or `ROS_DOMAIN_ID`, and neither should you.** NoMachine's physical-desktop session attaches to the same `:1` display where the robot's desktop session already lives, so a terminal inside that session inherits the ROS environment naturally.

### 4.2 The second trap: the ROS env only exists in *interactive* shells

`~/.bashrc` opens with the standard Ubuntu guard:

```bash
# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac
```

So `~/.profile` sourcing `~/.bashrc` does not help a non-interactive shell. Measured:

```
$ bash -lc 'echo "DOMAIN=[$ROS_DOMAIN_ID] LOCALONLY=[$ROS_LOCALHOST_ONLY]"'
DOMAIN=[] LOCALONLY=[]

$ bash -ic 'echo "DOMAIN=[$ROS_DOMAIN_ID] LOCALONLY=[$ROS_LOCALHOST_ONLY]"; command -v rviz2'
DOMAIN=[222] LOCALONLY=[1]
rviz2=/opt/ros/humble/bin/rviz2
```

**Practical consequence:** `rviz2` launched from a **desktop launcher / `.desktop` icon** (or via `bash -lc`) will run with `ROS_DOMAIN_ID` unset (→ domain 0) and `ROS_LOCALHOST_ONLY` unset, and will show an **empty ROS graph** while looking perfectly healthy. This is the most likely way to lose an afternoon.

**Correct ways to launch RViz inside the remote session:**

```bash
# In GNOME Terminal inside the NoMachine session (interactive shell):
rviz2

# Or explicitly, from anything non-interactive:
bash -ic 'rviz2'
bash -ic 'source ~/.bashrc; rviz2'
```

**Tell-tale:** in RViz, `Global Options → Fixed Frame` will be empty and no topics appear under `Add → By topic`. If you see that, you launched it wrong — check `echo $ROS_DOMAIN_ID` in the shell that started it.

---

## 5. Candidate comparison

Judged on: latency/jitter tolerance over 37–170 ms with ~40 ms jitter and **no UDP**; macOS client quality on Apple Silicon; setup effort on Ubuntu 22.04 GNOME (Wayland vs X11); whether it needs an attached monitor or a virtual display; multi-monitor/audio/clipboard.

| # | Option | Jitter/latency tolerance | macOS client | Setup on this robot | Needs monitor / virtual display | Multi-mon / audio / clipboard | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **NoMachine 9 (NX)** | **Best** — per-tile adaptive codec (H.264/VP8/VP9/MJPEG/PNG/raw), client-side frame buffering, adaptive congestion control; NX heritage is high-latency WAN | Official NoMachine Player, universal/Apple-Silicon native, free | **Zero — already installed, enabled, running, session Available** | **Shadows the existing physical `:1`**; no virtual display needed | Multi-mon per free-edition rules; audio (PulseAudio); clipboard sync | ✅ **PRIMARY** |
| 2 | **x11vnc** | **Poor** — RFB, TCP-only, single stream, no adaptive congestion control; ZRLE/hextile+zlib smears on scroll | **RealVNC VNC Viewer already installed**; macOS built-in Screen Sharing path | One `apt` package + one systemd unit (~2 min) | **Mirrors `:1` directly** (works because robot is X11) | Single screen; no audio; clipboard yes | ✅ **FALLBACK** |
| 3 | **xrdp (RDP)** | Medium — but Ubuntu 22.04 ships **xrdp 0.9.17**, which predates the GFX/H.264 channel (0.10.x), so bitmap-only over a jittery link | Excellent (Microsoft Windows App) | Moderate; GNOME-on-xrdp on 22.04 needs `.xsession`/polkit hacks, black-screen prone | **Cannot attach to the existing physical session** — always spawns a *new* Xorg/Xvnc session | Good (audio, clipboard, multi-mon with paid clients) | ❌ rejected |
| 4 | **X2Go (NX3)** | **Very good** — purpose-built for high-latency/low-bandwidth | ⚠️ Requires **XQuartz**; macOS builds are community/stale and Intel-oriented | Moderate (server 4.1.0.3 available in jammy) | Starts its own `nxagent` session, does not shadow `:1` | Good | ❌ rejected — client-side pain on Apple Silicon |
| 5 | **xpra** | **Very good** by design; seamless/rootless windows | **HTML5 client → zero Mac install** (just a browser); native macOS client via Homebrew | Needs install + config (jammy candidate is 3.1, old) | New session, not `:1` shadowing | Seamless windows; audio/clipboard yes | ❌ rejected (honourable mention — best "browser-only" option) |
| 6 | **Sunshine + Moonlight** | Would be excellent on a LAN; **needs UDP** and a small jitter buffer, so 40 ms jitter + 170 ms peaks stutter | Moonlight is excellent, Apple-Silicon native | Not in any configured repo; needs manual install | Needs KMS/X11 capture | Excellent multi-mon/audio/clipboard | ❌ **ruled out twice over** |
| 7 | **Chrome Remote Desktop** | Good protocol (WebRTC VP8/VP9/H.264 with strong jitter buffering) **but needs UDP** → degrades to relay/TCP | Browser or CRD app; fine | Google-hosted `.deb`, Google account, host registration | **Creates a separate virtual X session**, not the physical desktop | Good | ❌ rejected |
| 8 | **SunLogin (向日葵)** | Good (adaptive, built for poor links) | Official macOS client | **Already installed and running** | Captures `:1` | Good | ❌ not recommended as primary (third-party cloud relay) |
| 9 | **gnome-remote-desktop** | Mediocre (bitmap-ish on 42.9, no audio) | macOS RDP/VNC clients | Package present but **service disabled, RDP+VNC disabled, view-only: yes**, binary absent; needs GUI to enable | Needs an active session | Weak | ❌ rejected |

### 5.1 NoMachine (NX) — PRIMARY

The free edition's documented behaviour is exactly what this robot needs: it *"allows to connect to an existing X display (also known as display shadowing of a live session with a physical display)"*, and *"in default setup, the Free edition of NoMachine connects the client directly to an existing X session on the remote computer"*. The documented limitation — *"only a single remote desktop session may run on the server"* — is irrelevant for one user on one Mac. ([ArchWiki: NoMachine](https://wiki.archlinux.org/title/NoMachine))

It also needs **no intermediary/relay server**, unlike SunLogin or CRD ([ArchWiki](https://wiki.archlinux.org/title/NoMachine)), so the session stays inside your VPN.

The free edition is documented as **NX-protocol-only** (no SSH client-connection method), which is why the SSH tunnel workaround matters — and ArchWiki documents precisely that workaround:

> *"The free edition of NoMachine does not allow to use the SSH protocol to connect to the target computer and only NX protocol (listening on port 4000 by default) is used. … a workaround is to create a standard SSH tunnel between client and target computer and connect through it: `ssh -L 4000:localhost:4000 user@targetmachine -fN`, then start NoMachine Player and try to connect to `localhost`."* — [ArchWiki](https://wiki.archlinux.org/title/NoMachine)

That is my recommended transport, and it is the one I proved reaches `nxd`.

**Caveats:** physical-desktop means *shadowing* — you and anyone at the robot share one screen and one cursor. With no NVENC, NoMachine software-encodes (fine on a 24-thread i7). And the free-edition limit is from documentation, not from a licence check on this box.

### 5.2 x11vnc — FALLBACK

Works **only because the robot is X11** — the standard reason x11vnc fails on Ubuntu 22.04 GNOME (Wayland) does not apply. It mirrors `:1` directly, needs no virtual display, and the Mac already has a first-class client (**RealVNC VNC Viewer**, `/Applications/VNC Viewer.app`).

Its weakness is exactly our weakness: RFB is TCP-only with no adaptive congestion control, so at 37–170 ms with 40 ms jitter it will feel laggy. Mitigate with `-depth 16`, `-ncache 10`, `-deferupdate 20`, `-noxdamage`, `-threads`.

It is the right fallback because it is a **different protocol family and a different transport** (plain TCP 5900) from NoMachine, so it is a genuinely independent path if NoMachine's client install, licensing, or NX transport disappoints. It is also the better choice for **read-only monitoring** or if the single-NoMachine-session limit ever bites.

### 5.3 xrdp — rejected

Ubuntu 22.04's repo offers **xrdp 0.9.17-2ubuntu3** (verified with `apt-cache policy xrdp` on the robot). xrdp's H.264/GFX channel arrived in the 0.10.x series, so 0.9.x is bitmap-only ([xrdp H.264 encoding wiki](https://github.com/neutrinolabs/xrdp/wiki/H.264-encoding), [xrdp NEWS](https://github.com/neutrinolabs/xrdp/blob/master/NEWS.md)) — the wrong trade for a high-jitter link.

Decisively: **xrdp cannot attach to an existing physical X session.** It always creates a new one, so you would *not* get the robot's real `:1` desktop, its attached-monitor framebuffer, or its GPU-accelerated session — you would get a second, empty GNOME session. Plus GNOME-on-xrdp on 22.04 is a well-known source of black screens. ([xrdp black-screen fix writeup](https://cloud.tencent.com.cn/developer/article/2461525))

### 5.4 X2Go — rejected

The protocol is a genuinely good fit (NX3, built for slow links, and it is the modern FreeNX descendant — [ArchWiki](https://wiki.archlinux.org/title/NoMachine) notes FreeNX/X2Go are the NX derivatives). The blocker is the **Mac client**: it requires XQuartz and the macOS builds are community-maintained and stale ([X2Go on macOS guide](https://www.buffalo.edu/sens/documentation/software-guides/x2go/macos-26.html), [X2Go Launcher for macOS](https://bellagio.csn.edu/~kmess/getting-connected/x2go-launcher-for-macos/)). On an Apple Silicon Mac that is the wrong kind of friction. It also starts its own session rather than shadowing `:1`.

### 5.5 xpra — rejected, but the best "no-install" option

xpra is designed for exactly this network and its **HTML5 client means zero Mac-side installation** — genuinely attractive ([xpra networking docs](https://github.com/Xpra-org/xpra/blob/master/docs/Network/README.md)). Rejected as primary/fallback because it needs installing and configuring (jammy's candidate is the old 3.1), the HTML5 client is its weakest for interactive quality, and it creates a new session rather than shadowing `:1`. Worth revisiting if you ever want browser-only access.

### 5.6 Sunshine + Moonlight — ruled out twice over

Two independent blockers:

1. **NVENC is broken on this robot right now.** Sunshine's value is hardware encode; `h264_nvenc` fails with `CUDA_ERROR_COMPAT_NOT_SUPPORTED_ON_DEVICE` and cannot be fixed without a reboot. Sunshine setups universally depend on NVENC ([example: DGX Spark Sunshine+Moonlight with NVENC](https://github.com/seanGSISG/dgx-spark-sunshine-setup)).
2. **Moonlight is UDP-only, and UDP does not traverse this tunnel** (§2.4). Even with NVENC fixed it would not connect. And Moonlight's small playout buffer is tuned for LAN gaming, not 40 ms jitter ([playout-buffer study](http://web.cs.wpi.edu/~claypool/mqp/cloud-buffer-24/final-report.pdf)).

### 5.7 Chrome Remote Desktop — rejected

Protocol-wise WebRTC's jitter buffering would cope well, but CRD **needs UDP** (blocked here), requires the Google-hosted `chrome-remote-desktop` package plus a Google account and host registration (not in any repo configured on the robot), **creates a separate virtual X session** rather than attaching to `:1`, and relays through Google infrastructure. The Wayland-era constraints on its Linux host are also well documented ([Chrome Remote Desktop and Wayland discussion](https://lore.freedesktop.org/wayland-devel/CAA_UwzKgXpf6+Y0Ne2+RODoT8U8xau+kir=EMOtGMMy+0+-RAA@mail.gmail.com/t.atom)).

### 5.8 SunLogin (向日葵) — already installed, not recommended

`sunloginclient 15.2.0.63062` is installed and running with unattended access (`oray_rundaemon` as root since boot, `runsunloginclient.service` active), and it does have a macOS client. It is genuinely usable over poor links. But it relays through Oray's cloud, meaning the robot's screen leaves your VPN and depends on a third party's infrastructure and account — a materially different trust posture from NoMachine's direct connection. Flagging it because it is *already there* and the user may not know.

### 5.9 gnome-remote-desktop — rejected

Package present (`42.9-0ubuntu0.22.04.2`) but the service is `disabled`/`inactive`, `grdctl status` shows RDP and VNC both **disabled** with **view-only: yes**, and `/usr/libexec/gnome-remote-desktop` is missing. Enabling requires GUI interaction in the logged-in session. On 22.04 its quality is bitmap-ish with no audio. Not competitive.

---

## 6. Recipe for the Mac (primary: NoMachine)

### Step 0 — What the robot already has (nothing to do on the robot)

```
nxserver.service   active (running), enabled
nxd                LISTEN 0.0.0.0:4000 (TCP)  +  UNCONN 0.0.0.0:4000 (UDP)
nxserver --list    1  agilex  -  D6193426...  localhost:4000   <- Available physical-desktop session
firewall           ufw inactive / iptables ACCEPT  -> nothing to open
```

**No changes are needed on the robot.** Do not install anything there for the primary path.

### Step 1 — Install the NoMachine client on the Mac

Download the **Apple Silicon** macOS client from <https://www.nomachine.com/download> (NoMachine Player, free), or:

```bash
brew install --cask nomachine        # verify the cask name if this 404s
```

> Checked: the Mac currently has **no NoMachine client** (`/Applications` and `brew list --cask` contain no NoMachine entry). It *does* already have `VNC Viewer.app` (RealVNC), which is what makes the fallback free.

### Step 2 — Open the transport

**Recipe A — SSH tunnel (recommended; this is the transport I proved reaches `nxd`):**

```bash
ssh -N -L 4000:127.0.0.1:4000 agilex@<robot-ip>
# leave this terminal open; add -f to background it
```

Why this and not the direct port: the tunnel's userspace proxy makes every TCP port *appear* open (§2.5), so a bare connect proves nothing. SSH on `:22` is the only port that returned a genuine banner, and `ssh -L 14000:127.0.0.1:4000` demonstrably reached the robot's `nxd`. It also gives you encryption for free.

**Recipe A′ — direct (try first if you want one less step):**

Point the client straight at `<robot-ip>:4000`. There is precedent — `nxserver --history` shows a remote client (`10.92.0.118`) completing a `physical-desktop` session on 2026-09-14, and `:4000` had a real ~2.5 s connect time versus ~0.1 s for ports that do not exist. If it hangs on "Connecting", switch to Recipe A.

### Step 3 — Connect

In NoMachine Player:

| Field | Value |
|---|---|
| Protocol | **NX** |
| Host | `localhost` (Recipe A) or `<robot-ip>` (Recipe A′) |
| Port | `4000` |
| Username | `agilex` |
| Password | the robot's `agilex` password |

You should land on the **existing GNOME desktop on display `:1`** (currently 1024×768) — the same screen the robot's monitor shows, if one is attached.

### Step 4 — Tune for a 37–170 ms / 40 ms-jitter link

In the NoMachine client, during the session:

- **Display → Change settings / Quality**: start at **"WAN"** or a low quality preset, not "LAN"/"Maximum". NoMachine's adaptive encoder will settle in, but starting low avoids a painful first minute.
- **Disable** wallpapers/animations/desktop effects if offered (fewer changed tiles).
- Set the resolution explicitly rather than letting it stream 1440p: **Display → Change the resolution** → 1280×720 or 1600×900 is plenty for RViz panels.
- Expect **software encoding** (NVENC is broken) — if CPU on the robot is a concern, prefer a lower resolution over a higher one; encode cost scales with pixels.
- If the picture is good but input lags, that is the 37–170 ms RTT — no codec fixes it. Reduce resolution and accept ~100 ms cursor lag.

### Step 5 — Run RViz correctly

Inside the remote desktop, open **GNOME Terminal** (interactive shell) and run:

```bash
rviz2
```

**Not** from a desktop launcher — see §4.2. Verify first if in doubt:

```bash
echo "$ROS_DOMAIN_ID $ROS_LOCALHOST_ONLY"   # must print: 222 1
```

---

## 7. Recipe for the Mac (fallback: x11vnc) — NOT installed

I deliberately did **not** install this. The task allows installing at most **one** solution, and the primary needed no installation; adding a second daemon that mirrors the user's screen on port 5900 would also mean a second service exposed on a host whose **firewall is off**, while three other agents are actively mutating the same robot. Enable it only if NoMachine disappoints.

```bash
cd <repo>

# 1. install (0.9.16-8 is what jammy offers)
./bin/rsudo.sh -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y x11vnc'

# 2. set a VNC password + install a systemd unit that mirrors :1
./bin/rsudo.sh <<'UNIT'
install -d -m 700 /etc/x11vnc
x11vnc -storepasswd 'CHOOSE_A_STRONG_PASSWORD' /etc/x11vnc/passwd
chmod 600 /etc/x11vnc/passwd
cat >/etc/systemd/system/x11vnc.service <<'EOF'
[Unit]
Description=x11vnc mirroring the physical X11 display :1
After=graphical.target

[Service]
Type=simple
User=agilex
Environment=DISPLAY=:1
Environment=XAUTHORITY=/run/user/1000/gdm/Xauthority
ExecStart=/usr/bin/x11vnc -display :1 -auth /run/user/1000/gdm/Xauthority \
  -rfbauth /etc/x11vnc/passwd -rfbport 5900 -forever -shared -loop \
  -depth 16 -ncache 10 -deferupdate 20 -noxdamage -threads \
  -allow <gpu-host> -localhost
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now x11vnc
sleep 2
systemctl --no-pager --full status x11vnc | head -12
ss -tlnp | grep 5900
UNIT
```

> `-allow <gpu-host>` plus `-localhost` scopes it to the Mac's address as the robot sees it (that is the source IP of the current SSH session). If the Mac's tunnel address changes, update `-allow`. `-ncache 10` may print a deprecation warning on some builds; drop it if so.

**Connect from the Mac** (VNC Viewer is already installed; `open vnc://` also works):

```bash
open "vnc://agilex@<robot-ip>:5900"        # macOS VNC client path
# or launch /Applications/VNC Viewer.app and connect to <robot-ip>:5900
```

If `:5900` does not pass the tunnel, forward it over SSH exactly as for NoMachine:

```bash
ssh -N -L 5900:127.0.0.1:5900 agilex@<robot-ip>
# then connect VNC Viewer to localhost:5900
```

**Uninstall (exact, complete):**

```bash
./bin/rsudo.sh -c 'systemctl disable --now x11vnc; \
  rm -f /etc/systemd/system/x11vnc.service; \
  rm -rf /etc/x11vnc; \
  systemctl daemon-reload; \
  DEBIAN_FRONTEND=noninteractive apt-get remove -y --purge x11vnc; \
  apt-get autoremove -y'
```

---

## 8. What I installed, and the uninstall command

**I installed nothing.** The primary recommendation (NoMachine) was already installed, enabled, and running on the robot, with a session available — so there was no safe-and-reversible install to perform, and installing the fallback would have violated the "one solution" constraint.

For completeness, the **NoMachine uninstall command** (for reference — **I did not run it**, and I recommend against it: NoMachine is the recommendation):

```bash
./bin/rsudo.sh -c '/usr/NX/bin/nxserver --uninstall; \
  DEBIAN_FRONTEND=noninteractive dpkg -P nomachine'
```

⚠️ NoMachine's own uninstaller is documented as leaving residue that can break graphics — VirtualGL permission files (`/etc/opt/VirtualGL`, `/usr/share/gdm/greeter/autostart/virtualgl.desktop`, `/etc/modprobe.d/virtualgl.conf`, `/etc/udev/rules.d/99-virtualgl-dri.rules`, `/etc/X11/xorg.conf.d/99-virtualgl-dri`) and `LD_PRELOAD` lines in display-manager configs. Clean those up if you ever remove it. ([ArchWiki: NoMachine — troubleshooting](https://wiki.archlinux.org/title/NoMachine)) I verified none of those VirtualGL residue files exist on this robot today.

### Verification evidence for the primary (server side)

```
$ systemctl status nxserver --no-pager
● nxserver.service - NoMachine Server daemon
     Loaded: loaded (/lib/systemd/system/nxserver.service; enabled; vendor preset: enabled)
     Active: active (running) since Sat 2026-10-10 15:13:44 CST; 6h ago
     Main PID: 1550 (nxserver.bin)
      Tasks: 76 (limit: 38140)
     Memory: 212.7M

$ ss -tlnp | grep 4000
LISTEN 0 100 0.0.0.0:4000 0.0.0.0:* users:(("nxd",pid=1614,fd=5))
LISTEN 0 100    [::]:4000    [::]:* users:(("nxd",pid=1614,fd=6))

$ /usr/NX/bin/nxserver --status
NX> 111 New connections to NoMachine server are enabled.
NX> 161 Enabled service: nxserver.
NX> 161 Enabled service: nxnode.
NX> 161 Enabled service: nxd.

$ /usr/NX/bin/nxserver --list
Display Username Remote IP Session ID                       Node
1       agilex   -         D61934265482ACB59265D83FB6ADE608 localhost:4000
```

Plus the transport proof:

```
$ ssh -f -N -L 14000:127.0.0.1:4000 agilex@<robot-ip>   && echo OK
ssh -L tunnel established OK
$ python3 -c "socket.create_connection(('127.0.0.1',14000))"
forwarded 127.0.0.1:14000 -> robot:4000 CONNECTED; first_bytes=<no data / timeout (nxd waits for client handshake)>
```

`nxd` not sending an unsolicited banner is correct NX behaviour (the client speaks first), so "connected, no data" is the expected healthy result.

### Probe hygiene

All robot-side probing was read-only. The only writes anywhere were to `/tmp/udp_probe.log` on the robot (a temporary UDP echo listener on port 39999, which self-terminated on a 40/60 s timeout and was cleaned up) and two `ssh -L` tunnels, which I closed. I did not touch `~/.zshrc`, `~/.bashrc`, `/home/linuxbrew`, `/opt/ros`, `~/piper_ros`, or `~/agilex_ws`, and I ran no `apt-get upgrade`/`dist-upgrade`/`do-release-upgrade`/reboot. I did not modify `/etc/NX`, the display manager, or any systemd unit. Post-probe re-check: `gnome-shell` (pid 6303) still up, session 12 still `Type=x11 Active=yes State=active`, `:1` still answering `xdpyinfo`.

---

## 9. What I could NOT verify, and the risks

### 9.1 Could not verify (honest limits)

1. **No end-to-end desktop connection.** There is **no NoMachine client on this Mac** (checked `/Applications` and `brew list --cask`). I could not authenticate, complete the NX handshake, render a frame, or measure perceived latency. **Everything about NoMachine here is verified server-side only**: service active, socket listening, session `Available`, physical-desktop session type permitted, GLX healthy.
2. **No visual confirmation** of the desktop, its frame rate, colour fidelity, or how physical-desktop shadowing actually *feels* at 37–170 ms.
3. **No measurement of NoMachine's adaptive bitrate/framerate** on this link, and no confirmation that its TCP fallback behaves gracefully with 40 ms jitter. The jitter-tolerance claim is from NX's design and its documented purpose, not from a measurement on this link.
4. **No audio or clipboard test** end-to-end.
5. **I did not launch any GUI application** (including RViz) on the user's display. I proved `rviz2` exists (`/opt/ros/humble/bin/rviz2`) and that GLX visuals are selectable, but I did not open an RViz window.
6. **The direct-vs-tunnel question is not fully settled.** I proved the SSH tunnel reaches `nxd`; I could *not* prove a direct connection to `<robot-ip>:4000` does, because this tunnel makes every TCP port appear open (§2.5). Precedent (a September remote session) and a suspiciously long 2.5 s connect time suggest direct works, but treat Recipe A as the guaranteed path.
7. **NoMachine's free-edition limits** ("only a single remote desktop session may run on the server"; NX-protocol-only) come from the ArchWiki, not from a licence inspection on this robot.
8. **I did not test the fallback.** The x11vnc recipe in §7 is unexecuted. In particular I could not verify that `-ncache`/`-deferupdate` behave on this x11vnc build, or that RealVNC's macOS client performs acceptably at 37–170 ms.
9. **The UDP finding is configuration-specific.** It is solid for *this* tunnel as configured now (very likely a Clash-style TUN). If the user's proxy rules or VPN change, UDP may start working — which would reopen Sunshine/CRD as options.

### 9.2 Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Physical-desktop shadowing is shared.** You and anyone at the robot share one screen and one cursor; a local user can watch and interfere. | Medium | Robot appears unattended. If this is unacceptable, use the x11vnc fallback (also shared) or a *virtual* NoMachine session (set `PhysicalDisplays` in `/usr/NX/etc/node.conf` — **not done**, out of scope). |
| **Firewall is off.** `ufw inactive`, iptables `ACCEPT`. NoMachine `:4000`, SunLogin, and sshd are exposed to `<robot-ip>/23` and whatever the VPN reaches. | **High** | Keep it VPN-only, or add a scoped rule: `ufw allow from <gpu-host> to any port 4000 proto tcp`. Do not expose `:4000` to the internet. |
| **NoMachine is proprietary and was pre-installed by someone else**, with unattended access. Its uninstaller leaves graphics-breaking residue. | Medium | Already the recommendation; leave it in place. Audit who installed it if provenance matters. |
| **SunLogin (向日葵) is running with unattended access**, relaying through Oray's cloud. The user may not know. | **High** (privacy) | Out of my write scope to remove. Recommend the user decide: `sudo systemctl disable --now runsunloginclient.service` and `dpkg -P sunloginclient` if unwanted. |
| **NVENC/CUDA broken** (kernel 570 vs userspace 580) until reboot. Blocks Sunshine, hardware video encode, and any CUDA workload; also affects the robot *locally*. | Medium | Reboot the robot at a convenient time to reconcile the driver. **I did not reboot** (forbidden). Does not affect this recommendation (software encode; OpenGL healthy). |
| **Robot is on Wi-Fi** (`wlp2s0`), inside the VPN path — a second uncontrolled jitter source. `eno1`/`enp3s0` are down. | Medium | Plug the robot into Ethernet if physically possible. This is the highest-leverage latency fix available. |
| **Uplink is ~86× slower than downlink** (~0.11 MB/s up vs ~9.5 MB/s down). Benign for input, but pushes *from* the Mac are slow. | Low | Use `scp`/`rsync` (not in-session paste/upload) for anything sizeable Mac→robot. Screen updates and input are unaffected. |
| **Monitor currently disconnected / no EDID**; desktop fell back to 1024×768 at ~21:53:14. | Low | Fine (even helpful) for a jittery link. Reconnect/awake the monitor for 1440p. ⚠️ If X is restarted while headless, NoMachine would need a virtual display instead of shadowing. |
| **RViz launched from a desktop icon shows an empty graph** (non-interactive shell → `ROS_DOMAIN_ID` unset). | Medium (time sink) | Launch from GNOME Terminal, or `bash -ic 'rviz2'`. §4.2. |
| `ROS_LOCALHOST_ONLY=1` means **no Mac-side RViz**, ever, without changing the robot's config. | By design | Not a defect — run RViz on the robot. Do not change it. |
| The other three agents are mutating this robot concurrently. | Low | All my robot work was read-only except a self-terminating `/tmp` UDP probe. No systemd units, no `/etc` changes, no packages. |

---

## 10. Sources

- [ArchWiki: NoMachine](https://wiki.archlinux.org/title/NoMachine) — free-edition physical-display shadowing, single-session limit, NX-protocol-only, the `ssh -L` tunnel workaround, `PhysicalDisplays`, uninstall residue.
- [NoMachine Personal Edition](https://www.nomachine.com/everybody/personal-edition) — editions and downloads.
- [NoMachine KB AR10K00700 — physical desktop vs virtual desktop connection](https://kb.nomachine.com/AR10K00700)
- [NoMachine KB AR03P00973 — connecting to Linux headless machines](https://kb.nomachine.com/AR03P00973)
- [xrdp wiki: H.264 encoding](https://github.com/neutrinolabs/xrdp/wiki/H.264-encoding) · [xrdp NEWS](https://github.com/neutrinolabs/xrdp/blob/master/NEWS.md) — GFX/H.264 is 0.10.x; Ubuntu 22.04 ships 0.9.17.
- [xrdp black-screen remediation](https://cloud.tencent.com.cn/developer/article/2461525)
- [X2Go on macOS (XQuartz requirement)](https://www.buffalo.edu/sens/documentation/software-guides/x2go/macos-26.html) · [X2Go Launcher for macOS](https://bellagio.csn.edu/~kmess/getting-connected/x2go-launcher-for-macos/)
- [xpra networking documentation](https://github.com/Xpra-org/xpra/blob/master/docs/Network/README.md) · [xpra seamless mode](https://github.com/neuroradiology/xpra/blob/master/docs/Usage/Seamless.md)
- [Moonlight playout-buffer / jitter study (WPI)](http://web.cs.wpi.edu/~claypool/mqp/cloud-buffer-24/final-report.pdf)
- [Sunshine + Moonlight headless NVENC setup (DGX Spark)](https://github.com/seanGSISG/dgx-spark-sunshine-setup) — illustrates the hard NVENC dependency.
- [Chrome Remote Desktop and Wayland (wayland-devel)](https://lore.freedesktop.org/wayland-devel/CAA_UwzKgXpf6+Y0Ne2+RODoT8U8xau+kir=EMOtGMMy+0+-RAA@mail.gmail.com/t.atom)
- [TigerVNC (background on RFB/Windows-client situation)](https://en.wikipedia.org/wiki/TigerVNC) · [RealVNC vs TigerVNC](https://www.realvnc.com/en/blog/realvnc-vs-tigervnc/)

---

## Appendix A — Raw probe output index

| Evidence | Where in this report |
|---|---|
| Session type / Xorg cmdline / gnome-shell | §3.1 |
| `lspci`, `nvidia-smi`, `/proc/driver/nvidia/version`, dpkg NVIDIA versions | §3.2 |
| Xorg log GLX lines, `xdpyinfo` extension list, GLX visual test | §3.2 |
| `xrandr` before/after, DRM connector status, EDID size, mode-change log lines | §3.3 |
| `nxserver --status/--list/--history/--version`, `ss -tlnp`, server.cfg | §3.4 |
| SunLogin processes and unit | §3.4 |
| `ufw status`, `iptables -S` | §3.5 |
| CPU/RAM/kernel/repos | §3.6 |
| `ping`, throughput, UDP probe, port matrix | §2.2–§2.5 |
| Asymmetric throughput (up/down), SSH handshake round-trip, 30-sample connect distribution | §2.3, §2.5 |
| `bash -lc` vs `bash -ic` ROS env | §4.2 |
| `apt-cache policy` for every candidate | §5 (verdicts) |
| `systemctl status nxserver`, `ss -tlnp`, tunnel proof | §8 |
