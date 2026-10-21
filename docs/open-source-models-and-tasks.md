# 这台机器人能用什么开源模型，能干什么任务

调研时间 2026-10-20。所有结论标注了来源和可信度；**没用过的不写成用过**。

---

## 一、先说结论

| 路线 | 能不能现在做 | 说明 |
|---|---|---|
| **ACT（LeRobot 自训）** | ✅ 最推荐 | 数据管线已通，8 GB 显存够训，几十条示教就能出效果 |
| **π0-fast 微调（Piper 专用预训练）** | ⚠ 有现成权重，但 8 GB 训不动 | 可做推理；微调显存吃紧，需要量化/冻结 |
| **piper-aio 整套栈** | ❌ 硬件不匹配 | 它假设 4 臂 + 3 个 RealSense + ROS Noetic |
| **OpenVLA / π0 原版** | ❌ 只能推理 | 7B 级，8 GB 不可能训练 |
| **底盘导航（Nav2）** | ⚠ 缺传感器 | 没激光雷达，只能靠视觉或里程计——而里程计是坏的 |

---

## 二、真正对口的开源项目

### 1. `innovator-zero/piper-aio` —— 最接近的参考实现

[github.com/innovator-zero/piper-aio](https://github.com/innovator-zero/piper-aio)

AgileX Piper 的**完整模仿学习栈**，覆盖：硬件 bring-up → 遥操作采数据 → 回放 →
**LeRobot 数据集转换** → 策略推理。2026 年 4 月发布。

**关键差异（决定了不能直接用）**：

| 它要求 | 我们实际有 |
|---|---|
| **四条** Piper 臂（2 leader + 2 follower） | 两条（无 leader，靠我们自己的遥控器遥操作） |
| **3 个 RealSense** 相机 | 2 个 Orbbec |
| Ubuntu 20.04 + **ROS Noetic** | Ubuntu 22.04 + **ROS 2 Humble** |

**但它的价值在于**：`convert_data/`（HDF5 → LeRobot 转换）和 `inference/`（同步/异步
推理客户端）这两部分是**方法和代码结构**的极好参考，和硬件配置关系不大。我们的
`tools/export-lerobot.py` 已经在做同样的事，可以对照它的字段定义校准。

### 2. `agilexrobotics/tracer_ros2` —— 底盘官方包

[github.com/agilexrobotics/tracer_ros2](https://github.com/agilexrobotics/tracer_ros2)

就是机器人上已经装的那个（`~/agilex_ws/src/tracer_ros2`），底座 + `ugv_sdk`。
没有额外的模型，只有驱动。

### 3. ALOHA / ACT —— ACT 方法的出处

[ALOHA 介绍](https://www.roboticscenter.ai/guides/aloha-robot)

Stanford 的双臂遥操作 + ACT 论文。**我们的机器人形态（双 6 轴臂 + 双臂相机）
和 ALOHA 几乎一模一样**，所以 ACT 是理论上最匹配的方法，不是硬凑。

---

## 三、已经存在的预训练权重（重点）

在 HuggingFace 上找到了**专门针对 Piper 微调过的 π0-fast 模型**，Apache-2.0：

| 模型 | 标签 | 含义 |
|---|---|---|
| `MODEL_AUTHOR/pi0-fast-piper-h-plate-pick-ft-v2` | lerobot, pi0, pi0-fast, **piper-h**, **agilex**, imitation-learning | π0-fast 在 Piper 上微调，任务是**盘子抓取** |
| `MODEL_AUTHOR/piper-pi0fast-multistage` | lerobot, **vla**, pi0-fast, **piper-robot**, curriculum-learning | 多阶段课程学习的 Piper π0-fast |

> 验证方式：这两个模型通过 HuggingFace API（走 hf-mirror.com，官方域名被 DNS 污染）
> 查到了真实元数据，`pipeline_tag: robotics`，Apache-2.0。**下载量 0、likes 0**，
> 说明是个人发布的实验性权重，不是官方产品——参考价值大于直接投产价值。

**π0-fast 是什么**：Physical Intelligence 的 VLA（视觉-语言-动作）模型精简版，
输入图像 + 语言指令，直接输出动作。比原版 π0 小很多，但仍是**十亿参数级**。

**8 GB 显存能不能用**：
- ✅ **推理**：可以（fp16 权重约 2 GB，加激活够用）
- ⚠ **微调**：非常勉强——LoRA + 冻结主干 + 小 batch 才可能，且很慢
- ❌ **从零训练**：不可能

**结论**：这两个权重值得下载来看看它们的**数据格式和动作空间定义**（对校准我们自己的
导出格式很有用），但不要指望直接拿来驱动我们的机器人——它们是在别人的 Piper 上、
用别人的相机布局、针对特定任务训的。

---

## 四、按任务类型的可行性

### ✅ 现在就能做（机械臂 + 相机都正常）

| 任务 | 难度 | 需要的数据量 | 说明 |
|---|---|---|---|
| **定点抓取放置** | ★★ | 50–100 条 | 最经典，ACT 的标准场景。桌面放物体，示教"移过去→夹住→抬起→放到目标" |
| **按钮/开关按压** | ★★ | 30–50 条 | 动作简单，适合第一个验证任务 |
| **倒水 / 倒颗粒** | ★★★ | 80–150 条 | 需要腕部姿态控制，夹爪要能夹住杯子 |
| **抽屉开合** | ★★★ | 60–100 条 | 需要力反馈感知（我们只有位置），容易卡住 |
| **双臂协同搬运** | ★★★★ | 150+ 条 | 两臂时序配合，ACT 支持双臂但数据要求高 |

### ⚠ 有条件可做

| 任务 | 障碍 |
|---|---|
| **移动抓取**（底盘 + 臂） | 底盘能动，但 `/odom` 恒为 0，位置反馈只能靠视觉 |
| **视觉伺服 / 跟踪** | 相机帧率经隧道只有 11–12 fps，机上直接读可以到 28 fps |
| **深度感知抓取** | 相机有深度头（`2bc5:0657`），但当前链路只取 MJPEG 彩色流 |

### ❌ 现在做不了

| 任务 | 原因 |
|---|---|
| **导航 / SLAM / 建图** | 无激光雷达（驱动装了但硬件不存在），无有效里程计 |
| **语音指令控制** | 无麦克风 |
| **室外巡检** | 无 GPS，底盘定位缺失 |

---

## 五、任务指导：从零到能自主执行

### 阶段 A —— 选一个任务，把场景固定下来（半天）

**建议第一个任务：定点抓取放置。** 理由：动作可分解、成功判据明确、对视觉要求低。

要点：
1. **桌面标记固定位置**，物体每次放在 ±3 cm 的小范围内随机
2. **光照稳定** —— 别在窗边做，自然光变化会让模型学不动
3. **相机视角固定** —— 臂动相机也动，所以示教时保持相同的起始位姿
4. **写清楚任务描述**（就是 `task` 字段），后面多任务训练要用

### 阶段 B —— 采数据（关键，决定成败）

**数量**：50 条起步，100 条稳定。别一上来采 500 条——先训一版看效果，再决定加多少。

**质量 > 数量**，具体：

- ✅ **速度均匀**，别忽快忽慢
- ✅ **路径干净**，不要来回试探
- ✅ **每次动作风格一致** —— 模型学的是你的习惯，风格不一致的数据会互相打架
- ✅ **成功的和失败的都留着**，失败的可做负样本或分析
- ❌ 不要一个场景重复采 200 次 —— 多样性比数量重要

**每回合流程**（对应我们的 API）：

```bash
# 1. 摆好物体
# 2. 开始录
curl -X POST localhost:8765/api/record/start \
  -d '{"task":"把红色方块放进盒子","notes":"场景A"}'
# 3. 用遥控器/摇杆完成任务
# 4. 停止并标记成败
curl -X POST localhost:8765/api/record/stop -d '{"success":true}'
# 5. Home 复位，回到第 1 步
```

**踩坑提醒**：录之前确认两条臂都已使能、没有故障灯。使能前清空工作空间。

### 阶段 C —— 第一次训练 ACT

```bash
# 环境（已建好）
~/agilex-data/.venv-lerobot/bin/activate

# 导出
python tools/export-lerobot.py \
  --episodes ~/agilex-data/episodes \
  --out ~/agilex-data/lerobot/agilex_dual_arm

# 训练（ACT 在 LeRobot 里的命令，参数按显存调小）
lerobot-train \
  --dataset.repo_id=local/agilex_dual_arm \
  --policy.type=act \
  --batch_size=8 \
  --steps=50000
```

**8 GB 显存的关键参数**：
- `batch_size` 从 8 开始（ACT 原论文用 64，我们显存不够）
- 图像分辨率降到 224×224 或 96×96
- `chunk_size` 别超过 100
- 先训 5000 步看 loss 走势，别直接上 5 万步

**判据**：训练 loss 稳定下降 → 验证集 MSE 能到个位数度 → 才上真机。

### 阶段 D —— 上机验证（最容易被忽略）

1. **限速**：把 `AGILEX_MAX_LIN` 调到 0.05，`AGILEX_ARM_SPEED_PCT` 调到 10
2. **手放急停上**，第一次跑一定有人在旁边
3. **期望值现实一点**：第一次上机成功率 0–30% 是正常的，不要因为失败就怀疑一切
4. **记录失败案例**，这些是下一轮最值钱的数据
5. **迭代**：采数据 → 训练 → 上机 → 看失败 → 补数据。这个循环以**周**计

---

## 六、诚实的风险提示

- **数据量陷阱**：网上"50 条示教就能学会"通常指高度受控的场景（固定光照、固定物体、
  固定相机）。真实场景需要更多数据，或者更强的泛化架构。
- **双臂不同步**：ACT 原版支持双臂，但我们两条臂是独立 CAN 控制，时间戳要对齐好
  （我们的 recorder 已经保证同帧同 tick）。
- **相机在臂上**：这是 ALOHA 的配置，好处是能看到夹爪附近，坏处是臂动相机也动，
  模型必须同时处理视角变化。
- **底盘没里程计**：任何需要底盘位移的任务，位置反馈只能靠视觉，精度会差很多。
- **预训练权重别迷信**：HuggingFace 上那两个 Piper π0-fast 是个人实验权重，
  下载量 0。可以看格式，不要指望开箱即用。

---

## 七、参考链接

- [innovator-zero/piper-aio](https://github.com/innovator-zero/piper-aio) — Piper 模仿学习全栈参考
- [agilexrobotics/tracer_ros2](https://github.com/agilexrobotics/tracer_ros2) — 底盘官方 ROS2 包
- [agilexrobotics](https://github.com/agilexrobotics/agilexrobotics) — 厂商仓库汇总
- [ALOHA 双机械臂系统](https://www.roboticscenter.ai/guides/aloha-robot) — ACT 方法出处
- [MODEL_AUTHOR/pi0-fast-piper-h-plate-pick-ft-v2](https://huggingface.co/MODEL_AUTHOR/pi0-fast-piper-h-plate-pick-ft-v2) — Piper 微调的 π0-fast（盘子抓取）
- [MODEL_AUTHOR/piper-pi0fast-multistage](https://huggingface.co/MODEL_AUTHOR/piper-pi0fast-multistage) — Piper π0-fast 多阶段课程学习
