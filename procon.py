#!/usr/bin/env python3
"""USB HID Nintendo Switch Pro Controller emulator for a Pi Zero W gadget.

UNTESTED ON A REAL SWITCH.

Talks Nintendo's vendor protocol on /dev/hidg0 (64-byte reports), not a generic
HID gamepad. Handshake, subcommand stubs, SPI cal blobs, and the 203-byte
descriptor live in public dumps:

  - mzyy94 gist (gadget + simulate_procon.py + wired bypass log)
    https://gist.github.com/mzyy94/60ae253a45e2759451789a117c59acf9
  - dekuNukem USB-HID-Notes / bluetooth HID / IMU / SPI notes
    https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering

No physical IMU: 0x30 reports pack synthetic gravity (zeros gyro, ~1 g on one
accel axis). Default boot gadget on this Pi is still g_cdc; run this only after
add_procon_gadget.sh has bound the HID-only composite.

CLI:
    sudo python3 procon.py              # HID daemon + stdin (TTY) + Unix socket
    sudo python3 procon.py --tap a
    python3 procon.py --send 'tap a'    # client; does not open /dev/hidg0
    python3 procon_send.py tap a

Only one process may open /dev/hidg0. systemd runs this as a daemon (stdin is
EOF); button/stick commands then arrive on the Unix socket, default
/run/procon.sock (mode 0666 so user s3-coaching can connect).

API (same process, owns the gadget):
    from procon import ProController
    pad = ProController()
    pad.start()
    pad.press("A")
    pad.set_stick("left", 2048, 4095)
    pad.tap("B", 0.15)
    pad.release("A")

API (second process, talks to the daemon socket):
    from procon import ProconClient
    ProconClient().tap("a")
"""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
from typing import Dict, Iterable, Optional, Tuple

HIDG_DEFAULT = "/dev/hidg0"
SOCKET_DEFAULT = "/run/procon.sock"
# Personal Pi Zero: world-writable so s3-coaching can connect without a group.
# Prefer root:procon 0660 if you add a procon group.
SOCKET_MODE = 0o666
REPORT_SIZE = 64
STICK_CENTER = 0x800  # 2048; 12-bit neutral
STICK_MAX = 0xFFF
ACCEL_1G = 4096  # uncalibrated counts per g (hid-nintendo / dekuNukem)
USB_INTERVAL_S = 0.008  # HID bInterval 8 on full-speed → ~125 Hz

# Public wired Pro dump (mzyy94 bypass_procon_log): Nintendo OUI 04:03:D6.
MAC_BE = bytes.fromhex("0403d61d861f")
MAC_LE = MAC_BE[::-1]

# Battery full + USB-powered Pro (protocol plan). Charging Grip bit is separate.
BATTERY_USB_PRO = 0x81

# Gist idle sticks + vibration byte (near-center 12-bit pack).
IDLE_LEFT = bytes.fromhex("f8d77a")
IDLE_RIGHT = bytes.fromhex("22c87b")
VIBRATION_IDLE = 0x0C

# Firmware 3.72 / type Pro / unknown 0x02 from the wired dump device-info reply.
FW_VERSION = bytes.fromhex("0348")

# ---------------------------------------------------------------------------
# SPI flash stubs (sparse 0xFF image). Addresses from dekuNukem spi_flash_notes
# and hid-nintendo; payloads from mzyy94 simulate_procon.py / bypass log, plus
# dekuNukem's published factory IMU sample at 0x6020.
# ---------------------------------------------------------------------------

SPI_FLASH_SIZE = 0x10000


def _le_s16_xyz(*values: int) -> bytes:
    return struct.pack("<%dh" % len(values), *values)


def build_spi_flash() -> bytearray:
    flash = bytearray(b"\xff" * SPI_FLASH_SIZE)

    def put(addr: int, data: bytes) -> None:
        flash[addr : addr + len(data)] = data

    # 0x6000 serial: unset (first byte >= 0x80 / all 0xFF)
    put(0x6000, b"\xff" * 16)
    put(0x6012, b"\x03")  # Pro Controller
    put(0x6013, b"\xa0")
    put(0x601B, b"\x01")  # use SPI colors

    # 0x6020 factory IMU (24 bytes). dekuNukem sample, stored little-endian:
    # acc origin FFB0 FEB9 00E0, acc coeff 4000, gyro origin 000E FFDF FFD0,
    # gyro coeff 343B (±2000 dps uncalibrated).
    put(
        0x6020,
        _le_s16_xyz(
            -80,
            -327,
            224,
            0x4000,
            0x4000,
            0x4000,
            14,
            -33,
            -48,
            0x343B,
            0x343B,
            0x343B,
        ),
    )

    # 0x603D factory stick cal (mzyy94). Left 9 + right 9.
    put(0x603D, bytes.fromhex("ba156211b87f29065b"))
    put(0x6046, bytes.fromhex("ffe77e0e36569e8560"))
    put(0x604F, b"\xff")
    # Body/button colors from the real wired dump (grey / white), not invented.
    put(0x6050, bytes.fromhex("323232"))
    put(0x6053, bytes.fromhex("ffffff"))
    put(0x6056, bytes.fromhex("ffffff"))
    put(0x6059, bytes.fromhex("ffffff"))

    # 0x6080 factory sensor + stick params (mzyy94 / Pro horizontal offsets
    # FD50,0000,0FC6 match dekuNukem Pro defaults).
    put(0x6080, bytes.fromhex("50fd0000c60f0f30619630f3d41454411554c7799c333663"))
    put(0x6098, bytes.fromhex("0f30619630f3d41454411554c7799c333663"))

    # 0x8010 user stick cal unused (0xFF). 0x8026 magic B2A1 → user IMU present,
    # as in the wired 0x8010 length-0x18 read that ended ...ffff b2a1.
    put(0x8010, b"\xff" * 22)
    put(0x8026, bytes.fromhex("b2a1"))
    put(0x8028, bytes.fromhex("beff3e00f001004000400040fefffeff0800e73be73be73b"))
    return flash


