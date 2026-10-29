# nvidia-smi driver/library mismatch — FIXED, plus CUDA audit & upgrade

**Task:** task-5 (owner `nvidia-cuda`) · **Robot:** `agilex@<robot-ip>` · Ubuntu 22.04.5 LTS, x86_64
**Executed:** 2026-10-10 22:32 → 23:02 CST (robot clock) · **Reboot performed: NO**

---

## 1. Status at a glance

| Item | Before | After |
|---|---|---|
| `nvidia-smi` | `Failed to initialize NVML: Driver/library version mismatch` (rc=18) | **works**, rc=0, driver 580.178.04 |
| Kernel module in memory | 570.195.03 | **580.178.04** |
| Kernel module on disk (dkms) | 580.178.04 | 580.178.04 (unchanged) |
| `torch.cuda.is_available()` | `False` (Error 804) in every env | **`True`** in `aloha` and `lerobot` |
| `nvcc` on PATH | not found (`~/.bashrc` pointed at nonexistent `/usr/local/cuda-11.8`) | **found** via `/home/agilex/.dsh/cuda-env.sh` |
| CUDA toolkits in `/usr/local` | `cuda-12.8` only | `cuda-12.8` **+ `cuda-12.9`** (12.9.1, V12.9.86) |
| `/usr/local/cuda` symlink | `-> cuda-12.8` | **`-> cuda-12.8`** (deliberately unchanged) |
| Duplicate driver packages | `nvidia-driver-570` **and** `-580` both installed | **only 580** (570 purged) |
| `dpkg -l \| grep -c 570` | 24 | **3** (all unrelated ROS packages) |
| SSH | enabled + active | enabled + active (never at risk) |
| Desktop (gdm) | user session live on tty2 | gdm **active**, sitting at the **login screen** — see §9 |

**Nothing was rebooted.** The live module reload was authorised by the user (`保证能进行ssh连接的情况下可以关闭gdm桌面，重新加载nvidia模块`) and executed exactly as specified.

---

## 2. Root cause (independently re-verified, and one correction to the original diagnosis)

The diagnosis in the task description was **correct on the mismatch** but **wrong about the GPU**.

### 2.1 The mismatch — confirmed

```
=== BEFORE ===
$ cat /proc/driver/nvidia/version          # module IN MEMORY
NVRM version: NVIDIA UNIX x86_64 Kernel Module  570.195.03  Sat Sep 20 01:10:00 UTC 2025
GCC version:  gcc version 12.3.0 (Ubuntu 12.3.0-1ubuntu1~22.04.2)

$ modinfo nvidia | grep -E '^(version|filename)'   # module ON DISK
filename:       /lib/modules/6.8.0-90-generic/updates/dkms/nvidia.ko
version:        580.178.04
vermagic:       6.8.0-90-generic SMP preempt mod_unload modversions

$ ls -l /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1      # userspace NVML
/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1 -> libnvidia-ml.so.580.178.04

$ nvidia-smi
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 580.178
nvidia-smi exit=18
```

**Precise root cause:** an nvidia driver package upgrade to **580.178.04** was applied to the box (580 userspace libs dated `8月10 06:18`, 580 GSP firmware `10月10 20:16`, dkms module built `10月10 20:18`) **while the 570.195.03 module stayed resident in the kernel**. Userspace NVML (580.178) therefore could not talk to the loaded 570 module. The box had never been rebooted and the module had never been reloaded, so the on-disk 580 module was never activated. DKMS had the 580 module correctly built for **both** the running kernel and the newer installed kernel:

```
$ dkms status
nvidia/580.178.04, 6.8.0-138-generic, x86_64: installed
nvidia/580.178.04, 6.8.0-90-generic, x86_64: installed
```

The old 570 userspace had itself been partly upgraded to 570.211.01 while `nvidia-firmware-570-570.195.03` remained — i.e. the box had accumulated **two** un-activated driver generations. That is exactly why the duplicate-570 purge (§8) was worth doing.

### 2.2 CORRECTION — the GPU is *not* Blackwell

The original diagnosis said "Blackwell-class". It is not. Authoritative read-back:

```
$ nvidia-smi --query-gpu=name,driver_version,vbios_version,compute_cap,memory.total,pci.device_id --format=csv
name, driver_version, vbios_version, compute_cap, memory.total [MiB], pci.device_id
NVIDIA GeForce RTX 4060, 580.178.04, 95.07.31.00.EA, 8.9, 8188 MiB, 0x288210DE
```

**NVIDIA GeForce RTX 4060, compute capability 8.9 (Ada Lovelace, AD107), 8 GB, PCI `10de:2882`.**
`lspci` could not name it because the box's `pci.ids` is stale (`NVIDIA Corporation Device [10de:2882]`).

**Why this matters:** sm_89 is supported by CUDA 11.8 and later, so the claim "Blackwell-class, which requires CUDA 12.8+" — used to justify 12.8 as the appropriate toolkit — does not hold. CUDA 12.8 was fine but was never *required*. It also changes the upgrade calculus in §10.

`nvidia-smi` reports `CUDA Version: 13.0` — that is the **maximum CUDA runtime the 580 driver supports**, and it is what caps the toolkit choices below.

---

## 3. The safety premise: SSH cannot be affected by stopping gdm

This was proven **before** touching anything. SSH is the user's lifeline, so it was checked first.

