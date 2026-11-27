# Task G：面向双 Piper 夹取/移动操作的世界模型与规划调研（2024–2026）

**调研日期**：2026-11-24  
**对象**：AgileX Tracer + 双 Piper 6 轴臂；机器人主机 Ubuntu 22.04 / ROS 2 Humble，板载 **RTX 4060 8 GB**  
**方法**：arXiv 元数据 + 官方项目页 + 官方 GitHub README/配置文件 + 交付包实测文档。  
**边界**：未下载任何模型权重；未对机器人做运动、使能、复位、模式切换或服务变更；未读取任何秘密文件。  
**证据标记**：`[官方]` 官方博客/项目页；`[论文]` arXiv/会议论文；`[代码]` 官方仓库/配置；`[本地]` 交付包实测文档；`[推断]` 基于硬件的判断、未实测；`[未验证]` 没有一手证据。

---

## 0. 结论先行

1. **Genie 1–4 不能作为机械臂规划器。** 它们是交互式视频/世界模拟器，控制接口是键盘、导航或文本提示，不是 14 维关节目标或末端位姿；而且全部闭源，Genie 3/4 只对受限测试者开放。把它们当“机器人世界模型”是范畴错误。
2. **DreamerV3 / TD-MPC2 是真正“能规划”的开源 latent 世界模型**，但都需要奖励函数和大量在线交互。TD-MPC2 官方明确：单任务在线 RL 推荐 8 GB 显存；DreamerV3 小/中配置单卡可试（官方未给 VRAM 数字，需实测）。它们适合 MuJoCo 仿真和接触技能，不适合拿现有 41 条退化 episode 直接上真机做在线 RL。
3. **UniPi / RoboDreamer 是“视频计划 + 逆动力学”路线。** UniPi 官方代码没有释出（1.7B 视频扩散）；RoboDreamer 有官方训练代码，但只报告仿真执行，README 未提供预训练权重。两者都不是工程基线。
4. **V-JEPA 2-AC / DINO-WM 是“图像目标 + latent 规划”的开源路线。** V-JEPA 2-AC 用 <62 h Droid 无标注视频后训练，零样本在 Franka 上抓放；但官方后训练配置是 4 节点 × 8 卡（32 卡）。DINO-WM 可在单卡、离线轨迹上训练，但只在 PushT/迷宫/可变形物体上验证，**没有抓取证据**。
5. **Cosmos 家族是“视频世界模型 + 数据/评估/策略”平台。** Predict2.5 有 2B/14B，Cosmos 3 有 4B/16B/64B；开源但吃卡。其中 **Cosmos Policy 2B** 是目前最接近“开源、能真机、双臂”的世界模型策略：ALOHA 双臂、185 条演示、直接推理 **6.0 GB**、best-of-N 规划 **10.0 GB**；但微调需要 **8×H100 48 h**。
6. **对这台双 Piper 的现实结论：不要让世界模型替代低层控制。** 合理分工是：**经典视觉伺服 / MoveIt / ACT 做低层与基线；世界模型做高层子目标、离线评估、合成数据、接触技能仿真。** 具体优先级见 §6。

---

## 1. 本机约束（已核实，决定了所有选型）

| 项目 | 实测值 | 来源 |
|---|---|---|
| 机载计算 | **NVIDIA RTX 4060，8 GB**，compute 8.9，driver 580.178.04，CUDA 12.8/12.9 | `[本地]` `03-文档与报告/nvidia-cuda.md` |
| 系统/中间件 | Ubuntu 22.04.5，ROS 2 Humble，Piper SDK/CAN 双总线 | `[本地]` `hardware.md`、`controller-modes.md` |
| 相机 | 2× Orbbec 彩色 UVC（`2bc5:0557`）；2× 深度 USB（`2bc5:0657`）**没有 ROS 驱动**；第三“front”相机未在 USB 上检测到 | `[本地]` `hardware.md`、`sensors-and-limits.md` |
| 动作空间 | 双 Piper **14 维关节目标**（每臂 6 关节 + 夹爪）；MOVE_J 以 **50 Hz** 流式下发 `JointCtrl` | `[本地]` `controller-modes.md`、`oss-data-models-sim.md` |
| 录制 | recorder 默认 **10 Hz**（`AGILEX_RECORD_HZ=10`）；彩色 JPEG + 关节角 + 下发目标；**不录深度** | `[本地]` `episode-recorder.md` |
| 数据现状 | 早期 41 条 episode 大多退化（姿势冻结、右臂几乎没动）；交付文档建议 50–100 条干净示教起步 | `[本地]` `00-先看这里.md`、`open-source-models-and-tasks.md` |
| 机械限制 | 腕部俯仰 **±70°**；夹爪在 **0.17 m 以上无法竖直朝下**；无六维力/力矩；`/odom` 长期为 0 | `[本地]` `00-先看这里.md`、`sensors-and-limits.md` |
| 第三人称 | **没有**；只有两个腕部相机 | `[本地]` `hardware.md` |

**这三条约束直接排除了大部分“大模型方案”：**

- **8 GB 显存**：14B/16B/64B 视频模型不能本地训练，推理也很勉强；2B 级模型才是可讨论的起点。
- **10 Hz 录制 / 50 Hz 控制**：多数视频世界模型按 15–50 Hz 训练；直接用 10 Hz 数据会丢失接触瞬间，必须重采样或提高录制频率。
- **2 个腕部相机、无第三人称、无深度**：ALOHA/Cosmos Policy 要 1 个第三人称 + 2 个腕部；SuSIE 要第三人称图像；V-JEPA 2-AC/Droid 要外部单目。相机布局是比“模型选型”更早的瓶颈。

---

## 2. 判定标准

本文把“世界模型”分成四类，避免把“会生成视频”误判成“会控制机器人”：

