# π0.5（pi0.5）专题研究：论文、机制、开放程度与 Tracer 双 Piper 适配路径

**日期：** 2026-11-24（Asia/Shanghai）
**范围：** 只做文献/代码/元数据核对与本地离线数据分析；**未下载任何大模型权重**，未连接机器人做任何运动/使能/服务操作。
**结论先行：** π0.5 是真实存在、经同行评审（CoRL 2025 Oral）、权重与代码部分开放的 VLA，但**开源的只是"低层 flow-matching 动作头"**；高层子任务预测、FAST 动作分词、RL 均未随权重发布。它对 Tracer（差速底盘 + 双 Piper + 仅双腕相机 + 10 Hz 录制 + 角度制关节）**不是 drop-in**，可行路径是"保留 32 维动作空间做 LoRA 微调 + 自己外挂高层规划"，而不是直接跑官方 checkpoint。

---

## 1. 一手来源与日期（全部实际访问核对）

| # | 来源 | 类型 | 日期 | URL | 核对到的关键事实 |
|---|---|---|---|---|---|
| 1 | arXiv 2504.16054（v1） | 论文预印本 | 2025-04-22 17:31 UTC | https://arxiv.org/abs/2504.16054 | 标题、作者列表、摘要；提交历史只有 v1 |
| 2 | OpenReview `vlhoswksBO` | 同行评审记录 | CoRL 2025 | https://openreview.net/forum?id=vlhoswksBO | venue 字段为 **CoRL 2025 Oral**，venueid `robot-learning.org/CoRL/2025/Conference` |
| 3 | PI 官方博客 | 官方项目页 | 2025-04-22 | https://www.physicalintelligence.company/blog/pi05 | 50 步（1 秒）动作块；高层/低层同一模型；消融排名（no WD / no ME / no CE） |
| 4 | PI "Knowledge Insulation" 研究页 | 官方技术页 | 2025-05-28 | https://www.physicalintelligence.company/research/knowledge_insulation | 把 π0.5 的做法形式化为 **π0.5 + KI**：VLM 用 FAST 离散 token 学表征，动作专家梯度**不回传** VLM |
| 5 | openpi 仓库 | 官方代码 | main 分支（2026-11-24 读取） | https://github.com/Physical-Intelligence/openpi | 代码 LICENSE = **Apache-2.0**；提供 π0 / π0-FAST / π0.5 三类模型；π0.5 只支持 flow matching 头 |
| 6 | openpi README 更新日志 | 官方发布说明 | "[Sept 2025] We released pi05" | 同上 | π0.5 权重发布月份；推理/微调显存门槛表 |
| 7 | GCS 权重桶（openpi-assets） | 官方权重存储 | 对象时间戳 **2025-09-08** | `gs://openpi-assets/checkpoints/pi05_base` | 三个 π0.5 checkpoint 各 **12.44 GB**（见 §4 实测） |
| 8 | LeRobot 模型卡 `lerobot/pi05_base` | 第三方再分发（HF） | 未标注 | https://huggingface.co/lerobot/pi05_base | `license: gemma`、base_model `google/paligemma-3b-pt-224`；明确写"只支持 flow-matching 头，子任务预测/动作分词/RL 未随上游发布" |
| 9 | PI 官方 HF 组织页 | 官方权重页 | — | https://huggingface.co/physical-intelligence/pi05_base | **匿名访问 401**（无法在未登录状态核对官方模型卡条款） |
| 10 | RoboColosseum | 独立第三方评测 | 2026-11-02 | https://arxiv.org/abs/2611.02497 | 124 个站点、5,699 次配对试验；**微调后 π0.5 在 Franka 与 Bimanual-YAM 上领先**，但所有系统开放世界仍不可靠 |
| 11 | FlashRT-PAI | 第三方推理优化 | 2026-10-17 | https://arxiv.org/abs/2610.24349 | 4-bit π0.5 在 LIBERO-10 达 94.04%（官方 FP16 参考 92.4%）——说明社区已在做 π0.5 量化部署 |

> 说明：第 1、2 条是同一工作的"预印本 + 会议接收"两份记录；arXiv 只有 v1，CoRL 的 camera-ready 未在 arXiv 更新，因此**引用具体数字时以 v1 为准**，并注明版本。

---

## 2. 它到底是什么：机制（逐条对应论文原文）

### 2.1 架构

