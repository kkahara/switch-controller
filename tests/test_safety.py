"""S2B-1A safety-kernel tests. No HID gadget, Pi, or controller input required."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest

from procon import ControlServer, ProController, ProconClient, initialize_safety_state, run_cli_command
from protocol_v1 import V1Client, V1Server
from safety import (
    MutationSource,
    OwnershipDenied,
    OwnershipGate,
    SafetyState,
    SafetyStateError,
    StopMarkerStore,
)


class MarkerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StopMarkerStore(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_marker_fails_closed(self) -> None:
        state = self.store.load()
        self.assertTrue(state.requires_human_rearm)
        self.assertEqual(state.cause, "missing_or_corrupt")

    def test_startup_is_neutral_and_disarmed_when_marker_is_missing(self) -> None:
        pad = ProController(reset_udc=False, verbose=False)
        pad.press("a")
        pad.set_stick("l", 0, 0)
        gate = initialize_safety_state(pad, self.store)
        self.assertTrue(gate.state.requires_human_rearm)
        self.assertEqual(gate.owner.value, "none")
        self.assertEqual(pad.state_dict()["buttons"], [])
        self.assertEqual(pad.state_dict()["sticks_raw"]["l"], [2048, 2048])

    def test_atomic_slots_and_stale_generation(self) -> None:
        first = self.store.persist_latched(1, "stop")
        second = self.store.persist_latched(2, "lease_loss")
        self.assertEqual(first.generation, 1)
        self.assertEqual(self.store.load(), second)
        with self.assertRaises(SafetyStateError):
            self.store.persist_latched(2, "stale")

    def test_partial_corrupt_and_conflicting_records_fail_closed(self) -> None:
        self.store.persist_latched(1, "stop")
        self.store.persist_latched(2, "lease_loss")
        first, second = self.store.paths
        first.write_text("{not-json", encoding="utf-8")
        state = self.store.load()
        self.assertTrue(state.requires_human_rearm)
        self.assertIn(state.cause, {"incomplete_redundancy", "missing_or_corrupt"})

        self.store._write_atomic(first, SafetyState(5, True, "stop"))
        self.store._write_atomic(second, SafetyState(5, False, "invalid_clear"))
        state = self.store.load()
        self.assertTrue(state.requires_human_rearm)
        self.assertEqual(state.cause, "conflicting_records")

        (self.store.directory / "stop-latch.a.tmp").write_text("partial", encoding="utf-8")
        self.assertEqual(self.store.load().cause, "partial_write")


class OwnershipGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = OwnershipGate(SafetyState(7, False, "human_rearmed"))
        self.pad = ProController(reset_udc=False, verbose=False, ownership_gate=self.gate)

    def test_concurrent_reservation_has_one_winner(self) -> None:
        outcomes: list[str] = []
        lock = threading.Lock()

        def reserve() -> None:
            try:
                self.gate.reserve_autonomy()
                outcome = "won"
            except OwnershipDenied:
                outcome = "denied"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(outcomes.count("denied"), 7)

    def test_direct_stdin_and_unix_mutations_cannot_bypass_autonomy(self) -> None:
        self.pad.press("a")
        self.gate.reserve_autonomy()
        with self.assertRaises(OwnershipDenied):
            self.pad.release("a")
        with self.assertRaisesRegex(Exception, "autonomous ownership"):
            run_cli_command(self.pad, "press b", MutationSource.STDIN)
        self.pad.neutral()
        self.assertEqual(self.pad.state_dict()["buttons"], [])

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "procon.sock")
            server = ControlServer(self.pad, path)
            server.start()
            try:
                with ProconClient(path, timeout=2) as client:
                    with self.assertRaisesRegex(RuntimeError, "autonomous ownership"):
                        client.press("x")
                    with self.assertRaisesRegex(RuntimeError, "external neutral"):
                        client.neutral()
            finally:
                server.stop()

    def test_every_public_non_neutral_mutator_obeys_the_gate(self) -> None:
        self.gate.reserve_autonomy()
        operations = (
            lambda: self.pad.press("a"),
            lambda: self.pad.release("a"),
            lambda: self.pad.release_all(),
            lambda: self.pad.tap("a", 0),
            lambda: self.pad.set_dpad("left"),
            lambda: self.pad.set_stick("l", 0, 0),
            lambda: self.pad.center_sticks(),
            lambda: self.pad.start_pitch_rotation(1, 50),
            lambda: self.pad.finish_pitch_rotation(),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(OwnershipDenied):
                    operation()

    def test_v1_control_is_busy_while_autonomy_is_reserved(self) -> None:
        self.gate.reserve_autonomy()
        server = V1Server(self.pad, "127.0.0.1", 0, "fixture-token-0123456789")
        server.start()
        try:
            client = V1Client(*server.address, "fixture-token-0123456789")
            try:
                reply = client.connect()
                self.assertEqual(reply["status"], "rejected")
                self.assertEqual(reply["error"]["code"], "busy")
            finally:
                client.close()
        finally:
            server.stop()

    def test_neutral_never_requires_autonomous_authority(self) -> None:
        self.pad.press("a")
        self.gate.reserve_autonomy()
        self.pad.neutral()
        self.assertEqual(self.pad.state_dict()["buttons"], [])
        self.assertEqual(self.gate.owner.value, "autonomous")
        self.assertEqual(self.gate.state, SafetyState(7, False, "human_rearmed"))

    def test_external_neutral_denied_while_autonomous_and_allowed_otherwise(self) -> None:
        self.pad.press("a")
        self.gate.reserve_autonomy()
        for source in (MutationSource.STDIN, MutationSource.UNIX_SOCKET, MutationSource.V1):
            with self.subTest(source=source):
                with self.gate.source(source), self.assertRaises(OwnershipDenied):
                    self.pad.neutral()
                self.assertEqual(self.pad.state_dict()["buttons"], ["a"])
                self.assertEqual(self.gate.owner.value, "autonomous")
        self.gate.release_autonomy()
        with self.gate.source(MutationSource.UNIX_SOCKET):
            self.pad.neutral()
        self.assertEqual(self.pad.state_dict()["buttons"], [])

    def test_trusted_neutral_preserves_latched_state(self) -> None:
        state = SafetyState(9, True, "emergency_stop")
        gate = OwnershipGate(state)
        pad = ProController(reset_udc=False, verbose=False, ownership_gate=gate)
        pad.press("a")
        pad.neutral()
        self.assertEqual(gate.state, state)
        self.assertEqual(gate.owner.value, "none")
        self.assertEqual(pad.state_dict()["buttons"], [])


if __name__ == "__main__":
    unittest.main()