```
$ systemctl is-enabled ssh          → enabled
$ systemctl is-active ssh           → active
$ systemctl status ssh | head -4
● ssh.service - OpenBSD Secure Shell server
     Loaded: loaded (/lib/systemd/system/ssh.service; enabled; vendor preset: enabled)
     Active: active (running) since Sat 2026-10-10 20:19:38 CST; 2h 12min ago
     Main PID: 87611 (sshd)                    ← PPID 1, no display-manager ancestor

$ systemctl cat ssh | grep -A4 '^\[Unit\]'     # (unit file, abridged)
After=network.target auditd.service
ConditionPathExists=!/etc/ssh/sshd_not_to_be_run
...
[Install]
WantedBy=multi-user.target                     ← pulled in by multi-user, NOT graphical
Alias=sshd.service

$ systemctl show ssh -p Wants -p Requires -p After -p Before
Requires=system.slice -.mount sysinit.target
Wants=
After=-.mount systemd-journald.socket sysinit.target basic.target network.target auditd.service system.slice
Before=shutdown.target multi-user.target nxserver.service
                     ↑ no graphical.target, no gdm.service anywhere

$ systemctl list-dependencies --reverse gdm     # who depends on gdm
gdm.service
● └─graphical.target                            ← only graphical.target

$ systemctl list-dependencies --reverse ssh     # who depends on ssh
ssh.service
● └─multi-user.target
●   └─graphical.target                          ← ssh is a PARENT of graphical, not a child
```

**Conclusion:** `ssh.service` is a plain system daemon with `WantedBy=multi-user.target` and zero dependency on `gdm`/`graphical.target`. `gdm` depends (transitively) on the multi-user target that contains ssh — the dependency runs *the other way*. Stopping or restarting gdm **cannot** stop sshd. Confirmed empirically: the SSH connection used for every step of this task stayed up through the entire reload, including while gdm was stopped.

---

## 4. The fix — live kernel-module reload (no reboot)

### 4.1 Method

The reload was **not** run inline over SSH. The script was written to the robot and launched as a **detached transient systemd unit**, so an SSH drop could not strand the robot half-way:

```bash
./bin/rsudo.sh -c 'systemd-run --unit=nvidia-reload --collect --no-block /bin/bash /tmp/nvidia-reload.sh'
```

`/tmp/nvidia-reload.sh` (sha256 `c15ec72ab99ab57ea7a15ddf218f6e5c0315e60bc138a5ac98757616e05228f1`) implements the required shape: `exec > log`, `set -x`, a **dead-man's-switch `trap ... EXIT`** that always restarts gdm, before/after state capture, `systemctl stop gdm`, `fuser -k /dev/nvidia*`, dependency-ordered `rmmod`, `modprobe`, and verification.

One addition beyond the specified shape: an **independent** rescue timer, armed before the reload, as a second safety net in case the script itself were ever SIGKILLed (where an EXIT trap does not run):

```bash
systemd-run --on-active=360 --unit=gdm-rescue --collect /usr/bin/systemctl start gdm
```

It fired at 22:47:21 and was a **no-op** (gdm was already active). It is verified benign and now gone (`0 timers listed`).

### 4.2 Result — everything worked on the first attempt

All four `rmmod` succeeded and all three `modprobe` succeeded. **No process fought the unload** — stopping gdm released every GPU holder cleanly, so the `fuser -k` step had nothing to kill.

```
+ systemctl stop gdm
systemctl stop gdm rc=0
+ fuser -v /dev/nvidia0 ... /dev/nvidia-uvm-tools      ← EMPTY: zero holders left
+ fuser -k /dev/nvidia0 ... /dev/nvidia-uvm-tools      ← no-op
+ rmmod nvidia_uvm    → rmmod nvidia_uvm: OK
+ rmmod nvidia_drm    → rmmod nvidia_drm: OK
+ rmmod nvidia_modeset→ rmmod nvidia_modeset: OK
+ rmmod nvidia        → rmmod nvidia: OK
(no nvidia modules loaded - good)
+ modprobe nvidia     → modprobe nvidia: OK
+ modprobe nvidia_drm → modprobe nvidia_drm: OK
+ modprobe nvidia_uvm → modprobe nvidia_uvm: OK
```

> **Honest note on two log lines.** The log contains
> `STILL HOLDING: 127855 bash` and `STILL HOLDING: 128001 sort`. **These are false positives from my own diagnostic**,
> not GPU holders: the check was `ls -l /proc/$p/fd | grep -q nvidia`, and it matched the *filenames*
> `/tmp/nvidia-reload.sh` and `/tmp/nvidia-reload.log` in the file descriptors of the script's own `bash` and the
> `sort` process. The authoritative signal — `fuser -v /dev/nvidia*` — was **empty**. No real holder existed.

`modprobe` picked up `modeset=1` automatically from `/etc/modprobe.d/nvidia-graphics-drivers-kms.conf`, so the desktop came back with KMS enabled (same config as before).

### 4.3 Full `/tmp/nvidia-reload.log`

Archived verbatim at [`reports/evidence/nvidia-reload.log`](evidence/nvidia-reload.log). Reproduced in full:

