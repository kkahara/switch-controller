# Pi Zero W — Nintendo Switch Pro Controller over Bluetooth

**UNTESTED ON A REAL SWITCH in this environment.** These steps were assembled from public joycontrol / nxbt / BlueZ notes. They have **not** been run against a Nintendo Switch here. Treat this as a starting point. If Bluetooth is flaky, **stop and use USB** (`README.md`, `add_procon_gadget.sh`, `procon.py`) — that remains the primary path.

Target device (same as USB):

- Raspberry Pi Zero W, hostname `SwitchController1`, user `s3-coaching`
- Raspberry Pi OS **32-bit Bookworm**, `uname -m` → **`armv6l`**
- USB gadget files in this folder stay installed. Do not delete them.

Wi-Fi SSH stays the control channel. Power the Pi from the **PWR** micro-USB socket while pairing so the **USB** data port is free (and not already enumerating a wired Pro gadget on the Switch).

---

## Short command sequence (on the Pi)

Copy the helper scripts from this folder onto the Pi (same `~/switch-controller/` as the USB files), then:

```bash
ssh s3-coaching@SwitchController1.local

# 1) USB gadget may stay installed. Stop the USB daemon so it is not also
#    pretending to be a Pro Controller, and so the Zero has CPU/RAM for BT.
sudo systemctl stop procon.service
sudo pkill -f '/home/s3-coaching/switch-controller/procon.py' || true

# 2) Unblock radio, start BlueZ, hide the input plugin (HID PSM 17/19).
chmod +x ~/switch-controller/enable_bluetooth.sh ~/switch-controller/install_joycontrol.sh
sudo ~/switch-controller/enable_bluetooth.sh

# 3) Install joycontrol into ~/switch-controller/.venv-joycontrol (once).
~/switch-controller/install_joycontrol.sh

# 4) On the Switch: Home → Controllers → Change Grip/Order.
#    Do NOT use System Settings → Bluetooth. Stay on Change Grip/Order.

# 5) First-time pair (Pi is the HID *device*; run as root):
sudo ~/switch-controller/.venv-joycontrol/bin/python3 \
  ~/switch-controller/joycontrol/run_controller_cli.py PRO_CONTROLLER
```

When a Pro Controller icon appears on Change Grip/Order, press **A once** on a real pad (or type `a` in the joycontrol CLI) and wait for the menu to exit. Then smoke-test:

```bash
# already-paired reconnect — replace with the Switch MAC from bluetoothctl
sudo ~/switch-controller/.venv-joycontrol/bin/python3 \
  ~/switch-controller/joycontrol/run_controller_cli.py PRO_CONTROLLER \
  -r AA:BB:CC:DD:EE:FF
# at the prompt:  a
# or one-shot:
sudo ~/switch-controller/.venv-joycontrol/bin/python3 \
  ~/switch-controller/bt_tap_a.py -r AA:BB:CC:DD:EE:FF
```

---

## 1. Enable Bluetooth (USB gadget can stay)

The USB HID gadget (`procon.service` / `procon.py` / `add_procon_gadget.sh`) is a **different stack**. Leave those files on disk. For pairing, only **stop the running USB emulator**:

```bash
sudo systemctl stop procon.service
# if you started it by hand:
sudo pkill -f '/home/s3-coaching/switch-controller/procon.py' || true
```

Do **not** `systemctl disable --now procon.service` unless you want it gone across reboots. Do **not** uninstall `g_cdc`. Power from **PWR**; keep Wi-Fi SSH. Unplug the Pi **USB** data cable from the Switch while pairing over Bluetooth so the console is not looking at a wired gadget at the same time.

Then:

```bash
sudo rfkill unblock bluetooth
sudo rfkill list                    # bluetooth should be Soft blocked: no
sudo systemctl enable --now bluetooth.service
hciconfig -a                        # expect hci0  (or: bluetoothctl show)
sudo bluetoothctl power on
sudo bluetoothctl show              # Powered: yes
```

`enable_bluetooth.sh` does the rfkill / service / input-plugin drop-in for you.

Pi OS sometimes soft-blocks BT after boot. `rfkill unblock bluetooth` is required even when `bluetooth.service` is enabled.

---

## 2. Recommended stack: **joycontrol** (primary)