# Button name → (byte index in the 3-byte Nintendo map, bit).
# Byte 0 = report[3] Y X B A SR SL R ZR
# Byte 1 = report[4] Minus Plus R-Stick L-Stick Home Capture — ChargingGrip
# Byte 2 = report[5] Down Up Right Left SR SL L ZL
BUTTONS: Dict[str, Tuple[int, int]] = {
    "y": (0, 0x01),
    "x": (0, 0x02),
    "b": (0, 0x04),
    "a": (0, 0x08),
    "r-sr": (0, 0x10),
    "r-sl": (0, 0x20),
    "r": (0, 0x40),
    "zr": (0, 0x80),
    "minus": (1, 0x01),
    "plus": (1, 0x02),
    "rstick": (1, 0x04),
    "lstick": (1, 0x08),
    "home": (1, 0x10),
    "capture": (1, 0x20),
    "charging": (1, 0x80),
    "down": (2, 0x01),
    "up": (2, 0x02),
    "right": (2, 0x04),
    "left": (2, 0x08),
    "l-sr": (2, 0x10),
    "l-sl": (2, 0x20),
    "l": (2, 0x40),
    "zl": (2, 0x80),
}

DPAD = ("up", "down", "left", "right")
ALIASES = {
    "+": "plus",
    "-": "minus",
    "select": "minus",
    "start": "plus",
    "l3": "lstick",
    "r3": "rstick",
    "zl": "zl",
    "zr": "zr",
    "screenshot": "capture",
}


def normalize_button(name: str) -> str:
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    if key not in BUTTONS:
        raise KeyError("unknown button %r; known: %s" % (name, ", ".join(sorted(BUTTONS))))
    return key


def pack_stick(x: int, y: int) -> bytes:
    x &= 0xFFF
    y &= 0xFFF
    return bytes((x & 0xFF, (x >> 8) | ((y & 0x0F) << 4), (y >> 4) & 0xFF))


def unpack_stick(data: bytes) -> Tuple[int, int]:
    horizontal = data[0] | ((data[1] & 0x0F) << 8)
    vertical = (data[1] >> 4) | (data[2] << 4)
    return horizontal, vertical


def parse_axis(value: str) -> int:
    """Accept 0..4095 raw, or a float in -1.0..1.0 (0 = center).

    Integers whose abs is <= 1 are treated as normalized so `stick l 0 1`
    means full up, not raw (0, 1). Use 0..4095 for raw.
    """
    text = value.strip()
    if "." in text:
        f = float(text)
        return max(0, min(STICK_MAX, int(round(STICK_CENTER + f * (STICK_CENTER - 1)))))
    n = int(text, 0)
    if n in (-1, 0, 1):
        return max(0, min(STICK_MAX, int(round(STICK_CENTER + n * (STICK_CENTER - 1)))))
    return max(0, min(STICK_MAX, n))


def pack_imu_sample(ax: int, ay: int, az: int, gx: int, gy: int, gz: int) -> bytes:
    return struct.pack("<6h", ax, ay, az, gx, gy, gz)


def synthetic_gravity_imu(axis: str = "z", g: int = ACCEL_1G) -> bytes:
    """Three identical samples: gyro at rest, ~1 g on one accel axis.

    All-zero accel is free-fall; do not send that as a rest pose.
    """
    ax = ay = az = 0
    sign = -1 if axis.startswith("-") else 1
    name = axis.lstrip("+-").lower()
    mag = sign * g
    if name == "x":
        ax = mag
    elif name == "y":
        ay = mag
    elif name == "z":
        az = mag
    else:
        raise KeyError("gravity axis must be x|y|z or ±x/±y/±z, got %r" % axis)
    sample = pack_imu_sample(ax, ay, az, 0, 0, 0)
    return sample * 3


IMU_TILT_PRESETS = {
    "neutral": (0, 0, ACCEL_1G),
    "look_up": (0, -2896, 2896),
    "look_down": (0, 2896, 2896),
    "look_left": (-2896, 0, 2896),
    "look_right": (2896, 0, 2896),
}


def synthetic_tilt_imu(preset: str) -> bytes:
    """Return three identical synthetic IMU samples for a named tilt pose.

    The accelerometer vector represents gravity at roughly a 45-degree tilt.
    Gyro values remain zero so a persistent preset represents a held pose
    rather than an unbounded continuous rotation.
    """
    try:
        ax, ay, az = IMU_TILT_PRESETS[preset]
    except KeyError as exc:
        raise KeyError(
            "unknown IMU tilt %r; known: %s" % (preset, ", ".join(IMU_TILT_PRESETS))
        ) from exc
    return pack_imu_sample(ax, ay, az, 0, 0, 0) * 3