```
+ date
2026年 10月 10日 星期六 22:41:21 CST
+ trap 'echo "[trap] EXIT - restarting gdm"; systemctl start gdm; echo "[trap] gdm is-active: $(systemctl is-active gdm)"' EXIT
+ echo '===== BEFORE: loaded module ====='
===== BEFORE: loaded module =====
+ cat /proc/driver/nvidia/version
NVRM version: NVIDIA UNIX x86_64 Kernel Module  570.195.03  Sat Sep 20 01:10:00 UTC 2025
GCC version:  gcc version 12.3.0 (Ubuntu 12.3.0-1ubuntu1~22.04.2)
+ echo '===== BEFORE: nvidia-smi (expected to fail) ====='
===== BEFORE: nvidia-smi (expected to fail) =====
+ nvidia-smi
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 580.178
+ echo 'nvidia-smi rc=18'
nvidia-smi rc=18
+ echo '===== BEFORE: lsmod ====='
===== BEFORE: lsmod =====
+ lsmod
+ grep -E '^nvidia'
nvidia_uvm           2052096  0
nvidia_drm            131072  12
nvidia_modeset       1548288  20 nvidia_drm
nvidia              90578944  292 nvidia_uvm,nvidia_modeset
+ echo '===== STOPPING GDM ====='
===== STOPPING GDM =====
+ systemctl stop gdm
+ echo 'systemctl stop gdm rc=0'
systemctl stop gdm rc=0
+ sleep 3
+ echo '===== REMAINING GPU HOLDERS (after gdm stop) ====='
===== REMAINING GPU HOLDERS (after gdm stop) =====
+ fuser -v /dev/nvidia0 /dev/nvidia-caps /dev/nvidiactl /dev/nvidia-modeset /dev/nvidia-uvm /dev/nvidia-uvm-tools
+ true
+ echo '===== KILLING REMAINING GPU HOLDERS ====='
===== KILLING REMAINING GPU HOLDERS =====
+ fuser -k /dev/nvidia0 /dev/nvidia-caps /dev/nvidiactl /dev/nvidia-modeset /dev/nvidia-uvm /dev/nvidia-uvm-tools
+ true
+ sleep 2
+ echo '===== HOLDERS AFTER KILL ====='
===== HOLDERS AFTER KILL =====
+ fuser -v /dev/nvidia0 /dev/nvidia-caps /dev/nvidiactl /dev/nvidia-modeset /dev/nvidia-uvm /dev/nvidia-uvm-tools
+ true
+ sort -u
STILL HOLDING: 127855 bash
STILL HOLDING: 128001 sort
+ echo '===== UNLOADING MODULES (dependency order) ====='
===== UNLOADING MODULES (dependency order) =====
+ for m in nvidia_uvm nvidia_drm nvidia_modeset nvidia
+ echo '--- rmmod nvidia_uvm ---'
--- rmmod nvidia_uvm ---
+ rmmod nvidia_uvm
+ echo 'rmmod nvidia_uvm: OK'
rmmod nvidia_uvm: OK
+ for m in nvidia_uvm nvidia_drm nvidia_modeset nvidia
+ echo '--- rmmod nvidia_drm ---'
--- rmmod nvidia_drm ---
+ rmmod nvidia_drm
+ echo 'rmmod nvidia_drm: OK'
rmmod nvidia_drm: OK
+ for m in nvidia_uvm nvidia_drm nvidia_modeset nvidia
+ echo '--- rmmod nvidia_modeset ---'
--- rmmod nvidia_modeset ---
+ rmmod nvidia_modeset
+ echo 'rmmod nvidia_modeset: OK'
rmmod nvidia_modeset: OK
+ for m in nvidia_uvm nvidia_drm nvidia_modeset nvidia
+ echo '--- rmmod nvidia ---'
--- rmmod nvidia ---
+ rmmod nvidia
+ echo 'rmmod nvidia: OK'
rmmod nvidia: OK
+ echo '===== MODULES AFTER UNLOAD ====='
===== MODULES AFTER UNLOAD =====
+ lsmod
+ grep -qE '^nvidia'
+ echo '(no nvidia modules loaded - good)'
(no nvidia modules loaded - good)
+ echo '===== RELOADING (modprobe picks up /etc/modprobe.d/nvidia-graphics-drivers-kms.conf: modeset=1) ====='
===== RELOADING (modprobe picks up /etc/modprobe.d/nvidia-graphics-drivers-kms.conf: modeset=1) =====
+ modprobe nvidia
+ echo 'modprobe nvidia: OK'
modprobe nvidia: OK
+ modprobe nvidia_drm
+ echo 'modprobe nvidia_drm: OK'
modprobe nvidia_drm: OK
+ modprobe nvidia_uvm
+ echo 'modprobe nvidia_uvm: OK'
modprobe nvidia_uvm: OK
+ echo '===== AFTER: loaded module (must read 580.178.04) ====='
===== AFTER: loaded module (must read 580.178.04) =====
+ cat /proc/driver/nvidia/version
NVRM version: NVIDIA UNIX x86_64 Kernel Module  580.178.04  Tue Jul  7 12:38:21 UTC 2026
GCC version:  gcc version 12.3.0 (Ubuntu 12.3.0-1ubuntu1~22.04.3)
+ echo '===== AFTER: nvidia-smi (must succeed) ====='
===== AFTER: nvidia-smi (must succeed) =====
+ nvidia-smi
Sat Oct 10 22:41:29 2026
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 580.178.04             Driver Version: 580.178.04     CUDA Version: 13.0     |
+-----------------------------------------+------------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|                                         |                        |               MIG M. |
|=========================================+========================+======================|
|   0  NVIDIA GeForce RTX 4060        Off |   00000000:01:00.0 Off |                  N/A |
| 33%   46C    P0            N/A  /  115W |       2MiB /   8188MiB |      2%      Default |
|                                         |                        |                  N/A |
+-----------------------------------------+------------------------+----------------------+

+-----------------------------------------------------------------------------------------+
| Processes:                                                                              |
|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |
|        ID   ID                                                               Usage      |
|=========================================================================================|
|  No running processes found                                                             |
+-----------------------------------------------------------------------------------------+
+ echo 'nvidia-smi rc=0'
nvidia-smi rc=0
+ echo '===== AFTER: lsmod ====='
===== AFTER: lsmod =====
+ lsmod
+ grep -E '^nvidia'
nvidia_uvm           2093056  0
nvidia_drm            139264  0
nvidia_modeset       1642496  1 nvidia_drm
nvidia              104222720  2 nvidia_uvm,nvidia_modeset
+ date
2026年 10月 10日 星期六 22:41:29 CST
+ echo '[trap] EXIT - restarting gdm'
[trap] EXIT - restarting gdm
+ systemctl start gdm
++ systemctl is-active gdm
+ echo '[trap] gdm is-active: active'
[trap] gdm is-active: active
```

**Total downtime of the graphical stack: ~8 seconds** (22:41:21 stop → 22:41:29 gdm restarted).

---

## 5. Post-fix verification

### 5.1 Driver / module / nvidia-smi

