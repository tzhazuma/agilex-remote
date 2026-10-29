# 机械臂"乱动"根因与安全启动验证

2026-10-29 实测。这台机器人 `<robot-ip>`，ROS 2 Humble，两个 Piper 6 轴臂（can_left / can_right）。

---

## 一、"乱动"的根因（已确认并修复）

### 现象

上一轮服务在跑的时候，机械臂自己动。当时记录到：

```
启动时    joints = [1.56, 0.0, -0.18, -5.58, 9.6, 18.86]
之后      target = [1.6,  0.0, -0.2,  -5.6,  9.6, 18.9]   ← 一直是启动姿态
         实测   = [19.0, -0.2,  0.2,  10.3, 22.4, 32.2]   ← 偏了约 20°
```

### 机制

Piper 臂**失能（disabled）时会失去力矩**，重力把它拉下去。但软件里持有的目标值**不会跟着变**。

于是：
1. 臂失能 → 重力把臂拉下去 20°
2. 目标值还停在原来的位置
3. 一旦重新使能 → 臂**猛地弹回**那个旧目标 → 看起来就是"乱动"

### 修复

`ArmBridge.set_enabled()` 里加了一步：**使能前先把目标重新对齐到当前实测位置**。

> 使能应该意味着"保持在你现在的位置"，绝不能是"跳回我们上次想要的位置"。

```python
if on:
    with self.lock:
        for j in range(6):
            self.joints[j] = clamp(self._measured[j],
                                   self.limits[j][0], self.limits[j][1])
    log(f"arm {self.side} target resynced to measured before enable: ...")
```

已部署到 `/usr/local/share/agilex-remote/agilex-remote-server.py`。

---

## 二、安全启动验证（实测通过）

### 启动前基线

```
can_left  2A2  00 00 C3 C4 00 00 59 0D
can_left  2A3  00 02 DE 0B 00 01 97 42
can_right 2A2  00 00 C9 F2 00 00 04 49
```

### 启动后立刻检查

```
estop: False
left   enabled=True
       measured: [18.8, -0.2, 0.0, 12.8, 15.6, 32.7]
       target  : [18.8, -0.2, 0.2, 12.8, 15.6, 32.7]
       lag     : [0.0, 0.0, 0.24, 0.0, -0.01, 0.0]  max=0.24
       faults  : none
right  enabled=True
       measured: [-25.7, 0.0, 0.0, 29.0, 32.0, -10.4]
       target  : [-25.7, 0.0, 0.3, 29.1, 31.9, -10.4]
       lag     : [0.0, 0.0, 0.34, 0.04, -0.09, 0.0]  max=0.34
       faults  : none
```

**目标 == 实测（最大偏差 0.34°），没有跳变。**

### 20 秒稳定性采样

每秒读一次，20 次结果**完全一致**（保留两位小数）：

```
  时间  left_j0  left_j4  left_j5   right_j0  right_j4  right_j5   maxlag
     1s    18.79    15.63    32.66     -25.66    31.99   -10.44     0.34
     ...
    20s    18.79    15.63    32.66     -25.66    31.99   -10.44     0.34
```

**无漂移、无抖动、lag 恒定。**

---

## 三、慢速运动验证（实测通过）

左臂 J1 每次 +0.5°，共 4 次：

```
  start       measured 18.79  target 18.79  lag  0.00
  +0.5 #1     measured 19.26  target 19.29  lag  0.03
  +0.5 #2     measured 19.71  target 19.79  lag  0.08
  +0.5 #3     measured 20.21  target 20.29  lag  0.09
  +0.5 #4     measured 20.80  target 20.79  lag -0.01

  总位移 2.01°（预期 2.0°）
```

到位后观察 5 秒，**完全静止无振荡**。回退到起始位置：18.84 vs 18.79，误差 0.05°。

**结论：修复后运动是平滑、可控、可重复的。**

---

## 四、这个机械臂有哪些控制方式（SDK 实证）

`piper_sdk.C_PiperInterface_V3` 暴露的全部控制入口。

### 4.1 模式枚举（`MotionCtrl_2`，CAN 0x151）

| `ctrl_mode` | 含义 |
|---|---|
| 0x00 | Standby 待机 |
| **0x01** | **CAN 指令控制（当前使用）** |
| 0x03 | 以太网控制 |
| 0x04 | Wi-Fi 控制 |
| 0x07 | 离线轨迹模式 |

| `move_mode` | 含义 |
|---|---|
| 0x00 | MOVE P（点到点） |
| **0x01** | **MOVE J（关节空间，当前使用）** |
| 0x02 | MOVE L（直线） |
| 0x03 | MOVE C（圆弧） |
| 0x06 | MOVE M（V1.8-8 起支持） |

| `is_mit_mode` | 含义 |
|---|---|
| 0x00 | 位置-速度模式 |
| **0xAD** | **MIT 阻抗模式** |
| 0xFF | 无效 |

### 4.2 六种控制方式