- **骨干**：PaliGemma 系 2B VLM。论文附录 E 给出精确尺寸：`width=2048, depth=18, mlp_dim=16384, heads=18, kv_heads=1, head_dim=256`。
- **动作专家**：300M 参数，`width=1024, mlp_dim=4096`，用 flow matching 输出连续动作块。
- **两种动作表示并存**：离散（FAST tokenizer，自回归）与连续（flow matching）；用注意力掩码保证两种表示互不 attend，论文原话是 "information flows unidirectionally from the VLM to the action expert"。
- **π0.5 相对 π0 的结构改动**：时间步 τ 不再与 noisy action 拼接，而是经独立 MLP + **adaRMS** 注入动作专家每一层；机器人本体状态被**离散化成文本 token** 输入（openpi 里对应 `discrete_state_input=True`）。

### 2.2 两阶段训练（论文 v1）与 KI 版本（2025-09 权重）

| 阶段 | 做什么 | 论文给出的规模 |
|---|---|---|
| 预训练 | 全部任务（含机器人动作）都用**离散 token** 表示，做标准自回归 next-token 预测 | 280k gradient steps |
| 后训练 | 加入**随机初始化**的动作专家，用 flow matching + 文本交叉熵联合训练 | 80k steps，α=10.0 |

2025-05-28 的 KI 页面把 π0.5 的做法进一步形式化为 **π0.5 + KI**：VLM 用 FAST 离散动作 token 学"运动表征"，动作专家的梯度**停止回传**到 VLM，避免连续动作损失破坏 VLM 的预训练知识；页面同时给出对比：单纯冻结骨干不行（学不到运动表征），联合训练会损害语言跟随。openpi 在 2025-09-08 放出的 `pi05_base` 即 KI 版，所以 README 写 "trained with knowledge insulation" 与论文 v1 的"两阶段联合训练"**不是矛盾，而是不同版本/不同配方**——引用时必须区分。

### 2.3 动作表示与频率（关键数字）

- **动作块 = 50 步（H=49）**，官方博客明确写 "50-step (1-second) action chunk" → **控制频率 50 Hz**。
- 推理：先自回归解码文本（高层子任务），再 **10 步去噪** 得到动作块（论文 §IV-B、openpi `sample_actions(num_steps=10)`）。
- **动作维固定 32**，低维机器人**零填充**（论文 §IV-C）；openpi 的 π0.5 微调配置也保持 `action_dim=32`（`pi05_droid_finetune` 注释原文："pi05 is trained with 32-dim actions"）。
- 归一化：按**每个数据集**各维的 1%/99% 分位缩放到 [-1,1]。
- 控制模式：用提示词 `<<control_mode>> joint/end effector <<control_mode>>` 区分"关节目标"与"末端位姿目标"两种标签。
- 机器人系统：两台移动双臂平台，**每台 4 个相机（前、后、双腕）**，双 6-DoF 臂 + 平行夹爪 + 轮式底盘 + 升降躯干；**状态/动作 18–19 维**，由模型直接给关节/夹爪/升降目标位姿与底盘速度，50 Hz 下发，底层仅 PD 跟踪，**无轨迹规划、无碰撞检测**。

### 2.4 训练数据配方（这是"开放世界泛化"的真正来源）

| 数据源 | 内容 | 规模/占比 |
|---|---|---|
| MM（移动双臂） | 约 100 个真实家庭的家务数据 | **约 400 小时**；首阶段仅占 **2.4%**（论文：97.6% 来自其他来源） |
| ME（非移动多环境） | 单臂/双臂固定在台面，覆盖更多家庭 | 未给小时数 |
| CE（跨本体实验室） | 各类桌面任务 + **OXE** 开源数据集 | 含单臂、双臂、静/动底盘 |
| HL（高层子任务） | 人工标注子任务文本 + 目标框 | 与动作联合预测 |
| WD（网络多模态） |  captioning / VQA / 物体定位 | CapsFusion、COCO、Cambrian-7M、PixMo、VQAv2 等 |
| VI（语言示教） | 人实时"用语言遥控"机器人完成长任务 | 仅后训练阶段 |

消融（论文 Fig.10–13、官方博客）：去掉 ME 或 CE 显著掉点；去掉 WD 对**分布外物体**的语言跟随影响最大；训练地点数从 3 → 104 时性能单调上升，104 个地点时接近"直接在测试家庭训练"的对照——这是论文"泛化"主张的核心证据，而不是演示视频。