```
$ cat /proc/driver/nvidia/version
NVRM version: NVIDIA UNIX x86_64 Kernel Module  580.178.04  Tue Jul  7 12:38:21 UTC 2026
GCC version:  gcc version 12.3.0 (Ubuntu 12.3.0-1ubuntu1~22.04.3)

$ modinfo nvidia | grep -E '^(version|filename)'
filename:       /lib/modules/6.8.0-90-generic/updates/dkms/nvidia.ko
version:        580.178.04

$ nvidia-smi            # rc=0
Sat Oct 10 23:02:31 2026
+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 580.178.04             Driver Version: 580.178.04     CUDA Version: 13.0     |
+-----------------------------------------+------------------------+----------------------+
| GPU  Name                 Persistence-M | Bus-Id          Disp.A | Volatile Uncorr. ECC |
| Fan  Temp   Perf          Pwr:Usage/Cap |           Memory-Usage | GPU-Util  Compute M. |
|                                         |                        |               MIG M. |
|=========================================+========================+======================|
|   0  NVIDIA GeForce RTX 4060        Off |   00000000:01:00.0 Off |                  N/A |
| 33%   46C    P8            N/A  /  115W |      32MiB /   8188MiB |      0%      Default |
|                                         |                        |                  N/A |
+-----------------------------------------+------------------------+----------------------+

+-----------------------------------------------------------------------------------------+
| Processes:                                                                              |
|  GPU   GI   CI              PID   Type   Process name                        GPU Memory |
|        ID   ID                                                               Usage      |
|=========================================================================================|
|    0   N/A  N/A          128996      G   /usr/lib/xorg/Xorg                        9MiB |
|    0   N/A  N/A          129073      G   /usr/bin/gnome-shell                      3MiB |
+-----------------------------------------------------------------------------------------+
```

Module in memory (580.178.04) now matches module on disk (580.178.04) and userspace NVML (580.178.04). The mismatch is gone.

### 5.2 GPU compute actually works (PyTorch, real matmul)

```
$ ~/miniforge3/envs/aloha/bin/python -c "import torch; ..."
aloha     torch 2.7.1+cu128  built-for-cuda 12.8  cuda.is_available() = True
          device: NVIDIA GeForce RTX 4060   capability: (8, 9)   MATMUL OK, sum= 2528.221
$ ~/miniforge3/envs/lerobot/bin/python -c "import torch; ..."
lerobot   torch 2.7.1+cu126  built-for-cuda 12.6  cuda.is_available() = True
          device: NVIDIA GeForce RTX 4060   capability: (8, 9)   MATMUL OK, sum= 16945.281
```

Before the reload **both** failed with:
`CUDA initialization: ... Error 804: forward compatibility was attempted on non supported HW`.

**Note for `lerobot`:** its torch is a **cu126** build while the box's default toolkit is 12.8. It works (cu126 wheels contain sm_89 kernels), but it is inconsistent with `aloha`'s cu128. Worth aligning to cu128 at some point — I did **not** touch it.

### 5.3 End-to-end custom CUDA extension (compiled *and executed* on the GPU)

A real `CUDAExtension` was built and run, to prove the toolchain is not just present but functional:

```
$ cd /tmp/extbuild && CUDA_HOME=/usr/local/cuda-12.9 ... python setup.py build_ext --inplace
build rc=0 ;  grep -c 'error' → 0
$ python -c "import torch; import addone; print(addone.addone(torch.zeros(4, device='cuda')).tolist())"
EXT_RESULT_nvcc12.9: [1.0, 1.0, 1.0, 1.0] | device: NVIDIA GeForce RTX 4060
```

nvcc emitted the correct Ada target: `-gencode=arch=compute_89,code=compute_89 -gencode=arch=compute_89,code=sm_89`.

*(The first attempt used `torch.utils.cpp_extension.load_inline`, which needs **ninja**; ninja is not installed in any env, so `load_inline`/`torch.utils.cpp_extension.load` cannot be used on this box as-is. The `setup.py` + `BuildExtension.with_options(use_ninja=False)` route works. Installing ninja into the envs would fix `load_inline`, but that means modifying the conda envs, which was out of scope.)*

### 5.4 gdm and sessions after the reload

```
$ systemctl is-active gdm
active
$ systemctl status gdm | head -4
● gdm.service - GNOME Display Manager
     Loaded: loaded (/lib/systemd/system/gdm.service; static)
     Active: active (running) since Sat 2026-10-10 22:41:29 CST; 3min 7s ago
     Main PID: 128965 (gdm3)

$ loginctl list-sessions --no-legend
 176 1000 agilex
  35 1000 agilex       pts/1
  c3  128 gdm    seat0 tty1        ← greeter (class=greeter, x11, seat0)
$ who
agilex   pts/1        2026-10-10 21:48 (<gpu-host>)     ← only the SSH session
```

**gdm is active and its greeter is rendering on the nvidia GPU** (Xorg + gnome-shell appear in `nvidia-smi`). But the **user's desktop session did not come back** — see §9, this needs a decision.

---

## 6. CUDA PATH bug — FIXED

`~/.bashrc` lines 138–139 point at `/usr/local/cuda-11.8`, which does not exist. Per instructions I did **not** touch `~/.bashrc`; the Lead owns that single integration edit.

**Created:** `/home/agilex/.dsh/cuda-env.sh` (mode 0644, owned `agilex:agilex`, 1938 bytes)

* Resolves the real toolkit via `readlink -f /usr/local/cuda` (so it follows a future symlink move automatically), with a fallback scan of `/usr/local/cuda-12.8`, `-12.9`, `-13.0`.
* Exports `CUDA_HOME`, `CUDA_PATH`, prepends `$CUDA_HOME/bin` to `PATH` and `$CUDA_HOME/lib64` to `LD_LIBRARY_PATH`.
* **Guarded and idempotent** — sourcing twice does not duplicate entries (verified below).
* Safe under `set -u` (all expansions use `${VAR:-}`).
* Adds a `cuda-info` helper that prints `CUDA_HOME`, the nvcc banner and the GPU.
* Prepending `lib64` to `LD_LIBRARY_PATH` was verified safe: the toolkit's `lib64` contains **no** `libcuda.so.1` (only `lib64/stubs/libcuda.so`), so it cannot shadow the driver library.

