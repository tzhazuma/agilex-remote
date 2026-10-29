# 安装与连接指导

给拿到这个包的同学。照着做，大约 10 分钟能让网页遥控器跑起来。

> 这份包在一台具体机器上调通：**AgileX Tracer 底盘 + 两条 Piper 6 轴臂 + 两个腕部相机 + Ubuntu 22.04 + ROS 2 Humble**。
> 换硬件的话，第 3 节的依赖和 `server/agilex-remote-server.py` 顶部的常量要改。

---

## 0. 包里有什么

```
install.sh                 一条命令安装（笔记本侧 / 机器人侧自适应）
server/                    跑在机器人上的服务 + 单文件网页 UI
scripts/                   发现 IP、建隧道、VNC、自检、验证脚本
tools/                     离线测试、LeRobot 导出、SmolVLA 推理、仿真
docs/                      各轮报告 + 三份 PPT
android/                   WebView 壳（可自行构建 APK）
```

先看 `README.md`（英文，偏架构和原理），本文只讲**怎么装、怎么连**。

---

## 1. 前提条件

### 机器人侧（缺一不可）

| 依赖 | 检查命令 | 说明 |
|---|---|---|
| Ubuntu 22.04 + ROS 2 Humble | `ls /opt/ros/humble` | 服务要 source 它 |
| Python 3.10 | `python3 -V` | 系统自带的就行 |
| ffmpeg | `ffmpeg -version` | 相机取流靠它 |
| piper_sdk（V3 接口） | `python3 -c "from piper_sdk import C_PiperInterface_V3"` | **别用 `pip install -U piper_sdk`**：PyPI 上的 0.6.2 更旧，没有 V3 接口 |
| 三条 CAN | `ip -brief link show type can` | `can0` 底盘 500k；`can_left`/`can_right` 各 1M |

CAN 没起来的话，Piper 的仓库里一般有 `can_config.sh`：

```bash
cd ~/piper_ros && bash ./can_config.sh     # 或者按你的接线手动 ip link set
ip -details link show can_left | grep -E "state|bitrate"   # 应为 UP / 1000000
```

### 电脑侧

- 能 `ssh` 到机器人（公钥认证；密码登录也行，但脚本里假设了 `BatchMode`）
- `curl`、`unzip`

---

## 2. 安装

### 方式 A：从笔记本一键装（推荐）

```bash
unzip agilex-remote-2026-10-29.zip
cd agilex-remote
./install.sh agilex@<机器人地址>
```

它会：打包 → scp 上传 → 在机器人上 `sudo` 安装 → 启动服务 → 建一条 SSH 隧道并检查 `/api/status`。

### 方式 B：直接在机器人上装

把包拷到机器人上，然后：

```bash
cd agilex-remote
sudo ./install.sh
```

装完会打印：

```
UI:      http://<机器人IP>:8765/
status:  curl -s http://127.0.0.1:8765/api/status | head -c 400
logs:    journalctl -u agilex-remote -f
```

### 设置网页密码

安装时如果没设，默认是 **`changeme`**。密码存在：

```
/etc/agilex-remote.env          # AGILEX_PASSWORD=...
```

改完重启：`sudo systemctl restart agilex-remote`。

> 这个文件是 0600，且不写进 systemd unit，所以 `systemctl cat` 看不到密码。

---

## 3. 连接

### 3.1 先找到机器人 IP

机器人是 DHCP，地址会变。按顺序试：

```bash
# 1) 最省事：机器人自己上报（开机 + 每 5 分钟）
echo '<你的 ntfy topic>' > ~/.config/agilex/ntfy-topic
curl -s "https://ntfy.sh/$(cat ~/.config/agilex/ntfy-topic)/json?poll=1" \
  | grep -o '"message":"[^"]*"' | tail -1

# 2) mDNS
ping -c1 agilex.local

# 3) 直接问（如果 ssh 已经配好 ProxyCommand）
ssh agilex 'hostname -I'
```

`scripts/agilex-proxy.sh` 把上面这些串起来了，配到 `~/.ssh/config` 后 `ssh agilex` 永远能用：

```
Host agilex
    ProxyCommand /绝对路径/agilex-remote/scripts/agilex-proxy.sh %h %p
```

### 3.2 打开网页

**同一网段**：直接访问，不需要隧道

```
http://<机器人IP>:8765/
```

**跨网段 / 走 VPN**：用 SSH 隧道（等价于把远端端口搬到本地）

```bash
ssh -N -L 8765:127.0.0.1:8765 agilex@<机器人地址>
# 然后浏览器打开 http://127.0.0.1:8765/
```

> 注意：`ROS_LOCALHOST_ONLY=1` 只限制 ROS 2 的 DDS 流量不出本机，**不影响网页的 HTTP**。
> 但这也意味着远端的 RViz / ros2 topic 看不到机器人的话题 —— 那是设计如此。

### 3.3 登录

