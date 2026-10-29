# task-2 — Homebrew + gcc/llvm/python toolchain on AgileX robot

**Agent:** `toolchain` · **Robot:** `agilex@<robot-ip>` (Ubuntu 22.04.5 LTS, x86_64, 24 cores, 31 GB RAM, 1.6 TB free)
**Status:** ✅ **COMPLETE** — Homebrew installed and working, newer gcc + clang/llvm installed, python3 venv working, shell contract honoured. 3 honest caveats below.
**Tooling:** all root work via `bin/rsudo.sh`, unprivileged via `bin/rrun.sh`. No hand-rolled ssh/sudo. No `apt-get upgrade` / `dist-upgrade` / `do-release-upgrade` / reboot was ever run by me.

---

## 1. TL;DR

| Item | Result |
|---|---|
| Homebrew | **Homebrew 7.0.9** at `/home/linuxbrew/.linuxbrew` (standard prefix, owned by `agilex`, no sudo needed for `brew install`) |
| brew proof formulae | `cmake 4.4.4`, `ninja 1.13.2`, `wget 1.25.0` — all run correctly |
| gcc | System default **UNCHANGED at gcc/g++ 11.4.0**; added the missing **g++-12 12.3.0** (gcc-12 was already present) |
| clang/llvm | **clang/clang++ 14.0.0**, **llvm-config 14.0.0**, **clangd**, **clang-format**, **lld/ld.lld 14.0.0**, **clang-tidy 14.0.0** (all apt, jammy) |
| python | **Python 3.10.12** unchanged; `python3-venv` installed — `python3 -m venv` now genuinely works (it was **broken before** me, see §6) |
| Shell contract | `/home/agilex/.dsh/brew-env.sh` created; **exactly one** guarded line appended to `.bashrc` (diff = `147a148`) |
| ROS2 impact | **No toolchain regression.** One residual, proven risk: brew's `bin` shadows 123 system tool names (incl. binutils) when sourced — §9 |

---

## 2. Homebrew

### 2.1 Result

```
$ brew --version
Homebrew 7.0.9
$ brew --prefix
/home/linuxbrew/.linuxbrew
$ brew --cellar
/home/linuxbrew/.linuxbrew/Cellar
$ brew --repository
/home/linuxbrew/.linuxbrew/Homebrew
```

* Prefix `drwxr-xr-x agilex agilex /home/linuxbrew` → **owned by the `agilex` user on purpose**, so every future `brew install` works without `sudo`. Location is the standard Linuxbrew multi-user prefix (not `~/.linuxbrew`), which is what the task asked for.
* Repository on branch `stable` at tag **7.0.9**, HEAD `cdc1ca5032`.
* Git origin: **`https://github.com/Homebrew/brew` (official)** — `brew update` verified working (5 s, `Already up-to-date.`).

### 2.2 `brew list --versions` (raw)

```
binutils 2.47                 lz4 1.10.0                readline 8.3.6
ca-certificates 2026-09-25    mpfr 4.2.2                sqlite 3.53.4
cmake 4.4.4                   ncurses 6.6               util-linux 2.42.4
gcc 16.2.0                    ninja 1.13.2              wget 1.25.0_2
glibc 2.39_1                  openssl@4 4.0.3           xz 5.8.4
gmp 6.3.0                     zlib-ng-compat 2.3.3_1    zstd 1.5.7_1
isl 0.28                      libidn2 2.3.8             libmpc 1.4.1
libpsl 0.23.3                 libunistring 1.4.2        libxcrypt 4.5.2
linux-headers@6.8 6.8.12_1
```

Proof formulae work:
```
cmake version 4.4.4
1.13.2                     (ninja)
GNU Wget 1.25.0 built on linux-gnu.
```

**Bonus:** brew pulled in **`gcc 16.2.0`** as a dependency of its `glibc` formula. It is exposed only as **`gcc-16` / `g++-16` / `c++-16`** — there is **no unversioned `gcc`/`g++` in brew's bin**, so it cannot shadow the ROS2 compiler.

