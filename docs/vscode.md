# VS Code Insiders on the AgileX robot — install report

**Task:** task-3 · **Owner:** vscode · **Robot:** `agilex@<robot-ip>` (Ubuntu 22.04.5 LTS, x86_64, kernel 6.8, 24 cores, 31 GiB RAM, 1.6 TB free)
**Status: ✅ SUCCESS — Insiders installed and coexisting with the pre-existing `code`. No fallback was needed.**

---

## 1. TL;DR

| Question | Answer |
|---|---|
| Route used | **Microsoft apt repo** (the preferred route). The `.deb` fallback was *not* needed. |
| Exact version installed | **`code-insiders` 1.142.0-1791566707** (amd64) → `code-insiders --version` reports **`1.142.0-insider`**, commit `dab4f45f6cbfbd55ff3de3ac3a328fb7d6d2f642` |
| What the pre-existing `code` is | **A genuine Microsoft `.deb` package** — `code` **1.102.2-1753187809** (stable channel). **Not** a snap, **not** a manual/loose binary. |
| Did the old `code` survive? | **Yes, completely untouched** — still `1.102.2`, still `ii`, still `/usr/bin/code`. |
| Was the network slow? | **No.** The Microsoft repo served the 240 MB `.deb` at **15.7 MB/s**. The feared China-campus/VPN slowdown did **not** materialise. |
| Anything broken? | **No.** One dependency (`socat`) was pulled in. |

Coexistence confirmed side by side:

```
code          -> 1.102.2            (stable, pre-existing, untouched)
code-insiders -> 1.142.0-insider    (newly installed)
```

---

## 2. Work item 1 — identification of the pre-existing `code`

This was the interesting part, and the initial recon hint was **misleading**. `dpkg -S /usr/bin/code` returns *nothing*, which looks like a manual/loose install. It is not — here is why:

`/usr/bin/code` is a **symlink created by the package's `postinst` script**, so it never appears in the package's file list, and therefore `dpkg -S` cannot attribute it. The package itself is perfectly well registered.

### 2.1 The package (authoritative)

```
$ dpkg -l | grep -E '^ii +code '
ii  code  1.102.2-1753187809  amd64  Code editing. Redefined.
```

```
$ dpkg -s code
Package: code
Status: install ok installed
Priority: optional
Section: devel
Installed-Size: 428060
Maintainer: Microsoft Corporation <vscode-linux@microsoft.com>
Architecture: amd64
Version: 1.102.2-1753187809
Replaces: visual-studio-code
Provides: visual-studio-code
Conflicts: visual-studio-code
Description: Code editing. Redefined.
Homepage: https://code.visualstudio.com/
```

`Maintainer: Microsoft Corporation <vscode-linux@microsoft.com>` + `Provides/Conflicts: visual-studio-code` + `Installed-Size: 428060` is the unmistakable signature of the official Microsoft `.deb`.

### 2.2 Why `dpkg -S /usr/bin/code` finds nothing

```
$ dpkg -S /usr/bin/code
dpkg-query: no path found matching pattern /usr/bin/code

$ grep -n '/usr/bin' /var/lib/dpkg/info/code.list
(no /usr/bin entries -> symlink made by postinst)

$ grep -n -E 'ln -s|/usr/bin' /var/lib/dpkg/info/code.postinst
6:# Symlink bin command to /usr/bin
7:rm -f /usr/bin/code
8:ln -s /usr/share/code/bin/code /usr/bin/code
13:update-alternatives --install /usr/bin/editor editor /usr/bin/code 0
```

**Conclusion:** the `postinst` does `rm -f /usr/bin/code; ln -s /usr/share/code/bin/code /usr/bin/code`. The symlink is a post-install artefact, hence invisible to `dpkg -S`. This is normal, correct behaviour for the Microsoft `code` package — **not** a broken or hand-rolled install.

### 2.3 It is not a snap