| 类别 | 定义 | 例子 |
|---|---|---|
| **A. 闭环规划** | 在每一步用预测模型评估候选动作，选动作并执行，形成闭环 | DreamerV3、TD-MPC2、V-JEPA 2-AC、DINO-WM、Cosmos Policy（best-of-N） |
| **B. 视频骨干策略** | 用视频世界模型的 latent 直接解码动作，不做显式 rollout | Cosmos 3（action 输出）、Genie Envisioner GE-Act、VPP |
| **C. 视频生成/数据** | 只生成未来视频或合成数据，动作由外部 IDM/策略给出 | Genie 1–4、Cosmos-Predict、DreamGen、UniPi/RoboDreamer 的视频计划部分 |
| **D. 表征** | 只提供视觉表征，不直接产生动作 | V-JEPA 2/2.1 编码器、VPP 的预测表征 |

“开源可本地跑”要同时满足：**代码 + 权重 + 许可证 + 8 GB 可行性**。只有代码没有权重、或只有权重没有训练代码，都只能算“部分可用”。

---

## 3. 逐项核实

### 3.1 Genie 1–4（DeepMind）

- **Genie 1**：arXiv 2402.15391，2024-02-23；11B 参数；从无标注互联网视频学习 latent action；2D 平台游戏 + 机器人视频；官方项目页没有代码/权重链接。`[论文]` `[官方]`
- **Genie 2**：官方博客 2024-12-04；从单张图生成可玩的 3D 世界；键盘/鼠标控制；一致性最长约 1 分钟；没有代码/权重。`[官方]`
- **Genie 3**：官方博客 2025-08-05；720p、24 fps、实时交互；一致性几分钟；官方明确写“**limited action space**”；limited research preview，只给少量学术/创作者。`[官方]`
- **Genie 4**：官方博客 2026-10-27；原生音频、实时语言改写场景、Gemini 参与长程一致性；仍是 research preview，先给 trusted testers / Google AI Ultra 的 Project Genie / Gemini API 早期伙伴；**没有开放权重**。`[官方]`

**能力判断**：Genie 是“可交互的未来视频生成器”，不是“给定关节动作预测下一帧”的机器人动力学模型。它的动作是键盘/导航/文本，不是 Piper 的 14 维关节或末端位姿；也没有度量精度的保证。  
**对 Piper**：**不建议投入**。最多作为“合成视频/环境生成”的灵感来源；但闭源、无 API 权重、无动作接口，工程上不可用。

### 3.2 DreamerV3 / Dreamer 4

**DreamerV3**

- arXiv 2301.04104，2023-01-10；Nature 正式版 2025-04-02（Nature 640, 647–653，DOI 10.1038/s41586-025-08744-2）。`[论文]`
- 方法：从经验学习 categorical latent world model，在 imagination 中训练 actor-critic；一套超参覆盖 150+ 任务。`[论文]`
- 开源：`danijar/dreamerv3`，MIT，JAX；README 说默认尝试 GPU，代码在 Linux/Mac 测试。`[代码]`
- 真实机器人证据：**DayDreamer（2022，CoRL）** 把 Dreamer 直接用在 4 台真实机器人上；两台机械臂从相机图像 + 稀疏奖励学会抓放，接近人类水平；四足 1 小时学会站立/行走。`[官方]`（这是 2022 年背景证据，不是 2024–2026 新工作）
- 关键限制：**需要奖励函数和在线交互**；不是“给几十条示教就能用”的模仿学习。

**Dreamer 4**

- arXiv 2509.24527，2025-09-29；shortcut forcing + 高效 transformer；Minecraft 仅离线数据拿到钻石；世界模型单 GPU 实时交互推理；真实机器人部分只展示定性 counterfactual 视频。`[论文]` `[官方]`
- 开源：官方项目页没有代码链接；搜索到的 `lucidrains/dreamer4`、`nicklashansen/dreamer4` 都是**非官方**实现。`[未验证]` 官方代码未释出。
- 对 Piper：**不能作为工程方案**；但它的“离线视频 + imagination training”思路值得关注。

**8 GB 可行性**：DreamerV3 小/中配置在 8 GB 上“很可能可行”，但官方 README 没有给 VRAM 数字，需实测；Dreamer 4 没有官方代码，无法评估。`[推断]`

### 3.3 TD-MPC2

- arXiv 2310.16828，2023-10-25；ICLR 2024；104 个连续控制任务；单一 317M 参数 agent 跨 80 任务/多本体/多动作空间。`[论文]`
- 方法：在 latent space 做局部轨迹优化（MPC），decoder-free implicit world model；**需要奖励**，以 online RL 为主，也支持多任务 offline RL。`[论文]`
- 开源：`nicklashansen/tdmpc2`，MIT；README 明确：**单任务 online RL 建议 ≥8 GB GPU、≥12 GB RAM**；317M 训练建议 ≥24 GB GPU；多任务 offline RL 需要 128 GB RAM。`[代码]`
- 真实机器人：论文/README 没有真机抓取结果；主要是 DMControl、Meta-World、ManiSkill2、MyoSuite 仿真。`[论文]`
- 对 Piper：**MuJoCo 仿真里做接触技能/基线的首选之一**；不要直接上真机在线 RL。

### 3.4 UniPi

- arXiv 2302.00111，2023-01-31；NeurIPS 2023 Spotlight。`[论文]`
- 方法：把策略写成“文本条件视频生成”：扩散模型生成未来帧计划，再用 inverse dynamics model（IDM）从计划中提取动作；统一不同本体的图像状态空间。`[论文]`
- 规模：论文 HTML 提到视频扩散 **1.7B 参数**，10 帧 48×64 基础生成 + 20 帧时间超分；Bridge 7.2k video-text 对 + 人类视频预训练。`[论文]`
- 开源：官方项目页明确写“Most of the experiments in the paper are run at Google and the source code cannot be released”，只指向 AVDC 作为“open-source codebase for training UniPi”的替代参考。`[官方]`
- 真机：项目页的“Real Robot Videos”展示的是**在真实图像上生成视频计划**，不是闭环执行成功率。`[官方]`
- 对 Piper：**没有可复现的官方实现/权重**；1.7B 视频扩散在 8 GB 上训练不现实。只能作为“视频计划 + IDM”的研究参考。

### 3.5 SuSIE