class ProController:
    """Nintendo 0x80/0x81 handshake then 0x30 input stream on a hidg device."""

    def __init__(
        self,
        device: str = HIDG_DEFAULT,
        stream_hz: float = 1.0 / USB_INTERVAL_S,
        verbose: bool = True,
        reset_udc: bool = True,
        gadget_path: str = "/sys/kernel/config/usb_gadget/procon",
    ) -> None:
        self.device = device
        self.stream_period = 1.0 / stream_hz if stream_hz else USB_INTERVAL_S
        self.verbose = verbose
        self.reset_udc_on_start = reset_udc
        self.gadget_path = gadget_path

        self._fd: Optional[int] = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._stream = threading.Event()
        self._threads: list[threading.Thread] = []
        self._rumble_last_log = 0.0
        self._rumble_suppressed = 0
        self._stream_report_logged = False

        self.timer = 0
        self.buttons = bytearray(3)
        self.buttons[1] = 0x80  # Charging Grip, as in wired idle dumps
        self.lx = self.ly = self.rx = self.ry = STICK_CENTER
        # Seed sticks from the public idle pack so first frames match the gist.
        self.lx, self.ly = unpack_stick(IDLE_LEFT)
        self.rx, self.ry = unpack_stick(IDLE_RIGHT)
        self.imu_enabled = False
        self.gravity_axis = "z"
        self.imu_preset = "neutral"
        self.player_lights = 0x00
        self.spi = build_spi_flash()

        # Serializes every command source (stdin, Unix socket, protocol v1).
        self.command_lock = threading.RLock()
        self.started_mono_ns = time.monotonic_ns()
        # state_gen bumps on every input change; report_seq counts successful
        # 0x30 writes and reported_gen is the newest state_gen they carried.
        self.state_gen = 0
        self.report_seq = 0
        self.reported_gen = 0
        self._report_cond = threading.Condition()

    def log(self, *args) -> None:
        if self.verbose:
            print(*args, flush=True)

    def log_always(self, *args) -> None:
        print(*args, flush=True)

    # --- public input API -------------------------------------------------

    def press(self, *names: str) -> None:
        keys = [BUTTONS[normalize_button(name)] for name in names]
        with self._lock:
            for idx, bit in keys:
                self.buttons[idx] |= bit
            self.state_gen += 1

    def release(self, *names: str) -> None:
        keys = [BUTTONS[normalize_button(name)] for name in names]
        with self._lock:
            for idx, bit in keys:
                self.buttons[idx] &= ~bit
            self.state_gen += 1

    def release_all(self, keep_charging_grip: bool = True) -> None:
        with self._lock:
            grip = self.buttons[1] & 0x80 if keep_charging_grip else 0
            self.buttons[0] = 0
            self.buttons[1] = grip
            self.buttons[2] = 0
            self.state_gen += 1

    def neutral(self) -> None:
        """Release every button, center sticks, and reset synthetic IMU tilt."""
        with self._lock:
            grip = self.buttons[1] & 0x80
            self.buttons[0] = 0
            self.buttons[1] = grip
            self.buttons[2] = 0
            self.lx = self.ly = self.rx = self.ry = STICK_CENTER
            self.imu_preset = "neutral"
            self.state_gen += 1

    def current_gen(self) -> int:
        with self._lock:
            return self.state_gen

    def is_streaming(self) -> bool:
        return self._stream.is_set()

    def wait_reported(self, gen: int, timeout: float) -> Optional[int]:
        """report_seq of the first 0x30 write carrying state_gen >= gen, or None."""
        with self._report_cond:
            if self._report_cond.wait_for(lambda: self.reported_gen >= gen, timeout):
                return self.report_seq
            return None

    def state_dict(self) -> Dict[str, object]:
        with self._lock:
            pressed = [
                name
                for name, (idx, bit) in BUTTONS.items()
                if name != "charging" and self.buttons[idx] & bit
            ]
            sticks = (self.lx, self.ly, self.rx, self.ry)
            imu = self.imu_enabled
            imu_preset = self.imu_preset
            lights = self.player_lights
        with self._report_cond:
            seq = self.report_seq
        return {
            "buttons": sorted(pressed),
            "sticks_raw": {"l": [sticks[0], sticks[1]], "r": [sticks[2], sticks[3]]},
            "stream": "on" if self.is_streaming() else "off",
            "imu": imu,
            "imu_preset": imu_preset,
            "player_lights": lights,
            "report_seq": seq,
        }

    def tap(self, name: str, hold_s: float = 0.12) -> None:
        self.press(name)
        time.sleep(hold_s)
        self.release(name)

    def set_dpad(self, direction: Optional[str]) -> None:
        target = None
        if direction:
            key = normalize_button(direction)
            if key not in DPAD:
                raise KeyError("not a d-pad direction: %r" % direction)
            target = BUTTONS[key]
        with self._lock:
            for name in DPAD:
                _, bit = BUTTONS[name]
                self.buttons[2] &= ~bit
            if target:
                self.buttons[target[0]] |= target[1]
            self.state_gen += 1

    def set_stick(self, which: str, x: int, y: int) -> None:
        x = max(0, min(STICK_MAX, int(x)))
        y = max(0, min(STICK_MAX, int(y)))
        with self._lock:
            if which.lower() in ("l", "left"):
                self.lx, self.ly = x, y
            elif which.lower() in ("r", "right"):
                self.rx, self.ry = x, y
            else:
                raise KeyError("stick must be left or right, got %r" % which)
            self.state_gen += 1

    def center_sticks(self) -> None:
        self.set_stick("left", STICK_CENTER, STICK_CENTER)
        self.set_stick("right", STICK_CENTER, STICK_CENTER)

    def set_gravity(self, axis: str) -> None:
        synthetic_gravity_imu(axis)  # validate
        with self._lock:
            self.gravity_axis = axis

    def set_imu_preset(self, preset: str) -> None:
        synthetic_tilt_imu(preset)  # validate
        with self._lock:
            self.imu_preset = preset
            self.state_gen += 1

    def snapshot(self) -> str:
        with self._lock:
            return (
                "buttons=%02x %02x %02x sticks=L(%d,%d) R(%d,%d) imu=%s gravity=%s stream=%s"
                % (
                    self.buttons[0],
                    self.buttons[1],
                    self.buttons[2],
                    self.lx,
                    self.ly,
                    self.rx,
                    self.ry,
                    "on" if self.imu_enabled else "off",
                    self.gravity_axis,
                    "on" if self._stream.is_set() else "off",
                )
            )

    # --- report packing ---------------------------------------------------

    def _input_prefix(self) -> bytes:
        """11-byte body used in both 0x21 replies and 0x30 (battery..vibration)."""
        with self._lock:
            body = bytearray(11)
            body[0] = BATTERY_USB_PRO
            body[1:4] = self.buttons
            body[4:7] = pack_stick(self.lx, self.ly)
            body[7:10] = pack_stick(self.rx, self.ry)
            body[10] = VIBRATION_IDLE
            return bytes(body)

    def pack_0x30(self) -> bytes:
        rpt = bytearray(REPORT_SIZE)
        rpt[0] = 0x30
        rpt[1] = self.timer & 0xFF
        prefix = self._input_prefix()
        rpt[2:13] = prefix
        with self._lock:
            imu_on = self.imu_enabled
            axis = self.gravity_axis
            preset = self.imu_preset
        if imu_on:
            rpt[13:49] = (
                synthetic_tilt_imu(preset)
                if preset != "neutral"
                else synthetic_gravity_imu(axis)
            )
        # else leave zeros in 13–48 (IMU disabled)
        return bytes(rpt)

    def pack_0x21(self, ack: int, subcmd: int, payload: bytes = b"") -> bytes:
        rpt = bytearray(REPORT_SIZE)
        rpt[0] = 0x21
        rpt[1] = self.timer & 0xFF
        rpt[2:13] = self._input_prefix()
        rpt[13] = ack
        rpt[14] = subcmd
        payload = payload[: REPORT_SIZE - 15]
        rpt[15 : 15 + len(payload)] = payload
        return bytes(rpt)

    def pack_0x81(self, cmd: int, payload: bytes = b"") -> bytes:
        rpt = bytearray(REPORT_SIZE)
        rpt[0] = 0x81
        rpt[1] = cmd
        payload = payload[: REPORT_SIZE - 2]
        rpt[2 : 2 + len(payload)] = payload
        return bytes(rpt)

    def _write(self, report: bytes, trace: bool = False) -> bool:
        if self._fd is None:
            if trace:
                self.log("TX dropped (hidg closed):", report.hex())
            return False
        buf = report if len(report) == REPORT_SIZE else (report + bytes(REPORT_SIZE - len(report)))[:REPORT_SIZE]
        try:
            with self._write_lock:
                written = os.write(self._fd, buf)
            if written != REPORT_SIZE:
                if trace:
                    self.log("TX short %d/%d: %s" % (written, REPORT_SIZE, buf.hex()))
                return False
            if trace:
                self.log("TX", buf.hex())
            return True
        except BlockingIOError:
            if trace:
                self.log("TX dropped (IN endpoint not ready):", buf.hex())
            return False
        except OSError as exc:
            # 108 ENOTCONN / 107 ENOTCONN-ish / 19 ENODEV / 32 EPIPE: no USB host
            # yet (unplugged) or the Switch dropped the gadget.
            if exc.errno in (19, 32, 107, 108, 113):
                if trace:
                    self.log("TX failed errno %d: %s" % (exc.errno, buf.hex()))
                return False
            self.log("write error:", exc)
            return False

    def _bump_timer(self) -> None:
        self.timer = (self.timer + 1) & 0xFF

    # --- USB init 0x80 / 0x81 ---------------------------------------------

    def _handle_usb_cmd(self, data: bytes) -> None:
        if len(data) < 2:
            return
        cmd = data[1]
        if cmd == 0x01:
            # 81 01 00 03 + MAC little-endian (wired dump). Type 03 = Pro.
            self._write(
                self.pack_0x81(0x01, bytes([0x00, 0x03]) + MAC_LE),
                trace=True,
            )
            self.log("USB 80 01 connection status → 81 01 Pro + MAC")
        elif cmd == 0x02:
            self._write(self.pack_0x81(0x02), trace=True)
            self.log("USB 80 02 handshake → 81 02")
        elif cmd == 0x03:
            self._write(self.pack_0x81(0x03), trace=True)
            self.log("USB 80 03 baud → 81 03")
        elif cmd == 0x04:
            self.log_always("USB 80 04 force USB HID — starting 0x30 stream")
            self._stream_report_logged = False
            self._stream.set()
        elif cmd == 0x05:
            # nscon stops reports here. A new 80 04 starts a fresh stream.
            self._stream.clear()
            self._stream_report_logged = False
            self.log_always("USB 80 05 allow BT timeout — stopped 0x30 stream")
        else:
            self.log("USB 80 %02x unhandled: %s" % (cmd, data[:16].hex()))

    def _spi_read(self, addr: int, length: int) -> bytes:
        length = max(0, min(0x1D, length))
        if addr < 0 or addr >= SPI_FLASH_SIZE:
            return b"\xff" * length
        end = min(SPI_FLASH_SIZE, addr + length)
        data = bytes(self.spi[addr:end])
        if len(data) < length:
            data += b"\xff" * (length - len(data))
        return data

    def _handle_subcommand(self, data: bytes) -> None:
        if len(data) < 11:
            return
        sub = data[10]
        arg = data[11] if len(data) > 11 else 0

        if sub == 0x01:
            self._write(self.pack_0x21(0x81, sub, bytes([0x03])), trace=True)
        elif sub == 0x02:
            # Device info: FW, type 03 Pro, 0x02, MAC big-endian.
            # Trailing 03 01 copied from the wired dump (dekuNukem lists 01 01).
            payload = FW_VERSION + bytes([0x03, 0x02]) + MAC_BE + bytes([0x03, 0x01])
            self._write(self.pack_0x21(0x82, sub, payload), trace=True)
            self.log("sub 0x02 device info")
        elif sub == 0x03:
            self._write(self.pack_0x21(0x80, sub), trace=True)
            self.log("sub 0x03 input mode %02x" % arg)
            if arg == 0x30:
                self._stream.set()
        elif sub == 0x04:
            self._write(self.pack_0x21(0x83, sub, bytes(14)), trace=True)
        elif sub == 0x08:
            self._write(self.pack_0x21(0x80, sub), trace=True)
        elif sub == 0x10:
            addr = int.from_bytes(data[11:15], "little") if len(data) >= 15 else 0
            length = data[15] if len(data) > 15 else 0
            blob = self._spi_read(addr, length)
            # Echo address (u32 LE) + length + payload. ACK 0x90.
            payload = addr.to_bytes(4, "little") + bytes([length]) + blob
            self._write(self.pack_0x21(0x90, sub, payload), trace=True)
            self.log("SPI read @0x%04x len %d" % (addr, length))
        elif sub == 0x11:
            self._write(self.pack_0x21(0x80, sub, bytes([0x01])), trace=True)  # write-protected
        elif sub == 0x12:
            self._write(self.pack_0x21(0x80, sub, bytes([0x01])), trace=True)
        elif sub == 0x21:
            self._write(
                self.pack_0x21(0xA0, sub, bytes.fromhex("0100ff0003000501")),
                trace=True,
            )
        elif sub == 0x30:
            self.player_lights = arg
            self._write(self.pack_0x21(0x80, sub), trace=True)
            self.log("player lights %02x" % arg)
        elif sub == 0x31:
            self._write(
                self.pack_0x21(0xB0, sub, bytes([self.player_lights])),
                trace=True,
            )
        elif sub == 0x38:
            self._write(self.pack_0x21(0x80, sub), trace=True)
        elif sub == 0x40:
            with self._lock:
                self.imu_enabled = arg == 0x01
                enabled = self.imu_enabled
            self._write(self.pack_0x21(0x80, sub), trace=True)
            self.log("IMU %s" % ("enable" if enabled else "disable"))
        elif sub == 0x41:
            self._write(self.pack_0x21(0x80, sub), trace=True)
        elif sub == 0x48:
            self._write(self.pack_0x21(0x80, sub), trace=True)
            self.log("vibration enable arg %02x" % arg)
        elif sub == 0x50:
            self._write(
                self.pack_0x21(0xD0, sub, bytes.fromhex("9006")),
                trace=True,
            )
        else:
            # Unused: ACK 0x80 + 0x03 (dekuNukem).
            self._write(
                self.pack_0x21(0x80, sub, bytes([0x03])),
                trace=True,
            )
            self.log("sub 0x%02x stub-ACK" % sub)

    def _log_host(self, data: bytes) -> None:
        """Log every host report; coalesce only high-rate 0x10 rumble."""
        if not self.verbose:
            return
        if data[0] != 0x10:
            self.log("RX", data.hex())
            return
        now = time.monotonic()
        if self._rumble_last_log == 0.0 or now - self._rumble_last_log >= 1.0:
            suffix = ""
            if self._rumble_suppressed:
                suffix = " (%d additional 0x10 reports suppressed)" % self._rumble_suppressed
            self.log("RX", data.hex() + suffix)
            self._rumble_last_log = now
            self._rumble_suppressed = 0
        else:
            self._rumble_suppressed += 1

    def _handle_host(self, data: bytes) -> None:
        if not data:
            return
        self._log_host(data)
        rid = data[0]
        if rid == 0x80:
            self._handle_usb_cmd(data)
        elif rid == 0x01:
            self._handle_subcommand(data)
        elif rid == 0x10:
            pass  # rumble only
        elif rid in (0x00,):
            pass
        else:
            self.log("host report %02x: %s" % (rid, data[:20].hex()))

    # --- gadget / threads -------------------------------------------------

    def _reset_udc(self) -> None:
        udc_file = os.path.join(self.gadget_path, "UDC")
        if not os.path.exists(udc_file):
            return
        self.log("rebind UDC before opening hidg0 (working-gist ordering)")
        try:
            with open(udc_file, "w") as fh:
                fh.write("\n")
            time.sleep(0.15)
            udcs = os.listdir("/sys/class/udc")
            if udcs:
                with open(udc_file, "w") as fh:
                    fh.write(udcs[0] + "\n")
            # Match the working gist: let enumeration settle, then open hidg0.
            # Host OUT reports queue until the reader starts.
            time.sleep(0.5)
        except OSError as exc:
            self.log("UDC reset failed:", exc)

    def start(self) -> None:
        if self._fd is not None:
            return
        self._stop.clear()
        self._stream.clear()
        if self.reset_udc_on_start:
            # Reset before opening hidg0 or starting threads. Closing/reopening
            # a live fd while its reader and streamer run races both threads.
            self._reset_udc()
        flags = os.O_RDWR
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        self._fd = None
        last_err: Optional[OSError] = None
        for _ in range(20):
            try:
                self._fd = os.open(self.device, flags)
                break
            except OSError as exc:
                last_err = exc
                time.sleep(0.05)
        if self._fd is None:
            raise last_err if last_err else OSError("cannot open %s" % self.device)
        self._start_threads()
        self.log("listening on %s (UNTESTED ON A REAL SWITCH)" % self.device)

    def _start_threads(self) -> None:
        reader = threading.Thread(target=self._read_loop, name="hidg-read", daemon=True)
        streamer = threading.Thread(target=self._stream_loop, name="hidg-0x30", daemon=True)
        self._threads = [reader, streamer]
        reader.start()
        streamer.start()

    def stop(self) -> None:
        self._stop.set()
        self._stream.clear()
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            fd = self._fd
            if fd is None:
                time.sleep(0.05)
                continue
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            if not ready:
                continue
            try:
                data = os.read(fd, 128)
            except BlockingIOError:
                continue
            except OSError:
                time.sleep(0.05)
                continue
            if data:
                self._handle_host(data)

    def _stream_loop(self) -> None:
        while not self._stop.is_set():
            if not self._stream.wait(0.1):
                continue
            self._bump_timer()
            first = not self._stream_report_logged
            gen = self.current_gen()
            if self._write(self.pack_0x30(), trace=first):
                with self._report_cond:
                    self.report_seq += 1
                    self.reported_gen = gen
                    self._report_cond.notify_all()
            self._stream_report_logged = True
            time.sleep(self.stream_period)


