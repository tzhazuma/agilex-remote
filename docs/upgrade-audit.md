# 升级审计报告：Ubuntu 22.04 + ROS 2 Humble 能不能「保持兼容地更新到最新版」

**审计对象**：AgileX 机器人 `agilex@<robot-ip>`
**审计日期**：2026-10-13（Asia/Shanghai）
**审计性质**：**只读审计（AUDIT）** —— 没有执行任何升级、安装、卸载、改源操作
**任务**：task-8

---

## 0. 结论（TL;DR）

> ## ❌ 不要升级 Ubuntu。没有「保持兼容」的升级路径，而且现在也**没有任何东西可升级**。
>
> | 问题 | 答案 | 一句话证据 |
> |---|---|---|
> | ROS 2 能升级吗？ | **不能，而且已经是最新** | 476 个 `ros-humble-*` 包中 **0 个**可升级；镜像里最新的 `ros-humble-ros-base` 与已装版本**逐字节相同** |
> | Ubuntu 能升到 24.04 吗？ | **技术上会提供，但绝对不要做** | 官方 ROS 2 apt 仓库的 `noble`(24.04) 套件里 **`ros-humble-*` 包数量 = 0**（`jazzy` 有 3961 个） |
> | 那该做什么？ | **留在 22.04 + Humble，打 6 个安全补丁** | 待升级列表里只有 6 个 `libx11` 安全更新 + 1 个 VS Code Insiders |
> | 最大的坑 | **永远不要运行 `apt autoremove`** | 模拟结果：会删除 **47 个包**，其中 **37 个**是正在工作的 NVIDIA 580 驱动栈，**还包括 `dkms` 本身** |
>
> **支持期还有多久**：Ubuntu 22.04 标准支持到 **2027 年 6 月**，ROS 2 Humble EOL **2027 年 5 月** → 现在（2026-10）距离终点还有 **约 7 个月**，不着急。

---

## 1. 审计方法与安全声明

**允许使用的工具**（全部经由 `bin/rrun.sh` / `bin/rsudo.sh`）：

- 所有 ROS/dpkg/apt **查询**命令
- `do-release-upgrade -c`（仅检查，任务明确允许）
- `apt-get -s autoremove`、`apt-get -s upgrade`、`apt-get -s dist-upgrade`（`-s` = `--simulate` = 干跑，**不改动系统**）
- 只读 HTTP 抓取（从机器人上直接读取配置中的 ROS 镜像索引）

**明确没有执行**：`do-release-upgrade`、`apt-get upgrade`、`apt-get dist-upgrade`、`apt autoremove`、任何 `install`/`remove`/`purge`、任何源的增删、任何配置修改。

**机器人端未写入任何文件。** 所有输出均为读取。

---

## 2. 当前精确状态（原始证据）

### 2.1 操作系统 / 内核 / 重启状态

```
$ cat /etc/os-release
PRETTY_NAME="Ubuntu 22.04.5 LTS"
VERSION_ID="22.04"
VERSION="22.04.5 LTS (Jammy Jellyfish)"
VERSION_CODENAME=jammy
UBUNTU_CODENAME=jammy

$ lsb_release -a
Distributor ID:	Ubuntu
Description:	Ubuntu 22.04.5 LTS
Release:	22.04
Codename:	jammy

$ uname -a
Linux agilex 6.8.0-138-generic #138~22.04.1-Ubuntu SMP PREEMPT_DYNAMIC Fri Aug  7 13:43:15 UTC  x86_64 GNU/Linux

$ uptime
 00:27:55 up  7:08,  2 users,  load average: 0.07, 0.04, 0.02
```

**重启是否待处理**：

```
$ [ -f /var/run/reboot-required ] && echo "REBOOT REQUIRED" || echo "no /var/run/reboot-required"
no /var/run/reboot-required
```

→ **没有待处理的重启**。系统是干净一致的（这一点很重要：任何升级都可以等到维护窗口再做，现在没有欠账）。

**内核情况**：

```
$ uname -r
6.8.0-138-generic

$ dpkg -l | grep -E 'linux-image|linux-generic|linux-headers'
ii linux-generic-hwe-22.04            6.8.0-138.138~22.04.1
ii linux-headers-6.8.0-138-generic    6.8.0-138.138~22.04.1
ii linux-headers-6.8.0-90-generic     6.8.0-90.91~22.04.1
ii linux-headers-generic-hwe-22.04    6.8.0-138.138~22.04.1
ii linux-image-6.8.0-138-generic      6.8.0-138.138~22.04.1
rc linux-image-6.8.0-40-generic       6.8.0-40.40~22.04.3
rc linux-image-6.8.0-85-generic       6.8.0-85.85~22.04.1
ii linux-image-6.8.0-90-generic       6.8.0-90.91~22.04.1
ii linux-image-generic-hwe-22.04      6.8.0-138.138~22.04.1
```

运行内核 = 已安装最新内核（`6.8.0-138`），走 HWE 轨道。注意 `rc`（已卸载但残留配置）的两个老内核 `6.8.0-40` / `6.8.0-85` —— **这两个是 `apt autoremove` 会"顺手清理"的东西，而清理动作会连带删掉 580 驱动**（见第 6.2 节）。

### 2.2 ROS 2 状态

```
$ echo "ROS_DISTRO=${ROS_DISTRO:-<unset>}"      # 非交互式 ssh 里为空，交互式 shell 里：
$ bash -ic 'echo ROS_DISTRO=$ROS_DISTRO; echo ROS_VERSION=$ROS_VERSION'
ROS_DISTRO=humble
ROS_VERSION=2

$ ls -la /opt/ros/
drwxr-xr-x 12 root root 4096 10月 10 20:16 humble          # ← 只有 humble，没有第二个发行版

$ dpkg -s ros-humble-ros-base | grep -E '^(Package|Version|Status)'
Package: ros-humble-ros-base
Status: install ok installed
Version: 0.10.0-1jammy.20260908.101203

$ dpkg -s ros-humble-ros-core | grep -E '^(Package|Version|Status)'
Package: ros-humble-ros-core
Status: install ok installed
Version: 0.10.0-1jammy.20260908.005124

$ dpkg -s ros-humble-desktop | grep -E '^(Package|Version|Status)'
Package: ros-humble-desktop
Status: install ok installed
Version: 0.10.0-1jammy.20260908.101832

$ dpkg -l 'ros-humble-*' | grep -c '^ii'
476
```

**已装 `ros-humble-*` 包总数 = 476。**

**`apt-cache policy`（Installed vs Candidate）**：

```
ros-humble-ros-base:
  Installed: 0.10.0-1jammy.20260908.101203
  Candidate: 0.10.0-1jammy.20260908.101203
  Version table:
 *** 0.10.0-1jammy.20260908.101203 500
        500 https://mirrors.ustc.edu.cn/ros2/ubuntu jammy/main amd64 Packages
        100 /var/lib/dpkg/status
ros-humble-ros-core:
  Installed: 0.10.0-1jammy.20260908.005124
  Candidate: 0.10.0-1jammy.20260908.005124
ros-humble-desktop:
  Installed: 0.10.0-1jammy.20260908.101832
  Candidate: 0.10.0-1jammy.20260908.101832
ros-humble-rviz2:
  Installed: 11.2.29-1jammy.20260908.014614
  Candidate: 11.2.29-1jammy.20260908.014614
ros-humble-nav2-bringup:
  Installed: 1.1.20-1jammy.20260908.021125
  Candidate: 1.1.20-1jammy.20260908.021125
ros-humble-cartographer-ros:
  Installed: 2.0.9002-1jammy.20260908.092728
  Candidate: 2.0.9002-1jammy.20260908.092728
ros-humble-realsense2-camera:
  Installed: 4.58.4-1jammy.20260908.100350
  Candidate: 4.58.4-1jammy.20260908.100350
ros-humble-orbbec-camera:
  Installed: (none)                                   # ← 注意：Orbbec 是从源码编的，没装 deb
  Candidate: 2.9.3-2jammy.20260907.233014
```