### 2.5 评测的边界（避免把宣传当结论）

- 论文自评：3 个真实家庭（3 厨房 + 3 卧室）+ 3 个 mock 家庭，每任务 10 次试验，按 3–8 分 rubric 计分；任务是"把盘子放水槽 / 把物品放抽屉 / 洗衣篮 / 铺床"。
- 作者自述局限（官方博客原文）："It does not always succeed on the first try"、"often makes mistakes both in terms of its high-level semantic deductions and motor commands"。
- 独立评测（RoboColosseum, 2026-11-02）：开放世界零样本时"frontier agents rival specialized VLA policies, while all systems struggle"；**给每任务 100 条示范微调后**，π0.5 在 Franka 与 Bimanual-YAM 上领先——这条对"能不能用"比厂商视频更有参考价值。
- 未验证：论文没有给推理时延、没有给失败率随物体类别的细表；网上"π0.5 能全屋清洁"的说法属于任务级演示，不等于稳定成功率。

---

## 3. 训练 / 推理的硬性要求（openpi 官方口径）

| 模式 | 显存 | 官方示例 GPU |
|---|---|---|
| 推理 | **> 8 GB** | RTX 4090 |
| LoRA 微调 | **> 22.5 GB** | RTX 4090 |
| 全参微调 | **> 70 GB** | A100 80GB / H100 |

其他已核实约束：

- 官方只在 **Ubuntu 22.04** 测试；依赖 `uv`；JAX 为主，2025-09 起有 PyTorch 实现（π0-FAST、混合精度、FSDP、LoRA、EMA 在 PyTorch 路径**尚不支持**）。
- 单机多卡可开 `fsdp_devices`；**暂不支持多节点**。
- π0.5 的 `max_token_len=200`（π0 为 48），`action_dim` 默认 32，`action_horizon` 默认 50（LIBERO 配置用 10，DROID 微调用 15/16）。
- **推理时延官方未给**。按 50 步块 @50 Hz 推算：每块 1 秒，策略必须 **< 1 s** 出块才能连续；若要 50 Hz 无零阶保持，则需 ~20 ms/次，这在实际部署里通常靠"块 + 异步重规划"实现，而不是每帧推理。

---

## 4. 开放权重、代码与许可（逐项核对）

| 项目 | 状态 | 证据 |
|---|---|---|
| 代码 | **开放，Apache-2.0** | `raw.githubusercontent.com/.../openpi/main/LICENSE` 全文为 Apache License 2.0 |
| 基座权重 | **开放**：`gs://openpi-assets/checkpoints/pi05_base` | GCS 公开可读；29 个对象，合计 **12.44 GB**，时间戳 2025-09-08 |
| 微调权重 | **开放**：`pi05_droid`（12.43 GB）、`pi05_libero`（12.44 GB） | 同上，实测对象大小 |
| 预训练覆盖的本体 | `arx, arx_mobile, droid, fibocom_mobile, franka, trossen, trossen_mobile, ur5e, ur5e_dual` | `pi05_base/assets/*/norm_stats.json` 列表；**含双臂（ur5e_dual）与移动平台**，**无 Piper** |
| 许可（权重） | **不是 Apache-2.0**：LeRobot 再分发标注 `license: gemma`，base_model 为 `google/paligemma-3b-pt-224` | HF API `lerobot/pi05_base` 的 tags/cardData；即权重受 **Gemma Terms of Use** 约束（含使用限制与再分发义务），商用前必须法务确认 |
| 官方 HF 模型卡 | **匿名 401**，未能核对原始条款 | `https://huggingface.co/physical-intelligence/pi05_base` 与 API 均返回 401 |
| 高层子任务头 | **未发布** | LeRobot 模型卡原文："Other components from the original work (e.g., subtask prediction, action tokenization, or RL) were not released upstream"；openpi `pi0.py` 只有 `sample_actions`，无文本解码头 |
| FAST 动作分词器（π0.5 用） | **未随 π0.5 权重发布**（π0-FAST 的 tokenizer 另有开源） | 同上；openpi README："we currently only support the flow matching head" |
| 训练数据 | **未发布**（仅发布 norm stats） | 资产目录只有 `norm_stats.json` |

