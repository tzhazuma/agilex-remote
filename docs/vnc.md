# VNC access to the AgileX robot's desktop (x11vnc) — replaces NoMachine

**Task:** task-6 · **Author:** teammate `vnc` · **Date:** 2026-10-13 00:55 CST
**Robot:** `agilex` → `<robot-ip>` (Ubuntu 22.04.5 LTS, X11 session on display `:0`) · **Mac:** M3 Pro, macOS 27.2

---

## 1. Bottom line

**It works end to end.** The Mac completes a full RFB 3.8 handshake with the robot's
x11vnc, authenticates with the password, and gets a `2560x1440` desktop named
`agilex:0` back — over the standing SSH tunnel, with nothing exposed to the campus
network.

| | |
|---|---|
| **What the user does** | Run `./bin/robot-vnc.sh` (or open **VNC Viewer** and connect) |
| **Address** | `localhost:5900` |
| **Password** | `agx` |
| **What they see** | The robot's **real, already-logged-in desktop on display `:0`** — the same thing that is on the robot's own monitor |
| **Robot side** | `x11vnc` 0.9.16 from apt, `systemd` unit `x11vnc.service` (enabled + active), bound to `127.0.0.1:5900` only |
| **Mac side** | launchd agent `com.example.agilex-vnc-tunnel` (RunAtLoad + KeepAlive), SSH tunnel `localhost:5900 → robot 127.0.0.1:5900` |
| **Client** | RealVNC Viewer 7.15.1, already at `/Applications/VNC Viewer.app` — nothing installed on the Mac |
| **NoMachine** | Left completely untouched. Its tunnel agent (`com.example.agilex-nx-tunnel`) is still loaded and running; VNC is simply now the primary path. |

### The one command

```bash
cd ~/Documents/deepseek-harness/default-workspace/agilex-setup
./bin/robot-vnc.sh          # ensures the tunnel, checks the chain, launches VNC Viewer
```

Then in VNC Viewer: **Address `localhost:5900`**, **Password `agx`**.

> RealVNC Viewer does **not** register the `vnc://` URL scheme (its `Info.plist` only
> declares `com.realvnc.vncviewer.sso` and `com.realvnc.vncviewer.connect`), so
> `open vnc://localhost:5900` would not reach it. Type the address once and let the
> viewer save the connection.

---

## 2. The trap the brief warned about — and a second one behind it

The brief's headline risk was that `x11vnc -auth guess` often fails on a GDM/GNOME
session. **It does fail here.** There is also a second failure that only appears
*after* you fix the first one, and it is the reason the service does **not** run as
root.

### 2.1 `-auth guess` fails (confirmed, as predicted)

Xorg on this robot was started with an explicit cookie file:

```
$ ps aux | grep [X]org
agilex  1226 ... /usr/lib/xorg/Xorg vt2 -displayfd 3 -auth /run/user/1000/gdm/Xauthority -nolisten tcp ...
```

`-auth guess` searches `$HOME/.Xauthority` **of the account running x11vnc**. As root
that is `/root/.Xauthority`, which does not exist:

```
$ x11vnc -display :0 -auth guess -rfbport 5903 -localhost -forever -shared
xauth:  file /root/.Xauthority does not exist
13/10/2026 00:30:33 -auth guess: failed for display=':0'
13/10/2026 00:30:33 -auth guess: since we are root, retrying with FD_XDM=1
13/10/2026 00:30:33 -auth guess: failed for display=':0'
→ nothing listening on 5903. FAILURE.
```

**Fix:** hardcode the path Xorg itself was started with —
`-auth /run/user/1000/gdm/Xauthority`. (`/home/agilex/.Xauthority` also attaches, but
the GDM one is authoritative because it is the file the X server was given.)

### 2.2 Running as root then dies on MIT-SHM

With the auth path fixed but still as root, x11vnc attaches, reads the framebuffer,
starts listening — **and is dead within the same second**:

```
$ x11vnc -display :0 -auth /run/user/1000/gdm/Xauthority -rfbport 5901 -localhost -forever -shared
13/10/2026 00:30:26 X display :0 is 32bpp depth=24 true color
13/10/2026 00:30:26 Listening for VNC connections on TCP port 5901
X11 MIT Shared Memory Attach failed:
  Is your DISPLAY=:0 on a remote machine?
caught X11 error:
X Error of failed request:  BadAccess (attempt to access private resource denied)
  Major opcode of failed request:  130 (MIT-SHM)
  Minor opcode of failed request:  1 (X_ShmAttach)
```

An x11vnc that starts, logs "Listening", and then exits is exactly the
"starts but cannot attach" failure mode the brief called out — the journal is the
only place it shows. Xorg runs as **uid 1000 (`agilex`)** and MIT-SHM access is
uid-checked, so a root client is refused.

**Fix, and the design decision:** the unit runs as **`User=agilex`**. Same uid as
Xorg ⇒ shared memory works ⇒ `fb read rate: 1342 MB/sec`, which is what makes this
pleasant over Wi-Fi. `-noshm` would also stop the crash, but it forces slow
framebuffer polling — the wrong trade for a link the user already finds sluggish.

### 2.3 Consequences that shaped the unit

* `/etc/x11vnc.pass` is **`agilex:agilex`, mode 600** (not `root:root`) so the service
  account can read it. It is the same password the user already knows, so this
  reveals nothing new to that account.
* `ExecStartPre` waits (bounded to 10 min) for `/tmp/.X11-unix/X0` **and** the cookie
  before starting, so a cold boot does not spin on a desktop that does not exist yet.
* `Restart=always` + the `ExecStartPre` wait means a logout/login (which restarts Xorg
  and rewrites the cookie) heals itself.

---

## 3. What was installed / changed

### Robot (`agilex@<robot-ip>`), all via `bin/rsudo.sh`

| Path | What |
|---|---|
| — | `apt-get install -y x11vnc` → **0.9.16-8** (amd64) |
| `/etc/x11vnc.pass` | VNC password file, `agilex:agilex` `0600`, 8 bytes: `5b9b15565fc5fcaf` |
| `/etc/systemd/system/x11vnc.service` | the service (md5 `236eb918d1375e66e0d06593e5f3fd29`), enabled + active |

Nothing else on the robot was touched — no NoMachine config, no `.bashrc`/`.zshrc`/
`.profile`, no `custom.conf`, no `agilex-report-ip.sh`, no ROS workspace.

**Proof the password file really holds `agx`:** the VNC password file format is
DES-ECB of the 8-byte password using the mirrored fixed key `17526b06234e5807`:

```
$ printf 'agx\0\0\0\0\0' | openssl enc -des-ecb -K e84ad660c4721ae0 -nopad | xxd -p
5b9b15565fc5fcaf        ← byte-for-byte identical to /etc/x11vnc.pass
```

### Mac (`<you>@<mac>`)

| Path | What |
|---|---|
| `~/Library/LaunchAgents/com.example.agilex-vnc-tunnel.plist` | standing tunnel `-L 5900:127.0.0.1:5900 agilex`, RunAtLoad + KeepAlive + ThrottleInterval 15 |
| `bin/robot-vnc.sh` | helper: default / `--status` / `--probe` / `--restart` / `--stop` |

`~/.ssh/config` was **not** modified (the existing `agilex` alias + `~/.local/bin/agilex-proxy.sh`
ProxyCommand are reused as-is), and the NoMachine plist was not touched.

---

## 4. Evidence — robot side

### 4.1 Version

```
$ x11vnc -version
x11vnc: 0.9.16 lastmod: 2019-01-05
$ dpkg -l x11vnc | tail -1
ii  x11vnc  0.9.16-8  amd64  VNC server to allow remote access to an existing X session
```

### 4.2 The unit as installed