- arXiv 2310.10639，2023-10-16；ICLR 2024。`[论文]`
- 方法：微调 InstructPix2Pix，把当前图像 + 语言指令“编辑”成未来子目标图像；低层 goal-conditioned policy（GCBC）执行子目标。高层负责语义/抓取姿态，低层负责电机动作。`[论文]` `[官方]`
- 开源：`kvablack/susie`，包含高层图像编辑扩散训练代码；HuggingFace 上有 Bridge + Something-Something 训练 40k 步的 UNet 权重；低层用 BridgeData V2 的 `gc_ddpm_bc`，action horizon 4。`[代码]`
- 真机：WidowX 上的真实抓放；CALVIN 上 SOTA；项目页报告在若干场景击败 RT-2-X 和 oracle GCBC。`[官方]`
- 对 Piper：**最现实的“高层子目标”方案**。需要第三人称图像；低层可换成 ACT/GCBC；不是动力学世界模型，不能预测接触力。

### 3.6 RoboDreamer

- arXiv 2404.12377，2024-04-18；ICML 2024。`[论文]`
- 方法：把语言指令分解成 action phrase / relation phrase，组合多个视频扩散模型，做 compositional text-to-video 世界模型；支持 goal image / sketch 多模态条件。`[论文]`
- 开源：`UMass-Embodied-AGI/robodreamer` 是官方 repo，含训练代码和示例数据；README 没有给预训练权重；代码基于 imagen-pytorch + AVDC。`[代码]`
- 执行：摘要明确“enables successful robot execution **in simulation**”；项目页展示 RT-X/RLBench 仿真。`[论文]` `[官方]`
- 对 Piper：**研究复现可以，工程落地不行**；视频扩散训练在 8 GB 上不现实，且没有真机闭环证据。

### 3.7 Cosmos 家族（NVIDIA）

**Cosmos-Predict2.5**

- arXiv 2511.00062，2025-10-28；2025-10 发布；2B/14B；flow-based，统一 Text2World/Image2World/Video2World；200M curated clips；Cosmos-Reason1 做文本 grounding。`[论文]` `[代码]`
- 开源：`nvidia-cosmos/cosmos-predict2.5`；代码 Apache-2.0；模型权重 NVIDIA Open Model License；提供 2B robot/action-cond、2B robot/policy（Libero/RoboCasa）等 checkpoint。`[代码]`
- 用途：合成数据、策略评估、closed-loop simulation；不是开箱即用的 Piper 策略。

**Cosmos 3**

- arXiv 2606.02800，2026-06-01；2026-05 发布；omnimodal（语言/图像/视频/音频/动作），Mixture-of-Transformers；Reasoner + Generator 两塔；Generator 可输出 action。`[论文]` `[代码]`
- 规模：**Super 64B**（H200/B200/GB200）、**Nano 16B**（H100/RTX Pro 6000）、**Edge 4B**（Jetson AGX Orin/Thor/RTX Pro 6000）；OpenMDW-1.1。`[代码]`
- 生态：Cosmos3-Edge-Policy-DROID（DROID 策略，edge 部署）；Cosmos3-Nano-Policy-DROID。`[代码]`
- 对 Piper：**16B/64B 完全不可能在 8 GB 上跑**；Edge 4B 官方目标也不是 8 GB 消费卡，而是 32–64 GB Jetson / RTX Pro 6000。`[推断]`

**Cosmos Policy（重点）**

- arXiv 2601.16163，2026-01-22。`[论文]`
- 方法：把 Cosmos-Predict2 **2B** 视频模型单阶段后训练成机器人策略；动作、未来状态图像、value 都编码成 latent frames；推理时可做 best-of-N 规划（用预测的未来帧 + value 评分）。`[论文]` `[代码]`
- 开源：`NVlabs/cosmos-policy`，Apache-2.0；代码、模型、ALOHA 训练数据都发布。`[代码]`
- 真实机器人：ALOHA 双臂 4 个任务（折衣服、糖果入碗/袋、茄子/鸡翅上盘等）；LIBERO 98.5%、RoboCasa 67.1% 平均成功率；真实 ALOHA 平均分最高。`[论文]`
- 资源（官方 README）：**ALOHA 直接推理 6.0 GB VRAM**；**带 best-of-N 规划最低 10.0 GB**；训练 **8×80 GB H100 48 h**（<200 条演示），RoboCasa 32 卡、LIBERO 64 卡。`[代码]`
- 数据：ALOHA 混合数据集 **185 条演示**；输入 **1 张第三人称 + 2 张腕部** 共 3 路图像 + proprio；action chunk 16。`[代码]`
- 对 Piper：**这是目前最值得认真评估的“开源世界模型策略”**。结构上 14 维双臂 + 3 相机与 ALOHA 同构；但 Piper 只有 2 个腕部相机，需要加第三人称；且必须微调，ALOHA checkpoint 不能零样本迁移。`[推断]`

### 3.8 V-JEPA 2 / 2-AC / 2.1

- V-JEPA 2：arXiv 2506.09985，2025-06-11；1B ViT-g 编码器，>1M 小时互联网视频预训练；V-JEPA 2-AC 用 **<62 h Droid 无标注机器人视频**后训练成 action-conditioned latent world model；零样本在 Franka 上做 reach/grasp/pick-place（图像目标，无奖励、无任务特定训练）。`[论文]`
- 开源：`facebookresearch/vjepa2`，MIT；ViT-L 300M / ViT-H 600M / ViT-g 1B checkpoints；V-JEPA 2-AC checkpoint；V-JEPA 2.1（2026-03-15/16）新增 80M–2B 模型，论文报告真实机器人抓取成功率比 V-JEPA 2-AC **+20 点**。`[代码]` `[论文]`
- 资源：官方 AC 后训练配置 `configs/train/vitg16/droid-256px-8f.yaml` 是 **4 nodes × 8 tasks = 32 卡**，220 GB host memory/GPU；这不是 8 GB 能训的。`[代码]`
- 推理：1B 编码器 + AC predictor，8 GB 上“有希望”，但官方没有给 VRAM 数字，必须实测；CEM 规划会放大显存/时延。`[推断]`
- 对 Piper：**低成本“图像目标规划”研究首选之一**。需要末端位姿动作（Droid 是 7-DoF EEF + gripper），Piper 可用 SDK 的 end pose + IK 转换；但 Droid/Franka 到 Piper 的域差异大，双臂扩展未验证。

### 3.9 Genie Envisioner（AgiBot）

