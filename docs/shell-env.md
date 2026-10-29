# shell-env — zsh + oh-my-zsh on the AgileX robot

**Status: COMPLETE.** Everything the task asked for is installed and verified, with two
deliberate plugin omissions and one deliberate robot-specific deviation (auto-update off).
No failures.

- **Target:** `agilex@<robot-ip>` — Ubuntu 22.04.5 LTS, x86_64, ROS 2 Humble
- **Date:** 2026-10-10
- **Tooling used:** `bin/rrun.sh` (unprivileged) and `bin/rsudo.sh` (root/apt) only.
  No hand-rolled ssh/sudo. `apt-get upgrade` / `dist-upgrade` / `do-release-upgrade` /
  reboot were **not** run.

---

## 1. What was installed

### 1a. apt packages (one `rsudo.sh` transaction)

```
apt-get install -y zsh autojump fasd fzf
→ 0 upgraded, 5 newly installed, 0 to remove.   APT_RC=0
```

| package | version | source |
|---|---|---|
| `zsh` | **5.8.1-1** | mirrors.ustc.edu.cn jammy/main |
| `zsh-common` | **5.8.1-1** | jammy/main (pulled in) |
| `autojump` | **22.5.1-1.1** | jammy/universe |
| `fasd` | **1.0.1-2** | jammy/universe |
| `fzf` | **0.29.0-1ubuntu0.1** | jammy-updates/universe |

5,791 kB fetched in 2 s (3,018 kB/s). The `zsh` postinst added `/bin/zsh` and
`/usr/bin/zsh` to `/etc/shells` automatically.

### 1b. oh-my-zsh + custom plugins (`git clone --depth=1`, unprivileged)

| path | HEAD | commit date |
|---|---|---|
| `~/.oh-my-zsh` | `42a4ccb` | 2026-10-09 |
| `~/.oh-my-zsh/custom/plugins/zsh-autosuggestions` | `85919cd` | 2025-06-24 |
| `~/.oh-my-zsh/custom/plugins/zsh-completions` | `6661b4b` | 2026-10-10 |
| `~/.oh-my-zsh/custom/plugins/zsh-syntax-highlighting` | `e6683f2` | 2026-10-09 |

Full commit of oh-my-zsh: `42a4ccb1b14dbeffe81259105a5243b4f4cb618e` (master).
The three custom plugins were cloned into `custom/plugins/` rather than apt-installed, to
match the Mac layout exactly. The official `install.sh` was **not** used — a manual clone
is deterministic and avoids the installer's interactive `chsh` prompt and `.zshrc` overwrite.

Bundled plugins confirmed present in `~/.oh-my-zsh/plugins/`: `git`, `z`, `extract`,
`web-search`, `fasd`, `fzf`, `autojump` — no extra install needed for those.

---

## 2. Files written (write scope respected)

| file | lines | md5 |
|---|---|---|
| `~/.zshrc` | 77 | `a2b6ff25cb8b54257d016e9230baeaf3` |
| `~/.zsh_aliases` | 7 | `c9b601da6c9a57f7d18a43db00440812` |

Neither file existed before, so nothing was overwritten and no backup was needed.

`~/.bashrc` was **not touched** — still `-rw-r--r-- agilex agilex 4708 2026-01-04 17:02`,
md5 `3f22c8a98a0dd1ba2daf79f22bf75ac4` (mtime predates this work by 9 months).
`~/.dsh/brew-env.sh` was not touched; it is only *referenced* from `.zshrc`.

### Required blocks, as written