```
$ cat /etc/systemd/system/x11vnc.service
[Unit]
Description=Share the existing X display :0 over VNC (x11vnc)
Documentation=man:x11vnc(1)
After=display-manager.service
Wants=display-manager.service

[Service]
Type=simple

# Run as the desktop user (uid 1000), NOT as root. Both reasons were verified
# on this robot on 2026-10-13:
#   1. `-auth guess` cannot work as root: it looks in $HOME/.Xauthority of the
#      account starting x11vnc (root -> /root/.Xauthority, which does not
#      exist) and fails with
#        xauth:  file /root/.Xauthority does not exist
#        -auth guess: failed for display=':0'
#   2. Xorg runs as uid 1000, and MIT-SHM access is uid-checked, so as root
#      x11vnc dies within a second of attaching with
#        X Error of failed request: BadAccess (attempt to access private
#        resource denied) ... (MIT-SHM) X_ShmAttach
#      As agilex the shared-memory framebuffer path works, which is what makes
#      this fast enough to be pleasant over Wi-Fi.
User=agilex
Group=agilex

# Xorg's own -auth argument (from `ps aux | grep Xorg` on this robot):
#   /usr/lib/xorg/Xorg vt2 -displayfd 3 -auth /run/user/1000/gdm/Xauthority ...
# That file is the authoritative session cookie. /home/agilex/.Xauthority also
# works, but this is the one the X server itself was started with. -auth guess
# is deliberately NOT used (see above).
Environment=XAUTHORITY=/run/user/1000/gdm/Xauthority

# Wait for a desktop to exist instead of dying at boot: this is a system
# service, so it starts long before anyone logs in graphically. Bounded to
# 10 minutes; on timeout the unit fails and Restart= retries.
ExecStartPre=/usr/bin/timeout 600 /bin/sh -c 'until [ -S /tmp/.X11-unix/X0 ] && [ -r /run/user/1000/gdm/Xauthority ]; do sleep 2; done'

# -localhost : bind 127.0.0.1 only - the Mac reaches this through an SSH
#              tunnel, so port 5900 is never exposed to the campus network.
# -forever   : keep serving after a client disconnects.
# -shared    : allow several clients and never kick an existing one off.
# -repeat    : pass key auto-repeat through.
# -noxdamage : do not rely on the X DAMAGE extension.
# -nolookup  : skip reverse-DNS on connect (it only ever sees 127.0.0.1).
ExecStart=/usr/bin/x11vnc -display :0 -auth /run/user/1000/gdm/Xauthority -rfbauth /etc/x11vnc.pass -rfbport 5900 -localhost -forever -shared -repeat -noxdamage -nolookup

# Restart when the X server goes away (logout, reboot, VT switch) so the unit
# picks up the freshly written cookie.
Restart=always
RestartSec=5
SyslogIdentifier=x11vnc

[Install]
WantedBy=multi-user.target
```

### 4.3 Status, listener, process

```
$ systemctl is-enabled x11vnc.service ; systemctl is-active x11vnc.service
enabled
active

$ systemctl status x11vnc.service --no-pager -l
● x11vnc.service - Share the existing X display :0 over VNC (x11vnc)
     Loaded: loaded (/etc/systemd/system/x11vnc.service; enabled; vendor preset: enabled)
     Active: active (running) since Tue 2026-10-13 00:41:55 CST
       Docs: man:x11vnc(1)
    Process: 20578 ExecStartPre=/usr/bin/timeout 600 /bin/sh -c until [ -S /tmp/.X11-unix/X0 ] && [ -r /run/user/1000/gdm/Xauthority ]; do sleep 2; done (code=exited, status=0/SUCCESS)
   Main PID: 20581 (x11vnc)
      Tasks: 1 (limit: 37985)
     Memory: 16.7M
        CPU: 345ms
     CGroup: /system.slice/x11vnc.service
             └─20581 /usr/bin/x11vnc -display :0 -auth /run/user/1000/gdm/Xauthority -rfbauth /etc/x11vnc.pass -rfbport 5900 -localhost -forever -shared -repeat -noxdamage -nolookup

$ ss -tlnp | grep 5900
LISTEN 0  32   127.0.0.1:5900  0.0.0.0:*  users:(("x11vnc",pid=20581,fd=8))
LISTEN 0  32       [::1]:5900     [::]:*  users:(("x11vnc",pid=20581,fd=9))

$ ps -o user,pid,args -C x11vnc
USER     PID   COMMAND
agilex   20581 /usr/bin/x11vnc -display :0 -auth /run/user/1000/gdm/Xauthority -rfbauth ...
```

