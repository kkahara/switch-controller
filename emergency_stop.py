"""Bounded independent emergency-stop processing; no execution, lease or rearm."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from safety import StopMarkerStore


@dataclass
class StopJob:
    received_ns: int
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] = field(default_factory=dict)


class StopCoordinator:
    """One safety worker and one persistence worker, coalescing concurrent requests."""

    def __init__(self, pad: Any, store: StopMarkerStore) -> None:
        self.pad, self.store = pad, store
        self._lock = threading.Lock()
        self._active: StopJob | None = None
        self._jobs: queue.Queue = queue.Queue(maxsize=1)
        self._persist: queue.Queue = queue.Queue(maxsize=1)
        self._closed = False
        self._workers = [
            threading.Thread(target=self._run, daemon=True, name="safety-stop"),
            threading.Thread(target=self._write_markers, daemon=True, name="safety-persist"),
        ]
        for worker in self._workers:
            worker.start()

    def submit(self) -> StopJob:
        with self._lock:
            if self._closed:
                raise RuntimeError("stop coordinator closed")
            if self._active is not None:
                return self._active
            job = StopJob(time.monotonic_ns())
            self._active = job
            self._jobs.put_nowait(job)
            return job

    def close(self) -> None:
        with self._lock:
            self._closed = True
        for channel in (self._jobs, self._persist):
            try:
                channel.put_nowait(None)
            except queue.Full:
                pass

    def _write_markers(self) -> None:
        while True:
            item = self._persist.get()
            if item is None:
                return
            generation, outcome, done = item
            try:
                self.store.persist_latched(generation, "emergency_stop")
                outcome["latch_durable"] = True
                outcome["pi_durable_mono_ns"] = time.monotonic_ns()
            except Exception as exc:
                outcome["persistence_error"] = type(exc).__name__
            finally:
                done.set()

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            result = {"pi_recv_mono_ns": job.received_ns, "latch_durable": False,
                      "neutral_hid_transmitted": False}
            try:
                result.update(self.pad.activate_emergency_stop())
                if result.get("internal_neutral"):
                    outcome: dict[str, Any] = {}
                    persisted = threading.Event()
                    try:
                        self._persist.put_nowait(
                            (result.get("generation", self.pad.ownership_gate.state.generation),
                             outcome, persisted))
                        if persisted.wait(0.25):
                            result.update(outcome)
                        else:
                            result["persistence_error"] = "timeout"
                    except queue.Full:
                        result["persistence_error"] = "busy"
                    # Report streaming remains independent of persistence.
                    deadline = time.monotonic() + 0.25
                    with self.pad._report_cond:
                        while (self.pad.neutral_write_evidence is None and
                               time.monotonic() < deadline):
                            self.pad._report_cond.wait(max(0, deadline - time.monotonic()))
                        evidence = self.pad.neutral_write_evidence
                    if (result.get("activated") and evidence is not None and
                            evidence.get("state_gen") == result.get("neutral_state_gen") and
                            evidence.get("tx_epoch") == result.get("tx_epoch") and
                            isinstance(evidence.get("report_seq"), int) and
                            evidence["report_seq"] > 0 and
                            evidence.get("pi_write_mono_ns", -1) >= result["pi_neutral_mono_ns"]):
                        result["neutral_hid_transmitted"] = True
                        result["neutral_hid_write"] = dict(evidence)
                success = all(result.get(key) for key in (
                    "activated", "internal_neutral", "latch_durable", "neutral_hid_transmitted"))
                result["status"] = "ok" if success else "degraded"
                if result.get("error") == "human_owner_active":
                    result["status"] = "rejected"
                if success:
                    if not self.pad.ownership_gate.finish_stop(result["generation"]):
                        result.update(status="degraded", error="stop_superseded")
            except Exception as exc:
                result.update(status="degraded", error=type(exc).__name__)
            finally:
                result["pi_end_mono_ns"] = time.monotonic_ns()
                job.result = result
                # Publish the completed result before allowing a new transaction.
                with self._lock:
                    job.done.set()
                    self._active = None