### 2.3 The install was hard — full honest account

The official installer **failed repeatedly** against GitHub. This was the single biggest obstacle and it is worth recording precisely.

**What failed.** `git fetch` of the full Homebrew/brew history (a ~154 MB pack) died every time near the *end* of the transfer:

```
error: RPC failed; curl 56 GnuTLS recv error (-54): Error in the pull function.
error: 8202 bytes of body are still expected
fetch-pack: unexpected disconnect while reading sideband packet
fatal: early EOF
fatal: fetch-pack: invalid index-pack output
Warning: Trying again in 2 seconds: /usr/bin/git fetch --quiet --force origin
```

The installer does `retry 5 git fetch --force origin` **and** `retry 5 git fetch --force --tags origin` (install.sh lines 1029–1030), then gives up. Inspection showed `.git/objects/pack/` full of **abandoned `tmp_pack_*` files totalling 183 MB from 7 failed attempts** — it was never converging, it was just burning bandwidth and leaving garbage.

Small GitHub requests were always fine (`git ls-remote` OK, 366 KB fetched in 1.3 s). So **"GitHub is reachable" was a false signal**: the link cannot sustain a multi-hundred-MB single pack.

**What I tried first (and did not work).** The standard cure for GnuTLS `-54` / early-EOF is HTTP/1.1 + relaxed low-speed aborts. I applied it **via environment only** (`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`, i.e. *no* `~/.gitconfig` write, because that file is outside my write scope):

```
GIT_CONFIG_KEY_0=http.version       GIT_CONFIG_VALUE_0=HTTP/1.1
GIT_CONFIG_KEY_1=http.postBuffer    GIT_CONFIG_VALUE_1=524288000
GIT_CONFIG_KEY_2=http.lowSpeedLimit GIT_CONFIG_VALUE_2=0
GIT_CONFIG_KEY_3=http.lowSpeedTime  GIT_CONFIG_VALUE_3=999999
```

It **still failed identically.** I therefore did *not* set `core.compression 0` (suggested to me): it cannot shrink a server-generated pack and would only waste CPU, so it would not have helped.

**What worked.** A full clone from the **USTC mirror** completed in **12 seconds** (154 MB). USTC is the same university mirror the robot already trusts for `apt`. I then verified authenticity by SHA rather than trusting the mirror:

```
USTC clone HEAD : 16f166d36cddcb485be5a3f2ed5b25dc445d1580
GitHub refs/heads/main : 16f166d36cddcb485be5a3f2ed5b25dc445d1580   <-- exact match
53107 commits, 541 tags, HEAD dated 2026-10-10 (same day)
```

**An important correction to a wrong turn I made.** I initially compared the mirror's HEAD against GitHub's `master` and got a mismatch, which looked alarming. Investigating showed **Homebrew/brew's default branch is `main`, not `master`** — GitHub's `HEAD` symref is `refs/heads/main`, and `master` is a leftover stub (`67984c7 Merge pull request #23733 from Homebrew/legacy-master-bootstrap`, a 180 KB / 6-object branch). Once compared against `main`, the mirror is byte-identical. Worth knowing for anyone else touching this repo.

I then seeded that authentic clone into `/home/linuxbrew/.linuxbrew/Homebrew`, so the installer's two fetches became **no-ops** instead of a doomed 154 MB transfer, and ran the **official installer** (`install.sh`, 35651 bytes, unmodified) with the mirror as remote:

```
NONINTERACTIVE=1 CI=1 HOMEBREW_NO_AUTO_UPDATE=1 \
HOMEBREW_BREW_GIT_REMOTE="https://mirrors.ustc.edu.cn/brew.git" \
HOMEBREW_CORE_GIT_REMOTE="https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/homebrew-core.git" \
HOMEBREW_BOTTLE_DOMAIN="https://mirrors.ustc.edu.cn/homebrew-bottles" \
bash /home/linuxbrew/install.sh
```

→ `==> Installation successful!` and `bin/brew` symlink created.