- arXiv 2508.05635，2025-08-07；2025-08-13 代码、08-14 GE-Base 权重；GE-Act v2 2026-09-10。`[论文]` `[代码]`
- 组成：GE-Base（LTX-Video **2B** 指令条件视频扩散）、GE-Act（latent → flow-matching 动作解码）、GE-Sim（Cosmos2-based action-conditioned 神经模拟器）、EWMBench。`[论文]` `[代码]`
- 开源：`AgibotTech/Genie-Envisioner-V1`；大部分代码/数据 **CC BY-NC-SA 4.0**，部分目录 Apache-2.0；权重在 ModelScope/HuggingFace。`[代码]`
- 数据格式：LeRobot，3 路相机 `top_head / hand_left / hand_right`；支持 joint/delta/absolute 动作。`[代码]`
- 对 Piper：**2B 视频模型 + 3 相机 + 非商用许可**；8 GB 上推理都要看优化，训练不现实。适合有 24–48 GB 卡的团队做合成数据/评估，不适合机器人板载。

### 3.10 DreamGen（NVIDIA GR00T-Dreams）

- arXiv 2505.12705，2025-05-19；用 Cosmos-Predict2 视频世界模型生成合成机器人视频，再用 IDM/latent action 恢复伪动作，最后微调 GR00T N1。`[论文]`
- 开源：`NVIDIA/GR00T-Dreams`，Apache-2.0；支持 franka / gr1 / so100 / robocasa；视频模型路径里出现 `cosmos_predict2_14b`。`[代码]`
- 结果：人类机器人只用一个 pick-place 任务/一个环境的数据，就能扩展到 22 个新行为（论文摘要）；这是“合成数据扩展”的证据，不是“规划器”的证据。`[论文]`
- 对 Piper：**14B Cosmos 在 8 GB 上不可行**；如果租 8×H100，可作为数据增广管线；需要为 Piper 训练 IDM。

### 3.11 DINO-WM

- arXiv 2411.04983，2024-11-07；用 DINOv2 patch 特征做世界模型，不重建像素；在离线轨迹上训练，测试时把 goal image 的特征当预测目标，用动作优化实现 zero-shot planning；不需要专家演示、奖励模型或预学 IDM。`[论文]`
- 开源：`gaoyuezhou/dino_wm`，MIT；PointMaze、PushT、Wall、deformable（granular/rope）等；提供预训练 checkpoint。`[代码]`
- 对 Piper：**单卡/小数据最友好的世界模型原型**；但原论文任务是 push/maze/deformable，**没有抓取/夹爪接触证据**。可以作为“世界模型规划是否值得做”的低成本试验，不宜直接当产品。

### 3.12 VPP（Video Prediction Policy）

- arXiv 2412.14803，2024-12-19；ICML 2025 Spotlight。`[论文]` `[官方]`
- 方法：用视频扩散模型的“预测视觉表征”作为策略条件，学习 implicit inverse dynamics；在 CALVIN、MetaWorld、真实 Panda、XHand 上评估。`[论文]` `[官方]`
- 开源：`roboterax/video-prediction-policy`（项目页 Code 链接）。`[代码]`
- 对 Piper：**是策略骨干/表征路线，不是规划器**；训练视频扩散的成本高于 ACT/DP，8 GB 不现实；可作为“视频预训练表征”的参考。

### 3.13 其他值得知道但不在第一优先级

- **Matrix-Game 2.0**（Skywork，2025-08）：开源的交互式视频世界模型，常被当作 Genie 3 的开源替代；但仍是游戏/导航控制，不是机械臂动作接口。`[未验证]` 未在本次一手核实中展开。
- **DreamZero**（2026，14B world-action model）：检索结果提到，但本次未做一手核实；14B 对 8 GB 不可行。`[未验证]`
- **GR-2 / GR00T N1**：视频预训练 + 策略，不是规划器；与本题“世界模型规划”关系较弱，未展开。

---

## 4. 对比矩阵

图例：✅ 支持/可行；⚠️ 有条件/需实测；❌ 不支持/不可行；— 不适用。

| 系统 | 日期 | 类型 | 闭环规划 | 开源/权重 | 8 GB 本地 | 对双 Piper 的定位 |
|---|---|---:|---|---|---|---|
| Genie 1 | 2024-02 | C 视频/世界 | ❌ | ❌ 无代码/权重 | ❌ | 不可用；范畴错误 |
| Genie 2 | 2024-12 | C 视频/世界 | ❌ | ❌ | ❌ | 不可用 |
| Genie 3 | 2025-08 | C 视频/世界 | ❌ | ❌ 受限预览 | ❌ | 不可用 |
| Genie 4 | 2026-10 | C 视频/世界 | ❌ | ❌ 受限预览 | ❌ | 不可用；可关注 API |
| DreamerV3 | 2023-01 / Nature 2025-04 | A latent RL | ✅（需奖励） | ✅ MIT | ⚠️ 小配置可试 | MuJoCo 仿真/接触技能；非首选真机 |
| Dreamer 4 | 2025-09 | A latent RL | ✅（Minecraft） | ❌ 无官方代码 | ❌ | 不可工程化；关注思路 |
| TD-MPC2 | 2023-10 / ICLR 2024 | A latent MPC | ✅（需奖励） | ✅ MIT | ✅ 单任务 8 GB | MuJoCo 仿真基线；非真机首选 |
| UniPi | 2023-01 / NeurIPS 2023 | C 视频计划 + IDM | ⚠️ 开环计划 | ❌ 官方代码未释出 | ❌ 1.7B | 研究参考；不可复现 |
| SuSIE | 2023-10 / ICLR 2024 | B 高层子目标 | ⚠️ 子目标 + 低层 | ✅ 代码+UNet 权重 | ⚠️ 推理可，微调紧 | 最现实的高层子目标方案 |
| RoboDreamer | 2024-04 / ICML 2024 | C 视频计划 | ⚠️ 仿真执行 | ⚠️ 代码有，权重无 | ❌ 视频扩散 | 研究复现；非工程 |
| Cosmos-Predict2.5 | 2025-10 | C 视频/数据 | ⚠️ 需后训练 | ✅ 2B/14B | ⚠️ 2B 推理勉强（需实测） | 合成数据/评估；非板载 |
| Cosmos 3 | 2026-06 | B/C 世界动作 | ⚠️ 可输出 action | ✅ 4B/16B/64B | ❌ 16B/64B；4B 非 8 GB 目标 | 有卡团队的前沿平台 |
| **Cosmos Policy** | **2026-01** | **A/B 策略+规划** | **✅ best-of-N** | **✅ Apache-2.0** | **✅ 直接 6 GB；⚠️ 规划 10 GB 超 8 GB** | **最值得评估的双臂世界模型策略** |
| V-JEPA 2-AC | 2025-06 | A latent 规划 | ✅ 图像目标 | ✅ MIT | ⚠️ 推理待实测；训练 32 卡 | 低成本图像目标规划研究 |
| V-JEPA 2.1 | 2026-03 | D/A 表征+规划 | ⚠️ 有 AC 版本 | ✅ MIT | ⚠️ 待实测 | 2026 更新；关注抓取提升 |
| Genie Envisioner | 2025-08 | B/C 平台 | ⚠️ GE-Act/GE-Sim | ⚠️ CC BY-NC-SA | ❌ 2B+3 相机 | 有卡团队的合成数据/评估 |
| DreamGen | 2025-05 | C 数据生成 | ❌ 非规划器 | ✅ Apache-2.0 | ❌ 14B | 合成数据管线；非板载 |
| DINO-WM | 2024-11 | A latent 规划 | ✅ 目标特征 | ✅ MIT | ⚠️ 小模型可试（需实测） | 单卡低成本原型；无抓取证据 |
| VPP | 2024-12 / ICML 2025 | B/D 表征策略 | ❌ 非规划器 | ✅ 代码 | ⚠️ 推理；训练难 | 表征/骨干参考 |