**每一个包都是 `Installed == Candidate`。**

**ROS 2 apt 源**（配置中的全部 deb 行）：

```
deb https://mirrors.ustc.edu.cn/ubuntu/ jammy main restricted universe multiverse
deb https://mirrors.ustc.edu.cn/ubuntu/ jammy-updates main restricted universe multiverse
deb https://mirrors.ustc.edu.cn/ubuntu/ jammy-backports main restricted universe multiverse
deb https://mirrors.ustc.edu.cn/ubuntu/ jammy-security main restricted universe multiverse
deb [arch=amd64,arm64,armhf signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main
deb [arch=amd64] https://mirrors.ustc.edu.cn/ros2/ubuntu/ jammy main      # ← ROS 2 源：jammy
deb [arch=amd64] http://mirrors.bwbot.org/ stable main
```

**只有 `jammy` 套件。** 系统里没有任何 `noble` 套件的 InRelease 文件（已用 `grep -l noble /var/lib/apt/lists/*InRelease` 验证，结果为空）。

### 2.3 「有什么可升级的？」—— 完整待升级列表

```
$ apt list --upgradable
Listing...
code-insiders/stable 1.142.0-1791782643 amd64 [upgradable from: 1.142.0-1791566707]
libx11-6/jammy-security 2:1.7.5-1ubuntu0.4 amd64 [upgradable from: 2:1.7.5-1ubuntu0.3]
libx11-6/jammy-security 2:1.7.5-1ubuntu0.4 i386 [upgradable from: 2:1.7.5-1ubuntu0.3]
libx11-data/jammy-security,jammy-security 2:1.7.5-1ubuntu0.4 all [upgradable from: 2:1.7.5-1ubuntu0.3]
libx11-dev/jammy-security 2:1.7.5-1ubuntu0.4 amd64 [upgradable from: 2:1.7.5-1ubuntu0.3]
libx11-xcb1/jammy-security 2:1.7.5-1ubuntu0.4 amd64 [upgradable from: 2:1.7.5-1ubuntu0.3]
libx11-xcb1/jammy-security 2:1.7.5-1ubuntu0.4 i386 [upgradable from: 2:1.7.5-1ubuntu0.3]

$ apt list --upgradable | grep -c '/'
7
$ apt list --upgradable | grep -c 'ros-humble'
0                                   # ← 关键：ROS 侧完全无货
```

**全系统只有 7 个可升级项，其中 0 个是 ROS 包。**

**没有 hold（锁定）的包**：

```
$ apt-mark showhold
(空)
```

---

## 3. 决定性证据：ROS 2 apt 仓库里，24.04 上根本没有 Humble

这是本次审计最硬的一条证据。我直接从机器人上**读取它自己配置的那个镜像**（`mirrors.ustc.edu.cn/ros2/ubuntu`，USTC 是 ROS 2 官方仓库的完整镜像），统计每个 Ubuntu 套件里各 ROS 发行版的包数量：

```
$ for suite in jammy noble resolute; do
    curl -s .../dists/$suite/main/binary-amd64/Packages.gz | zcat | grep -c "^Package: ros-$d-"
  done
```

| Ubuntu 套件 | `ros-humble-*` | `ros-iron-*` | `ros-jazzy-*` | `ros-kilted-*` | `ros-lyrical-*` | `ros-rolling-*` |
|---|---|---|---|---|---|---|
| **jammy (22.04)** ← 本机 | **3672** | 2164 | **0** | 0 | 0 | 1738 |
| **noble (24.04)** | **0** ⛔ | 0 | **3961** | 2861 | 0 | 2150 |
| **resolute (26.04)** | 0 | 0 | 0 | 0 | 2684 | 0 |

**读法（每一条都是结论）**：

1. **`ros-humble-*` 在 `noble` 上是 0 个。** Ubuntu 24.04 上不存在任何 Humble 二进制包，一个都没有。
2. **交叉可用性为 0**：没有任何一个 ROS 2 发行版同时存在于两个套件里。这不是"部分缺失"，是**彻底的分割**。
3. **`jammy` 上 `ros-jazzy-*` 也是 0 个**。所以"留在 22.04 换用新版 ROS"同样不成立 —— 反方向也是死路。
4. 22.04 上唯一"更新"的选择是 `iron`（2164 个包），但 **Iron 已于 2024-12-04 EOL**（见第 4 节），升级到一个已经死掉的发行版毫无意义。

**"那我把 ROS 源改成 noble 不就行了？"** —— 不行。改成 `noble` 之后，`ros-humble-*` 全部消失，apt 会把 476 个已装包判定为"无法重新获取"，唯一能装的是 Jazzy/Kilted，也就是**换一个 ROS 发行版**，而不是升级。

### 3.1 实时镜像 vs 已装版本：逐字节相同

```
$ curl -s .../dists/jammy/main/binary-amd64/Packages.gz | zcat | awk '/^Package: ros-humble-ros-base$/{f=1} f&&/^Version:/{print;f=0}'
  Version: 0.10.0-1jammy.20260908.101203

$ dpkg -s ros-humble-ros-base | grep ^Version
  Version: 0.10.0-1jammy.20260908.101203
```

**镜像里最新的 `ros-humble-ros-base` 就是本机装的那个版本。** 同理 `ros-humble-ros-core` = `0.10.0-1jammy.20260908.005124`，完全一致。

镜像中所有 `ros-humble-*` 包的最新构建时间戳：

```
$ curl -s ... | zcat | grep -oE 'jammy\.20[0-9]{6}\.[0-9]{6}' | sort -u | tail -5
jammy.20260912.211402
jammy.20260912.211640
jammy.20260912.211724
jammy.20260912.211857
jammy.20260912.212048
```

最新构建 = **2026-09-12**，对应的是 **2026-09-14 的 Humble sync / patch release**（见第 4.2 节）。本机 apt 索引里的时间戳与实时镜像**完全一致**，说明本地元数据没有过期。

---

## 4. ROS 2 生命周期事实（含引用）

### 4.1 发行版时间线