输入第 2 节设的密码。浏览器会拿到一个 cookie；局域网内不要把这个 cookie 或密码分享出去。

### 3.4 安卓

`android/` 是 WebView 壳，用 `android/build.sh` 自行构建。它走同一条 ntfy 通道自动发现机器人。
APK 不在这个包里（需要 Android SDK 自己打）。

---

## 4. 装完先做这几步自检

```bash
# 服务活着吗
systemctl is-active agilex-remote

# 两臂都连上了吗（connected 必须是 true）
curl -s http://127.0.0.1:8765/api/status | python3 -m json.tool | head -40

# 相机出图吗（两个都应返回 JPEG）
curl -s "http://127.0.0.1:8765/frame?cam=0" -o /tmp/c0.jpg && file /tmp/c0.jpg
curl -s "http://127.0.0.1:8765/frame?cam=2" -o /tmp/c2.jpg && file /tmp/c2.jpg
```

或者一条命令：

```bash
./scripts/check-connection.sh            # 本机就是机器人时
./scripts/check-connection.sh agilex@<地址>   # 从笔记本检查
```

期望看到：

- `connected: true`，`enabled` 由你决定
- `mode_actual` 是 `MOVE_J`（这是**手臂自己上报**的模式，不是软件记账）
- `jitter.cmd_hz` 接近 50
- 两个相机都返回 JPEG

---

## 5. 第一次动手臂之前

1. 场地清空，手放在急停上。
2. 点 **Enable arm**（`POST /api/arm/enable`）。使能前服务会把目标对齐到实测角度，所以**不会跳**。
3. 用 1° 的 nudge 试一下，确认方向和幅度。
4. **先别碰** MIT / 拖动示教 / 主从联动 / MOVE C —— 这四种会让手臂变柔顺或交给链路，需要人盯着第一次跑。

采集轨迹：

```bash
curl -X POST :8765/api/record/start -d '{"task":"pick"}'
#  ... 操作 ...
curl -X POST :8765/api/record/stop  -d '{"success":true}'
```

回放一段轨迹：

```bash
curl -X POST :8765/api/traj/list
curl -X POST :8765/api/traj/load-file -d '{"arm":0,"path":"/home/agilex/agilex-data/episodes/episode_0042"}'
curl -X POST :8765/api/traj/play      -d '{"arm":0,"loop":false,"speed":0.5}'
```

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 网页打不开 | 端口没监听 / 防火墙 / 不同网段 | `ss -ltnp \| grep 8765`；跨网段用 SSH 隧道 |
| 密码总是错 | 改过 `/etc/agilex-remote.env` 没重启 | `sudo systemctl restart agilex-remote` |
| `connected: false` | **CAN 总线静默**：手臂没上电、急停被按、CAN 线松 | 见下方"总线静默" |
| 手臂不动，`enabled: true` | 模式不是 MOVE_J，或电机未全部使能 | 看 `mode_actual` 和 `joints_enabled` |
| 手臂抖 / 嗡嗡响 | 目标与实测差太多（被限位或卡住） | `POST /api/arm/reset` 重新对齐，**不会产生位移** |
| 相机打不开 | 设备被别的进程占用（V4L2 独占） | `fuser -v /dev/video0`，停掉抢占的进程 |
| 服务起不来 | ROS 环境没 source | `journalctl -u agilex-remote -n 50` |

**总线静默**怎么确认：

```bash
for b in can_left can_right; do
  echo "$b: $(timeout 1 candump $b 2>/dev/null | wc -l) 帧/秒  $(ip -details link show $b | grep -o 'can state [A-Z-]*')"
done
```

正常是几千帧/秒、`ERROR-ACTIVE`。如果是 **0 帧 + ERROR-PASSIVE**，就是机器人侧没在说话：
先查手臂电源和急停，再查 CAN 线。恢复后重启服务：

```bash
sudo systemctl restart agilex-remote
```

> 服务启动时会检查内核 CAN 收包计数，2 秒没新帧就把该臂标记为 `NOT ok`，并且**不采纳任何 target / home**。
> 这是故意的：死总线会被读成 `[0,0,0,0,0,0]`，如果当成 home，之后一按 Home 手臂就会去一个凭空的零位。

---

## 7. 卸载

```bash
sudo systemctl disable --now agilex-remote
sudo rm -f /etc/systemd/system/agilex-remote.service
sudo rm -rf /etc/systemd/system/agilex-remote.service.d
sudo rm -rf /usr/local/share/agilex-remote /etc/agilex-remote.env
sudo systemctl daemon-reload
```

---

## 8. 发布前自检（改过代码的人看）

```bash
./scripts/verify-bundle.sh <打包出来的.zip>
```

它会解压到临时目录，跑**随包发布的那份测试**、检查所有 shell 语法、扫密码和真实 IP。
这个脚本不是摆设：曾经有一次旧版测试文件被覆盖，回归测试静默丢失，而 bundle 照样"构建成功"。
