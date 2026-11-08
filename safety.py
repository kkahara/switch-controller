"""S2B-1A fail-closed safety state and unified controller ownership gate."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator

MARKER_SCHEMA = "procon-safety-v1"
MARKER_A = "stop-latch.a"
MARKER_B = "stop-latch.b"


class SafetyStateError(RuntimeError):
    """A stale or otherwise unsafe persistent-state transition."""


class OwnershipDenied(RuntimeError):
    """A command source attempted to mutate while autonomy owns the pad."""


class MutationSource(str, Enum):
    INTERNAL = "internal"
    STDIN = "stdin"
    UNIX_SOCKET = "unix_socket"
    V1 = "v1"
    AUTONOMOUS = "autonomous"


class Owner(str, Enum):
    NONE = "none"
    AUTONOMOUS = "autonomous"


@dataclass(frozen=True)
class SafetyState:
    generation: int
    requires_human_rearm: bool
    cause: str


@dataclass(frozen=True)
class MutationGrant:
    instance: str
    generation: int


def _payload(state: SafetyState) -> dict[str, object]:
    return {
        "schema": MARKER_SCHEMA,
        "generation": state.generation,
        "requires_human_rearm": state.requires_human_rearm,
        "cause": state.cause,
    }


def _checksum(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StopMarkerStore:
    """Crash-safe, dual-slot persistent stop-marker store.

    Missing, partial, corrupt, conflicting, or stale-on-write state is always
    returned as requires_human_rearm. The checksum detects accidental
    corruption; authenticated integrity is deferred with credential provisioning.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._lock = threading.RLock()

    @property
    def paths(self) -> tuple[Path, Path]:
        return self.directory / MARKER_A, self.directory / MARKER_B

    def _ensure_directory(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def _read(self, path: Path) -> SafetyState | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            checksum = raw.pop("checksum", None)
            generation = raw.get("generation")
            latch = raw.get("requires_human_rearm")
            cause = raw.get("cause")
            state = SafetyState(generation, latch, cause)
            if (
                raw != _payload(state)
                or not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 0
                or not isinstance(latch, bool)
                or not isinstance(cause, str)
                or not cause
                or not isinstance(checksum, str)
                or checksum != _checksum(raw)
            ):
                return None
            return state
        except (OSError, TypeError, ValueError):
            return None

    def _temporary_write_exists(self) -> bool:
        return any(self.directory.glob("stop-latch.*.tmp")) if self.directory.exists() else False

    def load(self) -> SafetyState:
        """Return persistent state, defaulting to an explicit fail-closed latch."""
        with self._lock:
            first, second = (self._read(path) for path in self.paths)
            states = [state for state in (first, second) if state is not None]
            if self._temporary_write_exists():
                return SafetyState(max((state.generation for state in states), default=0), True, "partial_write")
            if not states:
                return SafetyState(0, True, "missing_or_corrupt")
            highest = max(state.generation for state in states)
            newest = [state for state in states if state.generation == highest]
            if len({(state.requires_human_rearm, state.cause) for state in newest}) != 1:
                return SafetyState(highest, True, "conflicting_records")
            if len(states) != 2:
                chosen = newest[0]
                return SafetyState(chosen.generation, True, "incomplete_redundancy")
            return newest[0]

    def persist_latched(self, generation: int, cause: str) -> SafetyState:
        """Durably write a newer stopped state, or reject a stale generation."""
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise SafetyStateError("generation must be a positive integer")
        if not isinstance(cause, str) or not cause:
            raise SafetyStateError("cause is required")
        with self._lock:
            current = self.load()
            if generation <= current.generation:
                raise SafetyStateError(
                    "stale safety generation %d (current %d)" % (generation, current.generation)
                )
            self._ensure_directory()
            state = SafetyState(generation, True, cause)
            first, second = self.paths
            first_state, second_state = self._read(first), self._read(second)
            if first_state is None or (
                second_state is not None and first_state.generation <= second_state.generation
            ):
                target = first
            else:
                target = second
            self._write_atomic(target, state)
            return state

    def _write_atomic(self, target: Path, state: SafetyState) -> None:
        payload = _payload(state)
        record = dict(payload, checksum=_checksum(payload))
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        temporary = target.with_name(target.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            remaining = memoryview(encoded)
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0:
                    raise OSError("short stop-marker write")
                remaining = remaining[count:]
            os.fsync(fd)
            os.replace(temporary, target)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.close(fd)
        if self._read(target) != state:
            raise SafetyStateError("stop marker read-back validation failed")


class OwnershipGate:
    """Single decision point for every mutating controller source.

    Human sources remain compatible while no autonomous owner exists. Once a
    future v2 path explicitly reserves autonomy, every non-autonomous mutation
    is denied. Full neutralization is always permitted because it reduces
    controller authority.
    """

    def __init__(self, state: SafetyState) -> None:
        self._state = state
        self._owner = Owner.NONE
        self._lock = threading.RLock()
        self._local = threading.local()
        self.instance = uuid.uuid4().hex
        self.generation = state.generation
        self.stop_activated = False
        self.stopping = False
        self._grant: MutationGrant | None = None
        self._humans: set[object] = set()

    @property
    def state(self) -> SafetyState:
        with self._lock:
            return self._state

    @property
    def owner(self) -> Owner:
        with self._lock:
            return self._owner

    def current_source(self) -> MutationSource:
        return getattr(self._local, "source", MutationSource.INTERNAL)

    @contextmanager
    def source(self, source: MutationSource, grant: MutationGrant | None = None) -> Iterator[None]:
        previous = self.current_source()
        previous_grant = getattr(self._local, "grant", None)
        self._local.source = source
        self._local.grant = grant
        try:
            yield
        finally:
            self._local.source = previous
            self._local.grant = previous_grant

    def allow_mutation(self) -> None:
        with self._lock:
            source = self.current_source()
            if source is MutationSource.AUTONOMOUS:
                grant = getattr(self._local, "grant", None)
                if (self._owner is not Owner.AUTONOMOUS or self.stop_activated or
                        grant is None or grant is not self._grant):
                    raise OwnershipDenied("revoked autonomous generation")
            if self.stopping:
                raise OwnershipDenied("emergency stop in progress")
            if self._owner is Owner.AUTONOMOUS and source is not MutationSource.AUTONOMOUS:
                raise OwnershipDenied("autonomous ownership blocks %s mutation" % source.value)

    @contextmanager
    def mutation(self) -> Iterator[None]:
        with self._lock:
            self.allow_mutation()
            yield

    def claim_human(self, identity: object) -> bool:
        with self._lock:
            if self._owner is Owner.AUTONOMOUS or self.stopping:
                return False
            self._humans.add(identity)
            return True

    def release_human(self, identity: object) -> None:
        with self._lock:
            self._humans.discard(identity)

    @contextmanager
    def stop_transition(self) -> Iterator[bool]:
        """Caller holds transmission fence; no I/O or callbacks inside this guard."""
        with self._lock:
            preserve = (bool(self._humans) and self._owner is Owner.NONE and
                        self._grant is None and not self.stop_activated and not self.stopping)
            if not preserve:
                self.generation += 1
                self.stop_activated = True
                self.stopping = True
                self._grant = None
                self._state = SafetyState(self.generation, True, "emergency_stop")
            yield not preserve

    def finish_stop(self, expected_generation: int) -> bool:
        with self._lock:
            if self.generation != expected_generation or not self.stop_activated:
                return False
            self._owner = Owner.NONE
            self.stopping = False
            return True

    def allows_legacy_control(self) -> bool:
        with self._lock:
            return self._owner is not Owner.AUTONOMOUS and not self.stopping

    @contextmanager
    def neutralization(self) -> Iterator[None]:
        """Serialize neutral with ownership decisions; trusted safety paths remain available."""
        with self._lock:
            if self.current_source() is MutationSource.AUTONOMOUS:
                self.allow_mutation()
            if (self._owner is Owner.AUTONOMOUS and
                    self.current_source() not in (MutationSource.INTERNAL, MutationSource.AUTONOMOUS)):
                raise OwnershipDenied("autonomous ownership blocks external neutral")
            yield

    def reserve_autonomy(self) -> MutationGrant:
        """Future v2-only entry point; S2B-1A has no production caller."""
        with self._lock:
            if self._state.requires_human_rearm or self.stop_activated:
                raise OwnershipDenied("human rearm required before autonomous authority")
            if self._owner is not Owner.NONE or self._humans:
                raise OwnershipDenied("controller already has autonomous ownership")
            self._owner = Owner.AUTONOMOUS
            self.generation += 1
            self._grant = MutationGrant(self.instance, self.generation)
            return self._grant

    def release_autonomy(self) -> None:
        with self._lock:
            self._owner = Owner.NONE
            self._grant = None

    def latch_stop(self, store: StopMarkerStore, cause: str) -> SafetyState:
        """Latch runtime before storage; persistence failure never releases control."""
        with self._lock:
            self.generation += 1
            state = SafetyState(self.generation, True, cause)
            self._state = state
            self.stop_activated = True
            self.stopping = True
            self._grant = None
        store.persist_latched(state.generation, cause)
        return state