**Smoke test (non-interactive shell, `~/.bashrc` not read):**

```
$ bash -c 'source ~/.dsh/cuda-env.sh && which nvcc && nvcc --version'
/usr/local/cuda-12.8/bin/nvcc
nvcc: NVIDIA (R) Cuda compiler driver
Copyright (c) 2005-2025 NVIDIA Corporation
Built on Fri_Feb_21_20:23:50_PST_2025
Cuda compilation tools, release 12.8, V12.8.93
Build cuda_12.8.r12.8/compiler.35583870_0

$ bash -c 'source ~/.dsh/cuda-env.sh; source ~/.dsh/cuda-env.sh; echo "$PATH" | tr ":" "\n" | grep -c cuda-12.8/bin'
1        ← idempotent (would be 2 if unguarded)
$ bash -c 'source ~/.dsh/cuda-env.sh && cuda-info'
CUDA_HOME=/usr/local/cuda-12.8
Cuda compilation tools, release 12.8, V12.8.93
NVIDIA GeForce RTX 4060, 580.178.04
```

**Exact line for the Lead's single `~/.bashrc` edit** (and the dead 11.8 lines 138–139 should be deleted at the same time):

```bash
[ -f "$HOME/.dsh/cuda-env.sh" ] && source "$HOME/.dsh/cuda-env.sh"
```

---

## 7. CUDA toolkits installed

```
$ ls -d /usr/local/cuda*
/usr/local/cuda
/usr/local/cuda-12.8
/usr/local/cuda-12.9

$ ls -l /usr/local/cuda
lrwxrwxrwx 1 root root 20 10月 10 22:57 /usr/local/cuda -> /usr/local/cuda-12.8

$ /usr/local/cuda-12.8/bin/nvcc --version | tail -2      # pre-existing
Cuda compilation tools, release 12.8, V12.8.93
Build cuda_12.8.r12.8/compiler.35583870_0
$ head -5 /usr/local/cuda-12.8/version.json  → "version" : "12.8.20250303"

$ /usr/local/cuda-12.9/bin/nvcc --version | tail -2      # NEW, installed by me
Cuda compilation tools, release 12.9, V12.9.86
Build cuda_12.9.r12.9/compiler.36037853_0
$ head -5 /usr/local/cuda-12.9/version.json  → "version" : "12.9.20250531"
```

Both are **tarball/`.run` installs** — there are no CUDA dpkg packages on this box (`dpkg -l | grep -i cuda` is empty). I matched that existing style deliberately.

**cuDNN: 8.9.7, present only inside `cuda-12.8`.**

```
$ ls /usr/local/cuda-12.8/include/cudnn.h          → present
$ ls /usr/local/cuda-12.8/lib64/libcudnn.so.8.9.7  → present
$ ls /usr/local/cuda-12.9/include/cudnn*.h         → No such file or directory
```

No conda env has a `cudatoolkit` / `cuda-version` conda package; all CUDA runtime comes from the pip wheels (`nvidia-cuda-runtime-cu12`, `nvidia-cudnn-cu12`).

| env | torch | torch.version.cuda | cuda.is_available() | pip CUDA/cuDNN packages |
|---|---|---|---|---|
| `aloha` | 2.7.1+cu128 | 12.8 | **True** | `nvidia-cuda-runtime-cu12 12.8.57`, `nvidia-cudnn-cu12 9.7.1.26`, `nvidia-cuda-nvrtc-cu12 12.8.61`, `nvidia-cuda-cupti-cu12 12.8.57` |
| `lerobot` | 2.7.1+cu126 | 12.6 | **True** | `nvidia-cuda-runtime-cu12 12.6.77`, `nvidia-cudnn-cu12 9.5.1.17`, `nvidia-cuda-nvrtc-cu12 12.6.77`, `nvidia-cuda-cupti-cu12 12.6.80` |
| `data` | *(no torch)* | — | — | — |
| `base` | *(no torch)* | — | — | — |

---

## 8. Leftover 570 driver packages — purged (safely)

`nvidia-driver-570` (570.211.01) and `nvidia-driver-580` were **both** fully installed. The 570 stack was the leftover generation that caused the mismatch to be possible. All 570 packages come from the official `jammy-updates/restricted` repo on the USTC mirror (not a PPA).

**Simulation first** — `apt-get -s purge <full list>`:

```
0 upgraded, 0 newly installed, 21 to remove and 1 not upgraded.
The following packages will be REMOVED:   ← all 21, every one a 570 package
  libnvidia-cfg1-570* libnvidia-common-570* libnvidia-compute-570*
  libnvidia-compute-570:i386* libnvidia-decode-570* libnvidia-decode-570:i386*
  libnvidia-encode-570* libnvidia-encode-570:i386* libnvidia-extra-570*
  libnvidia-fbc1-570* libnvidia-fbc1-570:i386* libnvidia-gl-570*
  libnvidia-gl-570:i386* nvidia-compute-utils-570* nvidia-dkms-570*
  nvidia-driver-570* nvidia-firmware-570-570.195.03* nvidia-kernel-common-570*
  nvidia-kernel-source-570* nvidia-utils-570* xserver-xorg-video-nvidia-570*
```
Machine-checked verdict: **`NON-570 packages that would be removed: <none>`** → SAFE.

Pre-flight checks I ran before purging (all clear):

