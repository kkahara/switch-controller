"""Deterministic fake-Pi stop races and crash faults; never opens hidg."""
from __future__ import annotations

import json
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from emergency_stop import StopCoordinator
from procon import ProController, initialize_safety_state
from safety import MutationSource, OwnershipDenied, OwnershipGate, SafetyState, StopMarkerStore


class FakePad(ProController):
    def __init__(self):
        super().__init__(verbose=False, reset_udc=False,
                         ownership_gate=OwnershipGate(SafetyState(10, False, "test_only")))
        self.writes = []

    def _write_device(self, report, trace=False):
        self.writes.append(bytes(report))
        return True


def assert_neutral(test, pad):
    state = pad.state_dict()
    test.assertEqual(state["buttons"], [])
    test.assertEqual(state["sticks_raw"], {"l": [2048, 2048], "r": [2048, 2048]})
    test.assertEqual(state["imu_pitch_deg"], 0)
    test.assertEqual(state["imu_pitch_rate_dps"], 0)


def measurement(name, samples):
    ordered = sorted(samples)
    result = {"scenario": name, "repetitions": len(samples),
              "p99_ms": ordered[math.ceil(len(samples) * .99) - 1],
              "max_ms": max(samples), "clock": "host_monotonic_fake_pi"}
    print("STOP_LATENCY " + json.dumps(result))
    if result["p99_ms"] > 100 or result["max_ms"] > 150:
        raise AssertionError(result)


