# Windows 遥控端（Git Bash / PowerShell）

这是 AgileX Tracer + 双 Piper 机械臂网页遥控器的 **Windows 客户端**，只负责把机器人上的
`127.0.0.1:8765` 通过 SSH 本地转发到本机，并打开浏览器。它不修改机器人、不启动
`tracer_base`、不发送任何运动/使能/复位/急停清除请求，也不安装 ROS。

> 机器人端仍然使用原来的 `server/agilex-remote-server.py` 和 systemd 服务；本目录没有改动机器人端代码，也没有改动 `scripts/` 下的原 Unix 脚本。

---

## 1. 文件

| 文件 | 作用 |
|---|---|
| `agilex-remote-win.sh` | 主实现：发现地址、校验主机密钥、建隧道、检查 `/api/status`、打开浏览器、停止隧道 |
| `agilex-remote-win.ps1` | PowerShell 入口：定位 Git Bash 并转发所有参数 |
| `agilex-remote.cmd` | 双击/CMD 入口；参数原样转发，出错时停留窗口 |
| `client.env` | 本机配置：机器人地址、用户、非默认 identity 路径（不含密钥内容） |
| `client.env.example` | 配置文件模板 |
| `tests/selftest.sh` | 离线自检：不连接机器人，用本地假 `/api/status` 验证复用、检查、停止和中文路径 |
| `tests/fixtures/status.json` | 自检用的假状态数据 |

状态、缓存、日志和独立 `known_hosts` 默认放在：

```
C:\Users\<你>\.cache\agilex-win\
```

可以用 `--state-dir` 或 `AGILEX_STATE_DIR` 改到任意路径（中文、空格路径都支持）。

### 本机配置文件

`windows-client/client.env` 是可选的机器本地配置，脚本启动时自动读取（只接受
`AGILEX_*` 键，不会执行 shell 代码）。优先级：命令行 > 环境变量 > `client.env` > 默认值。
示例（实际 `client.env` 只保留在机器本地，不随包发布）：

```ini
AGILEX_HOST=192.168.1.50
AGILEX_USER=agilex
AGILEX_IDENTITY_FILE=C:\Users\<you>\.ssh\id_ed25519_robot
```

这样每次 `start` 都会显式使用配置中的 identity，并通过 `-o IdentitiesOnly=yes` 只尝试
这一个密钥。`client.env` 只保存路径/地址，不包含私钥内容；可参考 `client.env.example`。
如果需要临时换密钥，用 `--identity <other-key>` 覆盖；需要换配置文件，用
`AGILEX_WIN_CONFIG=<path>`。

---

## 2. 前置条件

- Windows 10/11
- **Git for Windows**（提供 `bash.exe`、`ssh`、`ssh-keyscan`、`ssh-keygen`、`curl`、`netstat`）
- 能通过 SSH 登录机器人（默认用户 `agilex`）。本客户端只使用已有的 SSH 密钥/agent，
  **不会读取、复制或输出私钥内容**。
- 可选：Python 3（只用于把 `/api/status` 排版成易读摘要；没有 Python 也能建隧道和打开网页）

不要求安装 ROS、Python ROS 包、Node 或任何机器人端组件。

---

## 3. 快速开始

### 3.1 PowerShell（推荐）

```powershell
cd "C:\Users\<you>\Downloads\AgileX-项目交付-2026-11-19\AgileX-项目交付-2026-11-19\02-代码\windows-client"
.\agilex-remote.cmd
```

或者直接调用 PowerShell 包装器：

```powershell
.\agilex-remote-win.ps1
```

默认行为：