```zsh
export ZSH="$HOME/.oh-my-zsh"
zstyle ':omz:update' mode disabled
DISABLE_AUTO_UPDATE="true"
ZSH_THEME="robbyrussell"
plugins=(git z extract autojump web-search fasd zsh-completions \
         zsh-syntax-highlighting zsh-autosuggestions fzf)
source "$ZSH/oh-my-zsh.sh"
...
[ -f "$HOME/.dsh/brew-env.sh" ] && source "$HOME/.dsh/brew-env.sh"

[ -f /opt/ros/humble/setup.zsh ] && source /opt/ros/humble/setup.zsh
if [ -x "$HOME/miniforge3/bin/conda" ]; then
  eval "$("$HOME/miniforge3/bin/conda" shell.zsh hook 2>/dev/null)"
fi
if [ -d /usr/local/cuda-11.8 ]; then
  export PATH="$PATH:/usr/local/cuda-11.8/bin"
  export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}/usr/local/cuda-11.8/lib64"
fi
export ROS_DOMAIN_ID=222
export ROS_LOCALHOST_ONLY=1
[ -f "$HOME/data_ros/install/setup.zsh" ]   && source "$HOME/data_ros/install/setup.zsh"
[ -f "$HOME/camera_ros/install/setup.zsh" ] && source "$HOME/camera_ros/install/setup.zsh"
[ -f "$HOME/piper_ros/install/setup.zsh" ]  && source "$HOME/piper_ros/install/setup.zsh"
[ -f "$HOME/agilex_ws/install/setup.zsh" ]  && source "$HOME/agilex_ws/install/setup.zsh"
```

All five `.zsh` variants exist on the robot and were each verified to source cleanly under
`zsh -f` before use. Every line is `[ -f ]`-guarded, so a moved or deleted workspace cannot
break the shell.

### Login shell

```
chsh -s /usr/bin/zsh agilex      # via rsudo.sh, rc=0
```

---

## 3. Deliberate omissions and deviations

1. **`sublime` — OMITTED (as instructed).** The plugin is a set of macOS `subl`/Sublime Text
   aliases; it is meaningless on Linux. The other 9 plugins are all loaded.
2. **`zsh-syntax-highlighting` position — left as the Mac has it** (before
   `zsh-autosuggestions`, not last). Upstream recommends sourcing it last, but the task said
   to mirror the Mac's list. It works as-is (`ZSH_HIGHLIGHT_VERSION=0.8.1-dev`, highlighters
   active). If suggestion highlighting ever looks off, move that one entry to the end of the
   `plugins=(...)` array — one-line change, noted in a comment in `.zshrc`.
3. **oh-my-zsh auto-update DISABLED** (`zstyle ':omz:update' mode disabled` +
   `DISABLE_AUTO_UPDATE="true"`). Robot-specific deviation: GitHub here goes over a
   high-latency VPN, and the default 13-day background update would stall new shells.
   Run `omz update` manually when you want one.
