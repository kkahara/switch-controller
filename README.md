# Pi Zero W — wired Nintendo Switch Pro Controller gadget

**Verified baseline (2026-10-14, tag `baseline-working`):** with `procon.service`
running these exact files, `procon_send.py tap home` and `procon_send.py tap a`
over `/run/procon.sock` → `/dev/hidg0` were visibly received by a real Switch
(HOME menu opened; A registered). Findings from that session:

- `stream=off` meant the Switch was asleep. Waking it re-enumerated the gadget
  and `stream=on` followed about 3 s later; no code change was needed.
- The gadget negotiates **high-speed** (`max_speed` unset); a real Pro is full-speed.
- The Switch re-enumerated the gadget 14+ times that day, once right after a
  tap (`End Point Request ERROR: -108`). Unexplained; watch for it.
- `ok` from the daemon only means the state changed. It is returned even while
  `stream=off`, when nothing reaches the Switch.

Everything below predates that test. These files were assembled from public reverse-engineering (mzyy94 gist dumps, dekuNukem protocol notes) and have **not** been plugged into a Nintendo Switch in this environment. Treat them as a starting point, not a verified product.

**USB is the primary path.** Bluetooth (joycontrol) is optional and flaky on a Pi Zero W; see `bluetooth.md`. Do not delete the USB files if you try BT. Stop `procon.service` / `procon.py` while pairing over Bluetooth.

Target device:

- Raspberry Pi Zero W, Raspberry Pi OS 32-bit
- user `s3-coaching`, hostname `SwitchController1`
- Wi-Fi SSH on the **main LAN** (not guest)
- USB gadget already enumerates on a Mac as **CDC Composite Gadget (`g_cdc`)** plus ACM serial
- `getty@ttyGS0` and `ssh` enabled
- No physical IMU — `procon.py` packs **synthetic gravity** in `0x30` reports

**Default boot must keep the Mac setup gadget (`g_cdc`).** Do not put the Pro HID gadget in `cmdline.txt`. The HID-only composite is a script you run over **Wi-Fi SSH** immediately before plugging the Pi into a Switch.

**Only one process may open `/dev/hidg0`.** The systemd unit runs `procon.py` as that owner. A second Python process must send button/stick commands over the Unix socket (`/run/procon.sock`), not by starting another `procon.py`.

---

## What you copy onto the Pi

From the Mac (on the same LAN as `SwitchController1`):

```bash
scp add_procon_gadget.sh procon.py procon_send.py procon.service README.md \
  s3-coaching@SwitchController1.local:~/switch-controller/
# if .local does not resolve, use the Pi's LAN IP instead of SwitchController1.local
```

On the Pi:

```bash
ssh s3-coaching@SwitchController1.local
mkdir -p ~/switch-controller
# files already in ~/switch-controller after scp
chmod +x ~/switch-controller/add_procon_gadget.sh \
  ~/switch-controller/procon.py ~/switch-controller/procon_send.py
```

Use the Pi **USB** micro-USB port (next to mini-HDMI), a **data** cable, **no OTG hub**. Never the **PWR** socket.

---

## systemd daemon (preferred)

`procon.py` under systemd has **stdin EOF**, so it must not sleep-forever. The unit listens on **`/run/procon.sock`** (root-owned, mode **0666** so `s3-coaching` can connect without a extra group). Commands use the same language as the interactive CLI: `tap`, `press`, `release`, `dpad`, `stick`, `center`, `gravity`, `status`, `help`.

Copy the unit, reload, restart (over **Wi-Fi SSH**):

```bash
ssh s3-coaching@SwitchController1.local

# bind HID (ExecStartPre also does this) and install / restart the daemon
sudo cp ~/switch-controller/procon.service /etc/systemd/system/procon.service
sudo systemctl daemon-reload
sudo systemctl enable --now procon.service
# after later file edits:
sudo systemctl restart procon.service

systemctl status procon.service --no-pager
ls -l /run/procon.sock    # should be srw-rw-rw- root root
```

Then send commands **without** opening hidg0:

```bash
# as s3-coaching — no sudo
python3 ~/switch-controller/procon_send.py tap a
python3 ~/switch-controller/procon_send.py status
python3 ~/switch-controller/procon.py --send 'tap a'
python3 ~/switch-controller/procon.py --send stick l 0 1
```

Programmatic client (import; still does not open `/dev/hidg0`):

```python
import sys
sys.path.insert(0, "/home/s3-coaching/switch-controller")
from procon import ProconClient

pad = ProconClient()          # default /run/procon.sock
pad.tap("a")
pad.press("b")
pad.release("b")
pad.dpad("up")
pad.dpad(None)                # clear
pad.stick("l", 0, 1)          # same parse as stdin (float -1..1 or raw 0..4095)
pad.center()
pad.gravity("z")
print(pad.status())
```

Do **not** run a second `sudo python3 procon.py` while the unit is active — hidg0 is exclusive. Stop the unit first if you want the interactive TTY CLI instead.

Socket permissions: **0666** is intentional on this personal Pi. To tighten later: `groupadd procon`, `usermod -aG procon s3-coaching`, then change `SOCKET_MODE` in `procon.py` to `0o660` and `chgrp procon /run/procon.sock` (or patch the daemon to `chown`).

---

## Start HID by hand (unbind `g_cdc` first)

Do this over **Wi-Fi SSH**. Unbinding `g_cdc` drops the Mac USB serial/ethernet session. Stop `procon.service` first if it is enabled (`sudo systemctl stop procon`).

```bash
ssh s3-coaching@SwitchController1.local

# 1) Confirm you are on Wi-Fi, not ttyGS0
who
ip -4 addr show wlan0

# 2) Unbind the Mac setup gadget and bind HID-only Pro (VID 0x057E / PID 0x2009)
sudo ~/switch-controller/add_procon_gadget.sh

# 3) Start the protocol daemon *before* plugging into the Switch
sudo python3 ~/switch-controller/procon.py
```

Leave that SSH session open (`procon.py` reads stdin for button commands **and** still listens on `/run/procon.sock`). Then:

1. Unplug the Pi from the Mac if it is still there.
2. Plug the **same USB data port** into the Switch dock (USB-A) or a handheld Switch (USB-C data cable).
3. On the Switch: **Controllers** — look for a Pro Controller / player light.

`add_procon_gadget.sh` already `rmmod`s `g_cdc` / `g_serial` / `g_ether`, loads `libcomposite`, and writes the **203-byte** real Pro report descriptor from the mzyy94 dump. `/dev/hidg0` should exist afterward.

At startup, `procon.py` now follows the working mzyy94 gist ordering: it
rebinds the UDC, waits 0.5 seconds for enumeration, opens `/dev/hidg0`, and
then starts its reader. Host OUT packets queue until that open. Use
`--no-reset-udc` only when you intentionally do not want re-enumeration.

It does **not** send `0x30` input reports until the Switch sends `80 04`.
`80 05` stops that stream again. This ordering matters: unsolicited `0x30`
reports can occupy the IN endpoint and cause a nonblocking `81 01`/`81 02`
reply to be dropped before USB initialization completes.

---

## Stop HID and restore `g_cdc`

In an interactive `procon.py` session: `quit` or Ctrl-C. For the unit: `sudo systemctl stop procon.service` (its `ExecStopPost` restores `g_cdc`). Then, still over Wi-Fi SSH:

```bash
sudo ~/switch-controller/add_procon_gadget.sh remove   # if you started HID by hand
```

That unbinds the `procon` gadget and `modprobe g_cdc` again (and tries to start `getty@ttyGS0`). Reboot also restores `g_cdc` because boot still uses `modules-load=dwc2,g_cdc`.

Plug the Pi USB port back into the Mac to get CDC Composite + ACM as before.

---

## Test buttons (CLI)

**Interactive TTY** (when you started `sudo python3 procon.py` yourself, not systemd):

```text
help
press a
release a
tap a
tap b
tap x
tap y
tap plus
tap home
dpad up
dpad clear
stick l 0 1
stick l 2048 2048
center
status
quit
```