* `/etc/modprobe.d/nvidia-graphics-drivers-kms.conf` (`options nvidia_drm modeset=1`) is owned by **`nvidia-kernel-common-580`**, so purging 570 cannot delete it. **Verified still present after the purge.**
* `update-alternatives --get-selections | grep -i nvidia` → **empty**; no alternative could be repointed.
* Live GL/NVML libs are 580-owned: `libGLX_nvidia.so.0 -> libGLX_nvidia.so.580.178.04` (owner `libnvidia-gl-580:amd64`), `libnvidia-ml.so.1 -> libnvidia-ml.so.580.178.04`.
* `xserver-xorg-video-nvidia-570` ships **only `/usr/share`** (doc files); the live X driver `/usr/lib/x86_64-linux-gnu/nvidia/xorg/nvidia_drv.so` is 580-owned.
* The only files the 570 packages own under sensitive paths are the 570 GSP firmware blobs, which are exactly what should go; the 580 blobs `/lib/firmware/nvidia/580.178.04/{gsp_ga10x,gsp_tu10x}.bin` are present.

**Executed:** `apt-get -y purge` (deliberately **without** `--auto-remove` — see §9.1).

```
dpkg -l | grep -c 570 :  24  →  3      (the 3 remaining are unrelated ROS packages whose VERSION strings contain "570":
                                        ros-humble-ament-cmake-export-targets, ros-humble-ros2-control-test-assets, ros-humble-tf2-ros-py)
After this operation, 93.8 MB disk space will be freed.

$ dpkg -l | grep -E 'nvidia-driver|nvidia-utils-|nvidia-dkms|xserver-xorg-video-nvidia'
ii nvidia-dkms-580               580.178.04-0ubuntu0.22.04.1
ii nvidia-driver-580            580.178.04-0ubuntu0.22.04.1
ii nvidia-utils-580             580.178.04-0ubuntu0.22.04.1
ii xserver-xorg-video-nvidia-580 580.178.04-0ubuntu0.22.04.1
```

Post-purge integrity checks — **all passed**:

* `/lib/modules/6.8.0-90-generic/updates/dkms/nvidia.ko` still present (113,913,445 bytes)
* `dkms status` still lists `nvidia/580.178.04` for **both** `6.8.0-90-generic` and `6.8.0-138-generic`
* modeset conf intact; 580 GSP firmware intact; `/lib/firmware/nvidia/570.195.03` gone
* `nvidia-smi` still rc=0, gdm still active, GL/NVML symlinks still 580
* `apt-get -s check` clean, no broken/half-installed packages

---

## 9. Outstanding items — needs a decision

### 9.1 ⚠️ LANDMINE: never run `apt autoremove` on this box

`apt` currently classifies the **working 580 driver** as "automatically installed and no longer required":

```
The following packages were automatically installed and are no longer required:
  ...
  nvidia-compute-utils-580  nvidia-dkms-580  nvidia-driver-580
  nvidia-kernel-source-580  nvidia-prime  nvidia-settings  nvidia-utils-580
  xserver-xorg-video-nvidia-580
  python3-pygraphviz  screen-resolution-extra  ...
```

**`apt autoremove` (or `apt-get autoremove`, or `apt --fix-broken` style flows that autoremove) WOULD UNINSTALL THE 580 DRIVER AND THE DESKTOP GPU STACK.** This is why I used plain `apt-get purge` with no `--auto-remove`, and why nothing else on this box should run autoremove without `-s` first. Flagging it for the Lead and the other agents.

### 9.2 The desktop did not come back by itself — someone must log in

* **No autologin is configured** — `/etc/gdm3/custom.conf` has `#  AutomaticLoginEnable = true` / `#  AutomaticLogin = user1` commented out.
* **No monitor is attached** — every DRM connector reports `disconnected`:
  ```
  /sys/class/drm/card0/card0-DP-1/status: disconnected
  /sys/class/drm/card0/card0-DP-2/status: disconnected
  /sys/class/drm/card0/card0-DP-3/status: disconnected
  /sys/class/drm/card0/card0-HDMI-A-1/status: disconnected
  ```
  So there is **no physical console** to log in at; the desktop is remote-only.

Consequently the box now sits at the **GDM login screen**, with the greeter rendering on the nvidia GPU. The user's previous session (agilex on tty2) was ended by the gdm stop, as the authorised procedure requires.

**How to get the desktop back:** the remote-access daemons all survived the reload —

```
gdm                  active / static
ssh                  active / enabled
nxserver             active / enabled      (NoMachine)
runsunloginclient    active / enabled      (SunLogin / 向日葵)
```

so the user can reconnect with **SunLogin** or **NoMachine** and log in at the greeter.

**If the desktop should come back on its own**, that requires uncommenting `AutomaticLoginEnable=true` + `AutomaticLogin=agilex` in `/etc/gdm3/custom.conf` (plus a `systemctl restart gdm`). That file is **outside my write scope**, so I did not touch it — say the word and I will.

### 9.3 A reboot is now optional, not required

The box has kernel `6.8.0-138-generic` installed but boots `6.8.0-90-generic`, and still shows "System restart required". The 580 dkms module **is** already built for `6.8.0-138` (`dkms status`), so a future reboot will come up on 580 + the newer kernel. But **a reboot is no longer needed to fix nvidia-smi** — that is already fixed live. Recommendation: reboot whenever convenient, not urgently; if it is done, verify with `nvidia-smi` and `cat /proc/driver/nvidia/version` (expect 580.178.04).

---

## 10. CUDA upgrade evaluation — what I installed and why

### 10.1 What is available for Ubuntu 22.04

| Toolkit | Available | Size | Min. driver (Linux) | Compatible with this driver (580.178.04)? |
|---|---|---|---|---|
| 12.8.93 (installed) | yes | — | 570.x | yes |
| **12.9.1 (V12.9.86)** | yes | 5.86 GB | **575.57.08** | **yes** |
| **13.0.0** | yes | 4.30 GB | **580.65.06** | yes — *just* (580.178.04 ≥ 580.65.06) |
| 13.0.1 / 13.1+ | **no** (404) | — | — | — |