| Stack | On this board (32-bit Bookworm, **armv6l**) | Role |
|---|---|---|
| **[joycontrol](https://github.com/mart1nro/joycontrol)** | Python + system `libhidapi` + BlueZ L2CAP. Documented on **Pi Zero W**. No armv7 wheel. | **Primary** |
| **[nxbt](https://github.com/Brikwerk/nxbt)** | Author tested **Pi 3 / Pi 4** (armv7 / aarch64). PyPI is an sdist that pins `cryptography==3.3.2` and `dbus-python==1.2.16` — those **compile** on a Zero and often fail or take hours. Third-party “nxbt binaries” are typically **armv7/aarch64-only** and will not run on `armv6l`. Python **≥ 3.12** is broken; Bookworm’s 3.11 is theoretically OK, the CPU/ISA is the problem. | Fallback only, after joycontrol |

**Primary = joycontrol.** It is the one that actually matches this ISA: no prebuilt armv7 wheel, `pip` builds a pure-enough package against Debian `libhidapi-hidraw0` and `python3-dbus`.

`install_joycontrol.sh` uses a venv with `--system-site-packages` so `python3-dbus` comes from apt (compiling `dbus-python` on a Zero is a bad time). Bookworm’s PEP 668 block is why this is a venv, not `sudo pip3 install`.

---

## 3. BlueZ pitfalls on the Pi

The Switch talks HID over L2CAP **PSM 17** (control) and **PSM 19** (interrupt). BlueZ’s **input** plugin binds those ports for real Bluetooth keyboards/mice. If it is loaded, joycontrol/nxbt die with “Address already in use” / cannot advertise as a controller.

**Fix:** a systemd drop-in (do **not** edit `/lib/systemd/system/bluetooth.service` — upgrades overwrite it). Bookworm’s daemon is `/usr/libexec/bluetooth/bluetoothd` (older images used `/usr/lib/bluetooth/bluetoothd`).

```bash
# enable_bluetooth.sh writes this; shown here so you can inspect it
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
# ExecStart=  (empty) is required so the drop-in replaces the vendor command.
sudo tee /etc/systemd/system/bluetooth.service.d/01-disable-input.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=input
EOF
sudo systemctl daemon-reload
sudo systemctl restart bluetooth.service
ps aux | grep bluetoothd    # must show --noplugin=input
```

If `bluetoothd` is still at the old path, point `ExecStart` at `/usr/lib/bluetooth/bluetoothd` instead. `enable_bluetooth.sh` picks whichever exists.

Other traps:

| Pitfall | What to do |
|---|---|
| Must run the emulator **as root** | L2CAP bind + D-Bus adapter setup need uid 0. `sudo …/bin/python3 run_controller_cli.py …` |
| `input` plugin still loaded | Drop-in missing empty `ExecStart=`, or you edited the vendor unit and it was overwritten |
| Pairing from `bluetoothctl` / Switch **Bluetooth** list | Will not first-pair a Pro Controller. Use **Change Grip/Order** only |
| USB `procon.py` still running | Stop it. Two fake Pros + a 512 MB Zero is a disconnect factory |
| Wi-Fi drops during pair | Zero W **shares one antenna** between `wlan0` and `hci0`. Sit close to the Switch; retry; USB is the stable path |
| `No Available Adapters` / no `hci0` | `rfkill`, `bluetooth.service`, firmware; then `dmesg \| grep -i bluetooth` |

Disabling `input` means this Pi will not act as a host for Bluetooth keyboards/mice until you remove the drop-in. Fine for a dedicated Switch gadget.

---

## 4. Switch pairing (Change Grip/Order)

The Switch **does not** first-pair Pro Controllers from **System Settings → Bluetooth**. That list is for headsets and the like.

**First-time pairing**

1. Pi: USB daemon stopped, Bluetooth unblocked, `input` plugin off, joycontrol **not started yet**.
2. Switch: **Home → Controllers → Change Grip/Order** (or Controllers button on the Home screen). The screen is four gray pads and “Press L + R …”.
3. Pi, as root, start advertising:

   ```bash
   sudo ~/switch-controller/.venv-joycontrol/bin/python3 \
     ~/switch-controller/joycontrol/run_controller_cli.py PRO_CONTROLLER
   ```

4. Wait on Change Grip/Order until a Pro Controller icon / player light appears (can take 10–60 s; the menu itself is slow to ingest HID).
5. Exit that menu with **one** A (real pad or joycontrol prompt `a`). Mashing A/B while the 15 Hz pairing menu is up is a common disconnect.

**Reconnect** (after a successful first pair; Switch can be on the Home screen, not Change Grip/Order):

```bash
sudo bluetoothctl devices
# note the Switch MAC (often labelled like "Nintendo Switch")

sudo ~/switch-controller/.venv-joycontrol/bin/python3 \
  ~/switch-controller/joycontrol/run_controller_cli.py PRO_CONTROLLER \
  -r AA:BB:CC:DD:EE:FF
```

If reconnect flaps in `bluetoothctl` (connect/disconnect loop), the Switch is trying to **pair** again. Forget the controller on the Switch, go back to Change Grip/Order, and pair without `-r`.

---

## 5. Send A / buttons from Python (for later Splatoon 3 Review timeline)

After a pair, the joycontrol CLI understands `a`, `b`, `x`, `y`, `plus`, `home`, `up`, `down`, `left`, `right`, plus `hold a` / `release a`.

One-shot tap (helper in this folder):

```bash
sudo ~/switch-controller/.venv-joycontrol/bin/python3 \
  ~/switch-controller/bt_tap_a.py -r AA:BB:CC:DD:EE:FF
```

Programmatic (same process that owns the HID session — this is **not** the USB `/run/procon.sock` client):

```python
# UNTESTED ON A REAL SWITCH. Run as root with joycontrol on PYTHONPATH / the venv.
import asyncio
from joycontrol.protocol import controller_protocol_factory
from joycontrol.server import create_hid_server
from joycontrol.controller import Controller
from joycontrol.controller_state import button_push

SWITCH_MAC = "AA:BB:CC:DD:EE:FF"   # omit / None for first Change Grip/Order pair

async def tap_a():
    factory = controller_protocol_factory(Controller.PRO_CONTROLLER)
    transport, protocol = await create_hid_server(
        factory, reconnect_bt_addr=SWITCH_MAC, ctl_psm=17, itr_psm=19)
    state = protocol.get_controller_state()
    await state.connect()
    await button_push(state, "a")          # smoke test
    await button_push(state, "right")      # later: Review timeline nudge
    await transport.close()

asyncio.run(tap_a())
```

That is the hook for a later Splatoon 3 **Review** timeline scroller (d-pad left/right or stick, then A). Do not start that automation until this A tap is visible on the Switch Controllers / Test Input Devices screen.

**nxbt-shaped macros** (only if you install nxbt later on a Pi 3/4, not this Zero’s expected path):

```python
import nxbt
nx = nxbt.Nxbt()
idx = nx.create_controller(
    nxbt.PRO_CONTROLLER,
    reconnect_address=nx.get_switch_addresses() or None,
)
nx.wait_for_connection(idx)
nx.macro(idx, "A 0.1s\n0.1s")
nx.press_buttons(idx, [nxbt.Buttons.A])
```

CLI equivalent: `sudo nxbt macro -c "A 0.1s\n0.1s" -r` after a first pair.

---

## 6. Pi Zero W Bluetooth is flaky — USB stays primary

The Zero W’s CYW43438 shares the antenna between Wi-Fi and Bluetooth. joycontrol and nxbt both warn about random drops even on Pi 4. On this board, expect:

- Pairing that works once and never reconnects
- SSH hiccups while `hci0` is advertising
- Disconnect when leaving Change Grip/Order
- Need to sit the Pi within a metre of the Switch, away from 2.4 GHz noise

If BT does not pair after a couple of clean attempts (input plugin off, USB daemon stopped, Change Grip/Order, root), **use USB**:

```bash
sudo ~/switch-controller/add_procon_gadget.sh
sudo systemctl start procon.service
python3 ~/switch-controller/procon_send.py tap a
```

See `README.md`. USB is the path that already enumerates HID on this Pi (`80 01` / `80 02`). Bluetooth is optional.

---

## Files in this folder

| File | Purpose |
|---|---|
| `README.md`, `add_procon_gadget.sh`, `procon.py`, `procon_send.py`, `procon.service` | **USB** gadget — keep; primary path |
| `bluetooth.md` | This document |
| `enable_bluetooth.sh` | `rfkill`, `bluetooth.service`, disable BlueZ `input` plugin; stops `procon.service` |
| `install_joycontrol.sh` | apt deps + venv + clone `mart1nro/joycontrol` |
| `bt_tap_a.py` | Root helper: pair/reconnect and tap A |

Copy onto the Pi with the USB files:

```bash
scp enable_bluetooth.sh install_joycontrol.sh bt_tap_a.py bluetooth.md \
  s3-coaching@SwitchController1.local:~/switch-controller/
```

---

## References

- https://github.com/mart1nro/joycontrol
- https://github.com/Brikwerk/nxbt
- https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering
- USB path in this folder: `README.md`

Presenting as a Nintendo Pro Controller over Bluetooth is the same class of public reverse-engineering as the USB gadget. Personal gadget only; not a license to ship a product.