HELP_TEXT = (
    "commands:\n"
    "  press a b x y l r zl zr plus minus home capture up down left right\n"
    "  release <btn...> | releaseall\n"
    "  tap <btn> [seconds]\n"
    "  dpad up|down|left|right|clear\n"
    "  stick l|r <x> <y>     # raw 0..4095 or float -1..1\n"
    "  center\n"
    "  hold <btn> <seconds>\n"
    "  gravity z|+z|-z|x|y   # synthetic 1g rest pose\n"
    "  status\n"
    "  help\n"
    "  quit                  # stdin: stop daemon; socket: close this client\n"
    "UNTESTED ON A REAL SWITCH."
)


class CommandError(Exception):
    """User-facing command error (unknown button, bad args)."""


class CommandQuit(Exception):
    """stdin CLI asked to exit the process."""


def run_cli_command(pad: ProController, line: str) -> str:
    """Execute one stdin-language command. Return output text (may be empty).

    Raises CommandQuit for quit/exit (stdin should stop the process).
    Raises CommandError for bad input. Socket clients treat quit as disconnect.
    """
    parts = line.strip().split()
    if not parts:
        return ""
    cmd = parts[0].lower()
    args = parts[1:]
    if cmd in ("q", "quit", "exit"):
        raise CommandQuit()
    if cmd in ("h", "help", "?"):
        return HELP_TEXT
    try:
        if cmd == "press":
            if not args:
                raise CommandError("press requires a button")
            pad.press(*args)
        elif cmd == "release":
            if not args:
                raise CommandError("release requires a button")
            pad.release(*args)
        elif cmd == "releaseall":
            pad.release_all()
        elif cmd == "tap":
            if not args:
                raise CommandError("tap requires a button")
            hold = float(args[1]) if len(args) > 1 else 0.12
            pad.tap(args[0], hold)
        elif cmd == "hold":
            if not args:
                raise CommandError("hold requires a button")
            hold = float(args[1]) if len(args) > 1 else 0.5
            pad.press(args[0])
            time.sleep(hold)
            pad.release(args[0])
        elif cmd == "dpad":
            direction = args[0] if args else "clear"
            pad.set_dpad(None if direction in ("clear", "neutral", "none") else direction)
        elif cmd == "stick":
            if len(args) < 3:
                raise CommandError("stick l|r <x> <y>")
            which, x, y = args[0], parse_axis(args[1]), parse_axis(args[2])
            pad.set_stick(which, x, y)
        elif cmd == "center":
            pad.center_sticks()
        elif cmd == "gravity":
            if not args:
                raise CommandError("gravity requires an axis")
            pad.set_gravity(args[0])
        elif cmd == "status":
            return pad.snapshot()
        else:
            raise CommandError("unknown command %r (help for list)" % cmd)
    except CommandError:
        raise
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        raise CommandError(str(exc)) from exc
    return ""


