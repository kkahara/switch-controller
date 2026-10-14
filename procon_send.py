#!/usr/bin/env python3
"""Send commands to a running procon.py daemon.

Does not open /dev/hidg0 — only the daemon may hold that device.

Legacy Unix socket (text command language):

    python3 procon_send.py tap a
    python3 procon_send.py status
    python3 procon_send.py --socket /run/procon.sock 'stick l 0 1'

Protocol v1 over TCP (PROTOCOL.md); prints the ack and result as JSON lines
and exits 0 only when the result status is "ok":

    python3 procon_send.py --v1 127.0.0.1 ping
    python3 procon_send.py --v1 192.168.0.252:47800 tap a 80
    python3 procon_send.py --v1 127.0.0.1 --observe state

Programmatic:

    from procon import ProconClient
    ProconClient().tap("a")
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from procon import SOCKET_DEFAULT, send_command


def _send_v1(args: argparse.Namespace) -> int:
    from protocol_v1 import DEFAULT_TOKEN_FILE, V1Client, parse_listen, read_token_file, words_to_cmd

    host, port = parse_listen(args.v1)
    try:
        token = read_token_file(args.token_file or DEFAULT_TOKEN_FILE)
        op, cmd_args = words_to_cmd(args.command)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client = V1Client(
        host,
        port,
        token,
        role="observe" if args.observe else "control",
        takeover=args.takeover,
    )
    try:
        hello = client.connect()
        if hello.get("status") != "ok":
            print(json.dumps(hello), file=sys.stderr)
            return 1
        ack, result = client.command(op, cmd_args)
    except (OSError, ConnectionError, ValueError) as exc:
        print("protocol v1: %s" % exc, file=sys.stderr)
        return 1
    finally:
        client.close()
    if ack is not None:
        print(json.dumps(ack))
    print(json.dumps(result))
    return 0 if result.get("status") == "ok" else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a command to procon.py (Unix socket, or protocol v1 with --v1). "
        "Does not open /dev/hidg0."
    )
    parser.add_argument(
        "--socket",
        default=SOCKET_DEFAULT,
        help="daemon socket (default %s)" % SOCKET_DEFAULT,
    )
    parser.add_argument("--v1", metavar="HOST[:PORT]", help="use protocol v1 over TCP")
    parser.add_argument("--token-file", help="protocol v1 token file (default ~/.config/procon/v1-token)")
    parser.add_argument("--observe", action="store_true", help="v1 observe-only session (state/ping)")
    parser.add_argument("--takeover", action="store_true", help="v1: replace an existing control session")
    parser.add_argument(
        "command",
        nargs="+",
        help="command words, e.g. tap a   or   stick l 0 1",
    )
    args = parser.parse_args(argv)
    if args.v1:
        return _send_v1(args)

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