`127.0.0.1` / `::1` only — **the loopback address is the only one bound**, so port 5900
is not reachable from the campus network even though the robot is on eduroam.

### 4.4 Journal — proof it really attached to `:0`

```
$ journalctl -u x11vnc.service --no-pager | grep -E "display :0|fb read rate|screen setup finished|Listening for VNC"
13/10/2026 00:41:55 Using X display :0
13/10/2026 00:41:55 X display :0 is 32bpp depth=24 true color
13/10/2026 00:41:55 Listening for VNC connections on TCP port 5900
13/10/2026 00:41:55 fb read rate: 1342 MB/sec
13/10/2026 00:41:55 screen setup finished.
```

No `BadAccess`, no auth error, no crash — this is the line that distinguishes
"listening" from "actually sharing the desktop".

---

## 5. Evidence — Mac side

### 5.1 The launchd agent

```
$ plutil -lint ~/Library/LaunchAgents/com.example.agilex-vnc-tunnel.plist
<home>/Library/LaunchAgents/com.example.agilex-vnc-tunnel.plist: OK

$ cat ~/Library/LaunchAgents/com.example.agilex-vnc-tunnel.plist
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.agilex-vnc-tunnel</string>

    <!-- Keeps a standing SSH tunnel so RealVNC Viewer can always reach the
         robot's x11vnc at localhost:5900, whatever DHCP address the robot
         currently has. Going through the "agilex" SSH alias means the
         ProxyCommand in ~/.ssh/config discovers the address, and SSH key auth
         means the tunnel never needs a password.

         The ProxyCommand lives at ~/.local/bin/agilex-proxy.sh and NOT under
         ~/Documents on purpose: macOS TCC refuses to let a launchd agent
         execute anything under ~/Documents ("Operation not permitted"), which
         is what broke the first version of the NoMachine tunnel.

         The robot's x11vnc binds 127.0.0.1 only, so this tunnel is the only
         way in - port 5900 is never exposed to the campus network. -->
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/ssh</string>
        <string>-N</string>
        <string>-o</string><string>BatchMode=yes</string>
        <string>-o</string><string>ExitOnForwardFailure=yes</string>
        <string>-o</string><string>ServerAliveInterval=30</string>
        <string>-o</string><string>ServerAliveCountMax=6</string>
        <string>-o</string><string>ConnectTimeout=30</string>
        <string>-L</string><string>5900:127.0.0.1:5900</string>
        <string>agilex</string>
    </array>

    <key>RunAtLoad</key>
    <true/>
    <!-- restart it whenever it drops (robot reboots, VPN blips, sleep/wake) -->
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>15</integer>

    <key>StandardOutPath</key>
    <string>/tmp/agilex-vnc-tunnel.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/agilex-vnc-tunnel.log</string>
</dict>
</plist>

$ launchctl print gui/501/com.example.agilex-vnc-tunnel | grep -E "^\s*(state|pid|runs) "
	state = running
	runs = 3
	pid = 41324

$ lsof -nP -iTCP:5900 -sTCP:LISTEN
COMMAND   PID  USER   FD   TYPE  ...  NODE NAME
ssh     41324 <you>    6u  IPv6  ...  TCP [::1]:5900 (LISTEN)
ssh     41324 <you>    7u  IPv4  ...  TCP 127.0.0.1:5900 (LISTEN)
```