`nvidia-smi` reports `CUDA Version: 13.0`, i.e. **13.0 is the newest CUDA this driver supports**. No CUDA apt repo is configured; I used the `.run` installer from the NVIDIA CDN (which redirects to `developer.download.nvidia.cn`, ~6.5 MB/s from this box).

### 10.2 Decision: installed **12.9.1 alongside**, and left `/usr/local/cuda` on **12.8**

```
$ sh cuda_12.9.1_575.57.08_linux.run --silent --toolkit \
      --toolkitpath=/usr/local/cuda-12.9 --no-man-page --override      → installer rc=0
```
* `--toolkit` (no `--driver`) → the driver was **not** touched. Verified afterwards: still `580.178.04`, `libcuda.so.1 -> libcuda.so.580.178.04` unchanged.
* Purely additive: it creates `/usr/local/cuda-12.9` and `/etc/ld.so.conf.d/cuda-12-9.conf`. Reversible with `rm -rf /usr/local/cuda-12.9 /etc/ld.so.conf.d/cuda-12-9.conf && ldconfig`.
* **The installer silently repointed `/usr/local/cuda` → `cuda-12.9`.** I detected this and **restored it to `cuda-12.8`**. This is worth knowing for anyone who runs the installer again.
* Loader-cache side effect checked: both conf files are now present, but the cache still resolves `libcudart.so.12` to **`/usr/local/cuda-12.8/targets/x86_64-linux/lib/libcudart.so.12`** (12.8 sorts first), so system-wide library resolution is **unchanged**.

**Why the symlink stays on 12.8 — this is the deciding argument:**

> **cuDNN 8.9.7 exists only inside `/usr/local/cuda-12.8`.** Repointing `/usr/local/cuda` to 12.9 would make `$CUDA_HOME/include/cudnn.h` and `-L$CUDA_HOME/lib64 -lcudnn` **disappear**, breaking any build that links cuDNN through `CUDA_HOME` (TensorRT / ONNX / PyTorch extensions — very common in this robotics stack). 12.9 ships no cuDNN.
>
> Secondary reason: keeping the default on 12.8 keeps torch extension builds exactly version-matched with `aloha`'s cu128 torch.

Per the task's instruction ("only repoint if it is clearly safe"), it is **not** clearly safe, so I left it and am reporting it. The new toolkit is fully usable by opting in:

```bash
export CUDA_HOME=/usr/local/cuda-12.9
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
nvcc --version        # → release 12.9, V12.9.86
```
or just call it directly: `/usr/local/cuda-12.9/bin/nvcc`.

### 10.3 PyTorch compatibility — tested, not assumed

torch's own rule, read out of the **installed** `torch/utils/cpp_extension.py`:

```python
if cuda_ver.major != torch_cuda_version.major:
    raise RuntimeError(CUDA_MISMATCH_MESSAGE.format(cuda_str_version, torch.version.cuda))
warnings.warn(CUDA_MISMATCH_WARN.format(cuda_str_version, torch.version.cuda))
```

→ **minor** version mismatch = **warning**; **major** mismatch = **hard `RuntimeError`**.

Verified empirically by building a real `CUDAExtension` (`addone`) three ways:

| Build | torch | nvcc | Result |
|---|---|---|---|
| control | 2.7.1+cu128 (`aloha`) | 12.8 | rc=0, no mismatch message |
| **new toolkit** | 2.7.1+cu128 (`aloha`) | **12.9** | **rc=0**, only `UserWarning: The detected CUDA version (12.9) has a minor version mismatch with the version that was used to compile PyTorch (12.8). Most likely this shouldn't be a problem.` → kernel ran: `[1.0, 1.0, 1.0, 1.0]` |
| **new toolkit** | 2.7.1+cu126 (`lerobot`) | **12.9** | **rc=0**, same benign minor-mismatch warning |

**So: CUDA 12.9 is safe with every torch on this box** (all are CUDA 12.x). CUDA 13.0 would be a **major** mismatch → `RuntimeError` for any extension build against these envs.

### 10.4 Recommendation on 13.0 — deliberately **not** installed

CUDA 13.0.0 is downloadable and this driver supports it, so it *would* install cleanly. I did **not** install it, because:

1. **It cannot be used with the current PyTorch stack.** Every torch here is cu126/cu128; nvcc 13.0 vs torch cu12x is a **major** mismatch → torch raises `RuntimeError` (rule quoted above). It would sit on disk unused.
2. **It drops older architectures** and is a breaking major release (new sonames, `libcudart.so.13`, CCCL 3.x).
3. It costs 4.3 GB of download/disk for no current benefit.

**Install it only when/if the torch envs are migrated to cu130 wheels.** The command is then:

```bash
curl -O https://developer.download.nvidia.cn/compute/cuda/13.0.0/local_installers/cuda_13.0.0_580.65.06_linux.run
sh cuda_13.0.0_580.65.06_linux.run --silent --toolkit --toolkitpath=/usr/local/cuda-13.0 --no-man-page --override
ln -sfn /usr/local/cuda-12.8 /usr/local/cuda     # the installer WILL move this — put it back
```
(and note cuDNN for CUDA 13 is v9.x, so cuDNN would have to be installed into the 13.0 tree too).

**Net answer to "upgrade to a newer CUDA": done — 12.8 → 12.9.1, which is the newest toolkit that is actually usable with this box's PyTorch, and it is verified working.**

---

## 11. Driver upgrade options (report only — nothing installed)

The configured repo offers much newer drivers than the installed 580.178.04:

```
nvidia-driver-580   Candidate: 580.178.04-0ubuntu0.22.04.1     ← installed & working
nvidia-driver-590   Candidate: 590.48.01-0ubuntu0.22.04.4
nvidia-driver-595   Candidate: 595.99.02-0ubuntu0.22.04.1
nvidia-driver-610   Candidate: 610.57.04-0ubuntu0.22.04.4
nvidia-driver-615   Candidate: 615.71.09-0ubuntu0.22.04.1
```
(`nvidia-driver-610`/`615` are described as transitional packages.)