**Afterwards I restored the official remote** and proved it works:

```
$ git -C /home/linuxbrew/.linuxbrew/Homebrew remote set-url origin https://github.com/Homebrew/brew
$ brew update
==> Updating Homebrew...
Already up-to-date.          (5 s)
```

This is the key insight: only the **initial full-history clone** was unviable. **Incremental** GitHub updates are small and work fine, so the installed Homebrew ends up with the **standard official origin** and a clean `brew doctor` origin check.

### 2.4 `brew doctor` — reported honestly

```
Warning: Unbrewed header files were found in /usr/local/include.
  /usr/local/include/libuvc/*
  /usr/local/include/livox_lidar_api.h
  /usr/local/include/livox_lidar_cfg.h
  /usr/local/include/livox_lidar_def.h
Warning: Unbrewed '.pc' files were found in /usr/local/lib/pkgconfig.
  /usr/local/lib/pkgconfig/libuvc.pc
Warning: Unbrewed static libraries were found in /usr/local/lib.
  /usr/local/lib/liblivox_lidar_sdk_static.a
  /usr/local/lib/libuvc.a
Warning: Your system glibc 2.35 is too old.
We will need to automatically install a newer version.
```

* The `/usr/local` "unbrewed" warnings are **pre-existing** (libuvc / Livox LiDAR SDK from the robot's own setup). **Not caused by me**, and I did not delete anything.
* The **glibc warning is real and unresolved by design**: Homebrew 7.x bottles want a newer glibc than jammy's 2.35. Homebrew's mitigation is to install **its own `glibc 2.39_1` + `linux-headers@6.8` inside the prefix**, which it did. This is confined to `/home/linuxbrew` and **does not touch system glibc** (verified: `libc6 2.35-0ubuntu3.15`, `ldd /usr/bin/gcc → /lib/x86_64-linux-gnu/libc.so.6`).
* Origin warning → **resolved** (official origin restored).
* `sbin` PATH warning → **resolved** (`brew-env.sh` includes `/home/linuxbrew/.linuxbrew/sbin`).

### 2.5 portable-ruby (a partial-mirror trap)

`brew update` tried the USTC bottle mirror first and got a **404**:

```
==> Downloading https://mirrors.ustc.edu.cn/homebrew-bottles/portable-ruby-4.0.7.x86_64_linux.bottle.tar.gz
curl: (22) The requested URL returned error: 404
==> Downloading https://ghcr.io/v2/homebrew/core/portable-ruby/blobs/sha256:bf2a9bf1...
######################################################################## 100.0%
==> Pouring portable-ruby-4.0.7.x86_64_linux.bottle.tar.gz
```

Homebrew **automatically fell back to the official `ghcr.io`** and succeeded, so the install is fine. But it means **the USTC bottle mirror is incomplete**. If a future `brew install` 404s, unset `HOMEBREW_BOTTLE_DOMAIN` (or drop the mirror) — see §10.

---

## 3. GCC — system default deliberately untouched

```
$ gcc --version
gcc (Ubuntu 11.4.0-1ubuntu1~22.04.3) 11.4.0      <-- still the default, as required
$ g++ --version
g++ (Ubuntu 11.4.0-1ubuntu1~22.04.3) 11.4.0
$ ls /usr/bin/gcc-*
/usr/bin/gcc-11  /usr/bin/gcc-12  /usr/bin/gcc-ar  /usr/bin/gcc-ar-11
/usr/bin/gcc-ar-12  /usr/bin/gcc-nm  /usr/bin/gcc-nm-11  /usr/bin/gcc-nm-12
/usr/bin/gcc-ranlib  /usr/bin/gcc-ranlib-11  /usr/bin/gcc-ranlib-12
$ gcc-12 --version ; g++-12 --version
gcc-12 (Ubuntu 12.3.0-1ubuntu1~22.04.3) 12.3.0
g++-12 (Ubuntu 12.3.0-1ubuntu1~22.04.3) 12.3.0
```

* **gcc-12 12.3.0 was already installed** on the robot; what was **missing was `g++-12`**, which I installed. Both are now present.
* **gcc-13/g++-13 do not exist in the jammy repos** (`Candidate: <none>`) — deliberately not installed. 12.3.0 is the newest apt gcc available; brew's `gcc-16` covers "newer than 12" if ever needed.
* **`update-alternatives` untouched** — `/etc/alternatives/cc → /usr/bin/gcc`, `/etc/alternatives/c++ → /usr/bin/g++`, i.e. still gcc-11. ROS2 Humble's ABI is safe.

---

## 4. LLVM / clang (all from the jammy apt repo)

```
Ubuntu clang version 14.0.0-1ubuntu1.1        (/usr/bin/clang, /usr/bin/clang++)
Ubuntu clangd version 14.0.0-1ubuntu1.1       (/usr/bin/clangd)
Ubuntu clang-format version 14.0.0-1ubuntu1.1 (/usr/bin/clang-format)
14.0.0                                        (llvm-config --version)
Ubuntu LLD 14.0.0 (compatible with GNU linkers)  (ld.lld --version)
Ubuntu LLVM version 14.0.0                    (/usr/bin/clang-tidy-14)
```

Installed apt packages (all `1:14.0.0-1ubuntu1.1` unless noted):

```
clang-14  llvm-14  lld-14  clangd-14  clang-format-14  clang-tools-14  clang-tidy-14
(+ metapackages clang 1:14.0-55~exp2, llvm 1:14.0-55~exp2, lld 1:14.0-55~exp2,
 clangd, clang-format, llvm-runtime, llvm-14-tools, libclang-cpp14, llvm-14-dev)
```

* **14.0.0 is what jammy ships** — this is the exact version the task asked me to report.
* `lld` is present but is a **generic driver** (it prints "lld is a generic driver… invoke ld.lld"); use **`ld.lld`** for actual linking. Reported because `lld --version` looks like a failure and isn't.
* **`clang-tools-14` does NOT contain `clang-tidy`** on jammy — `clang-tidy` lives in its own `clang-tidy-14` package, which I installed. Note the binary is **versioned**: `clang-tidy-14` (there is no unversioned `/usr/bin/clang-tidy`).
* **No competing brew LLVM was installed**, per instructions: apt's clang 14 is perfectly usable and brew's `llvm` is multi-GB — the wrong trade on a jittery link.

---

## 5. Python

```
$ python3 --version
Python 3.10.12
$ readlink -f /usr/bin/python3
/usr/bin/python3.10                <-- not repointed
$ python3 -m venv --help | head -1
usage: venv [-h] [--system-site-packages] [--symlinks | --copies] [--clear]
$ /home/agilex/miniforge3/bin/python --version
Python 3.12.11                     <-- miniforge untouched
```

`python3-dev 3.10.6-1~22.04.1` and `python3-pip 22.0.2+dfsg-1ubuntu0.7` were already present; I added:

```
python3-venv      3.10.6-1~22.04.1
python3.10-venv   3.10.12-1~22.04.18
```

### 5.1 `python3 -m venv` was actually BROKEN before I started

This is worth flagging, because `python3 -m venv --help` *succeeded* and would have fooled a shallower check:

```
$ python3 -m venv --help      -> "venv OK"     (venv module present)
$ python3 -c "import ensurepip"
ModuleNotFoundError: No module named 'ensurepip'   <-- venv creation would have FAILED
```

Installing `python3-venv` fixed it (`ensurepip OK 22.0.2`). **Real create + activate + install smoke test:**

```
$ python3 -m venv /tmp/venvsmoke2 && . /tmp/venvsmoke2/bin/activate
activated: Python 3.10.12 at /tmp/venvsmoke2/bin/python3
pip: pip 22.0.2 from /tmp/venvsmoke2/lib/python3.10/site-packages/pip (python 3.10)
$ python3 -m pip install six
$ python3 -c "import six; print(six.__version__)"
1.17.0                     <-- real package installed into the venv
$ deactivate
after deactivate python3: /usr/bin/python3 Python 3.10.12   <-- cleanly restored
```

**Pre-existing behaviour I did NOT change:** in an *interactive* shell, `python3` resolves to `/home/agilex/miniforge3/bin/python3` (3.12.11) because `.bashrc` has a `conda initialize` block that prepends miniforge. In non-interactive and login shells it is `/usr/bin/python3` (3.10.12). I left this exactly as it was — it is the robot's own conda setup, not mine to alter.

---

## 6. Shell integration (the contract with `shell-env`)

### 6.1 `/home/agilex/.dsh/brew-env.sh` — created (470 bytes, `agilex:agilex`)

Contains the `brew shellenv` output:

```sh
export HOMEBREW_PREFIX="/home/linuxbrew/.linuxbrew";
export HOMEBREW_CELLAR="/home/linuxbrew/.linuxbrew/Cellar";
export HOMEBREW_REPOSITORY="/home/linuxbrew/.linuxbrew/Homebrew";
export PATH="/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin${PATH+:$PATH}";
[ -z "${MANPATH-}" ] || { export MANPATH="${MANPATH%"${MANPATH##*[!:]}"}"; export MANPATH=":${MANPATH#"${MANPATH%%[!:]*}"}"; };
export INFOPATH="/home/linuxbrew/.linuxbrew/share/info:${INFOPATH:-}";
```

**Deliberate choice:** generated with `brew shellenv bash` (bash syntax), **not** bare `brew shellenv`. The user's login shell is `zsh`, so bare `brew shellenv` could have emitted zsh-only syntax such as `fpath[1,0]=…`, which **errors when sourced by bash**. Bash-syntax output is safe in **both** bash and zsh, which matters because `shell-env` sources this same file from `.zshrc`. Verified sourcing it puts brew on PATH while leaving `gcc` and `python3` on the system ones:

```
$ bash -c 'source /home/agilex/.dsh/brew-env.sh; command -v brew; command -v gcc; command -v python3'
/home/linuxbrew/.linuxbrew/bin/brew
/usr/bin/gcc
/usr/bin/python3
```

### 6.2 `/home/agilex/.bashrc` — exactly ONE line appended

Appended line 148, byte-exact:

```
[ -f "$HOME/.dsh/brew-env.sh" ] && source "$HOME/.dsh/brew-env.sh"
```

Verified it is the *only* change:

```
$ diff /home/linuxbrew/bashrc.before-toolchain.bak /home/agilex/.bashrc
147a148
> [ -f "$HOME/.dsh/brew-env.sh" ] && source "$HOME/.dsh/brew-env.sh"
```

* `147a148` = **one added line, nothing else** — no modifications, no deletions.
* `.bashrc` had 147 lines before, 148 after; it ended with a newline, so nothing was corrupted.
* `grep -c brew-env` → **1**.
* `bash -n .bashrc` → **syntax OK**.
* Backup kept **inside my own write scope** at `/home/linuxbrew/bashrc.before-toolchain.bak`
  (sha256 `544203490852ff54d0d244b72651e51b65e97d0580f60ea653996596949f190a`) so the Lead can diff the final integration edit.

### 6.3 Login-shell resolution — honest result

```
user's login shell: /usr/bin/zsh

1) bash -lc  'command -v brew' -> []                                   <-- EMPTY
2) bash -lic 'command -v brew' -> /home/linuxbrew/.linuxbrew/bin/brew   <-- works
3) bash -c 'source ~/.dsh/brew-env.sh; command -v brew' -> /home/linuxbrew/.linuxbrew/bin/brew
4) .zshrc references brew-env.sh? -> 1                                  <-- shell-env honoured the contract
5) zsh -lic 'command -v brew' -> /home/linuxbrew/.linuxbrew/bin/brew    <-- the REAL login shell works
```

**The required evidence line `bash -lc 'command -v brew'` returns empty, and I could not make it pass without breaking the task's own contract.** The reason is a stock Ubuntu guard at the very top of `.bashrc` (lines 5–9):

```sh
# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac
```

`.profile` does source `.bashrc` for login shells, but a **non-interactive** login shell hits that `return` before ever reaching line 148. Fixing it would require either editing that guard or adding a `.bash_profile` — both outside my write scope (append-only `.bashrc`, and exactly one line). I am reporting the real behaviour rather than faking the check.

**Practically, brew resolves correctly in every shell a human actually uses:** the user's real login shell is zsh, `.zshrc` already sources `brew-env.sh` (confirmed, count = 1), and `zsh -lic 'command -v brew'` and `bash -lic 'command -v brew'` both return the brew path. If the Lead wants `bash -lc` to work too, a `~/.bash_profile` is the correct place — that is a deliberate integration decision for the Lead, not something I should have done unilaterally.

---

## 7. Write-scope compliance

| Path | Action |
|---|---|
| `/home/linuxbrew/` | ✅ created/written (prefix, logs, backup) |
| `/home/agilex/.dsh/brew-env.sh` | ✅ created (also created `~/.dsh` itself) |
| `/home/agilex/.bashrc` | ✅ **append only** — one line, `147a148` |
| `reports/toolchain.md` | ✅ this file |
| `/home/agilex/.zshrc`, `/home/agilex/.oh-my-zsh` | ❌ **never touched** (owned by `shell-env`) |
| `/home/agilex/.dsh/cuda-env.sh` | ❌ never touched (owned by `nvidia-cuda`) |
| `.bashrc` lines 138–139 (`/usr/local/cuda-11.8` exports) | ❌ **left byte-for-byte untouched**, still at lines 138–139, as the Lead instructed |

`~/.gitconfig` was also **not** written: I used `GIT_CONFIG_*` env vars instead, to stay inside scope.

---

## 8. What I deliberately skipped, and why

| Skipped | Why |
|---|---|
| `gcc-13` / `g++-13` | Not in the jammy repos (`Candidate: <none>`). 12.3.0 is the newest apt gcc; brew's `gcc-16` is available if newer is wanted. |
| brew's `llvm` | Multi-GB download; apt's clang/llvm 14 is complete and usable. Wrong trade on this link. |
| brew's `python` | Would risk shadowing the system python that ROS2 uses, and it isn't needed — the apt venv works. |
| brew's `gcc` as a *requested* formula | Arrived anyway as a `glibc` dependency (`gcc 16.2.0`), exposed only as versioned `gcc-16`, so it cannot shadow system gcc. |
| `apt-get upgrade` / `dist-upgrade` / `do-release-upgrade` / reboot | Forbidden by the task. **Verified I ran none** (§11). |
| Fixing the broken CUDA exports in `.bashrc` | Explicitly the Lead's integration edit, not mine. |

---

## 9. Risk to the ROS2 setup

### 9.1 What is safe (verified, not assumed)

* **System compilers preserved.** `gcc`/`g++` → `/usr/bin/gcc`, `/usr/bin/g++` (11.4.0) even with brew fully on PATH. No unversioned brew `gcc`/`g++`/`cc`/`c++` exists.
* **System python preserved.** `python3` → `/usr/bin/python3` (3.10.12) with brew on PATH. Brew installs no `python3`.
* **System clang preserved.** `clang` → `/usr/bin/clang` (14.0.0); brew ships no clang.
* **System glibc untouched.** `libc6 2.35-0ubuntu3.15`; brew's `glibc 2.39_1` lives entirely under `/home/linuxbrew` and is only used by brew-linked binaries.
* **`update-alternatives` untouched** — `cc`/`c++` still resolve to gcc-11, which is what ROS2 Humble was built against.
* No `apt` transaction of mine upgraded or removed anything (§11).

### 9.2 The one real residual risk: brew's `bin` shadows 123 system tool names

`brew shellenv` **prepends** `/home/linuxbrew/.linuxbrew/bin`. A full enumeration of brew's `bin` against `/usr/bin` found **123 shadowed names**, the consequential ones being:

* **binutils 2.47** — `as`, `ld`, `ld.bfd`, `ar`, `nm`, `objdump`, `objcopy`, `strip`, `readelf`, `ranlib`, `size`, `strings`, `gprof`, `elfedit`, `addr2line`, `c++filt`
* **util-linux 2.42.4** — `mount`, `umount`, `dmesg`, `lsblk`, `lscpu`, `taskset`, `setsid`, `flock`, `nsenter`, `unshare`, `script`, `whereis`, `getopt`, `ionice`, `renice`, `uuidgen`, …
* **others** — `openssl`, `xz`/`zstd` (+ all their aliases), `wget`, `cmake`, `ncurses` tools (`clear`, `tput`, `tic`, `infocmp`, …)

**This is proven, not theoretical:**

```
no brew  : as=/usr/bin/as                          ld=/usr/bin/ld
with brew: as=/home/linuxbrew/.linuxbrew/bin/as    ld=/home/linuxbrew/.linuxbrew/bin/ld

shim test: gcc-11 invoked a PATH-shimmed 'as' 1 time  => gcc resolves 'as' via PATH
system as: GNU assembler (GNU Binutils for Ubuntu) 2.38
brew   as: GNU assembler (GNU Binutils) 2.47.20260726
system ld: GNU ld (GNU Binutils for Ubuntu) 2.38
brew   ld: GNU ld (GNU Binutils) 2.47.20260726
```

So **in a brew-sourced shell, a gcc-11 compile uses brew's binutils 2.47 instead of Ubuntu's 2.38.**

**Assessment: low-to-moderate.** binutils is independent of gcc, and the combination works — I verified a real compile+link+run with brew on PATH (`hello-from-gcc-11`, exit 0), and the resulting binaries run. But binutils 2.47 + gcc 11.4 is *not* the combination ROS2 Humble is validated against, and the shadowed `mount`/`umount`/`dmesg` from brew's util-linux are a genuine (if unlikely) behavioural surprise.

**Mitigations, in order of preference:**
1. **Do ROS2 builds in a shell that does not source `brew-env.sh`** — e.g. a dedicated build shell, `env -i PATH=/usr/bin:/bin …`, or a systemd unit. This is the clean answer.
2. If brew must be present during a build, force the system binutils: `PATH=/usr/bin:$PATH` after sourcing, or `gcc -B/usr/bin`.
3. Note that the user's **login shell is zsh and `.zshrc` now sources `brew-env.sh`**, so an ordinary interactive zsh login *will* have brew's binutils ahead of the system ones. Anyone doing ROS2 work interactively should be aware.

I honoured the mandated contract exactly (`brew-env.sh` = `brew shellenv` output, one guarded `.bashrc` line) and am surfacing this rather than silently deviating from it. **If the Lead prefers, the surgical fix is to append `/usr/bin` re-precedence for the binutils/util-linux names, or to ship `brew-env.sh` as `brew shellenv` *followed by* a PATH re-order** — but that changes the file `shell-env` depends on, so it should be a Lead-level decision.

---

## 10. Reproducing / operating notes

```bash
# enable brew in a shell
source /home/agilex/.dsh/brew-env.sh

# brew installs need no sudo (prefix is user-owned)
brew install <formula>

# if a brew install 404s on the USTC bottle mirror, drop the mirror and use the official registry:
env -u HOMEBREW_BOTTLE_DOMAIN brew install <formula>

# if `brew update` ever fails on this flaky link, fall back to the verified USTC mirror:
git -C /home/linuxbrew/.linuxbrew/Homebrew remote set-url origin https://mirrors.ustc.edu.cn/brew.git
brew update
# (mirror content is SHA-identical to upstream main; switch back to
#  https://github.com/Homebrew/brew afterwards if desired)
```

Logs kept on the robot for audit: `/home/linuxbrew/brew-install.log`, `brew-install.log.attempt1`, `brew-install.log.attempt2`, `brew-formulae.log`, `/home/linuxbrew/install.sh` (pristine, 35651 bytes).

---

## 11. Failure / honesty log

1. **Homebrew's official GitHub install failed 7+ times** (`GnuTLS recv error -54`, `early EOF` on a ~154 MB pack), leaving 183 MB of abandoned `tmp_pack_*` garbage. Worked around with the USTC mirror, **SHA-verified identical to upstream `main`**, then the **official remote was restored and `brew update` verified**. Not a silent workaround.
2. **The standard GnuTLS cure did not work.** HTTP/1.1 + `postBuffer` + `lowSpeedLimit`/`lowSpeedTime` (applied via env, no `~/.gitconfig` write) failed identically. Recorded so nobody repeats it.
3. **I made a wrong turn worth recording:** I first compared the mirror against GitHub's `master` and saw a SHA mismatch. The real cause is that **Homebrew/brew's default branch is `main`; `master` is a legacy stub.** Comparing against `main` gives an exact match. Anyone auditing this repo should compare `main`.
4. **The USTC bottle mirror is incomplete** — it 404s on `portable-ruby`. Homebrew auto-fell back to `ghcr.io` and succeeded, so the install is sound, but future formulae may 404 there.
5. **`brew doctor`'s glibc warning is unresolved and unresolvable here**: host glibc 2.35 is genuinely older than Homebrew 7.x wants. Homebrew's own `glibc 2.39_1` is the mitigation. It is not a defect I introduced and not one I can fix without an OS upgrade (forbidden).
6. **`bash -lc 'command -v brew'` returns empty** — the required evidence line cannot pass under the mandated one-line `.bashrc` contract, because of the stock interactive guard. Reported honestly with the working alternatives (`bash -lic`, `zsh -lic`, and `.zshrc` already wired). §6.3.
7. **`lld --version` "fails"** with "lld is a generic driver" — that is normal; use `ld.lld --version`.
8. **`clang-tools-14` does not ship `clang-tidy`** on jammy; I installed the separate `clang-tidy-14` package. Binary is versioned (`clang-tidy-14`), no unversioned symlink.
9. **I upgraded nothing.** `/var/log/apt/history.log` shows a large `apt dist-upgrade -y` at **2026-10-10 20:15:21 — before my session started (~21:50)**; that is not mine. My two transactions (`21:52:52` and `21:54:47`) contain **`Install:` only, no `Upgrade:` line**. I also observed `apt-get -y purge libnvidia-*-570` at `22:50:02` — that is the `nvidia-cuda` agent's authorized driver work, not mine.
10. **One package (`code`) currently shows as upgradable** (it was 0 at recon). I did **not** upgrade it.
11. `brew install cmake ninja wget` was killed by my own 420 s `timeout` on the first attempt (SIGTERM mid-download). Re-run detached, it completed in ~90 s. Recorded because the SIGTERM looks alarming in the log and was my harness, not a brew fault.

---

## 12. Final state summary

**Installed by me:** Homebrew 7.0.9 (+25 formulae incl. cmake 4.4.4, ninja 1.13.2, wget 1.25.0, gcc 16.2.0, glibc 2.39_1) · `g++-12 12.3.0` · `clang/clang++/clangd/clang-format/clang-tidy 14.0.0` · `llvm 14.0.0` · `lld 14.0.0` · `python3-venv` + `python3.10-venv` · `/home/agilex/.dsh/brew-env.sh` · one guarded line in `.bashrc`.

**Unchanged and verified:** system `gcc`/`g++` 11.4.0 · system `python3` 3.10.12 · miniforge 3.12.11 · system glibc 2.35 · `update-alternatives` · `.zshrc` / `.oh-my-zsh` / `cuda-env.sh` / `.bashrc` CUDA lines 138–139 · 0 packages upgraded or removed by me.

**Caveats for the Lead:** (a) brew's binutils shadowing in brew-sourced shells — §9.2; (b) `bash -lc` login-shell check needs a `.bash_profile` if you want it to pass — §6.3; (c) `brew doctor` glibc warning is inherent to Ubuntu 22.04 + Homebrew 7.x — §2.4.