1. 如果本机 `127.0.0.1:8765` 已经有健康的 AgileX 控制器，直接复用它，不再开第二条隧道；
2. 否则按缓存 → ntfy（可选）→ 历史地址 → `agilex.local` → `AGILEX_EXTRA_HOSTS` 的顺序找机器人；
3. 首次连接显示 SSH 主机密钥的 SHA256 指纹，要求你确认；
4. 建立 `127.0.0.1:8765 -> 机器人:127.0.0.1:8765` 的 SSH 本地转发；
5. `GET /api/status` 确认控制器真的在响应；
6. 打开 `http://127.0.0.1:8765/`。

没有 `client.env` 时，直接指定地址：

```powershell
.\agilex-remote.cmd --host 192.168.1.50
```

### 3.2 Git Bash

```bash
cd "/c/Users/<you>/Downloads/AgileX-项目交付-2026-11-19/AgileX-项目交付-2026-11-19/02-代码/windows-client"
./agilex-remote-win.sh
```

指定地址：

```bash
./agilex-remote-win.sh --host 192.168.1.50
./agilex-remote-win.sh --host agilex.local
./agilex-remote-win.sh --host agilex@192.168.1.50
```

---

## 4. 常用命令

```powershell
.\agilex-remote.cmd                  # 启动并打开浏览器
.\agilex-remote.cmd --no-browser     # 只建隧道，不打开浏览器
.\agilex-remote.cmd status           # 只读：本机隧道 + HTTP + 状态摘要
.\agilex-remote.cmd check            # 只读：端到端检查 /api/status
.\agilex-remote.cmd check --strict   # 底盘/两臂未连接时报失败
.\agilex-remote.cmd check --wait 15  # 最多等 15 秒 API 就绪
.\agilex-remote.cmd find             # 只发现并打印机器人地址（stdout 只有地址）
.\agilex-remote.cmd stop             # 只停止本工具启动的本地隧道
```

`status` / `check` / `find` 都不会发 POST，不会使能或移动任何东西。

兼容原脚本的环境变量：`AGILEX_REMOTE_LOCAL_PORT`（本地端口）和 `AGILEX_PORT`
（机器人端端口）也识别；新名字 `AGILEX_LOCAL_PORT` / `AGILEX_REMOTE_REMOTE_PORT` 优先。

退出码：

| 码 | 含义 |
|---|---|
| `0` | 成功/健康 |
| `2` | 找不到机器人，或没有本地监听（`check`） |
| `3` | API 不健康（`check`） |
| `4` | SSH 隧道提前退出 |
| `5` | 本地端口被其他进程占用，或不是 AgileX 服务 |
| `6` | 隧道在，但 `/api/status` 一直没就绪 |
| `127` | PowerShell 包装器找不到 Git Bash |

---

## 5. 地址发现

不带 `--host` 时，顺序如下（命中即停）：

1. `~/.cache/agilex-win/current-host` 缓存；
2. ntfy 上报地址（可选，见下）；
3. `seen-hosts` 历史地址；
4. `agilex.local`（mDNS）；
5. `AGILEX_EXTRA_HOSTS` 里的额外候选（空格/逗号分隔）。

每个候选都用 `ssh-keyscan` 读取真实的 SSH 主机密钥来验证，**不是**只看 TCP 端口是否通。
本客户端**不做整段扫描**：Windows 上慢，而且会碰到无关主机；地址变了就显式
`--host`，或配置 ntfy。

### ntfy（可选）

机器人会把自己的地址上报到 ntfy。把 topic 放在以下任一位置即可，客户端只读取、不打印：

- 环境变量 `AGILEX_NTFY_TOPIC`
- `AGILEX_NTFY_TOPIC_FILE` 指向的文件
- `~/.cache/agilex-win/ntfy-topic`
- `~/.config/agilex/ntfy-topic`

如果不想用 ntfy，完全不用创建这些文件。

---

## 6. 首次主机密钥确认（重要）

客户端使用 **独立的** `known_hosts`（默认 `~/.cache/agilex-win/known_hosts`），并用
`HostKeyAlias=agilex-robot` 作为稳定别名。这样机器人 DHCP 换 IP 后，只要主机密钥没变，
就不需要重新确认，也不会去改你的全局 `~/.ssh/known_hosts`。