class StopRaces(unittest.TestCase):
    def run_stop(self, pad, samples):
        received = time.monotonic_ns()
        result = pad.activate_emergency_stop()
        self.assertTrue(result["activated"], result)
        samples.append((result["pi_neutral_mono_ns"] - received) / 1e6)
        assert_neutral(self, pad)
        return result

    def test_queued_delayed_and_compound_mutations_100(self):
        samples = []
        for _ in range(100):
            pad = FakePad()
            grant = pad.ownership_gate.reserve_autonomy()
            ready, resume = threading.Event(), threading.Event()
            outcomes = []

            def compound():
                with pad.ownership_gate.source(MutationSource.AUTONOMOUS, grant):
                    pad.press("a", "left")
                    pad.set_stick("r", 1, 0)
                    pad.start_pitch_rotation(15, 50)
                    ready.set()
                    if not resume.wait(2):
                        return
                    for operation in (lambda: pad.press("b"),
                                      lambda: pad.set_stick("l", 1, 1),
                                      pad.finish_pitch_rotation, pad.neutral):
                        try:
                            operation()
                            outcomes.append("unsafe")
                        except OwnershipDenied:
                            outcomes.append("revoked")
            worker = threading.Thread(target=compound)
            worker.start()
            self.assertTrue(ready.wait(2))
            self.run_stop(pad, samples)
            resume.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes, ["revoked"] * 4)
            assert_neutral(self, pad)
        measurement("delayed_compound_generation_fence", samples)

    def test_blocked_ordinary_queue_100(self):
        samples = []
        for _ in range(100):
            pad = FakePad()
            held, release = threading.Event(), threading.Event()
            def ordinary():
                with pad.command_lock:
                    held.set()
                    release.wait(2)
            thread = threading.Thread(target=ordinary)
            thread.start()
            self.assertTrue(held.wait(2))
            self.run_stop(pad, samples)
            release.set()
            thread.join(2)
        measurement("ordinary_command_lock_blocked", samples)

    def test_packed_report_fence_100(self):
        samples = []
        for _ in range(100):
            pad = FakePad()
            pad.press("a")
            old = [pad.pack_0x30(), pad.pack_0x21(0x80, 1)]
            self.run_stop(pad, samples)
            for report in old:
                self.assertFalse(pad._write(report))
            self.assertEqual(pad.writes, [])
            self.assertTrue(pad._write(pad.pack_0x30()))
            self.assertIsNotNone(pad.neutral_write_evidence)
        measurement("previously_packed_hid_reports", samples)

    def test_human_ownership_orderings_100(self):
        samples = []
        for _ in range(100):
            pad = FakePad()
            human = object()
            self.assertTrue(pad.ownership_gate.claim_human(human))
            pad.press("a")
            self.assertEqual(pad.activate_emergency_stop()["error"], "human_owner_active")
            self.assertEqual(pad.state_dict()["buttons"], ["a"])
            # An inconsistent human+autonomous record cannot suppress enforcement.
            with pad.ownership_gate._lock:
                from safety import Owner
                pad.ownership_gate._owner = Owner.AUTONOMOUS
            self.run_stop(pad, samples)
            self.assertFalse(pad.ownership_gate.claim_human(object()))
            # Already activated stop cannot be suppressed by a human record.
            self.assertTrue(pad.activate_emergency_stop()["activated"])
        measurement("human_autonomous_inconsistency", samples)

    def test_inflight_hid_write_100(self):
        samples = []
        for _ in range(100):
            pad = FakePad()
            pad.press("a")
            entered, release, requested = threading.Event(), threading.Event(), threading.Event()
            original = pad._write_device
            def blocked(report, trace=False):
                entered.set()
                release.wait(2)
                return original(report, trace)
            pad._write_device = blocked
            writer = threading.Thread(target=lambda: pad._write(pad.pack_0x30()))
            writer.start()
            self.assertTrue(entered.wait(2))
            result = []
            def stop():
                requested.set()
                result.append((time.monotonic_ns(), pad.activate_emergency_stop()))
            stopper = threading.Thread(target=stop)
            stopper.start()
            self.assertTrue(requested.wait(2))
            release.set()
            writer.join(2); stopper.join(2)
            received, outcome = result[0]
            self.assertTrue(outcome["activated"])
            samples.append((outcome["pi_neutral_mono_ns"] - received) / 1e6)
            self.assertEqual(len(pad.writes), 1)  # in-flight write precedes activation
            assert_neutral(self, pad)
        measurement("inflight_write_released_before_activation", samples)

    def test_stuck_hid_write_reports_degraded(self):
        pad = FakePad()
        pad._write_lock.acquire()
        try:
            result = pad.activate_emergency_stop(timeout=0.001)
            self.assertFalse(result["activated"])
            self.assertEqual(result["error"], "hid_fence_timeout")
            self.assertTrue(pad.ownership_gate.state.requires_human_rearm)
            assert_neutral(self, pad)
        finally:
            pad._write_lock.release()

    def test_mutation_guard_linearization_100(self):
        samples = []
        for _ in range(100):
            pad = FakePad()
            grant = pad.ownership_gate.reserve_autonomy()
            inside, release, requested = threading.Event(), threading.Event(), threading.Event()
            writes, outcomes = [], []
            def mutate():
                with pad.ownership_gate.source(MutationSource.AUTONOMOUS, grant):
                    with pad.ownership_gate.mutation():
                        inside.set()
                        release.wait(2)  # deliberate test-only guard stall
                        pad.press("a")
                        writes.append(time.monotonic_ns())
            thread = threading.Thread(target=mutate)
            thread.start()
            self.assertTrue(inside.wait(2))
            def stop():
                requested.set()
                received = time.monotonic_ns()
                outcomes.append((received, pad.activate_emergency_stop()))
            stopper = threading.Thread(target=stop)
            stopper.start()
            self.assertTrue(requested.wait(2))
            release.set()
            thread.join(2); stopper.join(2)
            received, result = outcomes[0]
            self.assertLessEqual(writes[0], result["pi_activated_mono_ns"])
            assert_neutral(self, pad)
            samples.append((result["pi_neutral_mono_ns"] - received) / 1e6)
        measurement("mutation_guard_linearization", samples)

    def test_concurrent_human_claim_and_stop_100(self):
        samples = []
        for _ in range(100):
            pad = FakePad()
            barrier = threading.Barrier(3)
            claims, results = [], []
            def claim():
                barrier.wait()
                claims.append(pad.ownership_gate.claim_human(object()))
            def stop():
                barrier.wait()
                received = time.monotonic_ns()
                results.append((received, pad.activate_emergency_stop()))
            threads = [threading.Thread(target=claim), threading.Thread(target=stop)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(2)
            received, result = results[0]
            self.assertEqual(bool(result["activated"]), not claims[0])
            if result["activated"]:
                assert_neutral(self, pad)
                samples.append((result["pi_neutral_mono_ns"] - received) / 1e6)
        print("HUMAN_RACE " + json.dumps({"repetitions": 100, "stop_wins": len(samples)}))

    def test_missing_grant_is_rejected(self):
        pad = FakePad()
        pad.ownership_gate.reserve_autonomy()
        with pad.ownership_gate.source(MutationSource.AUTONOMOUS):
            with self.assertRaises(OwnershipDenied):
                pad.press("a")

    def test_human_record_cannot_hide_orphaned_grant(self):
        from safety import Owner
        pad = FakePad()
        pad.ownership_gate.reserve_autonomy()
        with pad.ownership_gate._lock:
            pad.ownership_gate._owner = Owner.NONE
            pad.ownership_gate._humans.add(object())
        self.assertTrue(pad.activate_emergency_stop()["activated"])


class CoordinatorTests(unittest.TestCase):
    def test_mismatched_hid_evidence_cannot_succeed(self):
        for mismatch in ("state_gen", "tx_epoch", "pi_write_mono_ns", "report_seq"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as directory:
                pad = FakePad()
                store = StopMarkerStore(directory)
                original = store.persist_latched
                def persist(generation, cause):
                    evidence = {"state_gen": pad.current_gen(), "tx_epoch": pad._tx_epoch,
                                "pi_write_mono_ns": time.monotonic_ns(), "report_seq": 1}
                    evidence[mismatch] = -1
                    with pad._report_cond:
                        pad.neutral_write_evidence = evidence
                        pad._report_cond.notify_all()
                    return original(generation, cause)
                store.persist_latched = persist
                coordinator = StopCoordinator(pad, store)
                try:
                    job = coordinator.submit()
                    self.assertTrue(job.done.wait(2))
                    self.assertEqual(job.result["status"], "degraded")
                    self.assertFalse(job.result["neutral_hid_transmitted"])
                    self.assertTrue(pad.ownership_gate.stopping)
                finally:
                    coordinator.close()

    def test_old_completion_cannot_release_new_stop(self):
        pad = FakePad()
        old = pad.activate_emergency_stop()
        new = pad.activate_emergency_stop()
        self.assertFalse(pad.ownership_gate.finish_stop(old["generation"]))
        self.assertTrue(pad.ownership_gate.stopping)
        self.assertTrue(pad.ownership_gate.finish_stop(new["generation"]))

    def test_storage_hang_is_bounded_and_does_not_hold_mutation_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            pad = FakePad()
            entered, release = threading.Event(), threading.Event()
            store = StopMarkerStore(directory)
            def hang(generation, cause):
                entered.set()
                release.wait(3)
            store.persist_latched = hang
            coordinator = StopCoordinator(pad, store)
            try:
                job = coordinator.submit()
                self.assertTrue(entered.wait(2))
                pad.neutral()
                self.assertTrue(job.done.wait(2))
                self.assertEqual(job.result["status"], "degraded")
                self.assertEqual(job.result["persistence_error"], "timeout")
                self.assertTrue(pad.ownership_gate.state.requires_human_rearm)
            finally:
                release.set()
                coordinator.close()

    def test_verified_v1_human_is_preserved(self):
        from protocol_v1 import V1Client, V1Server
        pad = FakePad()
        server = V1Server(pad, "127.0.0.1", 0, "fixture-token-0123456789")
        server.start()
        client = V1Client(*server.address, "fixture-token-0123456789")
        try:
            self.assertEqual(client.connect()["status"], "ok")
            pad.press("a")
            outcome = pad.activate_emergency_stop()
            self.assertEqual(outcome["error"], "human_owner_active")
            self.assertEqual(pad.state_dict()["buttons"], ["a"])
            self.assertFalse(pad.ownership_gate.stop_activated)
        finally:
            client.close()
            server.stop()

    def test_simultaneous_requests_and_independent_persistence_100(self):
        samples = []
        with tempfile.TemporaryDirectory() as directory:
            for index in range(100):
                pad = FakePad()
                entered, release = threading.Event(), threading.Event()
                store = StopMarkerStore(Path(directory) / str(index))
                original = store.persist_latched
                def persist(generation, cause):
                    entered.set()
                    release.wait(2)
                    return original(generation, cause)
                store.persist_latched = persist
                coordinator = StopCoordinator(pad, store)
                try:
                    first = coordinator.submit()
                    self.assertTrue(entered.wait(2))
                    second = coordinator.submit()
                    self.assertIs(first, second)
                    # Persistent I/O is outside mutation/authority locks.
                    self.assertTrue(pad.ownership_gate.state.requires_human_rearm)
                    pad._write(pad.pack_0x30())
                    pad.neutral()
                    release.set()
                    self.assertTrue(first.done.wait(2))
                    self.assertEqual(first.result["status"], "ok", first.result)
                    self.assertTrue(first.result["latch_durable"])
                    samples.append((first.result["pi_neutral_mono_ns"] - first.received_ns) / 1e6)
                finally:
                    release.set()
                    coordinator.close()
        measurement("coalesced_requests_persistence_worker", samples)

    def test_persistence_failure_preserves_runtime_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            pad = FakePad()
            store = StopMarkerStore(directory)
            coordinator = StopCoordinator(pad, store)
            try:
                entered = threading.Event()
                def fail(generation, cause):
                    entered.set()
                    raise OSError("disk")
                with patch.object(store, "persist_latched", side_effect=fail):
                    job = coordinator.submit()
                    self.assertTrue(entered.wait(2))
                    pad._write(pad.pack_0x30())
                    self.assertTrue(job.done.wait(2))
                self.assertEqual(job.result["status"], "degraded")
                self.assertFalse(job.result["latch_durable"])
                self.assertTrue(job.result["neutral_hid_transmitted"])
                self.assertTrue(pad.ownership_gate.state.requires_human_rearm)
                self.assertTrue(pad.ownership_gate.stopping)
                assert_neutral(self, pad)
            finally:
                coordinator.close()


class CrashFaults(unittest.TestCase):
    def test_short_writes_are_completed(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            store = StopMarkerStore(directory)
            original = os.write
            with patch("safety.os.write", side_effect=lambda fd, data: original(fd, data[:3])):
                store.persist_latched(1, "stop")
            self.assertEqual(store._read(store.paths[0]), SafetyState(1, True, "stop"))

    def test_storage_fault_boundaries_and_restart(self):
        for operation in ("write", "fsync_file", "replace", "fsync_directory", "readback"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                store = StopMarkerStore(directory)
                store.persist_latched(1, "baseline")
                store.persist_latched(2, "baseline")
                if operation == "write":
                    context = patch("safety.os.write", side_effect=OSError("write"))
                elif operation == "replace":
                    context = patch("safety.os.replace", side_effect=OSError("rename"))
                elif operation.startswith("fsync"):
                    import os
                    original, count = os.fsync, [0]
                    def fsync(fd):
                        count[0] += 1
                        if count[0] == (1 if operation == "fsync_file" else 2):
                            raise OSError("sync")
                        return original(fd)
                    context = patch("safety.os.fsync", side_effect=fsync)
                else:
                    original_read = store._read
                    def read(path):
                        state = original_read(path)
                        return None if state is not None and state.generation == 3 else state
                    context = patch.object(store, "_read", side_effect=read)
                with context, self.assertRaises(Exception):
                    store.persist_latched(3, "emergency_stop")
                reboot = FakePad()
                gate = initialize_safety_state(reboot, StopMarkerStore(directory))
                self.assertTrue(gate.state.requires_human_rearm)
                self.assertGreaterEqual(gate.generation, 3)
                assert_neutral(self, reboot)

    def test_clear_markers_always_require_rearm_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StopMarkerStore(directory)
            store._ensure_directory()
            for path in store.paths:
                store._write_atomic(path, SafetyState(20, False, "old_clear"))
            pad = FakePad()
            gate = initialize_safety_state(pad, store)
            self.assertEqual(gate.generation, 21)
            self.assertTrue(gate.state.requires_human_rearm)
            with self.assertRaises(OwnershipDenied):
                gate.reserve_autonomy()


if __name__ == "__main__":
    unittest.main()