**Daemon / systemd** (stdin is EOF — use the socket, same words):

```bash
python3 ~/switch-controller/procon_send.py help
python3 ~/switch-controller/procon_send.py tap a
python3 ~/switch-controller/procon_send.py dpad up
python3 ~/switch-controller/procon_send.py stick l 0 1
python3 ~/switch-controller/procon_send.py center
python3 ~/switch-controller/procon_send.py status
```

Smoke tests that are easy to see on the Switch Controllers screen or in a menu: **A**, **B**, **Plus**, **Home**, d-pad **Up**. Analog sticks: `stick l -1 0` / `stick r 0 1` (floats in `-1..1`) or raw 12-bit `0..4095` (neutral is `2048`).

Same-process gadget API (only the daemon should call this; it opens hidg0):

```python
from procon import ProController
pad = ProController()
pad.start()
pad.press("A", "B")
pad.set_stick("left", 2048, 4095)
pad.set_dpad("up")
pad.tap("X", 0.15)
pad.release("A")
pad.set_gravity("z")   # synthetic ~1g on accel Z, gyro zeros
```

One-shot tap that **starts** a gadget process (still needs the gadget bound, and **conflicts** with `procon.service`):

```bash
sudo python3 ~/switch-controller/procon.py --tap a
```

One-shot that talks to the **already running** daemon:

```bash
python3 ~/switch-controller/procon.py --send 'tap a'
```

---

## What the code implements

| Piece | Source | Notes |
|---|---|---|
| VID/PID `0x057E`/`0x2009`, strings, 64-byte interrupt HID | mzyy94 `add_procon_gadget.sh` | HID **only** — no ACM/ECM on this composite |
| 203-byte report descriptor | mzyy94 `report_desc` / gist dump | Do not invent or “simplify” these bytes |
| USB `0x80` → `0x81` handshake | dekuNukem USB-HID-Notes + gist log | `80 01` status+MAC, `80 02` handshake, `80 03` baud, `80 04` start stream; `80 05` stops it |
| Subcommands `0x02` device info, `0x10` SPI, `0x03` mode `0x30`, `0x40` IMU | dekuNukem subcommand notes + gist `simulate_procon.py` | Other subcommands are ACK stubs |
| SPI cal blobs | gist SPI table + bypass log; factory IMU sample from dekuNukem | Sticks/gyro scale may still be wrong |
| `0x30` buttons/sticks/IMU | dekuNukem bluetooth HID + IMU notes | D-pad is bits on byte 5, **not** a HID hat |
| Synthetic gyro | software | Rest pose: gyro `0`, one accel axis `±4096` (~1 g). All-zero accel is free-fall |
| Control socket `/run/procon.sock` | this tree | Second process / `ProconClient`; stdin TTY still works; systemd EOF drives the socket |

Handshake MAC uses the **public wired dump** `04:03:D6:1D:86:1F` with correct LE (`80 01`) vs BE (subcommand `0x02`) byte order. The gist’s `mac_addr[::-1]` on a hex **string** is a known footgun and is not used here.

---

## If it does not show up on the Switch

These files are **untested on a real Switch**. Common failures from the protocol notes:

- Extra USB functions (you plugged in `g_cdc` instead of HID-only)
- Charge-only cable, or cable in **PWR**
- Python not running before / during enumeration (missed `0x80` handshake)
- `0x30` stream stopped (Python crashed or you quit)
- Wrong port / not `dr_mode=peripheral`
- A second process tried to open `/dev/hidg0` while the daemon holds it

Stay on Wi-Fi SSH, check `ls -l /dev/hidg0` and `ls -l /run/procon.sock`, and watch `journalctl -u procon.service -f` (or an interactive `procon.py` log) for `USB 80 …` and `SPI read` lines.

### Install this handshake fix and capture one clean attempt

First preserve the currently installed Pi files. Run this on the Pi over
Wi-Fi SSH, before copying the new files:

```bash
mkdir -p ~/switch-controller/rollback-pre-handshake-fix
cp -a ~/switch-controller/procon.py \
  ~/switch-controller/add_procon_gadget.sh \
  ~/switch-controller/README.md \
  ~/switch-controller/rollback-pre-handshake-fix/
```

On the computer holding this directory, copy the three audited files:

```bash
scp procon.py add_procon_gadget.sh README.md \
  s3-coaching@SwitchController1.local:~/switch-controller/
```

Then run this exact clean test on the Pi:

```bash
sudo systemctl stop procon.service 2>/dev/null || true
pgrep -af '[p]rocon.py' || true
python3 ~/switch-controller/procon.py --self-test
sudo ~/switch-controller/add_procon_gadget.sh
sudo python3 -u ~/switch-controller/procon.py --no-socket 2>&1 | \
  tee ~/switch-controller/switch-init.log
```

Do not plug the USB data cable into the Switch until the final command prints
`listening on /dev/hidg0`. Leave it connected for one initialization attempt,
then press Ctrl-C. The log prints every host report as `RX` and every `0x81` or
`0x21` initialization reply as `TX`, each as a complete 64-byte hex report.
High-rate `0x10` rumble is logged at most once per second with a suppressed
count. A healthy USB transition includes:

```text
RX 8001...  then TX 81010003...
RX 8002...  then TX 8102...
(possibly RX 8003... then TX 8103...)
RX 8004...
```

There must be no `0x30` write before `RX 8004`. If a critical reply cannot be
queued, the transcript explicitly says `TX dropped (IN endpoint not ready)`.
Copy the capture back for review:

```bash
scp s3-coaching@SwitchController1.local:~/switch-controller/switch-init.log .
```

### Roll back

Stop Python with Ctrl-C, then run on the Pi:

```bash
cp -a ~/switch-controller/rollback-pre-handshake-fix/procon.py \
  ~/switch-controller/rollback-pre-handshake-fix/add_procon_gadget.sh \
  ~/switch-controller/rollback-pre-handshake-fix/README.md \
  ~/switch-controller/
sudo ~/switch-controller/add_procon_gadget.sh remove
```

---

## Protocol v1 (remote clients)

`procon.py --v1-listen 0.0.0.0:47800 --v1-token-file ~/.config/procon/v1-token`
also serves the controller-level JSON-lines protocol in [PROTOCOL.md](PROTOCOL.md)
(command IDs, ack on receipt, results that say whether the input reached a 0x30
report, duplicate detection, neutral on disconnect). It runs alongside the Unix
socket; all command sources share one lock. `procon.service` enables it, and
`./deploy.sh` creates the token on first run.

```bash
python3 -m unittest discover -s tests          # no Switch or hidg0 needed
./deploy.sh --install-service                   # needs passwordless sudo on the Pi
python3 procon_send.py --v1 127.0.0.1 ping      # on the Pi
python3 procon_send.py --v1 127.0.0.1 tap a 80
```

---

## Splatoon 3 replay code → recorded video

From the Mac (this repo is under git; `./deploy.sh` rsyncs it to the Pi and
runs both self-tests):

```bash
./deploy.sh
./capture.py list                          # HDMI capture card must show up
./capture.py snap captures/now.jpg         # check what the Switch shows
ssh s3-coaching@192.168.0.252 'python3 ~/switch-controller/replay.py play --dry-run RXE0-4NDK-KSEC-A05W'
./review.sh RXE0-4NDK-KSEC-A05W            # records to captures/
```

`review.sh` expects the Lobby Terminal menu open with the cursor on its first
item and `procon_send.py status` reporting `stream=on`. `replay.py type CODE`
types only the code (keyboard already open). The keyboard grid and menu
positions in `replay.py` are **unverified** until checked against a capture
frame.

---

## References

- https://gist.github.com/mzyy94/60ae253a45e2759451789a117c59acf9
- https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering
- Plan: `docs/pi-zero-switch-controller.md` in this store

Presenting Nintendo’s USB-IF VID/PID makes the Pi impersonate Nintendo hardware. This is a technical reconstruction of public research for a personal gadget, not a license to ship a product or bypass anything Nintendo forbids.
