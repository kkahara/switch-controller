# switch-controller — network-controlled virtual Pro Controller

A Raspberry Pi Zero W that presents itself to a Nintendo Switch as a **wired
Pro Controller** over USB, and accepts controller-level commands from the Mac
over the LAN (protocol v1, [PROTOCOL.md](PROTOCOL.md)).

The Pi knows controllers only. It has no notion of games, screens, menus,
capture or automation; those live in the Mac repository
`switch-capture-automation`. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status

| Area | Status | Evidence |
|---|---|---|
| USB gadget, Pro Controller handshake, `0x30` stream, buttons via the Unix socket | **Verified** on a real Switch | tag `baseline-working` (`11821fd`, 2026-10-14): `procon_send.py tap home` / `tap a` visibly received |
| Protocol v1 over TCP (Pi-local and Mac → Pi), neutral on disconnect | **Verified** on a real Switch | tag `step2-hardware-accepted` (`f819194`, 2026-10-14) |
| Buttons, d-pad, sticks, neutral from the Mac controller UI | **Supported**, in daily manual use | |
| Synthetic IMU pitch (`rotate_pitch`) | **Experimental** | Produces visible camera motion in game. Gyro sign vs. accelerometer direction and the scale relationship are not calibrated (and the Pi deliberately knows nothing about any game's motion sensitivity) |
| Bluetooth (joycontrol), Splatoon replay-code scripts, Mac capture helper | **Legacy**, unsupported | moved to [legacy/](legacy/README.md) |

Known hardware observations (from the baseline session):

- `stream=off` means the Switch is asleep or not polling. Waking it re-enumerates
  the gadget and `stream=on` follows ~3 s later. Protocol v1 reports this as
  `not_streaming`; the legacy socket's `ok` only means the state changed.
- The gadget negotiates **high-speed** (`max_speed` unset); a real Pro is
  full-speed. It works.
- The Switch occasionally re-enumerates the gadget (e.g.
  `End Point Request ERROR: -108`). Unexplained; watch for it.

## Repository layout

| Path | Role |
|---|---|
| `procon.py` | The controller: USB handshake, subcommands, SPI blobs, `0x30` reports, controller state, synthetic IMU, Unix-socket server, CLI, self-test. Sole owner of `/dev/hidg0` |
| `protocol_v1.py` | Protocol v1 TCP server (sessions, ack/result, duplicates, idle neutral) |
| `PROTOCOL.md`, `protocol/v1/` | **Authoritative** protocol specification, version, and golden fixtures |
| `procon_send.py` | Command-line client (Unix socket or protocol v1) |
| `add_procon_gadget.sh` | configfs: replace the boot `g_cdc` gadget with the HID-only Pro Controller gadget, and restore it |
| `procon.service` | systemd unit (gadget setup, daemon, restore on stop) |
| `deploy.sh` | rsync to the Pi, run the self-test and unit tests there, optionally install the service |
| `tests/` | Protocol and IMU tests; run without a Switch or `/dev/hidg0` |
| `docs/` | Architecture notes |
| `legacy/` | Historical, unsupported code (not deployed) |

Target device: Raspberry Pi Zero W, Raspberry Pi OS 32-bit, user `s3-coaching`,
hostname `SwitchController1` (LAN `192.168.0.252`), Wi-Fi SSH on the main LAN.

## Deploy

From the Mac:

```bash
python3 -m unittest discover -s tests     # local; no Switch needed
./deploy.sh                                # copy + tests on the Pi; daemon untouched
./deploy.sh --install-service              # also install and restart procon.service (passwordless sudo)
```

`deploy.sh` never deletes remote files and creates the protocol v1 token
(`~/.config/procon/v1-token`) on first run.

Cabling: the Pi **USB** micro-USB port (next to mini-HDMI), a **data** cable,
no OTG hub. Never the **PWR** socket for data.

## Boot gadget vs. controller gadget

Default boot keeps the Mac setup gadget (`g_cdc`: CDC composite + ACM serial,
`getty@ttyGS0`). **Do not** put the Pro HID gadget in `cmdline.txt`.
`add_procon_gadget.sh` unbinds `g_cdc` and binds the HID-only gadget
(VID/PID `0x057E`/`0x2009`, the real 203-byte report descriptor);
`add_procon_gadget.sh remove` restores `g_cdc`. Do this over **Wi-Fi SSH**:
unbinding `g_cdc` drops any USB serial session.

## systemd daemon (normal operation)

`procon.service` runs the gadget script, then `procon.py` as root with the
Unix socket `/run/procon.sock` (mode 0666) and protocol v1 on `0.0.0.0:47800`.
Stopping it restores `g_cdc`.

```bash
sudo systemctl status procon.service --no-pager
journalctl -u procon.service -f           # USB 80 xx, v1 {json} events
```

**Only one process may open `/dev/hidg0`.** Everything else talks to the
daemon:

```bash
python3 ~/switch-controller/procon_send.py tap a           # Unix socket
python3 ~/switch-controller/procon_send.py status
python3 ~/switch-controller/procon_send.py --v1 127.0.0.1 tap a 80
python3 ~/switch-controller/procon_send.py --v1 127.0.0.1 rotate_pitch 15 500
```

Socket words: `tap`, `press`, `release`, `dpad`, `stick`, `center`,
`neutral`, `status`, `help`. Sticks take floats `-1..1` or raw `0..4095`
(neutral `2048`).

Programmatic, still without opening `/dev/hidg0`:

```python
import sys
sys.path.insert(0, "/home/s3-coaching/switch-controller")
from procon import ProconClient

pad = ProconClient()          # /run/procon.sock
pad.tap("a")
pad.stick("l", 0, 1)
pad.neutral()                 # also zeroes the synthetic IMU orientation
print(pad.status())
```

Socket permissions **0666** are intentional on this personal Pi. To tighten:
create a `procon` group, change `SOCKET_MODE` in `procon.py` to `0o660`, and
`chgrp` the socket.

## Interactive / manual start

Stop the service first (`sudo systemctl stop procon.service`), then over Wi-Fi SSH:

```bash
sudo ~/switch-controller/add_procon_gadget.sh
sudo python3 ~/switch-controller/procon.py          # TTY commands + /run/procon.sock
# ... plug the Pi USB port into the Switch, test, then quit / Ctrl-C
sudo ~/switch-controller/add_procon_gadget.sh remove
```

At startup `procon.py` rebinds the UDC, waits 0.5 s for enumeration, opens
`/dev/hidg0`, then starts its reader. It sends **no** `0x30` reports until the
Switch sends `80 04`; `80 05` stops the stream. Unsolicited `0x30` reports
before then can occupy the IN endpoint and drop a handshake reply.

### Capturing a USB initialization log

```bash
sudo systemctl stop procon.service
python3 ~/switch-controller/procon.py --self-test
sudo ~/switch-controller/add_procon_gadget.sh
sudo python3 -u ~/switch-controller/procon.py --no-socket 2>&1 | tee ~/switch-controller/switch-init.log
```

Plug into the Switch only after `listening on /dev/hidg0`. A healthy
transition logs `RX 8001 → TX 8101…`, `RX 8002 → TX 8102`, optionally
`RX 8003 → TX 8103`, then `RX 8004`, with no `0x30` write before `RX 8004`.

## If the Switch does not see the controller

- The boot `g_cdc` gadget is still bound (run `add_procon_gadget.sh`).
- Charge-only cable, or cable in **PWR**.
- `procon.py` not running during enumeration (missed `0x80` handshake).
- The Switch is asleep (`stream=off` / `not_streaming`).
- A second process tried to open `/dev/hidg0`.

Check `ls -l /dev/hidg0 /run/procon.sock` and `journalctl -u procon.service -f`.

## What the code implements

| Piece | Source | Notes |
|---|---|---|
| VID/PID `0x057E`/`0x2009`, strings, 64-byte interrupt HID | mzyy94 `add_procon_gadget.sh` | HID **only** — no ACM/ECM in this composite |
| 203-byte report descriptor | mzyy94 dump | Do not invent or “simplify” these bytes |
| USB `0x80` → `0x81` handshake | dekuNukem USB-HID-Notes + gist log | `80 01` status+MAC, `80 02` handshake, `80 03` baud, `80 04` start stream; `80 05` stops it |
| Subcommands `0x02` device info, `0x10` SPI, `0x03` mode `0x30`, `0x40` IMU | dekuNukem subcommand notes + gist | Other subcommands are ACK stubs |
| SPI calibration blobs | gist SPI table; factory IMU sample from dekuNukem | |
| `0x30` buttons/sticks/IMU, ~125 Hz | dekuNukem HID + IMU notes | D-pad is bits on byte 5, **not** a HID hat |
| Synthetic IMU | this repo | Persistent pitch orientation → gravity `(sin, 0, cos) × 4096`; gyro Y carries the pitch rate only while a `rotate_pitch` is in progress |
| Control socket `/run/procon.sock`, protocol v1 TCP | this repo | All command sources share one lock |

Handshake MAC uses the public wired dump `04:03:D6:1D:86:1F` with LE (`80 01`)
vs. BE (subcommand `0x02`) byte order.

## References

- https://gist.github.com/mzyy94/60ae253a45e2759451789a117c59acf9
- https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering

Presenting Nintendo’s USB-IF VID/PID makes the Pi impersonate Nintendo
hardware. This is a technical reconstruction of public research for a personal
gadget, not a license to ship a product or bypass anything Nintendo forbids.
