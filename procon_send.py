#!/usr/bin/env python3
"""Send stdin-language commands to a running procon.py daemon.

Does not open /dev/hidg0 — only the daemon may hold that device.

    python3 procon_send.py tap a
    python3 procon_send.py status
    python3 procon_send.py --socket /run/procon.sock 'stick l 0 1'

Programmatic:

    from procon import ProconClient
    ProconClient().tap("a")

UNTESTED ON A REAL SWITCH.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from procon import SOCKET_DEFAULT, send_command


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a command to procon.py over its Unix socket. "
        "Does not open /dev/hidg0. UNTESTED ON A REAL SWITCH."
    )
    parser.add_argument(
        "--socket",
        default=SOCKET_DEFAULT,
        help="daemon socket (default %s)" % SOCKET_DEFAULT,
    )
    parser.add_argument(
        "command",
        nargs="+",
        help="stdin-language command, e.g. tap a   or   stick l 0 1",
    )
    args = parser.parse_args(argv)
    line = " ".join(args.command)
    try:
        text = send_command(line, path=args.socket)
    except FileNotFoundError:
        print(
            "no socket at %s — is procon.service / procon.py running?" % args.socket,
            file=sys.stderr,
        )
        return 1
    except ConnectionRefusedError:
        print(
            "nothing listening on %s — start the HID daemon first." % args.socket,
            file=sys.stderr,
        )
        return 1
    except (RuntimeError, OSError, ConnectionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