补充（估算，已标注）：`pi05_base` 的 12.44 GB 是 **checkpoint 文件体积**，按 ~3B 参数推算约 4.2 B/参数，即多为 fp32 存储；bf16 载入显存约 6 GB。下载/磁盘规划按 12.5 GB 计，显存规划按 openpi 的 >8 GB（推理）计。

**结论：** "开源"应准确表述为 **"代码 Apache-2.0 + 权重（Gemma 条款）开放低层策略"**，不是"完整 π0.5 系统开源"。任何"π0.5 全栈可复现"的说法都不成立。

---

## 5. 本地可核实的数据（本次实际运行）

### 5.1 样例外数据审计（`06-数据样例/timeline.csv`，84 帧）

命令（在 `06-数据样例/` 下）：

```bash
python - <<'PY'
import csv, statistics
rows=list(csv.DictReader(open('timeline.csv',encoding='utf-8')))
t=[float(r['t']) for r in rows]
dt=[b-a for a,b in zip(t,t[1:])]
print(len(rows), "rows; median dt", statistics.median(dt), "->", 1/statistics.median(dt), "Hz")
for side in ('L','R'):
    js=[[float(r[f'{side}_j{i}']) for r in rows] for i in range(6)]
    print(side, "max |dq/dt| deg/s:", [round(max(abs(b-a)/(t[i+1]-t[i]) for i,(a,b) in enumerate(zip(js[j],js[j][1:]))),1) for j in range(6)])
    print(side, "range deg:", [round(max(js[j])-min(js[j]),1) for j in range(6)])
PY
```

实测输出：

```
84 rows; median dt 0.1000 -> 10.0 Hz
L max |dq/dt| deg/s: [14.7, 121.3, 3.5, 94.8, 31.1, 163.4]
L range deg:        [2.6, 18.4, 0.6, 16.4, 6.1, 27.8]
R max |dq/dt| deg/s: [1.3, 0.0, 0.0, 0.0, 2.8, 0.0]
R range deg:        [0.1, 0.0, 0.0, 0.0, 0.6, 0.0]
gripper range 0.1 .. 3.7 (deg)
cam0 seq 208..294 | cam2 seq 212..300
```

含义（可直接写进实验设计）：

1. **录制是 10 Hz，不是 π0.5 的 50 Hz**；左臂单关节峰值 **163.4 °/s** → 100 ms 内最大约 **16°** 的台阶。若策略以 10 Hz 出块、50 Hz 零阶保持，抖动/速度限幅会很明显。
2. **这条样例的右臂几乎冻结**（最大 2.8 °/s，5 个关节行程 0.0°），与交付包"早期 41 条 episode 大多退化"的警告一致——**不能直接当训练集**。
3. 夹爪记录是**角度（deg）**，不是 0–1 归一化；π0.5 的夹爪维需要按数据重标定。
4. 相机只有 `cam0`（左腕）/`cam2`（右腕），**没有前向相机**。

### 5.2 机器人侧控制能力（读代码，非运行）

| 事实 | 位置 |
|---|---|
| 关节目标流 **50 Hz**（`ArmBridge.RATE_HZ = 50`） | `02-代码/server/agilex-remote-server.py:469` |
| 反馈读取 5 Hz（`FEEDBACK_HZ = 5`），CAN 反馈本身 200 Hz | 同文件 `:470`、`hardware.md` |
| 相机 640×480 @ **12 fps**（MJPEG over ffmpeg） | 同文件 `:197` |
| 绝对关节角接口 `/api/arm/target`（`joints_deg`） | 同文件 `:2198` |
| 录制 10 Hz、`cam0`=左腕、`cam2`=右腕、动作列是**下发目标角** | `episode-recorder.md`、`06-数据样例` |
| 第三路"前向"Orbbec **USB 上根本不存在** | `hardware.md` §0 |
| 腕部俯仰仅 ±70°，**0.17 m 以上无法竖直朝下** | `00-先看这里.md` |

> 好消息：**50 Hz 关节目标流已经具备**，与 π0.5 的 50 Hz 动作频率天然对齐；瓶颈在"策略推理频率"与"录制频率"，不在执行器接口。

---

## 6. π0.5 vs Tracer 双 Piper：差距清单

