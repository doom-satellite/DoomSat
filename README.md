![DoomSat: playing Doom through a real mission stack](docs/images/doomsat-header.png)

# DoomSat

Doom runs as the payload of a spacecraft. An **F´** flight computer sends its telemetry and video down through
**CCSDS** frames to **Yamcs**, and you watch it in **Open MCT** or a small dashboard. The pilot on the ground is
**jev** (TypeSafe's fast decision model). Every half second it picks where to go and uplinks an intent.
**Claude** reviews each attempt. No part of the pilot ever reads the level file.

On 24 September 2026 jev finished **E1M1 in 105 s**, with no deaths, through the full stack.

<div align="center">
  <img src="docs/video/e1m1-finish.gif" alt="The last 28 seconds of the run that finished E1M1" width="600">
  <p><em>The last 28 seconds of the run that finished E1M1</em></p>
</div>

![Architecture](docs/diagrams/architecture.png)

## Install

You need about 3 GB of disk, 15 to 20 minutes, and a [TypeSafe API key](https://docs.typesafe.ai) for jev.
You don't need a key for Claude: the pilot uses your [Claude Code](https://code.claude.com/docs) login.
Without a TypeSafe key, you can still [fly it yourself](#play-it-yourself-no-jev-no-key).

> **Shortcut:** open [Claude Code](https://code.claude.com/docs) in this folder and say *"set DoomSat up on
> this machine"*. [`CLAUDE.md`](CLAUDE.md) gives it the steps.

<details>
<summary><b>Windows 10/11 (WSL2)</b></summary>

<br>

Everything runs inside WSL2 (Ubuntu). Your Windows browser reaches it on `localhost`.

1. In **PowerShell as Administrator**:
   ```powershell
   wsl --install -d Ubuntu-24.04
   ```
   Restart if Windows asks you to, then open **Ubuntu** from the Start menu and create your user.
2. From here on, work in the Ubuntu terminal and follow the **Linux** steps below. Clone into your Linux home
   (`~/`), not `/mnt/c/...`, because builds on the Windows drive are many times slower.
3. On Windows 11, WSLg shows the Doom window from `payload/play.py`. On Windows 10 use `--check` instead.

*Optional, if you'd rather keep the repo on the Windows side and use Git Bash:* set
`DOOMSAT_WSL_DISTRO` (and `DOOMSAT_WSL_USER`) in `.env`. `scripts/flight.sh` then forwards itself into WSL.
Run `scripts/setup_ground.sh` from Git Bash so the pilot, the dashboard and Open MCT run on Windows.

</details>

<details>
<summary><b>Linux (Ubuntu 22.04 / 24.04, or inside WSL)</b></summary>

<br>

```bash
# 1. System packages
sudo apt update
sudo apt install -y git curl build-essential binutils python3 python3-venv python3-pip

# 2. Node.js 24 (for Open MCT)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.nvm/nvm.sh && nvm install 24

# 3. Claude Code (for System Two; optional, the pilot runs without it)
curl -fsSL https://claude.ai/install.sh | bash      # then run `claude` once to log in

# 4. This repo, your key, and the install
git clone https://github.com/Devonance/DoomSat.git && cd DoomSat
cp .env.example .env && nano .env      # set TYPESAFE_API_KEY
scripts/flight.sh setup                # ViZDoom, F´ v4.3.0 + DoomSat build, Yamcs, WADs  (~10 min)
scripts/setup_ground.sh                # pilot venv + Open MCT build                      (~5 min)
python3 tools/doctor.py --jev          # checks every piece and makes one jev call
```

</details>

<details>
<summary><b>macOS (Apple Silicon or Intel), not yet tested</b></summary>

<br>

The scripts support macOS (the F´ build goes to `build-artifacts/Darwin/`), but nobody has run them end to end on
a Mac yet. The two likely weak spots are the Java runtime that fprime-yamcs bundles and ViZDoom's wheels. If a
step fails, please open an issue with the output.

```bash
# 1. Compiler, Python, git
xcode-select --install
brew install python@3.12 git          # `python3 --version` should now say 3.12

# 2. Node.js 24 and Claude Code
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.nvm/nvm.sh && nvm install 24
curl -fsSL https://claude.ai/install.sh | bash

# 3. This repo, your key, and the install
git clone https://github.com/Devonance/DoomSat.git && cd DoomSat
cp .env.example .env && open -e .env  # set TYPESAFE_API_KEY
scripts/flight.sh setup
scripts/setup_ground.sh
python3 tools/doctor.py --jev
```

</details>

Each `scripts/flight.sh setup` step can also run on its own: `payload`, `fprime` or `wads`. The same goes for
`scripts/setup_ground.sh python|openmct`. Re-running either script skips anything already installed.

## Run the whole stack

Use four terminals, or background the servers:

```bash
scripts/flight.sh start              # Doom payload + F´ + Yamcs          → http://localhost:8090
python3 tools/serve_dashboard.py     # mission dashboard                  → http://localhost:8070
scripts/start_openmct.sh             # Open MCT                           → http://localhost:9000
scripts/start_pilot.sh --duration 600   # jev plays; add --system-two none to leave Claude out
scripts/flight.sh stop
```

Logs go to `out/` (the pilot) and `~/doom/run/` (payload, Yamcs). `scripts/flight.sh check` prints a telemetry
health report.

## Play it yourself (no jev, no key)

To try the whole stack without a TypeSafe key, fly it yourself from the dashboard:

```bash
scripts/play.sh                 # you drive; opens http://localhost:8070
scripts/play.sh --autopilot     # or watch the code rules fly it (no model)
```

`play.sh` starts the flight side and the dashboard server if they aren't already running. Click the picture to
take the controls:

| Keys | |
|---|---|
| <kbd>W</kbd> <kbd>S</kbd> or <kbd>↑</kbd> <kbd>↓</kbd> | forward / back |
| <kbd>A</kbd> <kbd>D</kbd> or <kbd>←</kbd> <kbd>→</kbd> | turn left / right |
| <kbd>Q</kbd> <kbd>E</kbd> | strafe left / right |
| <kbd>F</kbd> or mouse button | fire |
| <kbd>Space</kbd> | use (doors, switches, the exit) |
| <kbd>2</kbd> <kbd>3</kbd> | pistol / shotgun |
| <kbd>Esc</kbd> | let go |

Your keys go up the same path the pilot's orders do: each change is a `CONTROL` command issued to Yamcs, which
uplinks it to F´, which passes it to the game. The picture comes back down as telemetry. So what you see is the
real mission loop, round-trip latency included. The commands appear in the dashboard's command panel and in
Yamcs, and the telemetry and video appear in Open MCT (`scripts/start_openmct.sh`). Manual mode restarts the
level when it starts (`--no-reset` keeps the game as it is). It logs to `out/manual.jsonl`, never to the pilot's
`out/decisions.jsonl`. <kbd>Ctrl</kbd>+<kbd>C</kbd> stops it, and `scripts/flight.sh stop` stops the flight
side.

`--autopilot` flies the pilot with `--system-one code`, the exact rules jev's questions restate. It is the
same code baseline as `research/runner.py --decider code`, flown on the full stack.

## Use each piece on its own

<details>
<summary><b>Doom (ViZDoom)</b>: play it, or check it headless</summary>

<br>

```bash
~/doom/payload-venv/bin/python payload/play.py            # a window; you play
~/doom/payload-venv/bin/python payload/play.py --check    # no window: 100 tics → out/doom_check.png
~/doom/payload-venv/bin/python payload/play.py --wad freedoom1.wad
```

</details>

<details>
<summary><b>F´</b>: the flight software with the stock F´ GDS</summary>

<br>

```bash
scripts/flight.sh gds          # the DoomSat deployment + F´ GDS → http://localhost:5000
scripts/flight.sh payload      # (another terminal) add the game, so the Doom channels move
```

The component is in `flight/Components/Doom/` and the topology in `flight/DoomSat/Top/`. After an edit, run
`scripts/flight.sh build`.

</details>

<details>
<summary><b>Yamcs</b>: mission control, fed by F´</summary>

<br>

```bash
scripts/flight.sh yamcs        # F´ + Yamcs, no game  → http://localhost:8090  (instance fprime-project)
scripts/flight.sh start        # the same, plus the game and the video frames
```

The Yamcs config is `ground/yamcs/`. The XTCE database is generated from the F´ dictionary at launch.

</details>

<details>
<summary><b>Open MCT</b>: the DoomSat displays</summary>

<br>

Open MCT reads everything from Yamcs, so start Yamcs first. You don't need the game or the pilot:

```bash
scripts/flight.sh yamcs        # or `start` for live Doom telemetry and video
scripts/start_openmct.sh       # → http://localhost:9000
```

The DoomSat configuration is `ground/openmct/index.html` and `index.js`. Edit it there:
`start_openmct.sh` copies it into the plugin's example on every start. In the tree, open
**fprime-project → DoomGround → DoomFrame** as an imagery view to see the video. Parameters under
**DoomSat_DoomSat** open as plots.

</details>

<details>
<summary><b>The dashboard</b></summary>

<br>

```bash
python3 tools/serve_dashboard.py      # → http://localhost:8070 (needs Yamcs on :8090)
```

This is one static page (`ground/dashboard/index.html`) with a proxy to the Yamcs API, and it needs nothing
installed.

</details>

<details>
<summary><b>jev (System One)</b>: with or without the stack</summary>

<br>

```bash
python3 tools/doctor.py --jev                          # one real call with your key
# jev (or the code-only baseline) playing Doom in one process, no F´ or Yamcs:
~/doom/payload-venv/bin/python research/runner.py bench --maps E1M1 --seeds 1 --budget 60 \
    --decider jev --wad ~/doom/wads/freedoom1.wad --out out/bench --allow-dirty
```

Use `--decider code` to run the same bench with no model.

</details>

<details>
<summary><b>Claude (System Two)</b></summary>

<br>

The pilot shells out to the `claude` CLI, so it uses your Claude Code login and needs no API key. Once a minute
Claude pushes exploration in a direction, and after each attempt it revises the questions jev is asked.

```bash
scripts/start_pilot.sh --system-two claude-cli      # default
scripts/start_pilot.sh --system-two anthropic       # the API instead; set ANTHROPIC_API_KEY in .env
scripts/start_pilot.sh --system-two none            # jev + code only
```

</details>

## Run it on a Jetson AGX Orin (local models, no cloud, no key)

The whole demo can run on one **Jetson AGX Orin 64 GB**. **Rune** (Surogate's decision model, a Gemma 4 26B-A4B)
plays as System One in place of jev, and **Gemma 4 31B** reviews as System Two in place of Claude. Both run on
[llama.cpp](https://github.com/ggml-org/llama.cpp) servers (`:8080` and `:8081`). `--system-one rune` speaks
Surogate's "decisions v1" protocol to llama.cpp and answers in the same shapes as jev.

This was set up and measured on 4 October 2026 on one Orin Developer Kit (JetPack 7.2, Ubuntu 24.04.4, L4T R39.2.1,
CUDA 13.2). **It flies, but it is not yet a real-time pilot**: read [What to expect](#what-to-expect-on-the-jetson)
before you build it. [docs/JETSON-RUNBOOK.md](docs/JETSON-RUNBOOK.md) is the plan this follows. The Teensy 4.1 part
was not done: see [docs/JETSON-HANDOFF.md](docs/JETSON-HANDOFF.md).

<details>
<summary><b>Setup, step by step</b> (1 to 2 hours, most of it downloads and builds)</summary>

<br>

Run these on the Jetson, over SSH or at a keyboard. Several steps need `sudo`. Anything that takes more than a
minute or two (apt, the llama.cpp build, the model downloads) is best run under `tmux` or `nohup` with a log file,
so a dropped SSH session doesn't kill it.

**1. Check the machine, and where the big files will go.** You need about 100 GB free for models, builds and swap.

```bash
uname -m                          # aarch64
grep VERSION= /etc/os-release     # 24.04
free -g | sed -n 2p               # total about 61 GB
findmnt -n -o SOURCE /; df -h /   # which disk is the OS on, and how much is free?
lsblk -d -o NAME,SIZE,MODEL
```

JetPack may have been flashed to the small eMMC (`/dev/mmcblk0p1`, about 24 GB free) even when a big NVMe is
installed. If so, use the NVMe as a **data disk**. These commands **erase the disk**, so run them only on a disk
you have checked is blank (`lsblk` shows no partitions and no filesystem):

```bash
sudo sfdisk /dev/nvme0n1 <<< $'label: gpt\n,,L'                  # one partition, the whole disk
sudo mkfs.ext4 -L doomdata -m 1 /dev/nvme0n1p1
sudo mkdir -p /mnt/nvme
echo "UUID=$(sudo blkid -s UUID -o value /dev/nvme0n1p1) /mnt/nvme ext4 defaults,noatime,nofail 0 2" | sudo tee -a /etc/fstab
sudo mount /mnt/nvme && sudo mkdir -p /mnt/nvme/$USER && sudo chown $USER: /mnt/nvme/$USER
for d in models llama.cpp doom venvs hf-cache; do mkdir -p /mnt/nvme/$USER/$d && ln -s /mnt/nvme/$USER/$d ~/$d; done
```

The symlinks keep every path in the runbook and the units below valid. If the OS is already on the NVMe, skip all of
this. Put `export PATH=/usr/local/cuda/bin:$PATH` and `export HF_HOME=$HOME/hf-cache` in `~/.bashrc`.

**2. Switch to MAXN and pin the clocks.** The default 30 W mode runs 8 of 12 cores and caps the GPU at 612 MHz. The
switch **needs a reboot**, so do it before the long builds:

```bash
sudo nvpmodel -q                  # MODE_30W; MAXN is mode 0 on this board (check /etc/nvpmodel.conf)
echo YES | sudo nvpmodel -m 0     # answers the reboot prompt: the Jetson reboots now
# after it comes back:
sudo nvpmodel -q                  # MAXN
sudo jetson_clocks                # a oneshot systemd unit that runs this at boot keeps it (runbook, phase 1)
```

Add a 16 GB swap file on the data disk (`/mnt/nvme/swapfile`, priority 10, an `/etc/fstab` line) and
`vm.swappiness=10`. Swap was never touched in the measured run, but it is the safety net.

**3. Packages, Node and the repo.**

```bash
sudo apt update
sudo apt install -y git curl build-essential binutils cmake pkg-config python3 python3-venv python3-pip \
  libcurl4-openssl-dev jq tmux htop libboost-all-dev libsdl2-dev libopenal-dev libgl1-mesa-dev zlib1g-dev libjpeg-dev
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash && . ~/.nvm/nvm.sh && nvm install 24
git clone https://github.com/doom-satellite/DoomSat.git ~/DoomSat && cd ~/DoomSat && cp -n .env.example .env
```

nvm only puts `node` on the `PATH` of interactive shells. In a script, or in `ssh <jetson> 'command'`, start with
`. ~/.nvm/nvm.sh`; without it `tools/doctor.py` reports Node as missing and `scripts/start_openmct.sh` fails.

**4. Build llama.cpp natively for the Orin GPU** (architecture 87). NVIDIA's JetPack 7.2 forum guide says prebuilt
CUDA 12 binaries fail on it (not tested here). The first third of the build took 17 minutes in the 30 W mode; the
rest took about 4 minutes after the switch to MAXN.

```bash
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp    # into the (empty) directory the symlink points to
cd ~/llama.cpp && cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j"$(nproc)" && ./build/bin/llama-server --version
```

**5. Download the two models** (about 34 GB). Rune is a third-party GGUF by Livesport; Surogate's own serving engine
targets other hardware, according to its README (not tried here). Gemma has three vision projectors: take the F16
one only.

```bash
python3 -m venv ~/venvs/hf && . ~/venvs/hf/bin/activate && pip install -U huggingface_hub
hf download Livesport/rune-26b-a4b-v3-GGUF rune-26b-a4b-v3-IQ4_XS-dyn.gguf --local-dir ~/models/rune-v3
hf download unsloth/gemma-4-31B-it-qat-GGUF gemma-4-31B-it-qat-UD-Q4_K_XL.gguf mmproj-F16.gguf --local-dir ~/models/gemma4-31b
```

**6. Run both as systemd services**, so a dropped SSH session cannot kill them. Wrap each command in a unit with
`User=<you>`, `Restart=on-failure`, `RequiresMountsFor=/mnt/nvme` (if the models are there) and
`WantedBy=multi-user.target`; the full unit text is in the runbook, phase 5. Start Rune first and wait for its
health check before Gemma, so the two model loads do not overlap.

```bash
# rune-server.service (ExecStart, one line)
~/llama.cpp/build/bin/llama-server -m ~/models/rune-v3/rune-26b-a4b-v3-IQ4_XS-dyn.gguf -ngl 99 -c 16384 -np 1 --swa-full -fa on -ctk q8_0 -ctv q8_0 -b 512 -ub 512 --jinja --cache-ram 0 --host 0.0.0.0 --port 8080
# gemma-server.service (ExecStart, one line)
~/llama.cpp/build/bin/llama-server -m ~/models/gemma4-31b/gemma-4-31B-it-qat-UD-Q4_K_XL.gguf --mmproj ~/models/gemma4-31b/mmproj-F16.gguf -ngl 99 -c 8192 -np 1 -fa on --jinja --cache-ram 0 --temp 1.0 --top-p 0.95 --top-k 64 --host 0.0.0.0 --port 8081
```

Use absolute paths in a unit (`/home/<you>/...`). Both came up healthy in under 20 seconds each, with about 41 GB of
RAM used. `--host 0.0.0.0` makes both ports reachable from your whole network, and this setup did not enable a
firewall: keep the Jetson on a network you trust. If you enable `ufw`, run `sudo ufw allow OpenSSH` first or you
lock yourself out.

**7. Install the flight stack.** Everything below ran natively on aarch64 with no source fixes: ViZDoom 1.3.0 has an
aarch64 wheel, the JRE bundled with `fprime-yamcs` is aarch64, and F´ builds on the Orin in a few minutes. The
scripts are stored in git without the executable bit, so run them with `bash`:

```bash
cd ~/DoomSat && . ~/.nvm/nvm.sh
bash scripts/flight.sh setup payload && bash scripts/flight.sh setup wads && bash scripts/flight.sh setup fprime
bash scripts/setup_ground.sh          # ground/.venv and Open MCT
```

**8. Check it.**

```bash
python3 tools/doctor.py --rune        # one real Rune call, and a check of the prompt the server builds
~/doom/payload-venv/bin/python payload/play.py --check          # writes out/doom_check.png (no display needed)
bash scripts/flight.sh start && sleep 40 && bash scripts/flight.sh check   # FRAMES_SENT rising, PAYLOAD_LINK True
bash scripts/flight.sh stop
ground/.venv/bin/python -m unittest discover -s tests           # no network, no game
```

`doctor.py --rune` prints "ok" when the server's chat template ends Rune's prompt with the empty thought block
(`<|channel>thought\n<channel|>`). If it does not, the answers are off-distribution: update and rebuild llama.cpp.

</details>

<details>
<summary><b>Fly it</b></summary>

<br>

```bash
cd ~/DoomSat
bash scripts/flight.sh start; sleep 40; bash scripts/flight.sh check
python3 tools/serve_dashboard.py &        # http://localhost:8070 on the Jetson
bash scripts/start_openmct.sh &           # http://localhost:9000 on the Jetson
RUNE_TIMEOUT_S=10 bash scripts/start_pilot.sh \
  --system-one rune --system-one-base-url http://127.0.0.1:8080 \
  --system-two openai --system-two-base-url http://127.0.0.1:8081/v1 --system-two-model gemma-4-31b \
  --duration 120
bash scripts/flight.sh stop               # leave the two model services running
```

The dashboard and Open MCT listen on the Jetson's loopback. To watch from your own computer, forward the ports and
open `http://localhost:8070` there:

```bash
ssh -N -L 8070:127.0.0.1:8070 -L 9000:127.0.0.1:9000 -L 8090:127.0.0.1:8090 <you>@<jetson-address>
```

`RUNE_TIMEOUT_S=10` is there on purpose: with the default 1.2 s, almost every tick would time out and fall back to
the code rule (next section), so you would be watching the rules fly, not Rune. Each role has its own endpoint:
`--system-one-base-url` and `--system-two-base-url`, or `RUNE_BASE_URL` and `SYSTEM_TWO_BASE_URL` in `.env`.

</details>

### What to expect on the Jetson

One 120 s flight on the unit above (23 decision ticks, so treat the numbers as indicative, not as a benchmark):

| | Result |
|---|---|
| Memory, both models and the whole flight stack | peak **43.1 GiB** of 61 GiB; swap unused |
| Rune's answer to a question | one question costs about **0.65 s** (about 390 new prompt tokens at about 780 tokens/s); questions run one after another |
| Rune per decision tick | **5.2 s** for the usual 8 candidates, 0.7 s for one. jev's median is 0.54 s and the pilot's ceiling is 1.2 s: **19 of the 20 ticks Rune answered were over it** |
| Rune while Gemma is running | worse: the last 3 ticks, which coincided with Gemma's first call, each timed out at 10 s and fell back to the code rule. Both models share one GPU |
| Rune's confidence against the graph | 11 of the 20 answered ticks landed in the graph's "unsure" band and used the code rule. Its thresholds were tuned against `jev-1.13.0` and **have not been re-tuned for Rune**; any change goes through the experiment ledger (`research/PROGRAM.md`) |
| Gemma 4 31B (System Two) | about **7.8 tokens/s**. A small JSON call took 39 to 55 s with its thinking on and about 7 s with thinking off. The flight's real after-action prompt, replayed afterwards, took **279 s** even with thinking off |
| System Two in the pilot | **did not deliver**: `OpenAIChat` waits 180 s, so the stall consult and the after-action review both failed with a read timeout, and no `[pilot] ... (gemma-4-31b, N ms)` line appeared |
| `probabilities` in `out/decisions.jsonl` | **not logged** on the default `--control intent` rows (only `answers` and `confidence`) |
| The dashboard's headings | still say "jev" and "Claude Sonnet 5": they are fixed text, not what is flying. The pilot log line `System One = rune plays` and the model name on each decision row are what to trust |

What is not verified: whether Rune's judgments are any good (there is no ground truth here), whether any of the
fixes below help, and anything over a longer flight. Ideas that were **not tried**: asking Rune fewer candidates per
tick or shortening the rubric text, larger `-b`/`-ub` batches for faster prompt processing, turning Gemma's thinking
off for System Two (`chat_template_kwargs: {"enable_thinking": false}`) and lengthening its 180 s timeout.

## Configuration

Everything is in one file, `.env` at the repo root. Copy it from [`.env.example`](.env.example):

| Variable | Needed for |
|---|---|
| `TYPESAFE_API_KEY` | jev (the pilot, the jev bench) |
| `ANTHROPIC_API_KEY` | only `--system-two anthropic` |
| `RUNE_BASE_URL`, `RUNE_TEMPERATURE`, `RUNE_TIMEOUT_S` | only `--system-one rune` (a local llama.cpp server) |
| `SYSTEM_TWO_BASE_URL` | only `--system-two openai`, to give System Two its own endpoint |
| `DOOMSAT_HOME` | where the flight side is installed (default `~/doom`) |
| `DOOMSAT_WSL_DISTRO`, `DOOMSAT_WSL_USER` | only when you drive WSL from Git Bash |

## Troubleshooting

<details>
<summary>Common problems</summary>

<br>

- **Start with `python3 tools/doctor.py`.** It lists what is missing and the command that fixes it.
- **Yamcs never comes up.** Read `~/doom/run/yamcs.log`. Port 8090 may already be taken.
- **Open MCT shows "Missing" rows.** That's the plugin's example layout. Browse the tree on the left instead.
  Yamcs has to be up first.
- **The jev call fails.** Check the key in `.env`. TypeSafe keys can expire.
- **The pilot can't reach Yamcs.** Wait about 30 s after `flight.sh start`, then run `scripts/flight.sh check`.
- **Scripts fail with `$'\r': command not found`.** Windows line endings have crept in. `.gitattributes`
  keeps `*.sh` as LF, so re-clone, or run `sed -i 's/\r$//' scripts/*.sh`.

</details>

## Learn more

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers what the player may see, the decision graph, who decides
  what, and the integration findings.
- [docs/results/](docs/results/) has the E1M1 finish: the [decision log](docs/results/e1m1-finished-decision-log.md),
  [every attempt](docs/results/e1m1-progress.html) and the [overnight report](docs/results/2026-09-24-overnight-report.md).
- [docs/CHARTER.md](docs/CHARTER.md) sets out the mission and its rules, and
  [docs/CHARTER-STATUS.md](docs/CHARTER-STATUS.md) says what is built.
- The full report is [docs/doomsat-report.pdf](docs/doomsat-report.pdf).
- Built on [F´](https://github.com/nasa/fprime) v4.3.0, [fprime-yamcs](https://github.com/fprime-community/fprime-yamcs) 0.2.1,
  [Yamcs](https://github.com/yamcs/yamcs) 5.12.8, [Open MCT](https://github.com/nasa/openmct) with
  [openmct-yamcs](https://github.com/akhenry/openmct-yamcs), [ViZDoom](https://vizdoom.farama.org) 1.3.0,
  [TypeSafe jev](https://docs.typesafe.ai) and [Claude Code](https://code.claude.com/docs).