```
$ snap list
Name                       Version                         Rev    Tracking         Publisher   Notes
bare                       1.0                             5      latest/stable    canonical** base
core22                     20260225                       2411   latest/stable    canonical** base
gnome-42-2204              0+git.4982e7b-sdk0+git.69b626a  263    latest/stable/…  canonical** -
gtk-common-themes          0.1-81-g442e511                 1535   latest/stable/…  canonical** -
snap-store                 41.3-72-g80e7130                1216   latest/stable/…  canonical** -
snapd                      2.76.1                          27591  latest/stable    snapd
snapd-desktop-integration  0.9                             315    latest/stable/…  canonical** -

$ ls -la /snap/bin/code*
ls: cannot access '/snap/bin/code*': No such file or directory
```

No `code` snap. (This matters: a snap `code` would have required a completely different coexistence strategy.)

### 2.4 It is not a manual binary either

```
$ ls -la /usr/bin/code*
lrwxrwxrwx 1 root root    24 10月 13  2025 /usr/bin/code -> /usr/share/code/bin/code
-rwxr-xr-x 1 root root 14488 12月 16  2022 /usr/bin/codepage

$ file -L /usr/bin/code
/usr/bin/code: a /usr/bin/env sh script, ASCII text executable
```

`/usr/share/code/bin/code` is the stock Microsoft launcher script (the one that handles `VSCODE_IPC_HOOK_CLI` remote-cli redirection, the WSL warning, the root-user guard, and then `ELECTRON_RUN_AS_NODE=1 "$ELECTRON" "$CLI" "$@"`). Verbatim Microsoft content.

### 2.5 What it actually runs

```
$ code --version
1.102.2
c306e94f98122556ca081f527b466015e1bc37b0
x64

$ grep -E '"(nameShort|nameLong|quality)"' /usr/share/code/resources/app/product.json
	"nameShort": "Code",
	"nameLong": "Visual Studio Code",
	"quality": "stable"

$ ELECTRON_RUN_AS_NODE=1 /usr/share/code/code -e 'console.log(process.versions.electron)'
node=22.15.1 electron=35.6.0 arch=x64
```

### 2.6 One finding worth flagging to the user

The pre-existing `code` was installed from a **standalone `.deb` with no apt repository configured** — there was no `packages.microsoft.com` source on the machine before this task. Consequences:

* `code` (stable) has **never auto-updated** and would never have. It is 1.102.2 (built 2025-07-22), roughly 15 months behind the current stable line.
* **It still will not**, unless the user asks for it. I did **not** upgrade it — explicitly out of scope, and the task forbids touching it.
* The `vscode.list` repo I added **also carries the `code` (stable) package**, so if the user ever wants stable to start tracking upstream, the source is now in place: `apt-get install --only-upgrade code` would do it. **I did not run this.**

Also: `code` is **not** held (`apt-mark showhold` is empty), so nothing is pinning it.

---

## 3. Work item 2 — installation (Microsoft apt repo route)

### 3.1 Decision process

Before choosing, I measured the network rather than assuming. Both candidate sources were fast:

| Source | Measured throughput (12 s capped probe) |
|---|---|
| `packages.microsoft.com/repos/code/pool/...` (apt repo pool) | **14.4 MB/s** |
| `vscode.download.prss.microsoft.com` (CDN, `.deb` fallback) | **18.5 MB/s** |

The apt repo was comfortably fast, so I used the **preferred apt route** and never needed the `.deb` fallback. Had the repo been slow, the plan was: download from the CDN to `/tmp` (outside the rsudo mutex, as user `agilex`), verify SHA-256 against the repo's own metadata, then `apt-get install -y ./file.deb` — same result, still dependency-correct. It was not required.

### 3.2 Step 1 — signing key

```
$ curl -fsSL https://packages.microsoft.com/keys/microsoft.asc   # 975 bytes
$ gpg --batch --yes --dearmor -o /etc/apt/keyrings/packages.microsoft.gpg ...
$ ls -la /etc/apt/keyrings/packages.microsoft.gpg
-rw-r--r-- 1 root root 640 10月 10 21:53 /etc/apt/keyrings/packages.microsoft.gpg
```