| 维度 | π0.5（论文/权重） | Tracer 现状 | 差距性质 |
|---|---|---|---|
| 动作维 | **32**（零填充），实际本体 18–19 | 双臂 6+6 + 夹爪 2 + 底盘 (v, ω) = **16**；无升降 | 结构可对齐（保持 32 维、后 16 维补零）；**但预训练动作投影的语义只覆盖其 9 种本体** |
| 动作语义 | 关节目标 **或** 末端位姿目标，靠 `<<control_mode>>` token 区分；归一化 [-1,1] | `MOVE_J` 绝对关节角（deg），夹爪为角度 | 需统一：建议**关节角（rad）+ 夹爪归一化**，按数据集分位数归一化 |
| 频率 | **50 Hz**，50 步块，10 步去噪 | 执行 50 Hz ✅；录制 **10 Hz** ❌；策略侧无 50 Hz 推理 | 录制与推理链路要改；执行器不用改 |
| 相机 | 低层：**双腕 + 前向**；高层：**4 路**（前后 + 双腕） | **仅双腕**，前向相机硬件缺失 | 硬缺口：要么加装前向 USB 相机，要么接受"仅双腕"的分布偏移并微调 |
| 底盘 | 全向（vx, vy, ω），3 维 | 差速（v, ω），2 维；`/odom` 坏、速度反馈不可靠 | 维度可补零，但**全向→差速**是语义差；里程计不可用意味着不能用"底盘位姿"做状态 |
| 躯干 | 1–2 维升降 | 无 | 补零 |
| 夹爪 | 平行夹爪，归一化 | 角度（deg），`GripperCtrl_V3(g*1000)` | 需重标定与限位保护 |
| 示范数据 | 400 h 移动双臂 + 10k+ h 机器人数据 | 41 条早期 episode（多为退化）；单条样例 8.3 s | 数量与质量都差 2–3 个数量级 |
| 高层推理 | 同一模型出子任务文本 | **权重里没有这个头** | 只能外挂 VLM/规则，或自己训练 |
| 许可 | 代码 Apache-2.0；权重 Gemma 条款 | 项目自身 MIT | 商用需法务评估 Gemma Terms |

**一句话：** 硬件层面"能对齐"（50 Hz 关节流、双臂、双腕相机都在），软件与数据层面"不能直接跑"（无前向相机、无 50 Hz 录制、无 14/16 维 Piper 权重、无高层头）。

### 6.1 本地算力可行性（基于项目自身已测事实）

- 本项目的 5090 机器（4×RTX 5090 32 GB，driver 570.211.01，**sm_120**）已验证：JAX 0.5.3 + CUDA 12.9 能编译并执行 `.target sm_120a` 内核；openpi 能严格形状校验加载 Piper 的 π0-FAST Orbax 权重并完成前向（`openpi-install.md`，2026-10-22）。**"5090 跑不了 JAX/openpi" 这个担心已被项目自己证伪。**
- 据此推 π0.5：**推理（>8 GB）与 LoRA（>22.5 GB）单卡 32 GB 可行**；**全参（>70 GB）单卡不可行**，需要 2×5090 + `fsdp_devices`（openpi 支持单机多卡，不支持多节点）。
- 真正的风险是**排卡**：该项目记录过 4 张卡分别被 llama-server / 旧 policy server 占用、单卡只剩 2.6–12.3 GB 的情况；上 π0.5 前必须先确认至少一张卡有 24 GB 以上空闲。

---

## 7. 建议的适配路径（按代价从低到高）

### 7.0 针对"夹取前方板子"这个具体任务

1. **抓取策略先定**：板子立/斜放时，让双腕从左右两侧夹住板子的两条竖边，**不要设计顶抓**——腕部俯仰只有 ±70°，且 0.17 m 以上无法竖直朝下（`00-先看这里.md`）。这条约束会直接决定示范数据的起始位姿。
2. **视角**：腕相机随夹爪朝向板边，能看到"夹爪—板边"的相对位姿，但**没有全局深度**。若板子位置固定，可先用一次手眼标定把板子位姿写入状态或 prompt，降低对前向相机的依赖；若板子会移动，前向相机几乎是必需项。
3. **动作**：用 `MOVE_J` 绝对关节角目标（rad），块内 50 Hz 线性插值；夹爪维在接近段保持张开、接触段闭合。注意当前录制**只有夹爪角度、没有电流/力矩**，接触检测只能靠位置阈值或外接传感器（缺口）。
4. **数据覆盖**：每条 episode 从"板子在前方 0.4–0.6 m、双臂自然位"开始，覆盖 ±10 cm 横向、±15° 偏航、±5 cm 高度；至少 100 条可用 episode，再谈微调。