首次连接时：

```
First connection to 192.168.1.50. Verify this SHA256 fingerprint out-of-band:
  ssh-ed25519            SHA256:...
  ssh-rsa                SHA256:...
Accept this host key for agilex-robot? [y/N]
```

请通过机器人本地控制台、已有可信记录或其他独立渠道核对指纹，再输入 `y`。
**客户端从不使用 `StrictHostKeyChecking=no`**，也不会静默接受未知密钥。

自动化场景可以显式固定指纹：

```bash
./agilex-remote-win.sh --host 192.168.1.50 \
  --fingerprint SHA256:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

指纹匹配时无需交互；不匹配直接拒绝。`--accept-host-key` 只用于你明确接受首次密钥的
场景（仍会打印指纹和警告）。

如果密钥变了：

- 默认 **拒绝连接**，并同时打印旧/新指纹；
- 确认是机器人重装/换机后，才用 `--trust-new-host-key`（交互确认）或
  `--fingerprint <新指纹>` 更新；
- 不会因为 `--accept-host-key` 就覆盖已信任的密钥。

---

## 7. 重复启动与停止

- 如果 `127.0.0.1:8765` 已经有健康的 AgileX `/api/status`，`start` 直接复用，不会启动第二条 SSH。
- 如果端口被其他程序占用，`start` 会报出 Windows PID 并退出，**不会杀未知进程**；可以换端口：

  ```bash
  ./agilex-remote-win.sh --local-port 8766
  ```

- `stop` 只停止：
  - `tunnel.pid` 记录且命令行确实是本工具 `ssh -L ...` 的进程；或
  - 端口监听者能映射到本工具 SSH 命令行的情况。
- `stop` 不会 SSH 到机器人，不会停止/重启机器人上的服务，也不会碰 `tracer_base`。

---

## 8. 中文路径与编码

- 所有路径都加了引号；`--state-dir "D:\实验 数据\遥控"`、中文用户名、空格路径都可使用。
- PowerShell 包装器把控制台输出设为 UTF-8；`.cmd` 使用 `%~dp0`，从中文目录双击也能找到脚本。
- 主脚本输出以 ASCII 英文为主，避免旧版 CMD 代码页把中文日志显示成乱码；README 为中文。

---

## 9. 只读自检

```bash
./agilex-remote-win.sh check
```

检查项：

1. 本机 `127.0.0.1:8765` 是否有监听；
2. `GET /api/status` 是否返回 HTTP 200；
3. 返回体是否真的是 AgileX 状态（`ok`、`limits`、`chassis`）；
4. 打印底盘、两臂连接/使能/模式/命令频率、限速和 watchdog；
5. `--strict` 时，底盘或任一机械臂 `connected != true`、或 E-STOP 为真则失败。

它不会请求 `/frame`（避免占用相机），不会 POST，不会发 `/cmd_vel`，不会调用
`/api/arm/enable`、`/api/arm/reset` 或任何轨迹/录制接口。

离线自检（完全不连机器人）：

```bash
bash tests/selftest.sh
```

它会用本地 Python HTTP 服务器提供假的 `/api/status`，验证复用、检查、停止不误杀、
中文路径、参数校验和启动器；不会访问真实机器人地址或 `agilex.local`。

---

## 10. 与原 macOS 脚本的对应关系

| 原脚本 | Windows 客户端里的做法 |
|---|---|
| `scripts/agilex-proxy.sh` | 用 `ssh-keyscan` 逐候选验证；不实现 `nc -G` 扫描和整段 sweep |
| `scripts/robot-remote.sh` | 只做 SSH 隧道 + `/api/status` + 浏览器；**不启动 `tracer_base`，不重启服务，不碰相机模块** |
| `scripts/check-connection.sh` | 改为客户端只读 `check`：只查隧道和 `/api/status`，不在机器人上跑 `candump`/`systemctl` |
| macOS `open` / `launchctl` | PowerShell `Start-Process` / `cmd start`；不安装 launchd/计划任务 |
| `~/.ssh/config` 的 `ProxyCommand` | 不依赖全局 SSH 配置；使用 `-F /dev/null` 和独立 `known_hosts` |

这样做的原因：Windows 上没有 `nc -G`、`launchctl`、`dscacheutil`，而且当前阶段要求
不修改机器人、不重启服务、不做硬件动作。客户端只承担“把网页搬到本机”的最小职责。

---

## 11. 排错

| 现象 | 处理 |
|---|---|
| `Git Bash not found` | 安装 Git for Windows，或设置 `AGILEX_GIT_BASH=C:\Program Files\Git\bin\bash.exe` |
| `Permission denied (publickey)` | 本机没有机器人认可的 SSH 密钥；先在 Git Bash 里配置密钥/agent。客户端不处理密码，也不会复制私钥 |
| `Host key verification failed` | 第一次未确认，或密钥变了；看状态目录下的 `tunnel.log`，按第 6 节核对指纹 |
| 端口 8765 被占用 | 用 `--local-port 8766`，或先停掉占用者；客户端不会杀未知进程 |
| `HTTP 000` / 隧道立刻退出 | 看 `~/.cache/agilex-win/tunnel.log`；确认机器人 SSH 可达、机器人端 8765 服务在跑（不要用本客户端重启它） |
| `HTTP 503` 或 API 一直不 200 | 机器人端服务/相机/手臂状态问题；本客户端不会重启服务，按原 `INSTALL-CONNECT.md` 排查 |
| `agilex.local` 找不到 | Windows 的 mDNS 解析不稳定；用 `--host <当前IP>`，或配置 ntfy |
| 浏览器没自动打开 | 手动打开命令输出的 `http://127.0.0.1:8765/` |
| 中文路径报错 | 确认通过 Git Bash/PowerShell 包装器运行，而不是 WSL 的 `bash.exe`；可用 `--state-dir` 指到纯英文临时目录定位问题 |