---

## 5. 对“单台双 Piper、小数据”的现实性

### 5.1 数据

- **现有 41 条早期 episode 大多退化**，不能直接作为世界模型训练集。交付文档建议 50–100 条干净示教起步；双臂协同任务建议 150–300 条。`[本地]`
- **录制默认 10 Hz，控制 50 Hz**。世界模型训练前要做时间对齐/插值；接触瞬间（夹爪闭合、物体滑动）在 10 Hz 下很容易丢帧。`[本地]`
- **14 维关节目标**与 ALOHA/Cosmos Policy 的 14 维双臂同构；但 Piper 的关节范围、速度、夹爪行程不同，必须重新归一化和微调。`[本地]` `[推断]`
- **没有第三人称、没有深度、没有力/力矩**。这会让“视频世界模型”失去很多关键观测：夹爪-物体接触、遮挡、深度。`[本地]`

**小数据判断**：

| 方法 | 数据需求（官方/论文） | 对本地 41 条 + 50–100 条干净数据的判断 |
|---|---|---|
| ACT / Diffusion Policy | 50–100 条固定场景示教 | ✅ 现实基线 |
| DreamerV3 / TD-MPC2 | 在线交互 + 奖励；DayDreamer 真机数小时 | ❌ 不适合用现有示教直接做 |
| V-JEPA 2-AC | <62 h Droid 无标注视频后训练 | ⚠️ 可借预训练，但域差异大；本地数据远少于 62 h |
| DINO-WM | 离线轨迹，无需奖励/IDM | ✅ 小数据可试；但任务简单，未验证抓取 |
| SuSIE | 220k Something-Something + Bridge 预训练；本地 100–200 条微调 | ⚠️ 高层可行；低层仍需 50–100 条 |
| Cosmos Policy | 185 条 ALOHA 演示；8×H100 48 h | ⚠️ 数据量接近，但算力是门槛 |
| Genie Envisioner | AgiBotWorld 预训练 + 本地 LeRobot 微调 | ⚠️ 需 2B 训练资源；3 相机不匹配 |
| DreamGen / Cosmos 14B | 预训练 200M clips；本地微调 | ❌ 8 GB 不可行 |

### 5.2 算力

- **8 GB（机器人板载 RTX 4060）**：
  - ✅ 可训练/推理：ACT、DP、DINO-WM、DreamerV3/TD-MPC2 小配置、小 V-JEPA 探针。
  - ⚠️ 可推理但需实测：Cosmos Policy 2B 直接推理（官方 6.0 GB）、SuSIE 高层（1B 级）、V-JEPA 2-AC（1B）、Cosmos-Predict2.5 2B（需 offload）。
  - ❌ 不可：Cosmos Policy best-of-N 规划（官方 10.0 GB）、Cosmos 3 16B/64B、Cosmos-Predict2.5 14B、DreamGen 14B、Genie Envisioner 训练。
- **24–48 GB 工作站**：会显著改变可行性——Cosmos Policy 规划、SuSIE 微调、Genie Envisioner 推理、V-JEPA 2-AC 批量推理都更舒服；但仍不够 8×H100 的全量后训练。
- **8×H100 租用**：Cosmos Policy 微调（<200 条演示，48 h）或 V-JEPA 2-AC 后训练的入门门槛；这是一笔真实的预算项，不是“本地跑一下”。

### 5.3 相机/动作空间错配

| 方法 | 期望输入/动作 | Piper 实际 | 差距/处理 |
|---|---|---|---|
| Cosmos Policy / ALOHA | 1 第三人称 + 2 腕部；14 维关节；action chunk 16 | 2 腕部；14 维关节；MOVE_J 50 Hz | **加一个固定第三人称 USB 相机**；动作可对齐；需微调 |
| V-JEPA 2-AC / Droid | 单目外部 RGB；7-DoF EEF delta + gripper；单臂 | 2 腕部；14 维关节；双臂 | 用 SDK end pose + IK 转 EEF；单臂先做；双臂需扩展 |
| SuSIE | 第三人称图像 + 语言；低层 GCBC | 2 腕部；14 维 | 加第三人称；低层换 ACT/GCBC |
| DINO-WM | 单视图；动作序列 | 2 腕部；14 维 | 可选一个腕部视角；动作用关节/EEF；需重训 |
| DreamerV3 / TD-MPC2 | 低维动作 + 奖励 | 14 维关节；无奖励 | 仿真里定义奖励；真机需奖励工程 |
| Genie Envisioner | 3 相机（top_head + 2 hand）；LeRobot | 2 腕部 | 相机布局不匹配；需改数据配置/加相机 |

