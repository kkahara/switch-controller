#!/usr/bin/env python3
"""Tap A on a Bluetooth Switch Pro Controller (joycontrol).

UNTESTED ON A REAL SWITCH. Run as root on the Pi after enable_bluetooth.sh
and install_joycontrol.sh. USB procon.service must be stopped.

First pair (Switch on Change Grip/Order, not System Settings → Bluetooth):

    sudo ~/switch-controller/.venv-joycontrol/bin/python3 \\
      ~/switch-controller/bt_tap_a.py

Reconnect:

    sudo ~/switch-controller/.venv-joycontrol/bin/python3 \\
      ~/switch-controller/bt_tap_a.py -r AA:BB:CC:DD:EE:FF

Later Splatoon 3 Review timeline work can copy the button_push() calls
(right/left, then a) — get a visible A tap on the Controllers screen first.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

if os.geteuid() != 0:
    sys.exit("error: run as root (sudo …/bt_tap_a.py)")

# Editable install + clone live in the same folder as this script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "joycontrol")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from joycontrol.controller import Controller  # noqa: E402
from joycontrol.controller_state import button_push  # noqa: E402
from joycontrol.protocol import controller_protocol_factory  # noqa: E402
from joycontrol.server import create_hid_server  # noqa: E402


async def tap_a(reconnect_bt_addr: str | None, repeats: int) -> None:
    factory = controller_protocol_factory(Controller.PRO_CONTROLLER)
    transport, protocol = await create_hid_server(
        factory,
        reconnect_bt_addr=reconnect_bt_addr,
        ctl_psm=17,
        itr_psm=19,
    )
    try:
        state = protocol.get_controller_state()
        print("Waiting for Switch… (Change Grip/Order if this is the first pair)")
        await state.connect()
        print("Connected; tapping A")
        for _ in range(repeats):
            await button_push(state, "a")
            await asyncio.sleep(0.25)
        # Hold the session briefly so the Switch does not see an instant drop.
        await asyncio.sleep(2)
    finally:
        await transport.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-r",
        "--reconnect-bt-addr",
        default=None,
        help="Switch Bluetooth MAC after first pair (skip Change Grip/Order)",
    )
    parser.add_argument(
        "-n",
        "--repeats",
        type=int,
        default=3,
        help="How many A taps (default 3)",
    )
    args = parser.parse_args()
    asyncio.run(tap_a(args.reconnect_bt_addr, args.repeats))


if __name__ == "__main__":
    main()