日志和状态文件都在状态目录里：

```
tunnel.log         SSH 的 stderr（认证/转发错误）
tunnel.pid         本工具启动的 SSH PID
tunnel.info        主机、用户、端口、启动时间（不含密码）
known_hosts        独立的 agilex-robot 主机密钥
current-host       最近一次成功的地址
seen-hosts         历史地址（最多 20 条）
last-status.json   最近一次 /api/status 原文
```

---

## 12. 安全边界

- 不读取、不复制、不输出 SSH 私钥；不保存 SSH 密码。
- 不打印 ntfy topic；只打印解析出的机器人地址。
- 不使用 `StrictHostKeyChecking=no`；首次确认和变更拒绝逻辑见第 6 节。
- 不向机器人上传文件；不改机器人端 `server/`、`scripts/`、systemd 或 ROS 工作区。
- 不发送运动、使能、复位、急停清除、录制、轨迹回放请求；只发 `GET /api/status` 和浏览器正常加载页面。
- `stop` 只作用于本机 SSH 进程，不作用于机器人。

---

## 13. 已知限制

- 只支持 SSH 公钥认证；没有实现密码登录（避免在脚本/日志里处理密码）。
- 不做整段 IP 扫描；地址变化时请用 `--host`、缓存或 ntfy。
- `check` 不抓相机帧；相机问题仍按原 `check-connection.sh` / `INSTALL-CONNECT.md` 在机器人侧排查。
- 不提供 Windows 服务/开机自启；需要时常驻可自行用“任务计划程序”调用 `agilex-remote.cmd --no-browser`，
  但要注意首次主机密钥确认必须先在交互终端完成。
- 机器人端仍要求原服务已安装并监听 8765；本客户端不负责安装或重启它。