### 路径 A（推荐，先做）：**π0.5 base + LoRA，16 维补零到 32，10 Hz 出块 + 50 Hz 插值**

1. **动作/状态定义**（先定死，再采数据）：
   - state = `[L_j0..L_j5 (rad), L_grip (0–1), R_j0..R_j5 (rad), R_grip (0–1), v, ω]` → 16 维；
   - action = 同一 16 维的**目标值**（与现有 recorder 的"下发目标角"语义一致），补零到 32；
   - 夹爪用实测 min/max 归一化，不用固定 0–1 假设。
2. **相机**：短期"仅双腕"训练（接受偏移）；中期在双臂之间加装 1 个 USB 前向相机（UVC，和现有 Orbbec 同路径），补齐 π0.5 低层所需的前向视角。
3. **频率**：策略 10 Hz 出 10–20 步块（`action_horizon=10/20`），执行端 50 Hz **线性插值**目标角；这与 `pi05_libero` 的 `action_horizon=10` 配置同构，风险最低。
4. **训练**：`pi05_base` 权重 + LoRA（>22.5 GB，单张 5090 可跑），`action_dim=32`、`discrete_state_input=True`（与基座一致），数据转 LeRobot 格式。
5. **高层**：**不要等官方头**。用外部 VLM（或简单的任务状态机）产出子任务文本，作为 `prompt` 送入低层策略；这等价于 π0.5 的"高层→低层"条件化，只是高层不在同一模型里。

### 路径 B（中期）：**自采 2–5 小时数据，全参或 LoRA 微调，补前向相机**

PI 对 π0 的经验是 **1–20 小时**可微调到多种任务（2025-02-04 openpi 博客）；RoboColosseum（2026-11）用**每任务 100 条示范**把 π0.5 微调到 Franka/Bimanual-YAM 领先。对"夹取前方板子"这种**单一任务、固定工位**，100–300 条、每条 10–30 s 是合理起点（约 0.5–2.5 h），但**必须先去退化 episode**。

### 路径 C（不推荐作为主线）：**继续用现有单臂 Piper π0-FAST checkpoint**

项目已有 `Kavin60606/pi0-fast-piper-h-plate-pick-ft-v2`（7 维、右臂、弧度、chunk 10 @20 Hz）的可行性结论（`pi0-openvla-feasibility.md`）：格式可转、权重可载，但**左臂无信息通路**、π0-FAST 改维需重训。它适合做"右臂单臂抓板"的对照，不适合双臂协同。

---

## 8. 建议的小规模离线实验（除 E4 外都不需要下载大模型；E4 本次未执行）

| ID | 实验 | 目的 | 成本 | 通过标准 |
|---|---|---|---|---|
| E0 ✅ | 本文 §5.1 的 episode 审计 | 量化退化率、频率、行程 | 已跑（<1 s） | 已得：右臂冻结、10 Hz、峰值 163 °/s |
| E1 | 把**全部**本地 episode 跑同一审计（逐条输出每臂行程/峰值速度/相机重复帧率） | 选出可用子集 | 纯 CPU，分钟级 | 可用 episode ≥ 30 条、每臂行程 > 10° |
| E2 | 用 1 条 episode 构造 LeRobot 格式样本（parquet + videos + `meta/info.json`），用 `lerobot` 的 dataset loader 读回 | 验证 schema/单位/时间对齐 | 无 GPU，需装 lerobot | 读回后 state/action 形状 = (T,16)，图像帧号与 `frame` 列一一对应 |
| E3 | **时序仿真**：把 10 Hz 关节序列按 (a) 零阶保持 (b) 线性插值 重采样到 50 Hz，比较每 20 ms 的关节增量与加速度 | 决定 10 Hz 出块是否可接受 | 纯 Python | 插值后最大单步增量 < 1.5°（当前 100 ms 台阶 16°） |
| E4 | **推理时延预算**：在 5090 上只跑 `pi05_base` 的 dummy forward（约 12.4 GB 权重；需下载，**本次未做**） | 确认 < 1 s/块 | 12.4 GB 下载 + 一张空卡 | p50 < 500 ms、p95 < 900 ms |
| E5 | MuJoCo 里用现有 Piper 模型验证"前方板子"的可达抓取位姿（腕俯仰 ≤ 70°、0.17 m 以上不能竖直向下） | 排除运动学不可行 | 已有仿真环境 | 存在 ≥ 3 组可达抓取位姿 |
| E6 | LoRA 微调 100 条（E1 筛选后）@10 Hz、horizon 10 | 验证"能不能学" | >22.5 GB 显存、数小时 | 留出集动作 L1 误差 < 0.1（归一化），真机成功率 > 0（需另行授权） |