def _json_reply(ok: bool, text: str) -> bytes:
    return (json.dumps({"ok": ok, "text": text}, ensure_ascii=False) + "\n").encode("utf-8")


class ControlServer:
    """Unix-socket command channel. Same language as stdin. Does not open hidg."""

    def __init__(self, pad: ProController, path: str = SOCKET_DEFAULT, mode: int = SOCKET_MODE) -> None:
        self.pad = pad
        self.path = path
        self.mode = mode
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(self.path)
            os.chmod(self.path, self.mode)
            sock.listen(8)
            sock.settimeout(0.4)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, name="procon-sock", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._unlink()

    def _unlink(self) -> None:
        try:
            if self.path and os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass

    def _accept_loop(self) -> None:
        assert self._sock is not None
        listen = self._sock
        while not self._stop.is_set():
            try:
                conn, _ = listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_conn, args=(conn,), name="procon-client", daemon=True
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        buf = b""
        try:
            conn.settimeout(120.0)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        with self.pad.command_lock:
                            text = run_cli_command(self.pad, line)
                    except CommandQuit:
                        conn.sendall(_json_reply(True, "bye"))
                        return
                    except CommandError as exc:
                        conn.sendall(_json_reply(False, str(exc)))
                    except Exception as exc:
                        conn.sendall(_json_reply(False, "internal: %s" % exc))
                    else:
                        conn.sendall(_json_reply(True, text if text else "ok"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


class ProconClient:
    """Talk to a running procon.py daemon over its Unix socket.

    Does not open /dev/hidg0. Same command language as stdin.

        from procon import ProconClient
        ProconClient().tap("a")
    """

    def __init__(self, path: str = SOCKET_DEFAULT, timeout: float = 60.0) -> None:
        self.path = path
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""

    def connect(self) -> "ProconClient":
        if self._sock is not None:
            return self
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._buf = b""
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._buf = b""

    def __enter__(self) -> "ProconClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    def _readline(self) -> str:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("control socket closed (%s)" % self.path)
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8")

    def command(self, line: str) -> str:
        """Send one stdin-language command. Return daemon text. Raise on error."""
        line = line.strip()
        if not line:
            return ""
        self.connect()
        assert self._sock is not None
        self._sock.sendall((line + "\n").encode("utf-8"))
        raw = self._readline()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConnectionError("bad reply from daemon: %r" % raw) from exc
        text = msg.get("text") or ""
        if not msg.get("ok"):
            raise RuntimeError(text or "command failed")
        return text

    def tap(self, name: str, hold_s: float = 0.12) -> str:
        return self.command("tap %s %s" % (name, hold_s))

    def press(self, *names: str) -> str:
        return self.command("press " + " ".join(names))

    def release(self, *names: str) -> str:
        return self.command("release " + " ".join(names))

    def release_all(self) -> str:
        return self.command("releaseall")

    def dpad(self, direction: Optional[str]) -> str:
        if not direction or direction in ("clear", "neutral", "none"):
            return self.command("dpad clear")
        return self.command("dpad " + direction)

    def stick(self, which: str, x, y) -> str:
        return self.command("stick %s %s %s" % (which, x, y))

    def center(self) -> str:
        return self.command("center")

    def gravity(self, axis: str) -> str:
        return self.command("gravity " + axis)

    def status(self) -> str:
        return self.command("status")

    def help(self) -> str:
        return self.command("help")


def send_command(line: str, path: str = SOCKET_DEFAULT, timeout: float = 60.0) -> str:
    """One-shot helper for --send / procon_send.py."""
    client = ProconClient(path=path, timeout=timeout)
    try:
        return client.command(line)
    finally:
        client.close()


def _self_test() -> int:
    """No gadget required. Checks report sizes and public dump constants."""
    pad = ProController(reset_udc=False, verbose=False)
    r30 = pad.pack_0x30()
    assert len(r30) == REPORT_SIZE, len(r30)
    assert r30[0] == 0x30
    assert r30[2] == BATTERY_USB_PRO
    assert r30[4] & 0x80  # charging grip
    pad.press("A")
    r30 = pad.pack_0x30()
    assert r30[3] & 0x08, "A should set bit 0x08 on byte 3"
    pad.release("A")
    pad.set_dpad("up")
    r30 = pad.pack_0x30()
    assert r30[5] & 0x02, "Up should set bit 0x02 on byte 5"
    pad.imu_enabled = True
    r30 = pad.pack_0x30()
    ax, ay, az, gx, gy, gz = struct.unpack_from("<6h", r30, 13)
    assert (gx, gy, gz) == (0, 0, 0)
    assert az == ACCEL_1G and ax == 0 and ay == 0
    r81 = pad.pack_0x81(0x01, bytes([0x00, 0x03]) + MAC_LE)
    assert r81[0] == 0x81 and r81[1] == 0x01
    assert r81[:10] == bytes.fromhex("810100031f861dd60304")
    assert r81[4:10] == MAC_LE
    assert not pad._stream.is_set(), "0x30 must stay off before USB 80 04"
    pad._handle_usb_cmd(bytes.fromhex("8004"))
    assert pad._stream.is_set(), "USB 80 04 must start 0x30"
    pad._handle_usb_cmd(bytes.fromhex("8005"))
    assert not pad._stream.is_set(), "USB 80 05 must stop 0x30"
    info = FW_VERSION + bytes([0x03, 0x02]) + MAC_BE + bytes([0x03, 0x01])
    r21 = pad.pack_0x21(0x82, 0x02, info)
    assert r21[0] == 0x21 and r21[13] == 0x82 and r21[14] == 0x02
    assert r21[15:21] == FW_VERSION + bytes([0x03, 0x02]) + MAC_BE[:2]
    assert parse_axis("0") == STICK_CENTER
    assert parse_axis("1") == STICK_MAX
    desc = bytes.fromhex(
        "050115000904A1018530050105091901290A150025017501950A550065008102"
        "0509190B290E150025017501950481027501950281030B01000100A1000B3000"
        "01000B310001000B320001000B35000100150027FFFF0000751095048102C00B"
        "39000100150025073500463B0165147504950181020509190F29121500250175"
        "01950481027508953481030600FF852109017508953F8103858109027508953F"
        "8103850109037508953F9183851009047508953F9183858009057508953F9183"
        "858209067508953F9183C0"
    )
    assert len(desc) == 203
    cal = pad._spi_read(0x603D, 0x19)
    assert cal[:9] == bytes.fromhex("ba156211b87f29065b")

    tmp = tempfile.mkdtemp(prefix="procon-sock-")
    sock_path = os.path.join(tmp, "procon.sock")
    server = ControlServer(pad, sock_path)
    try:
        server.start()
        with ProconClient(sock_path, timeout=5.0) as client:
            assert client.press("a") == "ok"
            assert pad.buttons[0] & 0x08, "socket press a should set A"
            client.release("a")
            assert not (pad.buttons[0] & 0x08)
            client.dpad("up")
            assert pad.buttons[2] & 0x02
            client.dpad(None)
            client.stick("l", 0, 1)  # stdin language: 0/1 are normalized, not raw
            assert pad.lx == STICK_CENTER and pad.ly == STICK_MAX
            client.center()
            assert pad.lx == STICK_CENTER and pad.ly == STICK_CENTER
            snap = client.status()
            assert "buttons=" in snap
            help_text = client.help()
            assert "tap" in help_text and "UNTESTED" in help_text
            try:
                client.command("no-such-cmd")
                raise AssertionError("unknown command should fail")
            except RuntimeError:
                pass
            # quit on the socket must not stop the pad; only this client.
            client.command("quit")
        with ProconClient(sock_path, timeout=5.0) as client:
            client.gravity("y")
            assert pad.gravity_axis == "y"
            client.tap("b", 0.01)
            assert not (pad.buttons[0] & 0x04)
    finally:
        server.stop()
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    print("self-test ok (still UNTESTED ON A REAL SWITCH)")
    return 0


def _serve_commands(pad: ProController, socket_path: Optional[str], quiet: bool) -> None:
    """Stdin while it is a TTY; always the Unix socket. EOF (systemd) → socket only."""
    server: Optional[ControlServer] = None
    if socket_path:
        server = ControlServer(pad, socket_path)
        try:
            server.start()
        except OSError as exc:
            interactive = False
            try:
                interactive = sys.stdin.isatty()
            except Exception:
                pass
            if not interactive:
                raise
            print("control socket unavailable (%s); stdin only" % exc, flush=True)
            server = None
        else:
            print("control socket %s mode %04o" % (socket_path, SOCKET_MODE), flush=True)

    stop = threading.Event()

    def _request_stop(signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    use_stdin = False
    try:
        use_stdin = sys.stdin.isatty()
    except Exception:
        use_stdin = False

    if use_stdin and not quiet:
        print(
            "stdin commands (help / quit). socket %s. UNTESTED ON A REAL SWITCH."
            % (socket_path or "(disabled)"),
            flush=True,
        )
    elif not quiet:
        print(
            "daemon mode: commands on %s (stdin EOF / not a TTY). UNTESTED ON A REAL SWITCH."
            % (socket_path or "(no socket)"),
            flush=True,
        )

    try:
        while not stop.is_set():
            if not use_stdin:
                if stop.wait(0.4):
                    break
                continue
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.4)
            except (OSError, ValueError):
                use_stdin = False
                continue
            if stop.is_set():
                break
            if not ready:
                continue
            line = sys.stdin.readline()
            if line == "":
                # TTY Ctrl-D: leave the CLI. Non-TTY EOF (systemd): keep the socket.
                try:
                    tty = sys.stdin.isatty()
                except Exception:
                    tty = False
                if tty:
                    break
                use_stdin = False
                if not quiet:
                    print(
                        "stdin EOF; driving %s until SIGTERM. UNTESTED ON A REAL SWITCH."
                        % (socket_path or "(no socket)"),
                        flush=True,
                    )
                continue
            try:
                with pad.command_lock:
                    text = run_cli_command(pad, line)
            except CommandQuit:
                break
            except CommandError as exc:
                print("error: %s" % exc, flush=True)
            else:
                if text:
                    print(text, flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        if server is not None:
            server.stop()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emulate a wired Switch Pro Controller on /dev/hidg0. "
        "UNTESTED ON A REAL SWITCH."
    )
    parser.add_argument("--device", default=HIDG_DEFAULT)
    parser.add_argument("--hz", type=float, default=1.0 / USB_INTERVAL_S)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no-reset-udc", action="store_true")
    parser.add_argument("--tap", metavar="BUTTON", help="tap a button once after start")
    parser.add_argument("--press", metavar="BUTTON", help="hold a button (until quit)")
    parser.add_argument(
        "--socket",
        default=SOCKET_DEFAULT,
        help="Unix socket for a second process (default %s)" % SOCKET_DEFAULT,
    )
    parser.add_argument(
        "--no-socket",
        action="store_true",
        help="do not listen on a Unix socket (stdin CLI only)",
    )
    parser.add_argument(
        "--send",
        nargs="+",
        metavar="WORD",
        help="send a command to a running daemon (does not open /dev/hidg0)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="pack handshake/0x30 reports without opening /dev/hidg0 and exit",
    )
    parser.add_argument(
        "--v1-listen",
        metavar="HOST:PORT",
        help="also serve protocol v1 (PROTOCOL.md) on TCP, e.g. 0.0.0.0:47800",
    )
    parser.add_argument(
        "--v1-token-file",
        metavar="PATH",
        help="file holding the protocol v1 shared token (required with --v1-listen)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.self_test:
        return _self_test()

    if args.send:
        line = " ".join(args.send)
        try:
            text = send_command(line, path=args.socket)
        except FileNotFoundError:
            print(
                "no socket at %s — is procon.py running as the HID daemon?" % args.socket,
                file=sys.stderr,
            )
            return 1
        except ConnectionRefusedError:
            print(
                "nothing listening on %s — start the daemon first (do not open hidg0 here)."
                % args.socket,
                file=sys.stderr,
            )
            return 1
        except (RuntimeError, OSError, ConnectionError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if text:
            print(text)
        return 0

    v1_token = None
    if args.v1_listen:
        from protocol_v1 import read_token_file

        try:
            v1_token = read_token_file(args.v1_token_file)
        except (OSError, ValueError) as exc:
            print("protocol v1: %s" % exc, file=sys.stderr)
            return 1

    if not os.path.exists(args.device):
        print(
            "missing %s — run add_procon_gadget.sh over Wi-Fi SSH first "
            "(unbind g_cdc, bind HID). UNTESTED ON A REAL SWITCH." % args.device,
            file=sys.stderr,
        )
        return 1

    pad = ProController(
        device=args.device,
        stream_hz=args.hz,
        verbose=not args.quiet,
        reset_udc=not args.no_reset_udc,
    )
    try:
        pad.start()
    except OSError as exc:
        print(
            "cannot open %s: %s (only one process may hold hidg0)." % (args.device, exc),
            file=sys.stderr,
        )
        return 1
    if args.press:
        pad.press(args.press)
    if args.tap:
        time.sleep(0.4)
        pad.tap(args.tap)

    v1_server = None
    if args.v1_listen:
        from protocol_v1 import V1Server, parse_listen

        host, port = parse_listen(args.v1_listen)
        v1_server = V1Server(pad, host, port, v1_token)
        try:
            v1_server.start()
        except OSError as exc:
            print("cannot listen for protocol v1 on %s: %s" % (args.v1_listen, exc), file=sys.stderr)
            pad.stop()
            return 1

    sock_path = None if args.no_socket else args.socket
    try:
        _serve_commands(pad, sock_path, quiet=args.quiet)
    except OSError as exc:
        print("cannot bind control socket %s: %s" % (sock_path, exc), file=sys.stderr)
        return 1
    finally:
        if v1_server is not None:
            v1_server.stop()
        pad.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
