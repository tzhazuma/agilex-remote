# SmolVLA 14 维权重核实结果

调研 2026-10-22。结论：**权重是真的、能加载、维度完全对上，但归一化统计量在 0.4.4 里装不进去。**

---

## 一、这个 checkpoint 是真的

`LBST/t16_pick_and_place_agilex_smolvla_18k`

| 项 | 值 |
|---|---|
| 策略类型 | **smolvla** |
| VLM 主干 | `HuggingFaceTB/SmolVLM2-500M-Video-Instruct`，裁到 **16 层** |
| 参数量 | **450,046,176（450 M）** |
| 文件大小 | **0.91 GB** |
| 显存占用 | **0.93 GB**（bf16） |
| 输入 | `observation.images.middle/right/left`（各 3×256×256）+ `observation.state` **[14]** |
| 输出 | `action` **[14]** |
| chunk | 50 步 / n_action_steps 50 |
| 归一化 | VISUAL=IDENTITY，STATE/ACTION=MEAN_STD |

**训练来源数据集**：`gauravpradeep/bottle_task_aug_29_lerobot_diffusion`
- 61 回合 / 19,872 帧 / **50 fps**
- 3 相机（middle/right/left）256×256
- **state [14] / action [14]** —— 真的是双臂 14 维
- Apache-2.0，73 次下载

所以维度上和我们的机器人**完全对得上**：14 维状态 → 14 维动作。

## 二、能加载（踩了三个依赖坑）

```bash
uv pip install "transformers>=4.57.1,<5" peft num2words
```

1. `transformers` 根本没装 → `ModuleNotFoundError`
2. `peft` 需要
3. `num2words` 是 SmolVLM processor 的隐藏依赖 → `ImportError: Package num2words is required`

装完就能加载：

```
LOADED: 450046176 params (450 M)
on: cuda:0 torch.bfloat16
vram: 0.93 GB
```

**0.93 GB 意味着它完全能塞进现在任何一张 5090，甚至能在 Mac 上跑。**

## 三、⚠️ 但归一化统计量装不进去 —— 这是真问题

加载时有这个警告：

```
WARNING: Unexpected key(s) when loading model:
  normalize_inputs.buffer_observation_state.mean/std
  normalize_targets.buffer_action.mean/std
  unnormalize_outputs.buffer_action.mean/std
```

我查了 safetensors，**这 6 个张量确实在 checkpoint 里**（共 506 个 tensor）。但加载后的 `SmolVLAPolicy` 对象上：

```
hasattr(pol, "normalize_inputs")  →  False
```

**根因**：这个 checkpoint 是用**旧版 LeRobot（0.3.x）** 训的，那时候归一化是策略对象内部的 buffer。LeRobot **0.4.4 把归一化挪到了独立的 preprocessor/postprocessor 管线**，所以模型对象上不再有这些属性，而 checkpoint 里只有 `config.json` / `model.safetensors` / `train_config.json` —— **没有 `policy_preprocessor.json`**。

后果：**直接拿它推理，输入没被归一化、输出没被反归一化，出来的是垃圾。**

手动注入也走不通（`injected 0`，因为目标属性不存在）。

## 四、三条可行路线

| 路线 | 工作量 | 说明 |
|---|---|---|
| **A. 自己写归一化包装** | 中 | 从 safetensors 读出 6 个统计量，手动 `(x-mean)/std` 和反变换。需要复现旧版 LeRobot 的归一化语义 |
| **B. 用旧版 LeRobot 加载** | 中 | 装 0.3.x 到独立 venv，它能原生加载这些 buffer。但要和 0.4.4 隔离 |
| **C. 直接用 SmolVLA 在我们的数据上微调** | **推荐** | 不加载旧 checkpoint，而是 `lerobot-train --policy.type=smolvla` 从 SmolVLM2-500M 起训。450M 参数、0.93 GB，8 GB 显卡都够 |

**C 是最好的**：它绕开了归一化兼容问题，而且 SmolVLA 只有 450M，比 ACT 的 51M 强得多、比 π0 的 2.95B 轻得多。真正的预训练价值在 `SmolVLM2-500M` 这个 VLM 主干上，而不是那个 bottle task 的微调权重。

## 五、还有三处不匹配（用 C 路线的话都不是问题）

| 项 | 它 | 我们 |
|---|---|---|
| 相机 | 3 个：middle/right/left | 2 个：cam_left/cam_right |
| 帧率 | 50 fps | 10 fps |
| 任务 | bottle pick_and_place | 我们的抓放 |

如果走 A/B 路线，需要：把 `cam_left→left`、`cam_right→right`、`middle` 补一张全黑或复制图。

## 六、结论

- **这个权重证明了 14 维双臂策略是可行的**，而且只要 450M / 0.93 GB
- **但它不能开箱即用**：旧版 LeRobot 的归一化布局和 0.4.4 不兼容
- **推荐路线 C**：用 `--policy.type=smolvla` 在我们的数据上从 SmolVLM2-500M 微调，绕开兼容问题，且资源需求极低

前提仍然是：**先有像样的数据**。现在的 30 个回合是固定轨迹，学不到东西。

---

## 附：验证用的命令

```bash
# 依赖
uv pip install "transformers>=4.57.1,<5" peft num2words

# 加载（必须在 from_pretrained 前 import 策略模块，否则 draccus 找不到 smolvla 这个 choice）
import lerobot.policies.smolvla  # noqa: F401
from lerobot.policies.smolvla.modeling_smolvla import SmolVLAPolicy
pol = SmolVLAPolicy.from_pretrained("LBST/t16_pick_and_place_agilex_smolvla_18k")
```

坑：`PreTrainedConfig.from_pretrained` 会报 `Couldn't find a choice class for 'smolvla'` —— 因为策略类是懒注册的，必须先 import 模块。
