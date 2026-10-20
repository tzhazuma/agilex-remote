# 回合录制器（Episode Recorder）

给遥控器加的数据采集能力。实现在 `remote/robot-files/agilex-remote-server.py`，
随服务一起部署，无需额外安装。

## 为什么做在服务端，而不是 rosbag

这台机器人的 ROS 图上**只有底盘话题**（`/cmd_vel`、`/odom`、`/tracer_status`、
`/tf`、`/tracer_rc_status`、`/light_control`）。没有 `/camera/*`，也没有
`/joint_states`：

- 两个 Orbbec 相机没有 ROS 驱动，控制器用 V4L2 + ffmpeg 直接抓流
- 两条 Piper 臂走 `piper_sdk` 直控 CAN，从来没经过 ROS

所以 `rosbag record -a` 录出来的包对训练**没有用**——既没有图像也没有关节角。
而控制器进程本身就同时握着 JPEG 帧、关节读数和速度指令，在这里录反而是
时间戳最同步、依赖最少的选择。

## 一个绕不开的硬约束：V4L2 是独占的

实测：一个 ffmpeg 占着 `/dev/video0` 时，第二个 ffmpeg 直接报
`Device or resource busy`。也就是说**实时画面和录制不能各开一个 ffmpeg**。

因此引入了 `FrameHub`：每个相机只跑一个 ffmpeg，最新帧放在共享槽里，
实时推流和录制器都从槽里取。引入引用计数——最后一个消费者离开才释放设备。

顺带修掉一个潜在 bug：以前同时开两个网页看画面，第二个会抢不到设备。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/record/start` | `{"task": "...", "notes": "..."}` |
| POST | `/api/record/stop` | `{"success": true/false/null}` |
| POST | `/api/record/status` | 当前状态 |
| GET | `/api/status` | 响应里多了 `recorder` 字段 |

`start` 会等两个相机都出第一帧之后才开始计时（最多 12 秒），
返回 `cameras_warm`。**这一步是必须的**：第一版没等，实测 cam0 的第一张图
是第 7 帧，而同一个 USB 总线上冷启动的 cam2 到第 9 秒才出图——122 帧里只有
30 帧有右臂画面，数据集是破的。

## 目录结构

```
/home/agilex/agilex-data/episodes/episode_0002/
    meta.json         任务描述、起止时间、帧数、成功标记、cameras_warm
    cam0/000001.jpg   左臂相机
    cam0/...  000101.jpg
    cam2/000001.jpg   右臂相机
    cam2/...  000101.jpg
    timeline.csv      每个 tick 一行
```

`timeline.csv` 列：

```
t, frame,
L_j0..L_j5, L_grip,       左臂六个关节角 + 夹爪
R_j0..R_j5, R_grip,       右臂六个关节角 + 夹爪
cmd_lin, cmd_ang,         当前下发的速度指令（= 动作）
odom_x, odom_y, battery_v,
cam0_seq, cam2_seq        相机源帧号，用来识别「重复帧」
```

**图像文件名和 `frame` 列是同一个编号**，所以「第 N 行 ↔ cam0/00000N.jpg ↔
cam2/00000N.jpg」按构造就是同一时刻，不需要额外对齐。

每个 tick 都会写两张图——如果相机源没出新帧，就重复上一张。这样数据集是
矩形的，下游不用处理空洞；是否重复看 `camN_seq` 是否变化。

## 实测结果

- 10 Hz 采样，10 秒录到 **101 帧**
- 两个相机各 **101 张图**，编号 000001→000101 连续无洞
- 10 秒双相机约 **5.2 MB**（静止场景）——运动场景会大一些
- **录制的同时还能实时推流**：测试中边录边看，两路各收到 97 帧

## 已知限制

- 相机冷启动需要 1–10 秒，`start` 会阻塞等它；如果某路相机没插，
  会在 12 秒后放弃并把 `cameras_warm` 标成 `false`，不会挂死。
- 默认上限 300 秒/回合（`AGILEX_RECORD_MAX_S`），防止忘记停止。
- 只录状态和动作，**不录深度图**——深度需要额外的格式转换，当前链路只取
  MJPEG 彩色流。
- 底盘电机目前不会真的动（见 `reports/` 里的记录），所以
  `cmd_lin`/`odom_x` 现在恒为 0；不需要底盘移动的任务可以直接采。

## 验证过程中发现并修掉的三个真 bug

都是跑真机才暴露的，光看代码不会发现：

**1. 冷启动消费者拿到 0 帧**
`FrameHub.wait()` 原本只在 `seq == last_seq` 时等待。但消费者刚 acquire 时
`latest=None, seq=0`，条件不成立 → 立即返回 None → 推流端立刻判定「没有新帧」
退出。表现为：录制完之后再单独打开实时画面，两路都是 0 帧。
修法：`latest is None` 时也要等。

**2. 快速 release→acquire 导致两个 pump 线程抢设备**
第一版每次 `refs` 从 0 变 1 就新开一个线程，而旧线程最多要 8 秒才退出。
于是「关掉画面 → 立刻开始录制」会产生两个线程抢同一个独占设备，
表现为 `cameras_warm: {0: False, 2: False}` 且其中一路整段录不到画面。
修法：pump 线程全生命周期只创建一次，用 `refs` 门控是否占用设备。

**3. 首帧超时预算不足**
cam2（右臂）冷启动实测要 ~9 秒，比稳态的 8 秒超时还长，于是新打开的画面在
相机就绪前就放弃了，看起来像「右臂相机坏了」。
修法：首帧单独给 20 秒（`AGILEX_FIRST_FRAME_TIMEOUT`），稳态仍用 8 秒。

另外顺带发现：这台 Mac 上 `python3` 默认解析到
`~/toolchains/pyocd-venv/bin/python3`，它的 stdout 是坏的（`print` 无输出）。
调试脚本要用 `/usr/bin/python3`。和机器人无关，但很浪费排查时间。

## 最终实测（episode_0005）

```
cam0: 102 张  (000001 -> 000102)
cam2: 102 张  (000001 -> 000102)
timeline: 102 数据行
大小: 5.3 MB / 10 秒（双相机，静止场景）
cameras_warm: {0: True, 2: True}
同时推流: cam0 96 帧 / cam2 97 帧（8 秒窗口）
```

推流稳态约 **11–12 fps**（经 SSH 隧道），录制期间不降速。
这个 11 fps 是**传输链路**上限，不是相机能力——裸 ffmpeg 落盘能到 28 fps。

## 配置

| 变量 | 默认 | 含义 |
|---|---|---|
| `AGILEX_EPISODE_ROOT` | `/home/agilex/agilex-data/episodes` | 落盘位置 |
| `AGILEX_RECORD_HZ` | `10` | 采样频率 |
| `AGILEX_RECORD_MAX_S` | `300` | 单回合上限 |