### 5.4 任务适配

| 任务 | 推荐主方案 | 世界模型的合理角色 |
|---|---|---|
| 固定物体定点抓放 | 视觉伺服 + MoveIt + ACT/DP | 不需要；最多做离线评估 |
| 新物体/新背景抓取 | SuSIE 子目标 + ACT/GCBC | 高层子目标生成；抓取姿态建议 |
| 双臂协同搬运/交接 | ACT/DP（150–300 条）或 Cosmos Policy 微调 | Cosmos Policy 的 future frame/value 可用于候选筛选 |
| 接触丰富（插拔/按压/开抽屉） | MIT 阻抗 + 力/电流阈值 + 经典控制 | DreamerV3/TD-MPC2 在 MuJoCo 学技能，再蒸馏 |
| 移动抓取 | Nav2 + 固定臂任务 | 暂不做：`/odom` 坏、无激光雷达 |
| 语言指令多任务 | VLA/BC + 子目标 | SuSIE/V-JEPA 提供视觉目标；Genie 不适合 |

---

## 6. 与经典视觉伺服 / MoveIt / 行为克隆的分工

### 6.1 各自最强的地方

- **视觉伺服**：度量精度高、延迟低、可解释；适合“已知物体/已知平面”的最终对准、抓取前微调、插入。缺点是依赖标定、遮挡/光照敏感、难以处理语义目标。
- **MoveIt**：关节空间/笛卡尔空间运动规划、碰撞检查、IK；适合自由空间接近、避障、受限工作空间。缺点是接触/不确定性处理弱。
- **行为克隆（ACT/DP）**：从 50–100 条示教学习固定场景的完整技能；反应式、实现简单、推理快。缺点是没有显式未来预测、分布外泛化差、难以自我评估。
- **世界模型**：能预测“动作之后会怎样”，可用于目标图像规划、候选动作评分、合成数据、离线评估。缺点是预测误差累积、物理/接触不可靠、训练/推理成本高。

### 6.2 推荐的分层架构

```
语言/目标图像
   │
   ▼
高层：世界模型/子目标生成（SuSIE、V-JEPA 2-AC、Cosmos Policy value）
   │  输出：子目标图像 / 抓取姿态 / 候选动作块
   ▼
中层：MoveIt/IK + 视觉伺服（碰撞检查、关节限位、笛卡尔直线）
   │
   ▼
低层：MOVE_J 50 Hz 关节流 + 夹爪电流阈值 + 急停/看门狗
```

**关键原则**：生成式世界模型**永远不直接输出未经校验的关节命令**。它的输出必须经过：关节限位、速度限制、工作空间、自碰/环境碰撞、夹爪行程、急停状态检查。Piper 腕部 ±70°、0.17 m 以上不能竖直下抓，这些硬约束必须由经典层兜底。

### 6.3 什么时候值得上世界模型

- 你需要**多步预测/候选评分**，而 BC 只会反应式输出；
- 你需要**从语言/目标图像生成子目标**，而不是固定轨迹；
- 你有**合成数据/离线评估**需求，且能接受视频模型的物理伪影；
- 你在**仿真里学接触技能**，再蒸馏到真机；
- 你有**24 GB+ 显卡或 8×H100 租用预算**。

如果只是固定桌面、固定物体、固定光照的抓放，**ACT/DP + 视觉伺服 + MoveIt 更便宜、更可靠**。

---

## 7. 优先级与实验路线

### P0（0–2 周）：先把基线做扎实，不碰世界模型

- 重新采集 **100 条干净 episode**（固定场景、3 个物体位置、2 种背景；避免早期退化数据）。
- 加一个**固定第三人称 USB 相机**（Cosmos Policy/SuSIE 都需要）；如果暂时没有，先用一个腕部视角做单臂基线。
- 安装/验证 Orbbec 深度 ROS 驱动；没有深度就先用 AprilTag/已知平面做视觉伺服。
- 训练 **ACT 14 维**（LeRobot），记录成功率、完成时间、干预次数。
- 成功标准：固定物体抓放 ≥50–70%（20 次试验），无关节限位/碰撞。
- 这是所有世界模型方案的**对照基线**；没有它，无法判断世界模型是否真的有用。

### P1（2–4 周）：DINO-WM 低成本原型（1×4060，可离线）

- 用现有/新采的 episode 构造 `(o_t, a_t, o_{t+1})`，动作先用 14 维关节 delta 或 7 维 EEF delta。
- 冻结 DINOv2（ViT-S/B），训练 patch 特征预测器；8 GB 足够。
- 在 MuJoCo `agilex_piper` 里做 goal-image CEM 规划；先在仿真验证，再考虑真机低速执行。
- 评估：open-loop action error、goal-image 距离、仿真成功率、规划时延。
- 成功标准：在至少一个桌面任务上，规划成功率 > 随机/BC 基线；否则及时止损。
- 风险：DINO-WM 未验证抓取/夹爪接触；可能只学到背景/手臂运动。

### P2（4–8 周）：Cosmos Policy 2B（需要租 8×H100；部署在 4060）

- 数据：150–200 条双臂 episode，14 维关节，3 路相机（加第三人称）；按 ALOHA/LeRobot 格式预处理。
- 训练：租 8×80 GB H100，按官方 ALOHA recipe 微调 48 h；成本约 384 GPU-hours。
- 部署：4060 上 **只跑直接推理（官方 6.0 GB）**；best-of-N 规划需要 10 GB，4060 不够；可在 24 GB 工作站上做规划。
- 评估：与 ACT 基线同样的 20 次试验；看成功率、完成时间、失败模式。
- 成功标准：在至少一个任务上超过 ACT；否则保留为离线评估器。
- 风险：相机/动作域差异、2B 推理时延、ALOHA checkpoint 不能零样本迁移、微调成本。

### P2b（4–8 周）：SuSIE 子目标 + ACT（24 GB 工作站更稳）

