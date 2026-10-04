# Jetson handoff: the Teensy 4.1 (Phase 8) was not started

Written 2026-10-04 at the end of a Claude Code session that set up the Jetson over SSH. Read this with
[JETSON-RUNBOOK.md](JETSON-RUNBOOK.md). The runbook's phases 0 to 7 and 9 were run; **phase 8 was skipped on
request** because the Teensy was unplugged. Nothing Teensy-related was installed, changed or flashed.

## State of the Teensy work

| Item | State |
|---|---|
| Teensy 4.1 on the Jetson's USB | **Not connected.** `lsusb` showed only hubs, a Bluetooth radio and a Logitech receiver: no `16c0:0483` (serial) or `16c0:0478` (bootloader), and no `/dev/ttyACM*`. It was unplugged by the user. |
| `doomguy` in the `dialout` group | No (`id` shows adm, cdrom, sudo, audio, dip, video, plugdev, users, i2c, lpadmin, gdm, weston-launch, gpio, render) |
| `/etc/udev/rules.d/00-teensy.rules` | Not installed |
| `arduino-cli`, the `teensy:avr` core, `teensy-loader-cli` | Not installed |
| `~/teensy-echo/` sketch | Not created |
| Teensy firmware, F´ components, serial bridge | None written. The repo has no serial, USB or Teensy code. |

`phase8a.sh`, a script that installs the toolchain and compiles the echo sketch without a board, was written but
**never run**. Do not assume it works: it was not tested.

## To pick this up

1. **Plug the Teensy into a Jetson USB port with a data cable** (charge-only cables are common). On the Jetson,
   `lsusb | grep -i 16c0` should show `16c0:0483`, and `ls -l /dev/ttyACM*` a device. If it shows only `16c0:0478`
   it is in bootloader mode: press its program button once.
2. Follow runbook phase 8. On this Jetson, three things differ from the runbook text:
   - **`PATH`:** `arduino-cli` goes to `~/.local/bin`, which a non-interactive SSH shell does not have. Use
     `export PATH=$HOME/.local/bin:$PATH` in every script.
   - **`dialout`:** the group change needs a new login. Until then run uploads and the serial test as
     `sg dialout -c '...'`, or reconnect SSH.
   - **Python:** the echo test needs `pyserial`. `~/venvs/hf` exists, but use it only for this, or make a new venv
     under `~/venvs/` (that path is a symlink to the NVMe).
3. **Unverified, check first:** whether PJRC's `teensy:avr` core ships an **aarch64 Linux** compiler and uploader.
   If it does not, the runbook's fallback applies: compile on a PC, copy the `.hex` to the Jetson and flash with
   `teensy_loader_cli --mcu=TEENSY41 -w -v file.hex`. `teensy-loader-cli` is an apt package (unverified for this
   release).
4. Prove the link with the echo sketch from the runbook (`ping` goes in, `ping` comes back at 115200 baud).

## The decision that is still open (runbook phase 8 GATE)

**What does the Teensy do in the demo?** Stop and ask before writing any firmware or any F´ component beyond the
echo test. What is known:

- The F´ deployment talks to the ground over TCP (`Drv.TcpClient`, `flight/DoomSat/Top/instances.fpp`). The Doom
  component (`flight/Components/Doom/`) sends video frames and is built for a Linux host. It cannot run on a Teensy.
- If the Teensy should run F´ itself: [fprime-arduino](https://github.com/32bitmicro/fprime-arduino) lists
  `teensy41` as a toolchain and needs `fprime-baremetal`. Whether it supports **F´ v4.3.0**, which DoomSat uses, is
  **unconfirmed**. It would be a second, small deployment linked to the Jetson's, with a new serial driver on the
  Jetson side.
- If the Teensy is a sensor or actuator board: the smallest change is a serial bridge on the Jetson that the pilot
  or the payload reads.

## What else a continuing session needs to know about this Jetson

- **Address and user:** `doomguy@192.168.1.44` (LAN). Key login is set up from the user's PC. Passwordless `sudo`
  works. **If `/etc/sudoers.d/90-claude-setup` exists, delete it** (`sudo rm /etc/sudoers.d/90-claude-setup`).
  It was not checked whether the user created it; `/etc/sudoers.d` could not be listed without sudo.
- **Disks:** the OS is on the 59 GB **eMMC** (`/dev/mmcblk0p1`, about 22 GB free). The 1 TB NVMe was blank and is
  now a single ext4 partition labelled `doomdata`, mounted at `/mnt/nvme` (UUID entry in `/etc/fstab` with
  `nofail`; backup at `/etc/fstab.bak-claude-setup`). `~/models`, `~/llama.cpp`, `~/doom`, `~/venvs` and
  `~/hf-cache` are symlinks into `/mnt/nvme/doomguy/`. A 16 GB swap file is at `/mnt/nvme/swapfile`.
- **Power mode:** MAXN, applied by a reboot. `jetson-clocks.service` pins the clocks.
- **Services:** `rune-server` (:8080) and `gemma-server` (:8081), systemd, enabled, started at boot.
- **Scripts in the repo are mode 100644** (`scripts/flight.sh`, `start_openmct.sh`, `start_pilot.sh`), so run them
  as `bash scripts/flight.sh ...`. Non-interactive shells need `. ~/.nvm/nvm.sh` before anything that uses Node.
- **Code changes** (runbook phase 7) are on the local branch `jetson-models` on the user's PC, **uncommitted**, and
  applied as a patch to `~/DoomSat` on the Jetson (the patch is at `~/setup-scripts/jetson-models.patch`).
- **Logs:** `~/jetson-setup.log` (everything), `~/logs/*.log` (one per phase), `~/setup-scripts/` (the scripts that
  were run).