**Key fingerprint (verified as Microsoft's genuine release-signing key):**

```
$ gpg --show-keys --with-fingerprint /etc/apt/keyrings/packages.microsoft.gpg
pub   rsa2048 2015-10-28 [SC]
      BC52 8686 B50D 79E3 39D3  721C EB3E 94AD BE12 29CF
uid                      Microsoft (Release signing) <gpgsecurity@microsoft.com>
```

`BC52 8686 B50D 79E3 39D3 721C EB3E 94AD BE12 29CF` is the well-known Microsoft package-signing fingerprint — the key is authentic.

`/etc/apt/keyrings/` existed but was **empty** before this task, so there was no key collision.

### 3.3 Step 2 — apt source

File `/etc/apt/sources.list.d/vscode.list` (new; no `vscode*.list` existed before):

```
deb [arch=amd64,arm64,armhf signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main
```

This is Microsoft's documented line, verbatim.

I ran a **targeted** update so the other agents' mirrors (`mirrors.bwbot.org`, `mirrors.ustc.edu.cn`) were not disturbed:

```
$ apt-get update -o Dir::Etc::sourcelist="sources.list.d/vscode.list" \
                 -o Dir::Etc::sourceparts="-" -o APT::Get::List-Cleanup="0"
Get:1 https://packages.microsoft.com/repos/code stable InRelease [3,590 B]
Get:2 https://packages.microsoft.com/repos/code stable/main amd64 Packages [30.6 kB]
Get:3 https://packages.microsoft.com/repos/code stable/main arm64 Packages [30.4 kB]
Get:4 https://packages.microsoft.com/repos/code stable/main armhf Packages [30.1 kB]
Fetched 94.6 kB in 1s (115 kB/s)
Reading package lists...
apt-get update exit=0
```

### 3.4 Step 3 — install

```
$ apt-get install -y code-insiders
The following additional packages will be installed:
  socat
The following NEW packages will be installed:
  code-insiders socat
0 upgraded, 2 newly installed, 0 to remove and 1 not upgraded.
Need to get 240 MB of archives.
After this operation, 1,017 MB of additional disk space will be used.
Get:1 https://mirrors.ustc.edu.cn/ubuntu jammy/main amd64 socat amd64 1.7.4.1-3ubuntu4 [349 kB]
Get:2 https://packages.microsoft.com/repos/code stable/main amd64 code-insiders amd64 1.142.0-1791566707 [240 MB]
Preconfiguring packages ...
Fetched 240 MB in 15s (15.7 MB/s)
Selecting previously unselected package code-insiders.
Preparing to unpack .../code-insiders_1.142.0-1791566707_amd64.deb ...
Unpacking code-insiders (1.142.0-1791566707) ...
Selecting previously unselected package socat.
Unpacking socat (1.7.4.1-3ubuntu4) ...
Setting up code-insiders (1.142.0-1791566707) ...
Setting up socat (1.7.4.1-3ubuntu4) ...
Processing triggers for mailcap (3.70+nmu1ubuntu1.22.04.1) ...
Processing triggers for desktop-file-utils (0.26-1ubuntu3) ...
Processing triggers for gnome-menus (3.36.0-1ubuntu3) ...
Processing triggers for man-db (2.10.2-1) ...
Processing triggers for shared-mime-info (2.1-2) ...
APT_INSTALL_EXIT=0
```

**240 MB in 15 seconds.** The `.deb` SHA-256 published by the repo for this exact version is `a4b00c51483152d1bdb56cd925ce4412a103349c95269a9358a61ad14ec17d73`, and `apt` verified it against `InRelease`/`Release.gpg` automatically as part of the install.

Only one extra dependency was pulled: **`socat` 1.7.4.1-3ubuntu4** (349 kB, from the USTC mirror) — required by the Insiders CLI tunnel feature. `0 upgraded, 0 to remove`; the "1 not upgraded" is an unrelated pre-existing package that apt deliberately left alone.

**No `apt-get upgrade` / `dist-upgrade` / `do-release-upgrade` / `reboot` was run at any point.**

---

## 4. Work item 3 — verification

### 4.1 Required evidence

```
$ code-insiders --version
1.142.0-insider
dab4f45f6cbfbd55ff3de3ac3a328fb7d6d2f642
x64
EXIT=0

$ code-insiders --list-extensions
EXIT=0            <- empty list, proves the CLI runs end-to-end

$ code-insiders --help | head -5
Visual Studio Code - Insiders 1.142.0-insider

Usage: code-insiders [options] [paths...]

To read from stdin, append '-' (e.g. 'ps aux | grep code | code-insiders -')
```

```
$ which -a code code-insiders
/usr/bin/code
/bin/code
/usr/bin/code-insiders
/bin/code-insiders
```

```
$ dpkg -l | grep -E '^ii +(code|code-insiders) '
ii  code                                                     1.102.2-1753187809                               amd64        Code editing. Redefined.
ii  code-insiders                                            1.142.0-1791566707                               amd64        Code editing. Redefined.
```

```
$ ls -la /usr/bin/code-insiders
lrwxrwxrwx 1 root root 42 10月 10 21:55 /usr/bin/code-insiders -> /usr/share/code-insiders/bin/code-insiders
```

```
$ apt-cache policy code-insiders
code-insiders:
  Installed: 1.142.0-1791566707
  Candidate: 1.142.0-1791566707
  Version table:
 *** 1.142.0-1791566707 500
        500 https://packages.microsoft.com/repos/code stable/main amd64 Packages
        100 /var/lib/dpkg/status
```

Installed == candidate, sourced from the Microsoft repo → a plain `apt-get update && apt-get install code-insiders` will pick up future Insiders builds automatically.

### 4.2 The old `code` is provably untouched

```
$ code --version
1.102.2
c306e94f98122556ca081f527b466015e1bc37b0
x64
EXIT=0
```

Same version, same commit as before the install. `dpkg -l code` still `ii`, same `1.102.2-1753187809`. `update-alternatives --query editor` still resolves to `/bin/nano` (the `postinst`'s `--install ... editor ... 0` registration did not steal the default) — so **no side effects on the rest of the system**.

### 4.3 Beyond the CLI: proof the app will actually start

The task's headless bar was `--version` + `--list-extensions`; both pass. I added stronger checks because `--version` runs the CLI shim (`ELECTRON_RUN_AS_NODE=1`), not the real GUI shell:

```
$ file /usr/share/code-insiders/code-insiders
ELF 64-bit LSB pie executable, x86-64, dynamically linked, interpreter /lib64/ld-linux-x86-64.so.2

$ ldd /usr/share/code-insiders/code-insiders | grep -c 'not found'
0                              <- every shared library resolves; GUI has no missing deps

$ ELECTRON_RUN_AS_NODE=1 /usr/share/code-insiders/code-insiders -e '...'
node=24.21.0 electron=43.7.7 v8=15.0.245.31-electron.0 arch=x64
EXIT=0                         <- the bundled 224 MB Electron runtime genuinely executes
```

```
$ grep -E '"(nameShort|nameLong|quality)"' /usr/share/code-insiders/resources/app/product.json
	"nameShort": "Code - Insiders",
	"nameLong": "Visual Studio Code - Insiders",
	"quality": "insider"        <- correct channel, not a repackaged stable
```

GNOME launcher integration is present and correct:

```
$ ls -la /usr/share/applications/ | grep -i code
-rwxrwxr-x 1 root root 738  7月 22  2025 code.desktop
-rw-rw-r-- 1 root root 312  7月 22  2025 code-url-handler.desktop
-rwxrwxr-x 1 root root 836 10月 10 01:20 com.microsoft.VSCodeInsiders.desktop
-rw-rw-r-- 1 root root 359 10月 10 01:20 com.microsoft.VSCodeInsiders.UrlHandler.desktop

$ grep -E '^(Exec|Name|Icon)=' /usr/share/applications/com.microsoft.VSCodeInsiders.desktop
Name=Visual Studio Code - Insiders
Exec=/usr/share/code-insiders/code-insiders %F
Icon=vscode-insiders
```

Icon `/usr/share/pixmaps/vscode-insiders.png` present; bash completion at `/usr/share/bash-completion/completions/code-insiders`; `serve-web` and `tunnel` subcommands available.

Both installs are self-contained and independent:

```
$ du -sh /usr/share/code /usr/share/code-insiders
424M	/usr/share/code
975M	/usr/share/code-insiders
```

---

## 5. Notes, caveats, and honest caveats

1. **`chrome-sandbox` is mode 0755 on Insiders, but 4755 (setuid) on the old `code`.** I investigated rather than blindly copying the old mode:
   * Neither package's `postinst` touches the sandbox mode.
   * `dpkg --verify code-insiders` and `dpkg --verify code` both exit 0 → **no drift from either package's shipped state**. 0755 is simply what this newer Insiders build ships; 4755 is what the older stable build shipped.
   * Modern Electron uses the **unprivileged user-namespace sandbox**, which I confirmed is available and working:
     ```
     kernel.unprivileged_userns_clone = 1
     kernel.apparmor_restrict_unprivileged_userns = 0
     $ unshare --user --map-root-user echo "userns OK"
     userns OK
     ```
   * **Therefore no change was made**, and none is needed. I deliberately did **not** set the setuid bit — it is unnecessary here and would add a setuid-root binary for no benefit. *If* a future GUI launch ever complains about the sandbox, the fix is `sudo chmod 4755 /usr/share/code-insiders/chrome-sandbox`.
2. **GUI launch was not smoke-tested under a real X server.** `xvfb` is not installed on the robot and installing it is outside my write scope, so I did not. `DISPLAY` is unset over SSH; the robot has an active GNOME session (`session 12, user agilex, seat0/tty2`) where the launcher will appear. The evidence above (0 unresolved libraries, working userns, valid Electron runtime, correct `.desktop` entry) is as far as headless proof can go without touching the user's desktop session.
3. **`socat` was installed** as an Insiders dependency. It is a normal, benign package and is not related to any other agent's scope.
4. **The Microsoft repo is NOT slow or blocked from this network** — contrary to the task's concern. It served 240 MB at 15.7 MB/s over the VPN. The `.deb` fallback was never needed.
5. **Scope compliance:** I touched only `/etc/apt/keyrings/packages.microsoft.gpg`, `/etc/apt/sources.list.d/vscode.list`, `/usr/share/code-insiders/`, `/usr/bin/code-insiders` (created by the package's own `postinst`), and apt's own cache. **Nothing** under `~/.zshrc`, `~/.bashrc`, `/home/linuxbrew`, `/opt/ros`, `~/piper_ros`, or `~/agilex_ws` was read or written. All root work went through `bin/rsudo.sh`; all unprivileged work through `bin/rrun.sh`; no hand-rolled `ssh`/`sudo` was used. No stray files were left in `/tmp` (the apt route downloads to apt's own cache, so no temp `.deb` was ever created).
6. **The pre-existing `code` (stable 1.102.2) will still not auto-update** unless the user explicitly asks — see §2.6. I left it exactly as found, as instructed.

---

## 6. Reproduce / operate

```bash
# on the robot — both editors coexist
code --version            # 1.102.2            (stable, pre-existing)
code-insiders --version   # 1.142.0-insider    (new)

# update Insiders to the newest build (safe; never touches `code`)
sudo apt-get update && sudo apt-get install --only-upgrade code-insiders

# launch the Insiders GUI on the robot's GNOME desktop
code-insiders
```

Local commands used (from `agilex-setup/`):

```bash
./bin/rrun.sh  -c 'code-insiders --version'     # unprivileged, as agilex
./bin/rsudo.sh -c 'apt-get install -y code-insiders'   # root, mutex-protected
```