### 5.2 The end-to-end proof (RFB banner + real authentication)

```
$ python3 -c "import socket;s=socket.create_connection(('127.0.0.1',5900),10);print(repr(s.recv(12)))"
b'RFB 003.008\n'

$ ./bin/robot-vnc.sh --probe
  banner         : 'RFB 003.008'
  security types : [2]  (2 = VNC password)
  auth           : OK - password accepted
  desktop        : 2560x1440, name='agilex:0'
```

The banner alone only proves *something* is listening. The probe above goes further:
it negotiates RFB 3.8, takes the 16-byte challenge, DES-encrypts it with `agx`
(mirrored-key DES, the VNC scheme) and gets `SecurityResult = 0`, then reads the
`ServerInit` that reports the real desktop size and name. `agilex:0` is x11vnc telling
us it is attached to the session on display `:0`.

Timed trace of the same probe:

```
[  0.00s] connected
[  0.12s] banner = b'RFB 003.008\n'
[  0.13s] security types = [2]
[  0.14s] challenge = 18b6c6c6317dac4c7106c77da2cac29f
[  0.15s] response sent - waiting for SecurityResult
[  0.17s] SecurityResult = 0 -> SUCCESS
[  0.18s] ServerInit: 2560x1440, name=b'agilex:0'
[  0.35s] framebuffer update header = 00000004 (msg-type 0)   ← real pixels flowed
```

### 5.3 Negative control — the password is genuinely enforced

With a wrong password the same probe is rejected, and the robot's journal records it:

```
$ python3 /tmp/vnc-stage/rfb_probe2.py wrongpw
[  0.18s] SecurityResult = 1 -> FAILURE

$ journalctl -u x11vnc.service | grep "authentication failed"
13/10/2026 00:43:18 authProcessClientMessage: authentication failed from 127.0.0.1
```

So `-rfbauth` is live; the server is not silently accepting anyone.

### 5.4 KeepAlive really works

The tunnel was killed to simulate a drop (robot reboot, VPN blip, sleep/wake):

```
killing tunnel pid 40994 (simulating a dropped tunnel)
waiting for launchd KeepAlive to bring it back...
recovered after 1s with pid 41324
	state = running
	runs = 3
```

### 5.5 The helper script

`bin/robot-vnc.sh` mirrors `bin/robot-desktop.sh`'s style (same `--status`/`--stop`
shape, same tunnel-through-the-alias reasoning) and adds `--probe`/`--restart`.
It uses the sibling `bin/rrun.sh` for robot-side checks rather than hand-rolled ssh.
All modes were exercised except the final `open -a` GUI launch, which was verified
separately (`open -g -a "/Applications/VNC Viewer.app"` → process
`/Applications/VNC Viewer.app/Contents/MacOS/vncviewer` came up, then was quit again).

```
$ ./bin/robot-vnc.sh --status
robot reachable  : yes (<robot-ip>)
launchd agent    : loaded (com.example.agilex-vnc-tunnel)
ssh tunnel       : listening on 127.0.0.1:5900
robot x11vnc     : active enabled
VNC Viewer app   : installed (7.15.1)
end-to-end RFB   :
  banner         : 'RFB 003.008'
  security types : [2]  (2 = VNC password)
  auth           : OK - password accepted
  desktop        : 2560x1440, name='agilex:0'
```

---

## 6. Security posture

* **Not exposed:** x11vnc binds `127.0.0.1` only (`-localhost`). The only route in is
  the SSH tunnel, which is authenticated by the existing `agilex` SSH key.
* **Password:** `agx`, same as the robot's sudo/SSH password, stored in
  `/etc/x11vnc.pass` (8-byte VNC-obfuscated form — note that VNC's DES scheme is
  obfuscation, not real encryption; it is safe here **only** because the port is not
  reachable off-host and the transport is SSH).
* **`-shared`:** multiple viewers can connect and nobody gets kicked off. The user's
  own local session on the robot's monitor is never disturbed.