**E4 是唯一需要下载权重的实验，按任务要求本次未执行**；E1–E3、E5 都不需要。

---

## 9. 明确可复用的部分 / 缺口 / 不建议做的事

**可直接复用**
- openpi 的 `pi05_base` + LoRA 微调脚本 + `serve_policy.py`（WebSocket 远程推理模式），代码 Apache-2.0。
- LeRobot 数据集格式与 `compute_norm_stats.py` 的归一化流程。
- 本机已有的 **50 Hz 关节目标流**（`RATE_HZ=50`）与绝对角接口，正好匹配 π0.5 的动作频率。
- 32 维动作空间 + 零填充的官方约定（避免自己改维度导致权重不匹配）。

**缺口（必须自己补）**
- 高层子任务头（未发布）→ 外挂 VLM 或状态机。
- 前向相机（硬件缺失）→ 加装或接受双腕训练。
- 50 Hz 录制（当前 10 Hz）→ 改 recorder 或客户端高频采样（**属于机器人文件修改，需另行授权**）。
- Piper 本体的 norm stats 与 14/16 维动作语义 → 只能自采数据。
- 权重许可（Gemma Terms）→ 商用前法务确认。

**不建议**
- 不要把 π0.5 当作"零样本就能抓板"的方案：论文与独立评测都表明开放世界仍不可靠，且你的相机/频率/维度全都偏移。
- 不要直接用 `pi05_droid` 或 `pi05_libero` 零样本上机：它们的动作空间是 Franka/LIBERO，与 Piper 无关。
- 不要为了对齐 18–19 维而虚构"升降/全向底盘"维度；补零 + 固定语义更安全。
- 不要在没有前向相机的情况下期待 π0.5 论文级的泛化；双腕视角对"前方板子"的空间定位信息不足。

---

## 10. 未验证项（诚实边界）

| 项目 | 状态 |
|---|---|
| 官方 HF 模型卡的许可原文 | **未读到**（匿名 401） |
| π0.5 在 5090 上的真实推理时延 | **未测**（未下载权重） |
| CoRL camera-ready 与 arXiv v1 的差异 | **未核对**（arXiv 无 v2） |
| "π0.5 + KI" 与 arXiv v1 配方在权重层面的具体差异 | 只能确认发布说明与 KI 页面措辞；**权重内部无从外部区分的方法** |
| Tracer 上 10 Hz 出块 + 插值的实际跟踪误差 | **未上机**（受运动禁令限制） |
| `lerobot/pi05_base` 与 `gs://openpi-assets/pi05_base` 是否逐位一致 | **未验证**（只核对了形状/大小与来源说明） |

---

## 附：本次使用的核对命令（可复现）

```bash
# 1) 论文元数据
curl -s https://arxiv.org/abs/2504.16054 | grep -E "Submitted|Title"

# 2) OpenReview 会议记录
curl -s "https://api2.openreview.net/notes/search?term=Vision-Language-Action%20Model%20with%20Open-World%20Generalization&type=terms&content=all&group=all&source=forum&limit=3"

# 3) 权重是否存在 + 体积（公开桶，无需登录）
curl -s "https://storage.googleapis.com/storage/v1/b/openpi-assets/o?prefix=checkpoints/pi05_base/&maxResults=200&fields=items(name,size)"

# 4) 代码许可
curl -s https://raw.githubusercontent.com/Physical-Intelligence/openpi/main/LICENSE | head -3

# 5) 权重许可（第三方再分发元数据）
curl -s https://huggingface.co/api/models/lerobot/pi05_base

# 6) openpi 实现细节
curl -s https://raw.githubusercontent.com/Physical-Intelligence/openpi/main/src/openpi/models/pi0_config.py
curl -s https://raw.githubusercontent.com/Physical-Intelligence/openpi/main/src/openpi/training/config.py
```
