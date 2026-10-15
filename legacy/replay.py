#!/usr/bin/env python3
"""Enter a Splatoon 3 replay code at the Lobby Terminal and start playback.

Runs on the Pi as a client of the procon.py daemon socket; never opens
/dev/hidg0.

    python3 replay.py type RXE0-4NDK-KSEC-A05W   # code keyboard already open
    python3 replay.py play RXE0-4NDK-KSEC-A05W   # Lobby Terminal menu open,
                                                 # cursor on the first item
    python3 replay.py play --dry-run RXE0-4NDK-KSEC-A05W
    python3 replay.py --self-test

UNVERIFIED: KEYBOARD_ROWS, KEYBOARD_START and TERMINAL_MENU have not been
checked against a real screen. Confirm them from a capture-card frame before
trusting a blind run.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from typing import List, Optional, Sequence, Tuple

from procon import SOCKET_DEFAULT, ProconClient

CODE_LEN = 16

KEYBOARD_ROWS: Sequence[str] = (
    "1234567890",
    "QWERTYUIOP",
    "ASDFGHJKL",
    "ZXCVBNM",
)
KEYBOARD_START: Tuple[int, int] = (0, 0)
KEYBOARD_CONFIRM = "plus"

# Lobby Terminal items, top to bottom (Splatoon wiki order).
TERMINAL_MENU: Sequence[str] = (
    "get_stuff",
    "view_replays",
    "battle_logs",
    "replay_code",
    "edit_splashtag",
    "change_nickname",
    "splatfest_region",
)

Action = Tuple[str, object]  # ("tap", button) | ("wait", seconds) | ("note", text)


def normalize_code(code: str) -> str:
    clean = re.sub(r"[\s-]", "", code).upper()
    if len(clean) != CODE_LEN or not clean.isalnum():
        raise ValueError(
            "replay code must be %d letters/digits (dashes optional), got %r" % (CODE_LEN, code)
        )
    return clean


def find_key(ch: str, rows: Sequence[str] = KEYBOARD_ROWS) -> Tuple[int, int]:
    for r, row in enumerate(rows):
        c = row.find(ch)
        if c >= 0:
            return r, c
    raise ValueError("character %r is not on the keyboard layout" % ch)


def _col_ok(rows: Sequence[str], r0: int, r1: int, col: int) -> bool:
    lo, hi = sorted((r0, r1))
    return all(col < len(rows[r]) for r in range(lo, hi + 1))


def plan_moves(
    start: Tuple[int, int], target: Tuple[int, int], rows: Sequence[str] = KEYBOARD_ROWS
) -> List[str]:
    """D-pad presses from start to target without leaving the ragged grid."""
    (r0, c0), (r1, c1) = start, target
    horiz = ["right" if c1 > c0 else "left"] * abs(c1 - c0)
    vert = ["down" if r1 > r0 else "up"] * abs(r1 - r0)
    if c1 < len(rows[r0]) and _col_ok(rows, r0, r1, c1):
        return horiz + vert
    if _col_ok(rows, r0, r1, c0):
        return vert + horiz
    raise ValueError("no in-grid path from %r to %r" % (start, target))


def keyboard_actions(code: str) -> List[Action]:
    actions: List[Action] = []
    cursor = KEYBOARD_START
    for ch in code:
        target = find_key(ch)
        actions.extend(("tap", d) for d in plan_moves(cursor, target))
        actions.append(("tap", "a"))
        cursor = target
    actions.append(("tap", KEYBOARD_CONFIRM))
    return actions


def play_actions(code: str) -> List[Action]:
    replay_code = TERMINAL_MENU.index("replay_code")
    view_replays = TERMINAL_MENU.index("view_replays")
    actions: List[Action] = [("note", "Terminal menu -> Replay Code")]
    actions += [("tap", "down")] * replay_code
    actions += [("tap", "a"), ("wait", 2.0)]
    actions += [("note", "type %s" % code)]
    actions += keyboard_actions(code)
    actions += [("wait", 4.0), ("note", "download (A twice)")]
    actions += [("tap", "a"), ("wait", 2.0), ("tap", "a"), ("wait", 10.0)]
    actions += [("note", "back to Terminal menu -> View Replays")]
    actions += [("tap", "b"), ("wait", 2.0)]
    actions += [("tap", "up")] * (replay_code - view_replays)
    actions += [("tap", "a"), ("wait", 3.0)]
    actions += [("note", "play first replay (A twice)")]
    actions += [("tap", "a"), ("wait", 1.5), ("tap", "a")]
    return actions


def run(actions: Sequence[Action], client: Optional[ProconClient], hold: float, gap: float) -> None:
    for kind, value in actions:
        if kind == "note":
            print("#", value, flush=True)
        elif kind == "wait":
            if client is None:
                print("wait %.1fs" % value)
            else:
                time.sleep(float(value))
        elif kind == "tap":
            if client is None:
                print("tap", value)
            else:
                client.tap(str(value), hold)
                time.sleep(gap)


def _self_test() -> int:
    assert normalize_code("rxe0-4ndk-ksec-a05w") == "RXE04NDKKSECA05W"
    for bad in ("RXE0", "RXE0-4NDK-KSEC-A05!"):
        try:
            normalize_code(bad)
            raise AssertionError("should reject %r" % bad)
        except ValueError:
            pass
    step = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}
    keys = "".join(KEYBOARD_ROWS)
    for a in keys:
        for b in keys:
            r, c = find_key(a)
            for move in plan_moves((r, c), find_key(b)):
                r, c = r + step[move][0], c + step[move][1]
                assert 0 <= r < len(KEYBOARD_ROWS) and 0 <= c < len(KEYBOARD_ROWS[r]), (a, b)
            assert KEYBOARD_ROWS[r][c] == b, (a, b)
    acts = keyboard_actions(normalize_code("RXE0-4NDK-KSEC-A05W"))
    assert sum(1 for a in acts if a == ("tap", "a")) == CODE_LEN
    assert acts[-1] == ("tap", KEYBOARD_CONFIRM)
    print("replay self-test ok (layout still UNVERIFIED)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Splatoon 3 replay code automation (UNVERIFIED layout).")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--socket", default=SOCKET_DEFAULT)
    parser.add_argument("--hold", type=float, default=0.08, help="seconds each press is held")
    parser.add_argument("--gap", type=float, default=0.12, help="seconds between presses")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    parser.add_argument("mode", nargs="?", choices=("type", "play"))
    parser.add_argument("code", nargs="?")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.mode or not args.code:
        parser.error("mode and code are required")

    code = normalize_code(args.code)
    actions = keyboard_actions(code) if args.mode == "type" else play_actions(code)
    if args.dry_run:
        run(actions, None, args.hold, args.gap)
        return 0

    with ProconClient(args.socket) as client:
        status = client.status()
        if "stream=on" not in status:
            print("controller is not streaming to the Switch (%s); aborting" % status, file=sys.stderr)
            return 1
        run(actions, client, args.hold, args.gap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