- 用 100–200 条 episode 构造“起始图 → 目标图 + 语言”对；LoRA 微调 InstructPix2Pix（8 GB 可能很紧，24 GB 更稳）。
- 高层生成子目标图像；低层用 ACT/GCBC 执行。
- 评估：新物体/新背景下的子目标合理性（人工/VLM 打分）和抓取成功率。
- 成功标准：在未见物体上比纯 ACT 提升；否则说明子目标生成没有带来增益。
- 风险：子目标图像可能物理不可达；需要经典 IK/碰撞过滤。

### P3（4–8 周，仅仿真）：DreamerV3 / TD-MPC2 学接触技能

- 在 MuJoCo `agilex_piper` 里搭一个接触任务（按压/开抽屉/插入），定义稠密/稀疏奖励。
- 训练 DreamerV3 或 TD-MPC2（8 GB 单任务；TD-MPC2 官方 8 GB 推荐）。
- 评估 sim 成功率、样本效率；成功后蒸馏成 ACT/DP，再上真机低速验证。
- 成功标准：仿真中明显优于 BC；否则说明世界模型 RL 在该任务上没有优势。
- 风险：sim2real 差距、奖励工程、真机在线 RL 的安全风险。

### P4（观察，不投入）：Genie 4 API、Cosmos 3 Edge、V-JEPA 2.1、DreamZero

- Genie 4 目前是受限预览；如果未来开放 Gemini API，可用于**合成视频/环境**，但仍不是关节动作接口。
- Cosmos 3 Edge 4B 是边缘档，但官方目标是 Jetson AGX Orin/Thor/RTX Pro 6000；等有 24 GB+ 卡再评估。
- V-JEPA 2.1 的抓取提升值得跟踪；等官方 8 GB 推理数据/社区复现。
- DreamZero 14B 等 2026 前沿 world-action model，8 GB 不可行；保持关注。

### 明确不建议

- ❌ 用 Genie 1–4 做机器人规划：闭源、无动作接口、无度量保证。
- ❌ 用 UniPi/RoboDreamer 做工程基线：UniPi 无官方代码；RoboDreamer 只有仿真、无权重。
- ❌ 在 4060 上训练 Cosmos 14B/16B/64B、DreamGen、Genie Envisioner：显存差一个数量级。
- ❌ 让世界模型直接输出关节命令：必须经经典安全层。
- ❌ 用现有 41 条退化 episode 做世界模型训练：先补数据。

---

## 8. 风险与安全

1. **生成式计划的物理不可行**：视频模型可能生成“穿模/漂浮/夹爪穿透物体”的未来；必须用 IK、碰撞检查、关节限位过滤。
2. **无六维力/力矩**：接触丰富任务（插拔、开抽屉、按压）不能只靠视觉世界模型；需要 MIT 阻抗模式、电流阈值或外部力传感器。
3. **腕部 ±70° 限制**：世界模型可能建议从上方竖直抓取，但 0.17 m 以上做不到；规划器必须把该约束编码进去。
4. **10 Hz 数据**：接触瞬间丢帧；训练前重采样/插值，或把录制频率提高到 30 Hz。
5. **相机标定/深度缺失**：视觉伺服和目标图像规划都依赖标定；没有深度时，抓取高度只能靠已知平面/标定。
6. **分布外泛化**：视频世界模型对光照、背景、物体材质敏感；合成数据不能替代真实多样性。
7. **许可证**：Genie Envisioner 大部分 CC BY-NC-SA 4.0（非商用）；Cosmos 用 OpenMDW/NVIDIA Open Model License；商用前逐项确认。
8. **成本**：Cosmos Policy 微调 8×H100 48 h；V-JEPA 2-AC 后训练 32 卡；这不是“本地跑一下”的量级。
9. **安全边界**：任何真机试验都要限速、清场、手放急停；先仿真、再低速、再逐步放开。

---

## 9. 未验证 / 不确定

- **V-JEPA 2-AC 在 8 GB 上的推理**：官方没有给 VRAM 数字；1B 编码器 + predictor 理论可行，但 CEM 规划会放大显存/时延，必须实测。`[未验证]`
- **Cosmos Policy ALOHA checkpoint 对 Piper 的零样本迁移**：没有证据；相机布局、关节范围、夹爪不同，默认认为不可零样本。`[推断]`
- **DINO-WM 在抓取/夹爪接触上的效果**：原论文只有 PushT/迷宫/可变形物体；抓取未验证。`[未验证]`
- **RoboDreamer 代码的可复现性**：有官方训练代码，但 README 未给权重；未实际跑通。`[未验证]`
- **Genie 4 的 API/权重开放时间**：官方只说“coming soon”到 Gemini API/AI Studio；截至 2026-11-24 仍是受限预览。`[官方]`
- **Cosmos 3 Edge 4B 在 8 GB 消费卡上的可行性**：官方目标是 Jetson AGX Orin/Thor/RTX Pro 6000，没有 8 GB 认证。`[推断]`
- **SuSIE 在 8 GB 上微调 InstructPix2Pix**：推理大概率可以，微调需要 LoRA/低分辨率；未实测。`[推断]`
- **DreamerV3 在 8 GB 上的具体配置**：官方没有 VRAM 表；小模型可试，需实测。`[推断]`

---

## 10. 来源与复现

### 10.1 复现命令（本次实际使用）