4. **Extra (not required): conda and CUDA were also mirrored** from `~/.bashrc`, because
   dropping them would have silently regressed the login environment. conda's `shell.zsh
   hook` was tested first (clean, 0.26 s) and both blocks are guarded. If you would rather
   zsh *not* auto-activate the miniforge `base` env, delete the `miniforge3/bin/conda`
   block in `.zshrc`.

---

## 4. Evidence (raw output)

### E1 — `zsh --version`
```
zsh 5.8.1 (x86_64-ubuntu-linux-gnu)
```

### E2 — `getent passwd agilex` (new login shell)
```
agilex:x:1000:1000:Agilex,,,:/home/agilex:/usr/bin/zsh
```

### E3 — `zsh -n ~/.zshrc`
```
exit=0
```

### E4 — spec smoke test `zsh -i -c '...' < /dev/null`
```
(eval):1: can't change option: zle
ZSH_OK
robbyrussell
git
z
extract
autojump
web-search
fasd
zsh-completions
zsh-syntax-highlighting
zsh-autosuggestions
fzf
exit=0
```
The single `zle` line is the expected tty-less warning — see §5.

### E5 — `ls -1 ~/.oh-my-zsh/custom/plugins/`
```
example
zsh-autosuggestions
zsh-completions
zsh-syntax-highlighting
```

### E6 — real-PTY login test (`script -qec "zsh -l -i -c ..."`)
```
LOGIN_SHELL=/usr/bin/zsh
THEME=robbyrussell
ROS=humble DOMAIN=222 LOCAL=1
PLUGINCOUNT=10
/opt/ros/humble/bin/ros2 /home/agilex/miniforge3/bin/python /usr/bin/fzf /usr/bin/fasd /usr/bin/autojump
```
**Zero errors or warnings on a real terminal.**

### E7 — ROS 2 functional test inside zsh
```
$ zsh -i -c 'ros2 pkg list | wc -l'
440
```
ROS 2 Humble is fully operational under zsh, not merely "variables set".
`AMENT_PREFIX_PATH` resolves to 33 entries across `data_ros`, `camera_ros`, `piper_ros`,
`agilex_ws` and `/opt/ros/humble`.

### E8 — installed versions
```
autojump 22.5.1-1.1
fasd 1.0.1-2
fzf 0.29.0-1ubuntu0.1
zsh 5.8.1-1
zsh-common 5.8.1-1
```

### E9 — plugin functionality spot-checks
```
_zsh_autosuggest_start → function     (zsh-autosuggestions active)
ZSH_HIGHLIGHT_VERSION=0.8.1-dev       (zsh-syntax-highlighting active)
autojump / fasd / fzf                 → all resolve to /usr/bin/*
```

### E10 — regression check after `chsh` (important for the other 3 agents)
```
rsudo.sh  → RSUDO_STILL_OK, uid=root, exit=0
rrun.sh   → still works (every command in this report ran through it, after chsh)
$ /usr/bin/zsh -c 'echo rc_sourced=...'   →  rc_sourced=NO
```
Non-interactive `zsh -c` does **not** source `.zshrc`, so command execution over ssh gets a
clean environment.

---

## 5. Known non-issues (investigated, not bugs)

- **`(eval):1: can't change option: zle`** — printed only when zsh is *interactive without a
  tty* (i.e. exactly the `zsh -i -c ... < /dev/null` smoke test). It comes from oh-my-zsh
  core, not from this config: it appears with `plugins=(<any single plugin>)`, and a bare
  `zsh -f -i` prints nothing. On a real PTY (E6) the output is completely clean. Harmless.
- **`j` resolves to fasd, not autojump.** Both plugins define a `j` jump alias; fasd is
  later in the list and wins. This is identical to the Mac's ordering, so it is mirrored
  deliberately. `autojump` remains available as the `autojump`/`j`-less command.

---

## 6. Follow-ups the user should know

1. **Non-interactive `ssh agilex '<cmd>'` now runs under zsh instead of bash**, because
   sshd uses the login shell from `/etc/passwd`. Both helper scripts are immune (they force
   `bash -c` explicitly), and `.zshrc` is interactive-only so it is never sourced by such
   commands. Anything that hand-rolls `ssh agilex '...'` with bash-only syntax should be
   checked.
2. **Shell startup is ~1.9 s** (`zsh -i -c true`: 1.97 / 1.89 / 1.98 s). Breakdown:
   ROS + 4 workspaces ≈ 1.2 s, conda hook ≈ 0.26 s, oh-my-zsh ≈ 0.19 s. This is inherent to
   the robot's existing ROS environment (bash pays the same cost) — not caused by zsh or the
   plugins. If it ever matters, lazy-loading the four workspace `setup.zsh` files is the
   lever.
3. **`fzf` is 0.29** (Ubuntu 22.04's version), so the modern `fzf --zsh` integration is
   unavailable; oh-my-zsh correctly falls back to the Debian path
   (`/usr/share/doc/fzf/examples/key-bindings.zsh`), which exists — fzf key bindings and
   completion work. A newer fzf would need a manual install.
4. **`sublime` is not available**; use `code`/`vim` for editing (the `vscode` agent owns VS
   Code Insiders).
5. `~/.zsh_aliases` holds two small extras (`sb`, `rosver`). `ll`/`la`/`l` were deliberately
   *not* redefined there, because oh-my-zsh's defaults are better than bash's.