**Recommendation: do not upgrade the driver now.** 580.178.04 works, supports CUDA up to 13.0 (more than the hardware needs), and is the newest version in the repo *for the 580 branch*. Any driver upgrade re-creates exactly the failure mode fixed today (new module on disk + old module resident) unless it is followed by the same live reload or a reboot. If a newer driver is ever wanted, the safe order is: install → then either reboot, or re-run the §4 procedure.

---

## 12. What changed on the robot (complete list)

| Path | Change |
|---|---|
| `/tmp/nvidia-reload.sh` | **created** (0755) — the reload script |
| `/tmp/nvidia-reload.log` | created (script output) |
| `/home/agilex/.dsh/cuda-env.sh` | **created** (0644) — CUDA env, resolves `/usr/local/cuda` |
| `/usr/local/cuda-12.9/` | **created** — CUDA 12.9.1 toolkit |
| `/etc/ld.so.conf.d/cuda-12-9.conf` | created by the installer |
| `/usr/local/cuda` | installer moved it to 12.9; **restored to `cuda-12.8`** |
| 21 × `*-570` dpkg packages | **purged** (93.8 MB freed) |
| `/lib/firmware/nvidia/570.195.03/` | removed with the 570 firmware package |
| `/tmp/extbuild/`, `/tmp/torch_ext_*` | test scratch (extension build proof, 34 MB) |
| `/tmp/cuda-12.9.1_575.57.08_linux.run` | downloaded installer, **5.5 GB left in `/tmp`** — harmless (auto-cleared on reboot); `rm -f` it if the space is wanted |
| kernel module in memory | 570.195.03 → **580.178.04** |

No stray systemd units remain (`nvidia-reload` collected, `gdm-rescue` timer fired and gone), no background jobs are running, and disk sits at 5 % (80 G used of 1.8 T).

**Not touched:** `~/.bashrc`, `~/.zshrc`, `/home/linuxbrew`, `/opt/ros`, `~/piper_ros`, `~/agilex_ws`, `/etc/gdm3/custom.conf`, all conda envs, `/usr/local/cuda-12.8`. No reboot, no `apt-get upgrade`/`dist-upgrade`/`do-release-upgrade`.

---

## 13. Rollback procedures

* **Undo the CUDA 12.9 install** (leaves everything else alone):
  `rm -rf /usr/local/cuda-12.9 /etc/ld.so.conf.d/cuda-12-9.conf && ldconfig`
* **Undo the reload** (back to the 570 module): requires rebooting into a state with the 570 module — but note 570 is now purged, so the *only* sane rollback is to stay on 580. If 580 ever misbehaves, reinstall the 570 stack with
  `apt-get install -y nvidia-driver-570` (then a reload or reboot), or `apt-get install --reinstall nvidia-driver-580`.
* **Restore the CUDA env**: delete `/home/agilex/.dsh/cuda-env.sh` (and remove the `source` line from `~/.bashrc`).
* **If gdm ever fails to start**: `systemctl start gdm`; `systemctl status gdm`; `journalctl -u gdm -n 50`. The 580 module and the greeter are both verified working, and the greeter has already started successfully twice today.
* **If nvidia-smi breaks again after any driver package change**: it is the same mismatch — run the §4 procedure (or reboot).

---

## 14. Honest list of caveats / what did not go perfectly

1. **The user's desktop session is gone** and will not return on its own (no autologin, no monitor attached). The greeter is up and remote access daemons are alive, so a remote login restores it. This is a real consequence of the authorised gdm stop — flagged in §9.2.
2. **`torch.utils.cpp_extension.load_inline` / `.load()` do not work on this box** because **ninja is not installed** in any conda env. I worked around it with `BuildExtension.with_options(use_ninja=False)`. Fixing it means installing ninja into the envs — out of scope, not done.
3. **`lerobot`'s torch is cu126 while `aloha`'s is cu128.** Both work, but the inconsistency is a latent annoyance; I did not touch either env.
4. **Two lines in the reload log (`STILL HOLDING: ... bash` / `... sort`) are false positives of my own diagnostic** — filename matches, not GPU holders. `fuser -v` was empty. Explained in §4.2.
5. **The CUDA 12.9.1 installer repointed `/usr/local/cuda` without asking.** Caught and reverted; recorded here because it will happen again to anyone who runs it.
6. **The original task's "Blackwell-class" premise was wrong** (it is an Ada RTX 4060, sm_89). This did not change the fix, but it invalidates the "12.8 is required" reasoning and it changes the 13.x upgrade calculus.
7. **The initial purge simulation was incomplete** — my first `dpkg-query` filter dropped the `:amd64` multi-arch qualifiers and so listed only 14 of the 21 packages. I caught it, regenerated the list, re-simulated (21 packages, all 570, verdict SAFE) and only then purged. The final numbers in §8 are from the corrected run.
8. **A reboot is still pending** for the newer kernel `6.8.0-138`. Not required for nvidia-smi any more, but the box still reports "System restart required".

---

## Appendix — evidence index

| Evidence | Where |
|---|---|
| Full reload log | [`reports/evidence/nvidia-reload.log`](evidence/nvidia-reload.log) and §4.3 |
| sshd independence | §3 |
| `/proc/driver/nvidia/version` before/after | §2.1, §4.3, §5.1 |
| `nvidia-smi` before (failure) / after (working) | §2.1, §4.3, §5.1 |
| gdm active + sessions + who after | §5.4 |
| `cuda-env.sh` smoke test | §6 |
| Toolkits + `/usr/local/cuda` symlink | §7 |
| conda env torch/CUDA versions | §7 |
| 570 purge list + before/after counts | §8 |
| PyTorch-extension compatibility proof (12.8 / 12.9 / cu126 / cu128) | §10.3 |
| CUDA 13.0 availability + min-driver checks | §10.1 |