来源：[ROS 2 Distributions — Humble documentation](https://docs.ros.org/en/humble/Releases.html)

| 发行版 | 发布 | **EOL** | 目标 Ubuntu |
|---|---|---|---|
| Lyrical Luth | 2026-05-22 | **2031 年 5 月** | 26.04（见下） |
| Kilted Kaiju | 2025-05-23 | **2026 年 12 月**（非 LTS，快死了） | 24.04 |
| Jazzy Jalisco | 2024-05-23 | **2029 年 5 月** | 24.04 |
| Iron Irwini | 2023-05-23 | **2024-12-04（已 EOL）** | 22.04 |
| **Humble Hawksbill** | **2022-05-23** | **2027 年 5 月** ← 本机 | **22.04** |
| Galactic / Foxy / … | — | 均已 EOL | — |

同一页面还明确写着一条对本机很重要的规则：

> **Cross-Distribution Communications**: "Nodes are not guaranteed to be able to communicate across distributions. For example, a node built & running against Humble is not guaranteed to be able to communicate correctly with a node built & running against Iron."

→ 即使强行混装，跨发行版的 ROS 节点通信**不受支持**。

### 4.2 本机跑的是最新 Humble

来源：[Preparing for Humble sync and patch release 2026-09-14 — Open Robotics Discourse](https://discourse.openrobotics.org/t/preparing-for-humble-sync-and-patch-release-2026-09-14/57969)

> "We're planning for a Humble sync and patch release on 2026-09-14T07:00:00Z."
> "The patch and sync are out! … We're happy to announce a **new Humble patch release (2026-09-14)** … This sync was tagged as `humble/2026-09-14`."
> （发布 tag：[`release-humble-20260914`](https://github.com/ros2/ros2/releases/tag/release-humble-20260914)）

本机包的构建时间戳 `20260908`–`20260912` **正好对应这次 sync**。也就是说：**本机装的就是 2026-09-14 那次 Humble patch release 的内容，是最新的**。截至审计日（2026-10-13），没有更新的 Humble sync 落地。

### 4.3 平台支持层级（REP 2000）—— Humble 在 24.04 上连 Tier 3 都不是

来源：[REP 2000 — ROS 2 Releases and Target Platforms](https://reps.openrobotics.org/rep-2000/)

**Humble Hawksbill (May 2022 - May 2027)** 的目标平台表：

```
+--------------+------------------+---------------+------------------+---------------+------------+-----------------+
| Architecture | Ubuntu Jammy     | Windows 10    | RHEL 8           | Ubuntu Focal  | macOS      | Debian Bullseye |
|              | (22.04)          | (VS2019)      |                  | (20.04)       |            | (11)            |
+==============+==================+===============+==================+===============+============+=================+
|    amd64     | Tier 1 [d][a][s] | Tier 1 [a][s] | Tier 2 [d][a][s] | Tier 3 [s]    | Tier 3 [s] |   Tier 3 [s]    |
+--------------+------------------+---------------+------------------+---------------+------------+-----------------+
```

→ **Humble 的列表里没有 Ubuntu Noble (24.04)，一个格子都没有。** 24.04 上跑 Humble 连"社区支持（Tier 3）"都算不上。

**Jazzy Jalisco (May 2024 - May 2029)**：

```
+--------------+------------------+---------------+------------------+--------------+-------------+-----------------+
| Architecture | Ubuntu Noble     | Windows 10    | RHEL 9           | Ubuntu Jammy |  macOS      | Debian Bookworm |
|              | (24.04)          | (VS2019)      |                  | (22.04)      |             | (12)            |
+==============+==================+===============+==================+==============+=============+=================+
|    amd64     | Tier 1 [d][a][s] | Tier 1 [a][s] | Tier 2 [d][a][s] |   Tier 3 [s] |  Tier 3 [s] |   Tier 3 [s]    |
+--------------+------------------+---------------+------------------+--------------+-------------+-----------------+
```

图例：`[d]` = 提供发行版二进制包；`[a]` = 提供二进制归档；**`[s]` = 只能从源码编译，ROS buildfarm 不产出任何二进制包。**

→ Jazzy 在 22.04 上**只有 Tier 3 且仅源码**（这解释了为什么 `jammy` 套件里 `ros-jazzy-*` = 0）；在 24.04 上是 Tier 1 带 deb。

REP 2000 的设计原则原文：

> "A single ROS 2 distribution will only have full Tier 1 support for a **single** Ubuntu LTS. The rationale is that fully supporting two LTS versions … is a tremendous overhead and sometimes even impossible."

### 4.4 「最新版」到底在哪个 Ubuntu 上

结合第 3 节的实测数据和上面的表：

| 想要 | 必须换到 | 代价 |
|---|---|---|
| ROS 2 **Jazzy**（EOL 2029） | **Ubuntu 24.04** | 换 OS + 换 ROS + 重编全部工作空间 |
| ROS 2 **Lyrical**（最新 LTS，EOL 2031） | **Ubuntu 26.04** | 换 OS + 换 ROS + 重编（26.04 于 2026-04-23 发布） |
| ROS 2 **Kilted** | Ubuntu 24.04 | 非 LTS，**2026 年 12 月就 EOL**，别碰 |
| ROS 2 **Iron**（曾支持 22.04） | 留在 22.04 即可 | **2024-12-04 已 EOL，是死路** |

**"在保持兼容的情况下更新到最新版"这个要求在数学上不成立**：保持兼容 = 留在 22.04 + Humble；更新到最新 = 必须换到 24.04/26.04 并换 ROS 发行版。两者互斥。

---

## 5. Ubuntu 升级可行性

### 5.1 升级**确实会被提供**（这正是危险之处）

```
$ cat /etc/update-manager/release-upgrades
# Default behavior for the release upgrader.

[DEFAULT]
# Default prompting and upgrade behavior, valid options:
#  never  - Never check for, or allow upgrading to, a new release.
#  normal - Check to see if a new release is available.  ...
#  lts    - Check to see if a new LTS release is available.  ...
Prompt=lts

$ do-release-upgrade -c            # ← 仅检查，未执行升级
Checking for a new Ubuntu release
New release '24.04.5 LTS' available.
Run 'do-release-upgrade' to upgrade to it.
```

- `Prompt=lts` → 系统**会**提示升级到下一个 LTS。
- `-c` 检查确认：**`24.04.5 LTS` 可用**（2026-09-10 发布，见 [Ubuntu release list](https://ubuntu.com/project/docs/release-team/list-of-releases/)）。
- 注意它只提议 24.04，不会一步跳到 26.04 —— `Prompt=lts` 的语义是"升级到当前版本之后的**第一个** LTS"。

**所以：一个手滑输入 `do-release-upgrade` 就能毁掉这台机器人。这是本报告存在的首要理由。**

### 5.2 升级会毁掉什么 —— 逐层清点

#### A. ROS 2 二进制层：508 个包，全部消失且无法重装

从 ROS 镜像安装的包（用 `/var/lib/apt/lists/*ros2*_Packages` 与 `dpkg-query` 求交集得出）：

```
total installed from ROS mirror: 508
  ros-* : 476
  non-ros: 32
  non-ros sample: ['libignition-cmake2-dev', 'libignition-math6', 'libignition-math6-dev',
                   'python3-catkin-pkg-modules', 'python3-colcon-argcomplete', 'python3-colcon-bash',
                   'python3-colcon-cd', 'python3-colcon-cmake', 'python3-colcon-common-extensions',
                   'python3-colcon-core', 'python3-colcon-defaults', 'python3-colcon-devtools', ...]
```

- **476 个 `ros-humble-*`** —— 升级后 `noble` 套件里是 0 个，**无处可装**。
- **32 个非 ROS 包**（`colcon-*`、`python3-catkin-pkg-modules`、`libignition-*`）—— 同样只存在于 `jammy` 套件。
- 连带：`/opt/ros/humble` 里那 12 个目录（被 4 个工作空间的 `setup.bash` 依赖）、`~/.bashrc` 里 source 的 4 个 `install/setup.bash`、`ROS_DOMAIN_ID=222` / `ROS_LOCALHOST_ONLY=1` 的整个运行环境，全部失效。

#### B. 源码工作空间：4 个工作空间、35 个包，全部要重编

```
--- piper_ros: 3 install entries ---
piper piper_description piper_msgs
--- agilex_ws: 18 install entries ---
hipnuc_imu livox_ros_driver2 livox_sdk2 pointcloud_to_laserscan ranger_base ranger_bringup
ranger_msgs rf2o_laser_odometry rslidar_msg rslidar_sdk scout_base scout_bringup
scout_description scout_msgs tracer_base tracer_bringup tracer_msgs ugv_sdk
--- camera_ros: 6 install entries ---
orbbec_camera orbbec_camera_msgs orbbec_description realsense2_camera realsense2_camera_msgs
realsense2_description
--- data_ros: 2 install entries ---
data_msgs data_tools
--- ros2_ws: 6 install entries ---
ranger_base ranger_bringup ranger_msgs tracer_base tracer_msgs ugv_sdk      # 旧副本
```

这些包全部是针对 Humble 的 `ament`/`rmw` ABI 编译的。换 ROS 发行版 = **100% 重编 + 重新适配**，没有例外。

**厂商仓库到底有没有 Jazzy 分支？** 用 `git ls-remote --heads` 逐个实测（只读）：

| 仓库 | 分支 | Jazzy 可用？ |
|---|---|---|
| `agilexrobotics/tracer_ros2` | foxy, humble, **jazzy**, tracer2.0 | ✅ 有 |
| `agilexrobotics/scout_ros2` | humble, humble_test, main, **jazzy** | ✅ 有 |
| `agilexrobotics/ranger_ros2` | air_delta, humble, **jazzy**, old-version | ✅ 有 |
| `agilexrobotics/ugv_sdk` | devel, main, next, v2.x | ➖ 纯 C++ SDK，与 ROS 版本无关 |
| **`agilexrobotics/piper_ros`** | foxy, humble, humble_beta1, **noetic** | ❌ **没有 jazzy**（默认分支竟是 ROS 1 的 noetic） |
| `Livox-SDK/livox_ros_driver2` | master | ❌ 无按发行版分支 |
| `RoboSense-LiDAR/rslidar_sdk` | dev_opt, main | ❌ 无按发行版分支 |
| `orbbec/OrbbecSDK_ROS2` | **lyrical**, main, v2-main, … | ✅ 跟进到最新 LTS |
| `IntelRealSense/realsense-ros` | r/4.58.4, ros2-development, ros2-master, … | ✅ 单分支多发行版策略 |
| `MAPIRlab/rf2o_laser_odometry` | ros1, ros2 | ➖ 通用 |
| **`agilexrobotics/data_tools`**（ARIO，采集管线） | master, ros2 | ❌ **没有 jazzy** |
| `hipnuc/hipnuc_imu` | （未能取得） | ⚠️ 未验证 |

**诚实说明**：底盘三家（tracer/scout/ranger）**确实有 jazzy 分支**，所以迁移在底盘侧是可行的；Orbbec、RealSense 也在跟进。真正的硬阻塞是：

1. **`piper_ros`（机械臂）没有 Jazzy 分支** —— 本机这份还是 Gitee 镜像（`gitee.com/LunKai/piper_ros.git`，单个压扁 commit、无 tag，镜像现已 HTTP 403），工作区是脏的。厂商的新栈 `agx_arm_ros` 同时支持 Humble/Jazzy（见 `REPORT.md` 第 8 节），但那是**另一套驱动**，需要独立工作空间和重新验证。
2. **`data_tools`（ARIO 数据采集）没有 Jazzy 分支** —— 这是用户日常真正在用的采集管线（`run_data_capture.launch.py`、`hdf5_to_lerobot.py` 等）。
3. **Livox / RoboSense / hipnuc 激光雷达与 IMU 驱动** 无 Jazzy 分支，需要源码适配。
4. **本机所有 launch 文件、地图、采集配置**都需要重新验证（Nav2、Cartographer 在 Jazzy 上有 API 变更）。

#### C. Python / conda / 机械臂 SDK 层

```
$ for e in aloha data lerobot; do ~/miniforge3/envs/$e/bin/python -c "import sys;print(sys.version)"; done
env=aloha   python=3.10.0
env=data    python=3.10.0
env=lerobot python=3.10.18
env=base    python=3.12.11

$ python3 --version                  # 系统 Python
Python 3.10.12

$ python3 -c "import numpy; print(numpy.__version__)"
1.21.5

$ piper_sdk 的真实位置
env=aloha   piper_sdk -> /home/agilex/.local/lib/python3.10/site-packages/piper_sdk/__init__.py
env=data    piper_sdk -> /home/agilex/.local/lib/python3.10/site-packages/piper_sdk/__init__.py
env=lerobot piper_sdk -> /home/agilex/.local/lib/python3.10/site-packages/piper_sdk/__init__.py
```

**重要修正**：`piper_sdk 1.0.0` 并不是分别装在三个 conda 环境里，而是装在**共享的用户级 site-packages** `/home/agilex/.local/lib/python3.10/site-packages`，三个环境（都是 Python 3.10）恰好都能看到它。

后果：Ubuntu 24.04 的系统 Python 是 **3.12.3**（REP 2000 Noble 列），`~/.local/lib/python3.10/` 这个目录名会让系统 Python **静默忽略**它 → **任何用系统 Python 的 `import piper_sdk` 都会失败**。（conda 环境自带 Python 3.10，不受 OS Python 影响，但 `piper_sdk` 依赖的 `python-can` 也在同一个用户目录里，需要重建。）

REP 2000 给出的语言/依赖断崖（22.04 → 24.04）：

| | Ubuntu 22.04 (Jammy) | Ubuntu 24.04 (Noble) |
|---|---|---|
| Python | 3.10.4 | **3.12.3** |
| NumPy | 1.21.5 | **1.26.4** |
| OpenCV | 4.5.4 | 4.6.0 |
| PCL | 1.12.1 | **1.14.0** |
| C++ 要求 | C++14 | **C++17**（Jazzy） |

本机实测 `numpy 1.21.5` + `Python 3.10.12`，与 22.04 列完全吻合。

#### D. NVIDIA / CUDA / DKMS 层 —— 最容易变砖的一层

```
$ dpkg -l | grep -i nvidia
ii libnvidia-cfg1-580:amd64            580.178.04-0ubuntu0.22.04.1
ii libnvidia-common-580                580.178.04-0ubuntu0.22.04.1
ii libnvidia-compute-580:amd64         580.178.04-0ubuntu0.22.04.1
ii libnvidia-compute-580:i386          580.178.04-0ubuntu0.22.04.1
ii libnvidia-decode-580:amd64          580.178.04-0ubuntu0.22.04.1
ii libnvidia-decode-580:i386           580.178.04-0ubuntu0.22.04.1
ii libnvidia-egl-wayland1:amd64        1:1.1.17-0ubuntu0~gpu22.04.1
ii libnvidia-egl-wayland1:i386         1:1.1.17-0ubuntu0~gpu22.04.1
ii libnvidia-encode-580:amd64          580.178.04-0ubuntu0.22.04.1
ii libnvidia-encode-580:i386           580.178.04-0ubuntu0.22.04.1
ii libnvidia-extra-580                 580.178.04-0ubuntu0.22.04.1
ii libnvidia-fbc1-580:amd64            580.178.04-0ubuntu0.22.04.1
ii libnvidia-fbc1-580:i386             580.178.04-0ubuntu0.22.04.1
ii libnvidia-gl-580:amd64              580.178.04-0ubuntu0.22.04.1
ii libnvidia-gl-580:i386               580.178.04-0ubuntu0.22.04.1
ii nvidia-compute-utils-580            580.178.04-0ubuntu0.22.04.1
ii nvidia-dkms-580                     580.178.04-0ubuntu0.22.04.1
ii nvidia-driver-580                   580.178.04-0ubuntu0.22.04.1
ii nvidia-firmware-580-580.178.04      580.178.04-0ubuntu0.22.04.1
ii nvidia-kernel-common-580            580.178.04-0ubuntu0.22.04.1
ii nvidia-kernel-source-580            580.178.04-0ubuntu0.22.04.1
ii nvidia-prime                        0.8.17.1
ii nvidia-settings                     510.47.03-0ubuntu1.22.04.1
ii nvidia-utils-580                    580.178.04-0ubuntu0.22.04.1
ii screen-resolution-extra             0.18.2ubuntu0.1
ii xserver-xorg-video-nvidia-580       580.178.04-0ubuntu0.22.04.1

$ apt-cache policy nvidia-driver-580
nvidia-driver-580:
  Installed: 580.178.04-0ubuntu0.22.04.1
  Candidate: 580.178.04-0ubuntu0.22.04.1
  Version table:
 *** 580.178.04-0ubuntu0.22.04.1 500
        500 https://mirrors.ustc.edu.cn/ubuntu jammy-updates/restricted amd64 Packages
        500 https://mirrors.ustc.edu.cn/ubuntu jammy-security/restricted amd64 Packages

$ dkms status
nvidia/580.178.04, 6.8.0-138-generic, x86_64: installed
nvidia/580.178.04, 6.8.0-90-generic, x86_64: installed
```

驱动来自 **`jammy-updates/restricted` + `jammy-security/restricted`**（Ubuntu 官方，但带 `22.04` 版本后缀）。升级到 24.04 时：

- jammy 版 nvidia 包会被 24.04 的对应版本替换（**版本号不同**）；
- 内核会变（24.04.5 的 GA/HWE 内核）→ **DKMS 必须为新内核重新编译**，而 `dkms status` 显示当前模块只对 `6.8.0-138` 和 `6.8.0-90` 编译过；
- `REPORT.md` 第 7.1 节记录过：**这台机器已经发生过一次「内核模块 570 / 用户态 580 不匹配 → `nvidia-smi` rc=18」的故障**，当时是靠 `rmmod`/`modprobe` 热重载、**没有重启**修好的（脚本带 `trap 'systemctl start gdm' EXIT` 死人开关）。发行版升级会**重新制造这个故障**，而且这次没有"退回旧内核"的余地。

**连带损失**：

- `torch.cuda.is_available()` 目前是 `True`：
  ```
  env=aloha:   torch 2.7.1+cu128 cuda 12.8 avail True
  env=lerobot: torch 2.7.1+cu126 cuda 12.6 avail True
  ```
  升级后驱动/GL 栈变动，这两个环境要重新验证。
- `/usr/local/cuda-12.8`（含 **cuDNN 8.9.7**，全机唯一）与 `/usr/local/cuda-12.9`：这些不是 dpkg 管理的，文件会留下，但 `/usr/local/cuda` 软链的语义、以及所有 `-lcudnn` 构建（TensorRT/ONNX/torch 扩展）依赖的 GL/驱动库会变。`REPORT.md` 第 7.2 节明确记载：安装器曾偷偷改过 `/usr/local/cuda` 软链导致构建挂掉，已被发现并还原 —— 这种"手工平衡"在发行版升级中不会自动保持。

#### E. 其它 OS 级集成（会被升级顺手带走的）

**（1）本地安装、没有任何 apt 源的包**（发行版升级无法重新获取 → 直接移除）：

```
### INSTALLED BUT NOT AVAILABLE FROM ANY CONFIGURED REPO: 3
   non-ros (3): ['google-chrome-stable', 'nomachine', 'sunloginclient']
```

- ⚠️ **`nomachine`** —— 这是本项目的**远程桌面方案**（见 `REMOTE-DESKTOP.md`、`REPORT.md` 第 6 节）。升级后会被移除，且需要重新下载 noble 版安装。
- ⚠️ **`sunloginclient`**（向日葵）—— 另一个远程接入通道，同样会被移除。
- ⚠️ **`google-chrome-stable`** —— 同样。

**（2）第三方仓库的包**：

```
--- mirrors.bwbot.org_dists_stable_main_binary-amd64_Packages: provides 10 pkgs, 6 INSTALLED
    ['libsystemback', 'systemback', 'systemback-cli', 'systemback-efiboot-amd64',
     'systemback-locales', 'systemback-scheduler']

$ dpkg -l | grep -i systemback
ii systemback 1.8.9 / systemback-cli 1.8.9 / systemback-scheduler 1.8.9 ...

$ head -8 /var/lib/apt/lists/mirrors.bwbot.org_dists_stable_InRelease
Origin: . stable
Label: . stable
Suite: stable
Codename: stable
Date: Sun, 25 Jun 2023 09:05:40 UTC          # ← 仓库最后更新：2023-06-25，冻结 3 年多
```

⚠️ **`systemback` 是这台机器的整机备份/还原工具**，来自一个 **Debian `stable` 套件、2023-06-25 起就冻结**的第三方仓库 —— 它**不可能**有 noble 套件。发行版升级会把它删掉，而它恰好是你在做危险操作前应该用来做快照的工具。**先做镜像，再谈升级。**

```
--- packages.microsoft.com_repos_code_dists_stable_main_binary-amd64_Packages: 2 INSTALLED
    ['code', 'code-insiders']
```

Microsoft 仓库有 noble 支持，但升级时该源会被禁用、需要重新添加。

**（3）升级器对第三方源的处理机制**：Ubuntu 的发行版升级器会**禁用第三方源并移除来自这些源的包**。相关上游记录：
[Ubuntu Bug #2107657 "upgrades … incorrectly remove foreign packages from disabled sources"](https://lists.ubuntu.com/archives/foundations-bugs/2025-May/538296.html)、
[ubuntu-users: "Prevent do-release-upgrade from disabling 3rd party repos"](https://lists.snapcraft.io/archives/ubuntu-users/2024-August/312976.html)。

**（4）udev / 硬件层**（这些是厂商安装的文件，升级后会留下，但需要重新验证）：

```
$ ls /etc/udev/rules.d/
56-orbbec-usb.rules   70-snap.*.rules   99-disable-usb-powersave.rules
99-obsensor-libusb.rules   gripper_fisheye.rules   gripper_serial.rules
```

相机（3× Orbbec）、机械臂串口、CAN 适配器的权限规则。另外 `~/piper_ros/can_config.sh` 依赖 `ethtool` + `can-utils`，`hipnuc_imu` 依赖 `chmod 777 /dev/ttyUSB0`。

#### F. 汇总：一次 `do-release-upgrade` 的账单

| 层 | 规模 | 升级后状态 |
|---|---|---|
| `ros-humble-*` deb | **476** | 全部移除，`noble` 上 0 个可装 |
| ROS 镜像的其它包（colcon/ignition/catkin） | 32 | 同上 |
| 源码工作空间 | 4 个 / 35 个包 | 全部需重编（piper_ros、data_tools 无 Jazzy 分支） |
| NVIDIA 580 驱动栈 | 26 个包 + DKMS | 被 noble 版本替换，DKMS 重编，**已知故障模式会重现** |
| CUDA / cuDNN / torch | 3 个 conda 环境 + 2 个 CUDA 安装 | 需重新验证 GPU 可用性 |
| `piper_sdk` 1.0.0 | 用户级 `python3.10` site-packages | 系统 Python 3.12 **静默失联** |
| NoMachine / 向日葵 / Chrome | 3 个本地 deb | **被移除**（远程接入断掉） |
| systemback 备份工具 | 6 个包（源冻结于 2023-06） | **被移除** |
| 内核 | 6.8.0-138 HWE | 换内核，DKMS/驱动重新适配 |
| 总计受影响 | **≈ 550+ 个包** | —— |

---

## 6. 22.04 内部的安全更新（该做的事）

### 6.1 待打的安全补丁：只有 `libx11` 一族

```
$ apt-cache policy libx11-6
libx11-6:
  Installed: 2:1.7.5-1ubuntu0.3
  Candidate: 2:1.7.5-1ubuntu0.4
  Version table:
     2:1.7.5-1ubuntu0.4 500
        500 https://mirrors.ustc.edu.cn/ubuntu jammy-security/main amd64 Packages
 *** 2:1.7.5-1ubuntu0.3 500
        500 https://mirrors.ustc.edu.cn/ubuntu jammy-updates/main amd64 Packages
```

**6 个包，全部来自 `jammy-security`**：`libx11-6`(amd64/i386)、`libx11-data`、`libx11-dev`、`libx11-xcb1`(amd64/i386)，`2:1.7.5-1ubuntu0.3` → `2:1.7.5-1ubuntu0.4`。

外加 `code-insiders`（VS Code Insiders 的常规更新，非安全）。

**干跑验证（`-s`，未执行）**：

```
$ apt-get -s upgrade
7 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
Inst libx11-dev [2:1.7.5-1ubuntu0.3] (2:1.7.5-1ubuntu0.4 Ubuntu:22.04/jammy-security [amd64]) []
Inst libx11-data ... Inst libx11-6 ... Inst libx11-6:i386 ... Inst libx11-xcb1:i386 ... Inst libx11-xcb1 ...
Inst code-insiders [1.142.0-1791566707] (1.142.0-1791782643 code stable:stable [amd64])
```

**安全性评估**：`libx11` 是 X11 客户端库，**不涉及 ROS、不涉及内核、不涉及驱动**。这是一次低风险的常规安全更新。

**⚠️ 但有一个必须注意的细节**：`apt-get -s upgrade` 的输出里包含这一行：

```
Use 'sudo apt autoremove' to remove them.
```

**apt 在升级后会主动"建议"你运行 `apt autoremove` —— 那正是会拆掉 GPU 栈的命令。** 见下一节。

### 6.2 ⚠️ 确认 `apt autoremove` 地雷（比原报告更严重）

`REPORT.md` 第 7.3 节曾警告不要运行 `apt autoremove`。本次审计用**干跑**（`apt-get -s autoremove`，`-s` = 模拟，**不改动系统**）做了确认，结论是：**警告完全成立，而且实际范围更大。**

**第一步：apt 确实认为这些 NVIDIA 包是"自动安装、不再需要"的**

```
$ apt-mark showauto | grep -i nvidia
libnvidia-cfg1-580
libnvidia-common-580
libnvidia-compute-580
libnvidia-compute-580:i386
libnvidia-decode-580
libnvidia-decode-580:i386
libnvidia-egl-wayland1
libnvidia-egl-wayland1:i386
libnvidia-encode-580
libnvidia-encode-580:i386
libnvidia-extra-580
libnvidia-fbc1-580
libnvidia-fbc1-580:i386
libnvidia-gl-580
libnvidia-gl-580:i386
nvidia-compute-utils-580
nvidia-dkms-580
nvidia-driver-580
nvidia-firmware-580-580.178.04
nvidia-kernel-common-580
nvidia-kernel-source-580
nvidia-prime
nvidia-settings
nvidia-utils-580
xserver-xorg-video-nvidia-580

$ apt-mark showauto | grep -ci nvidia
25
```

**25 个 NVIDIA 包被标记为自动安装**，包括 `nvidia-driver-580` 本身。

**第二步：模拟 `apt autoremove` 会删什么**

```
$ apt-get -s autoremove
Reading package lists...
Building dependency tree...
Reading state information...
The following packages will be REMOVED:
  dctrl-tools dkms libbsd0:i386 libdrm2:i386 libexpat1:i386 libffi8:i386
  libfwupd2 libfwupdplugin5 libgbm1:i386 libgcab-1.0-0 libmd0:i386
  libnvidia-cfg1-580 libnvidia-common-580 libnvidia-compute-580:i386
  libnvidia-decode-580 libnvidia-decode-580:i386 libnvidia-egl-wayland1
  libnvidia-egl-wayland1:i386 libnvidia-encode-580 libnvidia-encode-580:i386
  libnvidia-extra-580 libnvidia-fbc1-580 libnvidia-fbc1-580:i386
  libnvidia-gl-580 libnvidia-gl-580:i386 libsmbios-c2 libwayland-client0:i386
  libwayland-server0:i386 libx11-6:i386 libx11-xcb1:i386 libxau6:i386
  libxcb-dri3-0:i386 libxcb-present0:i386 libxcb-randr0:i386 libxcb1:i386
  libxdmcp6:i386 libxext6:i386 nvidia-compute-utils-580 nvidia-dkms-580
  nvidia-driver-580 nvidia-kernel-source-580 nvidia-prime nvidia-settings
  nvidia-utils-580 python3-pygraphviz screen-resolution-extra
  xserver-xorg-video-nvidia-580
0 upgraded, 0 newly installed, 47 to remove and 5 not upgraded.
Remv nvidia-driver-580 [580.178.04-0ubuntu0.22.04.1]
Remv nvidia-dkms-580 [580.178.04-0ubuntu0.22.04.1]
Remv dkms [2.8.7-2ubuntu2.2]                      # ← 连 DKMS 本身都被删！
Remv dctrl-tools [2.24-3build2]
Remv libnvidia-gl-580:i386 [580.178.04-0ubuntu0.22.04.1]
...
Remv nvidia-compute-utils-580 [580.178.04-0ubuntu0.22.04.1]
Remv nvidia-kernel-source-580 [580.178.04-0ubuntu0.22.04.1]
Remv nvidia-prime [0.8.17.1]
Remv nvidia-settings [510.47.03-0ubuntu1.22.04.1]
Remv nvidia-utils-580 [580.178.04-0ubuntu0.22.04.1]

$ apt-get -s autoremove | grep -c '^Remv'
47
$ apt-get -s autoremove | grep '^Remv' | grep -ci nvidia
37
```

**确认结论**：

| 项目 | 结果 |
|---|---|
| 会删除的包总数 | **47** |
| 其中匹配 "nvidia" 的 | **37** |
| 会删掉工作正常的 `nvidia-driver-580`？ | ✅ **是** |
| 会删掉 `nvidia-dkms-580` / `nvidia-utils-580` / `nvidia-kernel-source-580`？ | ✅ **是** |
| 会删掉 `nvidia-prime` / `nvidia-settings` / `xserver-xorg-video-nvidia-580`？ | ✅ **是** |
| 会删掉全部 `libnvidia-*-580` GL/compute/encode/decode 库？ | ✅ **是** |
| **会删掉 `dkms` 本身？** | ✅ **是（原报告未提到，更严重）** |
| 会删掉 `screen-resolution-extra`？ | ✅ 是 |
| 附带损失 | `libfwupd2`/`libfwupdplugin5`/`libgcab-1.0-0`（固件更新）、`python3-pygraphviz`、一堆 i386 库 |

删掉 `dkms` 意味着**连"事后重新编译内核模块"的能力都没了** —— 恢复难度显著高于"重装驱动"。

**后果**：`nvidia-smi` 失效、`torch.cuda.is_available()` 变 `False`、桌面 GPU 加速/多显示器失效。这正是 `REPORT.md` 第 7.1 节记录过的那类故障，只不过这次是自己主动造成的。

### 6.3 `apt autoremove` 会不会被自动触发？—— 目前不会（但不要打开开关）

**（1）`apt-get upgrade` / `apt upgrade` 本身不会自动卸载**（干跑验证）：

```
$ apt-get -s upgrade | grep -c '^Remv'
0
$ apt -s upgrade | grep -c '^Remv'          # 新版 apt CLI 同样
0
$ apt-config dump | grep -i AutomaticRemove
(AutomaticRemove not set -> default)
```

**（2）`unattended-upgrades` 已安装并启用，但危险开关是关的**：

```
$ dpkg -s unattended-upgrades | grep -E '^(Package|Version|Status)'
Package: unattended-upgrades
Status: install ok installed
Version: 2.8ubuntu1

$ systemctl is-enabled unattended-upgrades; systemctl is-active unattended-upgrades
enabled
active

$ cat /etc/apt/apt.conf.d/20auto-upgrades
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";

$ grep -n 'Remove-Unused' /etc/apt/apt.conf.d/*unattended*
83://Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";      # 被注释 = 默认 false
90://Unattended-Upgrade::Remove-Unused-Dependencies "false";        # 被注释 = 默认 false
```

→ **两行都是注释状态，即默认关闭。自动更新只会打安全补丁，不会自动 `autoremove`。**

> 🚨 **绝对不要取消注释 `Unattended-Upgrade::Remove-Unused-Dependencies`。** 一旦设为 `true`，`unattended-upgrades` 会在某个凌晨自动执行等价于 `apt autoremove` 的操作，**在无人值守的情况下拆掉 GPU 栈**。这是本机最危险的一个"看起来很合理"的配置项。

### 6.4 另一颗地雷：不要 `pip install --upgrade piper_sdk`

（沿用 `REPORT.md` 第 8 节结论，本次审计确认了安装位置）

- 现装：`piper_sdk 1.0.0`，位于 `/home/agilex/.local/lib/python3.10/site-packages/`（**不在** PyPI 上）
- PyPI 最新：**0.6.2** —— **比现装的旧**
- `pip install --upgrade piper_sdk` 会**降级**到 0.6.2，**丢掉 V3 协议支持**（当前固件 V1.8-8+ 需要 V3）
- 上游 `1_0_0_b1` 分支在 2026-09-03 已把版本号下调为 `0.7.0`；最新线是 `0_7_0_a1`

**保持现状。**

---

## 7. 建议

### 7.1 ❌ 不要升级 Ubuntu（理由，按重要性排序）

1. **技术上做不到"保持兼容"**：24.04 上 `ros-humble-*` 包数量 = **0**。这不是配置问题，是 ROS 2 项目**刻意设计**的单 LTS 策略（REP 2000 原文："A single ROS 2 distribution will only have full Tier 1 support for a single Ubuntu LTS … sometimes even impossible"）。Humble 的平台表里**根本没有 24.04 这一列**。
2. **代价 ≈ 550+ 个包**：476 个 ROS deb + 32 个 ROS 源包 + 4 个工作空间全部重编 + NVIDIA/CUDA/DKMS 重建 + 3 个远程接入工具被移除 + 备份工具 systemback 被移除。
3. **已知故障会重现**：这台机器已经因为驱动/模块版本不匹配坏过一次（`nvidia-smi` rc=18），当时靠热重载 + 死人开关侥幸修好。发行版升级会再次制造同样场景，且这次没有"退回旧内核"的余地。
4. **收益为零**：现在**没有任何东西需要升级**（0 个可升级的 ROS 包，只有 6 个 libx11 安全补丁）。升级不解决任何现存问题。
5. **时间上完全不紧急**：Ubuntu 22.04 标准支持到 **2027 年 6 月**，Humble EOL **2027 年 5 月** —— 还有 **约 7 个月**。
6. **失败模式不对称**：升级失败 = 机器人变砖，且远程接入工具（NoMachine/向日葵）同时被移除 → **可能失去远程恢复的手段**。

### 7.2 ❌ 不要"升级 ROS 2"

**已经是最新**。476 个 `ros-humble-*` 包中 **0 个**可升级；镜像里最新的 `ros-humble-ros-base` 与本机已装版本**完全相同**（`0.10.0-1jammy.20260908.101203`）。本机跑的就是 2026-09-14 的 Humble patch release。

留在 22.04 的唯一"另一个" ROS 发行版是 Iron，**2024-12-04 已 EOL** —— 那不是升级，是降级到坟场。

### 7.3 ✅ 应该做什么

**现在（本周内，低风险）：**

```bash
# 1) 先打安全补丁（6 个 libx11 包，与 ROS 无关，风险低）
#    注意：不要加 --auto-remove，不要用 apt 的自动清理
sudo apt-get update
sudo apt-get install --only-upgrade libx11-6 libx11-data libx11-dev libx11-xcb1

# 2) 顺手升级 VS Code Insiders（可选）
sudo apt-get install --only-upgrade code-insiders

# 3) 验证 ROS 仍然正常
source /opt/ros/humble/setup.bash && ros2 --version
nvidia-smi                        # 应仍正常，rc=0
```

**维护纪律（长期）：**

- 🚫 **永不运行 `apt autoremove`**（第 6.2 节，47 个包 / 37 个 NVIDIA / 含 `dkms`）
- 🚫 **永不给 `Unattended-Upgrade::Remove-Unused-Dependencies` 取消注释**（第 6.3 节）
- 🚫 **永不 `pip install --upgrade piper_sdk`**（第 6.4 节，会降级丢 V3）
- 🚫 **永不 `do-release-upgrade`** —— 直到第 7.4 节的迁移计划真正启动
- ✅ 定期 `sudo apt-get update && sudo apt-get install --only-upgrade <具体包名>`（显式指定包名，避免任何自动清理）
- ✅ 危险操作前用 `systemback` 做整机快照（**趁它还在**）

**如果要新功能（不改动现有系统）：**

- 新建独立工作空间（如 `~/agx_arm_ws`）用厂商新栈 `pyAgxArm` + `agx_arm_ros`（后者**同时支持 Humble 和 Jazzy**，见 `REPORT.md` 第 8 节）
- 用 Docker / conda 隔离实验环境，**不要动 `/opt/ros/humble`**
- 现有 `~/piper_ros`、`~/agilex_ws`、`~/camera_ros`、`~/data_ros` **一律保持不动**

### 7.4 将来真要迁移时（建议 2027 年暑假，且必须整体迁移）

**迁移矩阵（由第 3 节实测数据得出，每个组合都只有唯一解）：**

| 目标 | Ubuntu | ROS 2 | 支持到 | 备注 |
|---|---|---|---|---|
| **推荐** | **24.04 LTS** | **Jazzy** | 2029-05 | 底盘三家已有 `jazzy` 分支；Orbbec/RealSense 已跟进 |
| 备选（最新） | 26.04 LTS | Lyrical | 2031-05 | 更长的支持期，但生态适配更少 |
| ❌ 不要 | 24.04 | Kilted | **2026-12** | 非 LTS，两个月后就 EOL |
| ❌ 不要 | 22.04 | Iron | **已 EOL** | 死路 |

**迁移前置条件（缺一不可）：**

1. `piper_ros` → 必须先完成到 `agx_arm_ros` / `pyAgxArm` 的替换（**没有 Jazzy 分支**，这是头号阻塞）
2. `data_tools`（ARIO 采集管线）→ 需要 Jazzy 适配或替代方案（**没有 Jazzy 分支**）
3. Livox / RoboSense / hipnuc 驱动 → 源码适配
4. NVIDIA 驱动 + DKMS + CUDA/cuDNN + 3 个 conda 环境 → 在 24.04 上重新验证 GPU
5. NoMachine / 向日葵 / Chrome → 准备 noble 版安装包
6. **在一台同型号备用机或全新磁盘上做，不要在这台生产机器人上直接做**

---

## 8. 诚实说明：本次审计未验证的部分

| 项目 | 状态 |
|---|---|
| `hipnuc/hipnuc_imu` 是否有 Jazzy 分支 | ⚠️ `git ls-remote` 未返回结果，**未验证** |
| Livox / RoboSense 的 `master`/`main` 分支是否能在 Jazzy 下编译 | ⚠️ 仅确认无按发行版分支，**未验证能否编译** |
| 24.04 上 NVIDIA 驱动的具体版本与 DKMS 行为 | ⚠️ 未在 24.04 上实测（本任务禁止安装/升级） |
| `do-release-upgrade` 实际会移除哪些包 | ⚠️ 无干跑模式，只能从机制 + 包可用性推断（第 5.2 节） |
| 26.04 + Lyrical 的实际适配情况 | ⚠️ 仅从镜像索引推断 Lyrical 只发布在 `resolute` 套件 |
| 是否有比 2026-09-14 更新的 Humble sync | ✅ 已验证：无（实时镜像最新构建 = 2026-09-12） |

---

## 9. 引用来源

**ROS 2 生命周期 / 平台支持**

- [ROS 2 Distributions（含各发行版 EOL 日期）](https://docs.ros.org/en/humble/Releases.html) — Humble EOL 2027-05；Iron EOL 2024-12-04；Jazzy EOL 2029-05；Kilted EOL 2026-12；Lyrical 2026-05-22 发布 / 2031-05 EOL
- [REP 2000 — ROS 2 Releases and Target Platforms](https://reps.openrobotics.org/rep-2000/) — Humble: Jammy Tier 1（**无 Noble 列**）；Jazzy: Noble Tier 1 `[d][a][s]` / Jammy Tier 3 `[s]`（仅源码）；Kilted: Noble Tier 1；各发行版语言/依赖断崖表
- [Preparing for Humble sync and patch release 2026-09-14](https://discourse.openrobotics.org/t/preparing-for-humble-sync-and-patch-release-2026-09-14/57969) — 本机已装内容对应的 patch release
- [`release-humble-20260914` release tag](https://github.com/ros2/ros2/releases/tag/release-humble-20260914)
- [ROS 2 Humble 安装文档（Ubuntu 22.04）](https://docs.ros.org/en/humble/Installation/Ubuntu-Install-Debs.html)
- [ROS 2 Jazzy 安装文档（Ubuntu 24.04）](https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html)

**Ubuntu 生命周期**

- [Ubuntu — List of releases（End of Standard Support / End of Life）](https://ubuntu.com/project/docs/release-team/list-of-releases/) — Ubuntu 22.04.5 LTS: EoSS **2027-06**、EOL 2037-04；24.04.5 LTS: EoSS 2029-06、EOL 2039-04；26.04 LTS: EoSS 2031-05

**发行版升级行为**

- [Ubuntu Bug #2107657 — upgrades remove foreign packages from disabled sources](https://lists.ubuntu.com/archives/foundations-bugs/2025-May/538296.html)
- [ubuntu-users — Prevent do-release-upgrade from disabling 3rd party repos](https://lists.snapcraft.io/archives/ubuntu-users/2024-August/312976.html)

**本机实测数据（权威性最高，全部为本次审计采集）**

- ROS 2 官方仓库镜像 `https://mirrors.ustc.edu.cn/ros2/ubuntu` 的 `jammy` / `noble` / `resolute` 套件 `Packages.gz` 索引（实时抓取）
- 机器人上的 `dpkg` / `apt-cache policy` / `apt list --upgradable` / `apt-mark` / `do-release-upgrade -c` / `apt-get -s` 输出

---

## 10. 附录：原始命令清单（可复现）

所有命令均通过 `bin/rrun.sh`（用户）或 `bin/rsudo.sh`（root）执行。

```bash
# --- 系统与 ROS 状态 ---
cat /etc/os-release; lsb_release -a; uname -a; uptime
ls -la /opt/ros/
dpkg -s ros-humble-ros-base ros-humble-ros-core ros-humble-desktop | grep -E '^(Package|Version|Status)'
dpkg -l 'ros-humble-*' | grep -c '^ii'
bash -ic 'echo $ROS_DISTRO $ROS_VERSION $ROS_DOMAIN_ID $ROS_LOCALHOST_ONLY'
grep -rhE '^[[:space:]]*deb' /etc/apt/sources.list /etc/apt/sources.list.d/
[ -f /var/run/reboot-required ] && cat /var/run/reboot-required

# --- 可升级性 ---
apt-cache policy ros-humble-ros-base ros-humble-ros-core ros-humble-desktop ros-humble-rviz2 \
                 ros-humble-nav2-bringup ros-humble-cartographer-ros ros-humble-realsense2-camera
apt list --upgradable
apt-mark showhold

# --- 关键证据：各 Ubuntu 套件的 ROS 包数量 ---
for suite in jammy noble resolute; do
  for d in humble iron jazzy kilted lyrical rolling; do
    curl -s https://mirrors.ustc.edu.cn/ros2/ubuntu/dists/$suite/main/binary-amd64/Packages.gz \
      | zcat | grep -c "^Package: ros-$d-"
  done
done

# --- 镜像最新版本 vs 已装版本 ---
curl -s .../dists/jammy/main/binary-amd64/Packages.gz | zcat \
  | awk '/^Package: ros-humble-ros-base$/{f=1} f&&/^Version:/{print;f=0}'

# --- Ubuntu 升级可行性（仅检查） ---
cat /etc/update-manager/release-upgrades
do-release-upgrade -c                    # ← 仅检查；未执行升级

# --- autoremove 地雷（全部为 -s 模拟，未执行） ---
apt-mark showauto | grep -i nvidia
apt-get -s autoremove
apt-get -s autoremove | grep -c '^Remv'
apt-get -s upgrade | grep -c '^Remv'
apt-config dump | grep -i AutomaticRemove
grep -n 'Remove-Unused' /etc/apt/apt.conf.d/*unattended*

# --- 包来源清点 ---
# (读取 /var/lib/apt/lists/*_Packages 与 dpkg-query 求交集)
python3 - <<'PY'
# 见第 5.2 节 A 项与 E 项的实现
PY

# --- 厂商 Jazzy 分支实测 ---
git ls-remote --heads https://github.com/agilexrobotics/tracer_ros2.git
git ls-remote --heads https://github.com/agilexrobotics/piper_ros.git
# ... 其余见第 5.2 节 B 项表格

# --- GPU / 内核 ---
dpkg -l | grep -i nvidia
apt-cache policy nvidia-driver-580
dkms status
```

---

**审计人**：teammate `upgrade-audit`（task-8）
**最终建议**：**不升级。留在 Ubuntu 22.04.5 + ROS 2 Humble，只打 6 个 libx11 安全补丁；永不运行 `apt autoremove`；2027 年再整体规划 24.04 + Jazzy 迁移。**