* **Full control:** whoever holds the tunnel has keyboard/mouse control of the
  logged-in desktop. That is inherent to sharing an existing session.

---

## 7. Operations & troubleshooting

```bash
./bin/robot-vnc.sh --status     # every link: robot, agent, tunnel, service, RFB handshake
./bin/robot-vnc.sh --probe      # just the end-to-end RFB handshake
./bin/robot-vnc.sh --restart    # bounce the SSH tunnel
./bin/robot-vnc.sh --stop       # tear the tunnel down and keep it down
```

| Symptom | Check |
|---|---|
| Viewer can't connect | `./bin/robot-vnc.sh --status` — is the tunnel listening? is `x11vnc.service` active? |
| Tunnel keeps dying | `tail /tmp/agilex-vnc-tunnel.log` — usually the robot's DHCP address moved; `./bin/find-robot.sh` |
| Tunnel up, handshake fails | `./bin/rrun.sh -c 'systemctl status x11vnc.service'` and `journalctl -u x11vnc.service` |
| Robot rebooted, no desktop | The unit waits up to 10 min for a graphical login; it starts on its own once someone logs in at the robot |
| Restart the robot side | `./bin/rsudo.sh -c 'systemctl restart x11vnc.service'` |

**First connection after a long idle period:** the very first client connection
observed during this work took **~9 s** to complete the handshake (journal:
`executing handler for type 2` at 00:36:04, `created selwin` at 00:36:13) while
x11vnc built its initial state for a 2560×1440 screen; every connection since,
including immediately after a `systemctl restart`, completes in **under 0.5 s**.
If the viewer appears to hang on first connect, give it ten seconds.

---

## 8. Unknowns / not verified

1. **Not tested across a robot reboot.** The unit is `enabled` (symlink in
   `multi-user.target.wants`) and `ExecStartPre` waits for the desktop, but a real
   reboot was deliberately not performed — other agents were mid-work on the robot.
2. **Logout/login cycle not exercised.** The design (Restart=always + the wait for a
   fresh cookie) is intended to handle Xorg restarting and rewriting
   `/run/user/1000/gdm/Xauthority`, but it was not observed live.
3. **No GUI login was completed.** The protocol path is proven end to end, and the
   viewer binary launches, but I did not type the password into VNC Viewer's window —
   that is the user's step, and it is the last unverified inch.
4. **macOS Screen Sharing is not enabled**, so nothing else owns local port 5900 today.
   If the user ever turns on Screen Sharing / Remote Management, it will want 5900 and
   will collide with this tunnel.
5. **Display number changed.** The earlier `reports/desktop.md` (task-4) documented the
   desktop on `:1`; it is now `:0` (`who` → `agilex :0`, `loginctl` → `Type=x11`).
   The unit targets `:0`; if GDM ever hands out a different display the unit would need
   that one line changed.
6. **`-noxdamage`** was kept because the brief specified it; this build documents it as
   a real option (not deprecated). It has not been A/B tested for smoothness against the
   default. If the user finds motion sluggish, dropping `-noxdamage` is the first knob
   to try, followed by `-ncache 10` (which x11vnc itself suggests in the journal).

---

## 9. Files touched (write scope)

**Robot:** `/etc/systemd/system/x11vnc.service`, `/etc/x11vnc.pass`, apt install of
`x11vnc` — nothing else.

**Mac:** `~/Library/LaunchAgents/com.example.agilex-vnc-tunnel.plist`,
`agilex-setup/bin/robot-vnc.sh`, this report.

**Explicitly untouched:** the NoMachine setup and `com.example.agilex-nx-tunnel.plist`
(still loaded and running), `~/.ssh/config`, `/home/agilex/.bashrc`,
`/home/agilex/.zshrc`, `/home/agilex/.profile`, `/etc/gdm3/custom.conf`,
`/usr/local/bin/agilex-report-ip.sh`, and every ROS workspace.