| 方式 | 接口 | 说明 |
|---|---|---|
| **关节位置** | `JointCtrl(j1..j6)` | 当前用的，单位 0.001° |
| **MIT 阻抗** | `JointMitCtrl(motor_num, pos_ref, vel_ref, kp, kd, t_ref)` | **位置+速度+刚度+阻尼+力矩**，CAN 0x15A–0x15F。需要先把 `is_mit_mode=0xAD` |
| **笛卡尔 MoveC** | `MoveCAxisUpdateCtrl(instruction_num)` | 圆弧坐标点更新，需先切到 MoveC 模式 |
| **轨迹流** | `MotionCtrl_1(track_ctrl=...)` | 0x01 暂停 / 0x02 继续 / 0x03 清除当前 / 0x04 清除全部 / 0x05 获取 / 0x06 终止 / 0x07 传输 / 0x08 传输结束 |
| **拖动示教** | `MotionCtrl_1(grag_teach_ctrl=...)` | 0x01 开始记录 / 0x02 结束记录 / 0x03 执行 / 0x04 暂停 / 0x05 继续 |
| **离线轨迹** | `ctrl_mode=0x07` | 预下载轨迹后执行 |

**MIT 模式是最值得试的替代方案** —— 它给了 kp/kd，可以在"跟不上"的时候表现出柔性而不是硬顶，比纯位置控制更适合抓取和接触任务。

---

## 五、主从臂（leader-follower）怎么操作

### 5.1 官方接口

```python
MasterSlaveConfig(linkage_config, feedback_offset, ctrl_offset, linkage_offset)
```

**CAN ID = 0x470**

| 参数 | 值 | 含义 |
|---|---|---|
| `linkage_config` | 0x00 | 无效 |
| | **0xFA** | **设置为示教输入臂（主臂 / leader）** |
| | **0xFC** | **设置为运动输出臂（从臂 / follower）** |
| `feedback_offset` | 0x00 / 0x10 / 0x20 | 反馈 ID 基址 2Ax → 2Bx / 2Cx |
| `ctrl_offset` | 0x00 / 0x10 / 0x20 | 控制 ID 基址 15x → 16x / 17x |
| `linkage_offset` | 0x00 / 0x10 / 0x20 | 联动目标地址偏移 |

### 5.2 关键认识：偏移参数让两条臂挂同一根总线

`feedback_offset` / `ctrl_offset` 把 CAN ID 基址整体挪一段。这意味着**主臂和从臂可以共用一条 CAN 总线**，靠 ID 偏移区分。

这正好解释了为什么"主从臂"和"我们的遥控"会打架 —— 如果臂被设成从臂（0xFC），它会跟随总线上任何匹配 ID 的指令，包括我们发的。

### 5.3 当前状态：**没有开启主从**

实测 `GetArmStatus()`：

```
Control Mode: CAN_CTRL(0x1)      ← 不是主从联动模式
Teach Status: DISABLED(0x0)      ← 拖动示教没开
Arm Status:   NORMAL(0x0)
Mode Feed:    MOVE_J(0x1)
Motion Status: REACH_TARGET_POS_SUCCESSFULLY(0x0)
Error Code: 0
```

总线上也**没有 0x47x 帧**（主从配置的应答），说明当前**不是主从模式**。

**所以这一轮的"乱动"不是主从臂造成的，是上面第一节那个目标值不跟随的问题。**

### 5.4 还有

- `ReqMasterArmMoveToHome(mode)` —— 请求主臂回原点，mode 0/1/2
- 主从是**存在臂里的持久配置**，掉电不丢，所以排查时必须主动查 `GetArmStatus()`

---

## 六、这个机械臂支不支持遥控器 / 示教器

### 支持，但有三条不同的路径

| 方式 | 接口 | 说明 |
|---|---|---|
| **拖动示教** | `MotionCtrl_1(grag_teach_ctrl)` | 直接用手拖着臂走，臂记录轨迹再复现。**这是最接近"遥控器"的交互** |
| **示教器参数** | `GripperTeachingPendantParamConfig(teaching_range_per, max_range_config, teaching_friction)` | 夹爪示教器的范围/摩擦参数 |
| | `GetGripperTeachingPendantParamFeedback()` | 读回示教器参数 |
| **主从遥操作** | `MasterSlaveConfig` | 用一条主臂遥操作另一条从臂 |

**当前 `Teach Status: DISABLED`，即拖动示教没开。**

注意：这里说的"遥控器"是**机械臂的示教器/主臂**，不是底盘那个 RC 手柄。底盘（AgileX Tracer）有独立的 RC 手柄，走 `/tracer_rc_status`，和臂是两套东西。

---

## 七、结论

1. **"乱动"根因已定位并修复** —— 不是主从臂干扰，是失能后目标值不跟随实测
2. **安全启动已验证** —— 目标=实测（0.34°内），20 秒零漂移
3. **慢速运动已验证** —— 4×0.5° 得到 2.01°，lag < 0.1°，无振荡
4. **控制方式有六种**，MIT 阻抗模式最值得试（有 kp/kd，接触任务更柔顺）
5. **主从臂有官方接口**（0x470），当前未启用
6. **支持拖动示教和示教器**，当前未启用

---

## 附：本次用到的命令

```bash
# 查状态（只读，不发控制帧）
python3 -c "
from piper_sdk import C_PiperInterface_V3
import time
p = C_PiperInterface_V3('can_left')
p.ConnectPort(can_init=False, piper_init=False, start_thread=True)
time.sleep(1.2)
print(p.GetArmStatus())
"

# 看 CAN 上有没有命令帧（0x151/0x155/0x156）
candump -T 2000 can_left | grep -E " 15[156] "

# 看主从配置帧
candump -T 3000 can_left,can_right | grep -iE " 47[0-9A-F] "
```