```bash
# 1) arXiv 元数据（示例；其余论文同理）
curl -sL https://arxiv.org/abs/2402.15391
curl -sL https://arxiv.org/abs/2301.04104
curl -sL https://arxiv.org/abs/2509.24527
curl -sL https://arxiv.org/abs/2310.16828
curl -sL https://arxiv.org/abs/2302.00111
curl -sL https://arxiv.org/abs/2310.10639
curl -sL https://arxiv.org/abs/2404.12377
curl -sL https://arxiv.org/abs/2506.09985
curl -sL https://arxiv.org/abs/2601.16163

# 2) 官方项目页/博客
curl -sL https://deepmind.google/discover/blog/genie-3-a-new-frontier-for-world-models/
curl -sL https://deepmind.google/blog/genie-4-a-new-generation-of-world-simulation/
curl -sL https://danijar.com/project/dreamer4/
curl -sL https://universal-policy.github.io/unipi/
curl -sL https://rail-berkeley.github.io/susie/
curl -sL https://robovideo.github.io/
curl -sL https://video-prediction-policy.github.io/

# 3) 官方仓库/README/配置
curl -sL https://github.com/danijar/dreamerv3
curl -sL https://github.com/nicklashansen/tdmpc2
curl -sL https://github.com/facebookresearch/vjepa2
curl -sL https://github.com/NVlabs/cosmos-policy
curl -sL https://github.com/NVIDIA/Cosmos
curl -sL https://github.com/nvidia-cosmos/cosmos-predict2.5
curl -sL https://github.com/AgibotTech/Genie-Envisioner-V1
curl -sL https://github.com/NVIDIA/GR00T-Dreams
curl -sL https://github.com/gaoyuezhou/dino_wm
curl -sL https://github.com/UMass-Embodied-AGI/robodreamer
curl -sL https://github.com/kvablack/susie

# 4) Cosmos Policy 的显存/训练门槛（官方 README）
curl -sL https://raw.githubusercontent.com/NVlabs/cosmos-policy/main/README.md | grep -iE 'VRAM|H100|ALOHA|GB'
curl -sL https://raw.githubusercontent.com/NVlabs/cosmos-policy/main/ALOHA.md | grep -iE 'third-person|wrist|185|8 80GB'

# 5) 来源台账（本地生成）
cd C:/Users/tzh03/Downloads/_taskg
python build_source_ledger.py
```

### 10.2 本次生成的文件

| 文件 | 内容 |
|---|---|
| `03-文档与报告/world-models-planning-2026-11-24.md` | 本报告 |
| `C:/Users/tzh03/Downloads/_taskg/source_ledger.csv` | 43 条来源台账：key/title/date/type/url/claim/confidence |
| `C:/Users/tzh03/Downloads/_taskg/build_source_ledger.py` | 从 arXiv CSV + 手工来源生成台账的脚本 |
| `C:/Users/tzh03/Downloads/_taskg/arxiv_*.csv` | 11 个 arXiv 检索结果（Genie/TD-MPC2/UniPi/SuSIE/RoboDreamer/Cosmos/V-JEPA2/Dreamer4/DreamerV3/DreamGen/Genie Envisioner） |

### 10.3 关键来源（按日期）

| 日期 | 来源 | URL | 用途 |
|---|---|---|---|
| 2022 | DayDreamer 项目页 | https://danijar.com/project/daydreamer/ | Dreamer 真机双臂抓放证据（背景） |
| 2023-01-10 | DreamerV3 arXiv | https://arxiv.org/abs/2301.04104 | 方法/任务 |
| 2023-01-31 | UniPi arXiv | https://arxiv.org/abs/2302.00111 | 视频计划 + IDM |
| 2023-01-31 | UniPi 项目页 | https://universal-policy.github.io/unipi/ | 官方代码未释出 |
| 2023-10-16 | SuSIE arXiv | https://arxiv.org/abs/2310.10639 | 子目标 + 低层策略 |
| 2023-10-16 | SuSIE 项目页 | https://rail-berkeley.github.io/susie/ | 真机/CALVIN 结果 |
| 2023-10-25 | TD-MPC2 arXiv | https://arxiv.org/abs/2310.16828 | latent MPC |
| 2024-02-23 | Genie arXiv | https://arxiv.org/abs/2402.15391 | 11B 世界模型 |
| 2024-04-18 | RoboDreamer arXiv | https://arxiv.org/abs/2404.12377 | compositional video world model |
| 2024-11-07 | DINO-WM arXiv | https://arxiv.org/abs/2411.04983 | DINO 特征规划 |
| 2024-12-04 | Genie 2 博客 | https://deepmind.google/discover/blog/genie-2-a-large-scale-foundation-world-model/ | 3D 交互世界 |
| 2024-12-19 | VPP arXiv | https://arxiv.org/abs/2412.14803 | 预测表征策略 |
| 2025-01-07 | Cosmos arXiv | https://arxiv.org/abs/2501.03575 | 世界基础模型平台 |
| 2025-04-02 | DreamerV3 Nature | https://www.nature.com/articles/s41586-025-08744-2 | 正式发表 |
| 2025-05-19 | DreamGen arXiv | https://arxiv.org/abs/2505.12705 | 合成数据 + IDM |
| 2025-06-11 | V-JEPA 2 arXiv | https://arxiv.org/abs/2506.09985 | 1B 编码器 + AC 规划 |
| 2025-08-05 | Genie 3 博客 | https://deepmind.google/discover/blog/genie-3-a-new-frontier-for-world-models/ | 720p/24fps 预览 |
| 2025-08-07 | Genie Envisioner arXiv | https://arxiv.org/abs/2508.05635 | 机器人视频世界模型平台 |
| 2025-09-29 | Dreamer 4 arXiv | https://arxiv.org/abs/2509.24527 | 离线想象训练 |
| 2025-10-28 | Cosmos-Predict2.5 arXiv | https://arxiv.org/abs/2511.00062 | 2B/14B |
| 2026-01-22 | Cosmos Policy arXiv | https://arxiv.org/abs/2601.16163 | 2B 双臂策略 + 规划 |
| 2026-03-15 | V-JEPA 2.1 arXiv | https://arxiv.org/abs/2603.14482 | +20 点抓取提升 |
| 2026-06-01 | Cosmos 3 arXiv | https://arxiv.org/abs/2606.02800 | 4B/16B/64B |
| 2026-10-27 | Genie 4 博客 | https://deepmind.google/blog/genie-4-a-new-generation-of-world-simulation/ | 原生音频/实时改写 |

### 10.4 说明

- GitHub REST API 在本次调研后段触发速率限制；后段仓库信息改用 HTML 页面/raw 文件核实。`source_ledger.csv` 中已标注每条证据的置信度。
- 没有下载任何模型权重；没有在机器人上执行任何运动/服务变更。所有“8 GB 可行性”除官方明确数字外，均标为 `[推断]` 或 `[未验证]`。
- 交付包中的本地事实（RTX 4060 8 GB、10 Hz 录制、50 Hz MOVE_J、2 个腕部相机、无深度驱动、41 条退化 episode、腕部 ±70°）均来自 `03-文档与报告/` 的实测文档；不是网上推测。
