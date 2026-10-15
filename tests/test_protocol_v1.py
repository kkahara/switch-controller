"""Protocol v1 server tests. No Switch, no /dev/hidg0: a RecordingPad keeps
every 0x30 report in memory.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import glob
import json
import os
import socket
import struct
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from procon import BUTTONS, ProController  # noqa: E402
from protocol_v1 import V1Client, V1Server, words_to_cmd  # noqa: E402

TOKEN = "fixture-token-0123456789"
FIXTURES = sorted(glob.glob(os.path.join(ROOT, "protocol", "v1", "fixtures", "*.json")))


class RecordingPad(ProController):
    def __init__(self) -> None:
        super().__init__(reset_udc=False, verbose=False, stream_hz=500)
        self.reports: list = []

    def _write(self, report: bytes, trace: bool = False) -> bool:
        self.reports.append(bytes(report))
        return True

    def pressed_in(self, seq: int, button: str) -> bool:
        idx, bit = BUTTONS[button]
        return bool(self.reports[seq - 1][3 + idx] & bit)


def make_pad(stream: bool = True) -> RecordingPad:
    pad = RecordingPad()
    pad._start_threads()
    if stream:
        pad._handle_usb_cmd(bytes.fromhex("8004"))
    return pad


def matches(expected, actual) -> bool:
    if expected == "$int":
        return isinstance(actual, int) and not isinstance(actual, bool)
    if expected == "$str":
        return isinstance(actual, str)
    if expected == "$any":
        return True
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and matches(v, actual[k]) for k, v in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(matches(e, a) for e, a in zip(expected, actual))
        )
    return expected == actual


class ServerCase(unittest.TestCase):
    stream = True
    idle_neutral_s = 2.0

    def setUp(self) -> None:
        self.pad = make_pad(self.stream)
        self.events: list = []
        self.server = V1Server(
            self.pad,
            "127.0.0.1",
            0,
            TOKEN,
            idle_neutral_s=self.idle_neutral_s,
            log=lambda event, **fields: self.events.append(dict(fields, event=event)),
        )
        self.server.start()
        self.host, self.port = self.server.address
        self.clients: list = []

    def tearDown(self) -> None:
        for c in self.clients:
            c.close()
        self.server.stop()
        self.pad.stop()

    def client(self, **kw) -> V1Client:
        c = V1Client(self.host, self.port, kw.pop("token", TOKEN), **kw)
        self.clients.append(c)
        return c

    def connected(self, **kw) -> V1Client:
        c = self.client(**kw)
        hello = c.connect()
        self.assertEqual(hello["status"], "ok", hello)
        return c

    def wait_until(self, predicate, timeout: float = 2.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()


class FixtureConformance(unittest.TestCase):
    def test_fixtures_exist(self) -> None:
        self.assertGreaterEqual(len(FIXTURES), 7)

    def test_fixtures(self) -> None:
        for path in FIXTURES:
            with self.subTest(fixture=os.path.basename(path)):
                self._run_fixture(path)

    def _run_fixture(self, path: str) -> None:
        with open(path, encoding="utf-8") as fh:
            spec = json.load(fh)
        pad = make_pad(spec["setup"]["stream"])
        server = V1Server(pad, "127.0.0.1", 0, TOKEN, log=lambda *a, **k: None)
        server.start()
        client = V1Client(*server.address, TOKEN)
        try:
            client._sock = socket.create_connection(server.address, timeout=5.0)
            for step in spec["exchanges"]:
                client.send_raw(step["send"])
                for expected in step["expect"]:
                    got = client.read()
                    self.assertTrue(matches(expected, got), "expected %s\n     got %s" % (expected, got))
                if step.get("then_closed"):
                    with self.assertRaises(ConnectionError):
                        client.read()
        finally:
            client.close()
            server.stop()
            pad.stop()


class TapEvidence(ServerCase):
    def test_tap_report_seqs_bracket_the_press(self) -> None:
        c = self.connected()
        ack, res = c.command("tap", {"button": "a", "hold_ms": 60})
        self.assertIsNotNone(ack)
        self.assertLessEqual(ack["pi_recv_mono_ns"], res["pi_start_mono_ns"])
        self.assertEqual(res["status"], "ok")
        first, last = res["first_report_seq"], res["last_report_seq"]
        self.assertLess(first, last)
        self.assertTrue(self.pad.pressed_in(first, "a"))
        self.assertFalse(self.pad.pressed_in(last, "a"))
        if first > 1:
            self.assertFalse(self.pad.pressed_in(first - 1, "a"))
        self.assertGreaterEqual(res["held_ms"], 60)


class PitchRotation(ServerCase):
    def setUp(self) -> None:
        super().setUp()
        self.pad.imu_enabled = True

    def imu(self, seq: int) -> tuple:
        return struct.unpack_from("<6h", self.pad.reports[seq - 1], 13)

    def test_gyro_during_motion_then_persistent_orientation(self) -> None:
        c = self.connected()
        _, res = c.command("rotate_pitch", {"delta_deg": 15, "duration_ms": 200})
        self.assertEqual(res["status"], "ok")
        motion = res["motion"]
        self.assertEqual(motion["start_pitch_deg"], 0.0)
        self.assertEqual(motion["gyro_rate_dps"], 75.0)
        self.assertEqual(motion["end_pitch_deg"], 15.0)
        self.assertEqual(motion["accel"], [1060, 0, 3956])

        first, last = res["first_report_seq"], res["last_report_seq"]
        window = [self.imu(s) for s in range(first, last)]
        moving = [s for s in window if s[4] != 0]
        self.assertGreater(len(moving), 5)
        self.assertTrue(all(s[4] == motion["gyro_raw"] for s in moving))
        self.assertEqual(window[: len(moving)], moving, "gyro must not resume after stopping")
        ax_values = [s[0] for s in moving]
        self.assertEqual(ax_values, sorted(ax_values))
        self.assertTrue(0 <= ax_values[0] < ax_values[-1] <= 1060)

        self.assertEqual(self.imu(last), (1060, 0, 3956, 0, 0, 0))
        time.sleep(0.05)
        self.assertEqual(self.imu(len(self.pad.reports)), (1060, 0, 3956, 0, 0, 0))

    def test_diagnostic_sequence_accumulates_and_returns(self) -> None:
        c = self.connected()
        c.command("neutral")
        ends = []
        for delta in (15, 15, 15, 15, -15, -15, -15, -15):
            _, res = c.command("rotate_pitch", {"delta_deg": delta, "duration_ms": 50})
            self.assertEqual(res["status"], "ok")
            ends.append(res["motion"]["end_pitch_deg"])
        self.assertEqual(ends, [15.0, 30.0, 45.0, 60.0, 45.0, 30.0, 15.0, 0.0])
        self.assertEqual(self.imu(len(self.pad.reports)), (0, 0, 4096, 0, 0, 0))

    def test_neutral_resets_orientation_and_rate(self) -> None:
        c = self.connected()
        c.command("rotate_pitch", {"delta_deg": -30, "duration_ms": 50})
        self.assertEqual(self.pad.state_dict()["imu_pitch_deg"], -30.0)
        _, res = c.command("neutral")
        self.assertEqual(self.imu(res["last_report_seq"]), (0, 0, 4096, 0, 0, 0))
        state = self.pad.state_dict()
        self.assertEqual((state["imu_pitch_deg"], state["imu_pitch_rate_dps"]), (0.0, 0.0))

    def test_rejects_unbounded_rotations(self) -> None:
        c = self.connected()
        for args in (
            {"delta_deg": 0, "duration_ms": 500},
            {"delta_deg": 91, "duration_ms": 500},
            {"delta_deg": 15, "duration_ms": 10},
            {"delta_deg": 15},
        ):
            c.send_raw(c.make_cmd("rotate_pitch", args))
            c.read()  # acknowledgement
            self.assertEqual(c.read()["error"]["code"], "bad_args", args)


class QueueOrdering(ServerCase):
    def test_ack_precedes_execution_of_queued_command(self) -> None:
        c = self.connected()
        long_tap = c.make_cmd("tap", {"button": "b", "hold_ms": 300})
        quick = c.make_cmd("ping")
        c.send_raw(long_tap)
        c.send_raw(quick)
        seen = [c.read() for _ in range(4)]
        kinds = [(m["type"], m["id"]) for m in seen]
        self.assertLess(kinds.index(("ack", quick["id"])), kinds.index(("result", long_tap["id"])))
        self.assertEqual(seen[kinds.index(("ack", quick["id"]))]["queue_depth"], 1)


class NotStreaming(ServerCase):
    stream = False

    def test_no_reports_and_no_ok(self) -> None:
        c = self.connected()
        _, res = c.command("press", {"buttons": ["a"]})
        self.assertEqual(res["status"], "not_streaming")
        self.assertFalse(res["applied"])
        self.assertEqual(self.pad.reports, [])
        self.assertEqual(self.pad.state_dict()["buttons"], [])


class StreamStopsMidway(ServerCase):
    def test_stream_stop_turns_into_not_streaming(self) -> None:
        c = self.connected()
        self.pad._handle_usb_cmd(bytes.fromhex("8005"))
        _, res = c.command("tap", {"button": "a"})
        self.assertEqual(res["status"], "not_streaming")


class Duplicates(ServerCase):
    def test_duplicate_while_pending_is_not_reexecuted(self) -> None:
        c = self.connected()
        cmd = c.make_cmd("tap", {"button": "y", "hold_ms": 200})
        c.send_raw(cmd)
        c.send_raw(cmd)
        msgs = [c.read() for _ in range(4)]
        acks = [m for m in msgs if m["type"] == "ack"]
        results = [m for m in msgs if m["type"] == "result"]
        self.assertEqual([a["duplicate"] for a in acks], [False, True])
        self.assertEqual(sorted(bool(r.get("duplicate")) for r in results), [False, True])
        edges = sum(
            1
            for i in range(1, len(self.pad.reports) + 1)
            if self.pad.pressed_in(i, "y") and (i == 1 or not self.pad.pressed_in(i - 1, "y"))
        )
        self.assertEqual(edges, 1)

    def test_duplicate_across_reconnect(self) -> None:
        c = self.connected()
        cmd = c.make_cmd("tap", {"button": "x", "hold_ms": 30})
        c.send_raw(cmd)
        _, first = c.wait_for(cmd["id"])
        c.close()
        self.assertTrue(self.wait_until(lambda: self.server._control is None))
        c2 = self.connected()
        c2.send_raw(cmd)
        ack, again = c2.wait_for(cmd["id"])
        self.assertTrue(ack["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["first_report_seq"], first["first_report_seq"])


class Expiry(ServerCase):
    def test_command_queued_too_long_is_expired_not_run(self) -> None:
        c = self.connected()
        c.send_raw(c.make_cmd("tap", {"button": "b", "hold_ms": 300}))
        late = c.make_cmd("press", {"buttons": ["x"]}, max_queue_ms=50)
        c.send_raw(late)
        _, res = c.wait_for(late["id"])
        self.assertEqual(res["status"], "expired")
        self.assertNotIn("x", self.pad.state_dict()["buttons"])


class Sessions(ServerCase):
    def test_second_control_is_busy_takeover_wins(self) -> None:
        a = self.connected()
        b = self.client()
        self.assertEqual(b.connect()["error"]["code"], "busy")
        c = self.client(takeover=True)
        self.assertEqual(c.connect()["status"], "ok")
        with self.assertRaises((ConnectionError, OSError)):
            a.command("ping")

    def test_observer_alongside_control(self) -> None:
        self.connected()
        obs = self.connected(role="observe")
        _, res = obs.command("state")
        self.assertEqual(res["status"], "ok")

    def test_bad_version_hello(self) -> None:
        c = self.client()
        c._sock = socket.create_connection((self.host, self.port), timeout=5.0)
        c.send_raw({"v": 2, "type": "hello", "token": TOKEN})
        self.assertEqual(c.read()["error"]["code"], "bad_version")

    def test_disconnect_with_held_input_goes_neutral(self) -> None:
        c = self.connected()
        c.command("press", {"buttons": ["zr"]})
        c.command("stick", {"side": "r", "x": 1.0, "y": 0.0})
        self.assertIn("zr", self.pad.state_dict()["buttons"])
        c.close()
        self.assertTrue(self.wait_until(lambda: self.pad.state_dict()["buttons"] == []))
        self.assertEqual(self.pad.state_dict()["sticks_raw"]["r"], [2048, 2048])
        self.assertTrue(
            any(e["event"] == "session_closed_neutral" and e["had_active_input"] for e in self.events)
        )


class Watchdog(ServerCase):
    idle_neutral_s = 0.3

    def test_idle_with_held_input_goes_neutral(self) -> None:
        c = self.connected()
        c.command("press", {"buttons": ["a"]})
        self.assertTrue(self.wait_until(lambda: self.pad.state_dict()["buttons"] == [], timeout=2.0))
        self.assertTrue(any(e["event"] == "watchdog_neutral" for e in self.events))

    def test_pings_keep_held_input(self) -> None:
        c = self.connected()
        c.command("press", {"buttons": ["a"]})
        for _ in range(6):
            time.sleep(0.1)
            c.command("ping")
        self.assertEqual(self.pad.state_dict()["buttons"], ["a"])

    def test_long_tap_is_not_cut_by_watchdog(self) -> None:
        c = self.connected()
        _, res = c.command("tap", {"button": "a", "hold_ms": 600})
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(res["held_ms"], 600)


class LegacyStillWorks(unittest.TestCase):
    def test_self_test(self) -> None:
        from procon import _self_test

        self.assertEqual(_self_test(), 0)

    def test_words_to_cmd(self) -> None:
        self.assertEqual(words_to_cmd(["tap", "a", "90"]), ("tap", {"button": "a", "hold_ms": 90}))
        self.assertEqual(words_to_cmd(["dpad", "clear"]), ("dpad", {"dir": None}))
        self.assertEqual(words_to_cmd(["stick", "l", "0", "1"]), ("stick", {"side": "l", "x": 0.0, "y": 1.0}))


if __name__ == "__main__":
    unittest.main()
