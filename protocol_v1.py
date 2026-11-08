#!/usr/bin/env python3
"""Protocol v1: line-delimited JSON over TCP for a remote controller client.

Controller-level operations only (press/release/tap/dpad/stick/rotate_pitch/
neutral/state/ping). The specification is PROTOCOL.md; golden exchanges live in
protocol/v1/fixtures/. Every command source shares ProController.command_lock,
so v1, the Unix socket and stdin never interleave.
"""

from __future__ import annotations

import collections
import hmac
import json
import os
import queue
import socket
import threading
import time
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from procon import (
    BUTTONS,
    DPAD,
    PITCH_ROTATION_MAX_DEG,
    PITCH_ROTATION_MS,
    STICK_CENTER,
    STICK_MAX,
    gravity_for_pitch,
    gyro_raw_for_rate,
)
from safety import MutationSource

PROTOCOL_VERSION = 1
DEFAULT_PORT = 47800
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.config/procon/v1-token")
SERVER_NAME = "procon/1"

V1_BUTTONS: Tuple[str, ...] = tuple(
    sorted(b for b in BUTTONS if b not in ("charging", "r-sr", "r-sl", "l-sr", "l-sl"))
)
OPS = (
    "press", "release", "tap", "dpad", "stick", "rotate_pitch",
    "neutral", "state", "ping",
)
OBSERVE_OPS = ("state", "ping")
HOLD_MS_MIN = 30
HOLD_MS_MAX = 2000
DEFAULT_MAX_QUEUE_MS = 1000
MAX_ID_LEN = 64
ID_CACHE_SIZE = 256
IDLE_NEUTRAL_S = 2.0
REPORT_WAIT_S = 0.25
MAX_LINE = 8192
STICK_ACTIVE_RAW = 200  # off-center distance that counts as a held stick

Json = Dict[str, Any]


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def read_token_file(path: Optional[str]) -> str:
    if not path:
        raise ValueError("--v1-token-file is required with --v1-listen")
    with open(path, "r", encoding="utf-8") as fh:
        token = fh.read().strip()
    if len(token) < 16:
        raise ValueError("token in %s must be at least 16 characters" % path)
    return token


def parse_listen(text: str) -> Tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep:
        return text, DEFAULT_PORT
    return host or "0.0.0.0", int(port)


def boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return None


def axis_to_raw(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("bad_args", "stick axes must be numbers in -1..1")
    f = float(value)
    if not -1.0 <= f <= 1.0:
        raise ProtocolError("bad_args", "stick axis %r outside -1..1" % value)
    return max(0, min(STICK_MAX, int(round(STICK_CENTER + f * (STICK_CENTER - 1)))))


def raw_to_axis(raw: int) -> float:
    return round(max(-1.0, min(1.0, (raw - STICK_CENTER) / (STICK_CENTER - 1))), 4)


def _button(value: Any) -> str:
    if value not in V1_BUTTONS:
        raise ProtocolError("bad_args", "unknown button %r; known: %s" % (value, ", ".join(V1_BUTTONS)))
    return str(value)


def validate(op: Any, args: Any) -> Json:
    """Return normalized args or raise ProtocolError(unknown_op | bad_args)."""
    if op not in OPS:
        raise ProtocolError("unknown_op", "unknown op %r; known: %s" % (op, ", ".join(OPS)))
    if not isinstance(args, dict):
        raise ProtocolError("bad_args", "args must be an object")
    if op in ("press", "release"):
        buttons = args.get("buttons")
        if not isinstance(buttons, list) or not buttons:
            raise ProtocolError("bad_args", "%s needs a non-empty buttons list" % op)
        return {"buttons": [_button(b) for b in buttons]}
    if op == "tap":
        hold = args.get("hold_ms", 80)
        if isinstance(hold, bool) or not isinstance(hold, int) or not HOLD_MS_MIN <= hold <= HOLD_MS_MAX:
            raise ProtocolError("bad_args", "hold_ms must be an integer %d..%d" % (HOLD_MS_MIN, HOLD_MS_MAX))
        return {"button": _button(args.get("button")), "hold_ms": hold}
    if op == "dpad":
        direction = args.get("dir")
        if direction is not None and direction not in DPAD:
            raise ProtocolError("bad_args", "dir must be one of %s or null" % ", ".join(DPAD))
        return {"dir": direction}
    if op == "stick":
        side = args.get("side")
        if side not in ("l", "r"):
            raise ProtocolError("bad_args", "side must be 'l' or 'r'")
        return {"side": side, "x": axis_to_raw(args.get("x")), "y": axis_to_raw(args.get("y"))}
    if op == "rotate_pitch":
        delta = args.get("delta_deg")
        duration = args.get("duration_ms")
        if (
            isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or delta == 0
            or abs(delta) > PITCH_ROTATION_MAX_DEG
        ):
            raise ProtocolError(
                "bad_args", "delta_deg must be a non-zero number within ±%g" % PITCH_ROTATION_MAX_DEG
            )
        lo, hi = PITCH_ROTATION_MS
        if isinstance(duration, bool) or not isinstance(duration, int) or not lo <= duration <= hi:
            raise ProtocolError("bad_args", "duration_ms must be an integer %d..%d" % (lo, hi))
        return {"delta_deg": float(delta), "duration_ms": duration}
    return {}


def has_active_input(pad: Any) -> bool:
    state = pad.state_dict()
    if state["buttons"]:
        return True
    for x, y in state["sticks_raw"].values():
        if abs(x - STICK_CENTER) > STICK_ACTIVE_RAW or abs(y - STICK_CENTER) > STICK_ACTIVE_RAW:
            return True
    return False


def execute(pad: Any, op: str, args: Json, report_wait: float = REPORT_WAIT_S) -> Json:
    """Run one validated op. Caller holds pad.command_lock.

    status is "ok" only when the change was seen in a 0x30 report written to
    the host; "applied" says whether controller state was changed at all.
    """
    if op == "ping":
        return {"status": "ok"}
    if op == "state":
        return {"status": "ok", "state": pad.state_dict()}
    if op != "neutral" and not pad.is_streaming():
        return {"status": "not_streaming", "applied": False}

    def transmitted(seq: Optional[int], **extra: Any) -> Json:
        if seq is not None:
            return dict(status="ok", applied=True, **extra)
        if not pad.is_streaming():
            return dict(status="not_streaming", applied=True, **extra)
        return dict(
            status="error",
            applied=True,
            error={"code": "report_timeout", "message": "no 0x30 report within %.0f ms" % (report_wait * 1000)},
            **extra,
        )

    if op == "tap":
        pad.press(args["button"])
        pressed_ns = time.monotonic_ns()
        first = pad.wait_reported(pad.current_gen(), report_wait)
        if first is None:
            pad.release(args["button"])
            return transmitted(None, first_report_seq=None, last_report_seq=None)
        remaining = args["hold_ms"] / 1000.0 - (time.monotonic_ns() - pressed_ns) / 1e9
        if remaining > 0:
            time.sleep(remaining)
        pad.release(args["button"])
        held_ms = round((time.monotonic_ns() - pressed_ns) / 1e6, 1)
        last = pad.wait_reported(pad.current_gen(), report_wait)
        return transmitted(last, first_report_seq=first, last_report_seq=last, held_ms=held_ms)

    if op == "rotate_pitch":
        delta, duration_ms = args["delta_deg"], args["duration_ms"]
        start_pitch, rate = pad.start_pitch_rotation(delta, duration_ms)
        started_ns = time.monotonic_ns()
        first = pad.wait_reported(pad.current_gen(), report_wait)
        remaining = duration_ms / 1000.0 - (time.monotonic_ns() - started_ns) / 1e9
        if first is not None and remaining > 0:
            time.sleep(remaining)
        end_pitch = pad.finish_pitch_rotation()
        last = pad.wait_reported(pad.current_gen(), report_wait) if first is not None else None
        return transmitted(
            last,
            first_report_seq=first,
            last_report_seq=last,
            motion={
                "start_pitch_deg": round(start_pitch, 3),
                "delta_deg": delta,
                "duration_ms": duration_ms,
                "gyro_rate_dps": round(rate, 3),
                "gyro_raw": gyro_raw_for_rate(rate),
                "end_pitch_deg": round(end_pitch, 3),
                "accel": list(gravity_for_pitch(end_pitch)),
            },
        )

    if op == "press":
        pad.press(*args["buttons"])
    elif op == "release":
        pad.release(*args["buttons"])
    elif op == "dpad":
        pad.set_dpad(args["dir"])
    elif op == "stick":
        pad.set_stick(args["side"], args["x"], args["y"])
    elif op == "neutral":
        pad.neutral()
    seq = pad.wait_reported(pad.current_gen(), report_wait) if pad.is_streaming() else None
    return transmitted(seq, first_report_seq=seq, last_report_seq=seq)


def log_json(event: str, **fields: Any) -> None:
    record = {"event": event, "pi_mono_ns": time.monotonic_ns()}
    record.update(fields)
    print("v1 " + json.dumps(record, separators=(",", ":"), default=str), flush=True)


class _Entry:
    __slots__ = ("ack", "result", "waiters")

    def __init__(self, ack: Json) -> None:
        self.ack = ack
        self.result: Optional[Json] = None
        self.waiters: List["_Session"] = []


class IdCache:
    """Last ID_CACHE_SIZE command IDs with their ack and (eventual) result."""

    def __init__(self, size: int = ID_CACHE_SIZE) -> None:
        self._size = size
        self._items: "collections.OrderedDict[str, _Entry]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def get(self, cmd_id: str) -> Optional[_Entry]:
        with self._lock:
            return self._items.get(cmd_id)

    def put(self, cmd_id: str, entry: _Entry) -> None:
        with self._lock:
            self._items[cmd_id] = entry
            while len(self._items) > self._size:
                self._items.popitem(last=False)


class _Session:
    def __init__(self, server: "V1Server", conn: socket.socket, addr: Any) -> None:
        self.server = server
        self.conn = conn
        self.addr = addr
        self.session_id: Optional[str] = None
        self.role = "control"
        self.alive = True
        self.last_rx_ns = time.monotonic_ns()
        self.busy = False
        self._send_lock = threading.Lock()
        self._queue: "queue.Queue[Optional[Tuple[Json, int, _Entry]]]" = queue.Queue()

    # --- io ---------------------------------------------------------------

    def send(self, msg: Json) -> None:
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            if not self.alive:
                return
            try:
                self.conn.sendall(data)
            except OSError:
                self.alive = False

    def close(self) -> None:
        self.alive = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def _lines(self):
        buf = b""
        while self.alive:
            try:
                chunk = self.conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if raw.strip():
                    yield raw
            if len(buf) > MAX_LINE:
                self.send(_error("bad_request", "line longer than %d bytes" % MAX_LINE))
                return

    # --- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        worker: Optional[threading.Thread] = None
        try:
            lines = self._lines()
            first = next(lines, None)
            if first is None or not self._hello(first):
                return
            worker = threading.Thread(target=self._work, name="v1-exec", daemon=True)
            worker.start()
            for raw in lines:
                self.last_rx_ns = time.monotonic_ns()
                self._on_line(raw)
        finally:
            self._queue.put(None)
            if worker is not None:
                worker.join(timeout=HOLD_MS_MAX / 1000.0 + 1.0)
            self.close()
            self.server._session_closed(self)

    def _hello(self, raw: bytes) -> bool:
        try:
            msg = json.loads(raw)
        except ValueError:
            self.send(_hello_reject("bad_request", "first message must be JSON hello"))
            return False
        if not isinstance(msg, dict) or msg.get("type") != "hello":
            self.send(_hello_reject("bad_request", "first message must be hello"))
            return False
        if msg.get("v") != PROTOCOL_VERSION:
            self.send(_hello_reject("bad_version", "server speaks v%d" % PROTOCOL_VERSION))
            return False
        token = msg.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self.server.token):
            self.send(_hello_reject("unauthorized", "bad token"))
            return False
        role = msg.get("role", "control")
        if role not in ("control", "observe"):
            self.send(_hello_reject("bad_request", "role must be control or observe"))
            return False
        self.role = role
        self.session_id = str(msg.get("session_id") or uuid.uuid4().hex[:12])
        if role == "control" and not self.server._claim_control(self, bool(msg.get("takeover"))):
            self.send(_hello_reject("busy", "another control session is active"))
            return False
        pad = self.server.pad
        self.send(
            {
                "v": PROTOCOL_VERSION,
                "type": "hello",
                "status": "ok",
                "server": SERVER_NAME,
                "session_id": self.session_id,
                "role": self.role,
                "boot_id": boot_id(),
                "daemon_start_mono_ns": pad.started_mono_ns,
                "pi_mono_ns": time.monotonic_ns(),
                "limits": {
                    "hold_ms": [HOLD_MS_MIN, HOLD_MS_MAX],
                    "idle_neutral_ms": int(self.server.idle_neutral_s * 1000),
                    "id_cache": ID_CACHE_SIZE,
                },
                "buttons": list(V1_BUTTONS),
                "ops": list(OPS),
            }
        )
        self.server.log(
            "session_open",
            session_id=self.session_id,
            role=self.role,
            peer=str(self.addr),
            client=msg.get("client"),
            takeover=bool(msg.get("takeover")),
        )
        return True

    # --- commands -----------------------------------------------------------

    def _on_line(self, raw: bytes) -> None:
        recv_ns = time.monotonic_ns()
        try:
            msg = json.loads(raw)
        except ValueError:
            self.send(_error("bad_request", "not JSON"))
            return
        if not isinstance(msg, dict):
            self.send(_error("bad_request", "message must be an object"))
            return
        cmd_id = msg.get("id")
        if not isinstance(cmd_id, str) or not 0 < len(cmd_id) <= MAX_ID_LEN:
            self.send(_error("bad_request", "id must be a string of 1..%d chars" % MAX_ID_LEN))
            return
        if msg.get("v") != PROTOCOL_VERSION:
            self.send(_result(cmd_id, msg.get("op"), "rejected", recv_ns, error=("bad_version", "server speaks v%d" % PROTOCOL_VERSION)))
            return
        if msg.get("type") != "cmd":
            self.send(_result(cmd_id, msg.get("op"), "rejected", recv_ns, error=("bad_request", "type must be cmd")))
            return

        cache = self.server.ids
        prior = cache.get(cmd_id)
        if prior is not None:
            ack = dict(prior.ack, duplicate=True, dup_recv_mono_ns=recv_ns)
            self.send(ack)
            if prior.result is not None:
                self.send(dict(prior.result, duplicate=True))
            else:
                prior.waiters.append(self)
            self.server.log("duplicate", session_id=self.session_id, id=cmd_id, op=msg.get("op"), pending=prior.result is None)
            return

        ack: Json = {
            "v": PROTOCOL_VERSION,
            "type": "ack",
            "id": cmd_id,
            "op": msg.get("op"),
            "seq": msg.get("seq"),
            "pi_recv_mono_ns": recv_ns,
            "duplicate": False,
            "queue_depth": self._queue.qsize() + (1 if self.busy else 0),
        }
        if "mac_sent_mono_ns" in msg:
            ack["mac_sent_mono_ns"] = msg["mac_sent_mono_ns"]
        entry = _Entry(ack)
        entry.waiters.append(self)
        cache.put(cmd_id, entry)
        self.send(ack)

        op = msg.get("op")
        try:
            if self.role == "observe" and op not in OBSERVE_OPS:
                raise ProtocolError("observe_only", "observe sessions may only use %s" % ", ".join(OBSERVE_OPS))
            args = validate(op, msg.get("args", {}))
            max_queue_ms = msg.get("max_queue_ms", DEFAULT_MAX_QUEUE_MS)
            if isinstance(max_queue_ms, bool) or not isinstance(max_queue_ms, (int, float)) or max_queue_ms < 0:
                raise ProtocolError("bad_args", "max_queue_ms must be a non-negative number")
        except ProtocolError as exc:
            self.server._finish(entry, _result(cmd_id, op, "rejected", recv_ns, error=(exc.code, exc.message)))
            return
        self._queue.put(({"id": cmd_id, "op": op, "args": args, "max_queue_ms": max_queue_ms}, recv_ns, entry))

    def _work(self) -> None:
        pad = self.server.pad
        while True:
            item = self._queue.get()
            if item is None:
                return
            cmd, recv_ns, entry = item
            self.busy = True
            try:
                start_ns = time.monotonic_ns()
                queued_ms = (start_ns - recv_ns) / 1e6
                if queued_ms > cmd["max_queue_ms"]:
                    res = _result(
                        cmd["id"], cmd["op"], "expired", recv_ns,
                        error=("expired", "queued %.1f ms > max_queue_ms %s" % (queued_ms, cmd["max_queue_ms"])),
                    )
                else:
                    with pad.command_lock, pad.ownership_gate.source(MutationSource.V1):
                        start_ns = time.monotonic_ns()
                        try:
                            outcome = execute(pad, cmd["op"], cmd["args"], self.server.report_wait)
                        except Exception as exc:  # report, never kill the session
                            outcome = {"status": "error", "error": {"code": "internal", "message": str(exc)}}
                        end_ns = time.monotonic_ns()
                    res = _result(cmd["id"], cmd["op"], outcome.pop("status"), recv_ns)
                    res.update(outcome)
                    res["pi_start_mono_ns"] = start_ns
                    res["pi_end_mono_ns"] = end_ns
                res["stream"] = "on" if pad.is_streaming() else "off"
                self.server._finish(entry, res)
            finally:
                self.busy = False


def _error(code: str, message: str) -> Json:
    return {"v": PROTOCOL_VERSION, "type": "error", "error": {"code": code, "message": message}}


def _hello_reject(code: str, message: str) -> Json:
    return {"v": PROTOCOL_VERSION, "type": "hello", "status": "rejected", "error": {"code": code, "message": message}}


def _result(cmd_id: str, op: Any, status: str, recv_ns: int, error: Optional[Tuple[str, str]] = None) -> Json:
    res: Json = {"v": PROTOCOL_VERSION, "type": "result", "id": cmd_id, "op": op, "status": status, "pi_recv_mono_ns": recv_ns}
    if error is not None:
        res["error"] = {"code": error[0], "message": error[1]}
    return res


class V1Server:
    """TCP listener. One control session at a time; any number of observers."""

    def __init__(
        self,
        pad: Any,
        host: str,
        port: int,
        token: str,
        idle_neutral_s: float = IDLE_NEUTRAL_S,
        report_wait: float = REPORT_WAIT_S,
        log: Callable[..., None] = log_json,
    ) -> None:
        self.log = log
        self.pad = pad
        self.host = host
        self.port = port
        self.token = token
        self.idle_neutral_s = idle_neutral_s
        self.report_wait = report_wait
        self.ids = IdCache()
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._control: Optional[_Session] = None
        self._sessions: List[_Session] = []
        self._threads: List[threading.Thread] = []

    @property
    def address(self) -> Tuple[str, int]:
        assert self._sock is not None
        return self._sock.getsockname()[:2]

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
            sock.listen(4)
            sock.settimeout(0.4)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._stop.clear()
        for target, name in ((self._accept_loop, "v1-accept"), (self._watchdog, "v1-watchdog")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self.log("listening", host=self.address[0], port=self.address[1], protocol=PROTOCOL_VERSION)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        with self._lock:
            sessions = list(self._sessions)
        for s in sessions:
            s.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            conn.settimeout(1.0)
            session = _Session(self, conn, addr)
            with self._lock:
                self._sessions.append(session)
            threading.Thread(target=session.run, name="v1-session", daemon=True).start()

    def _claim_control(self, session: _Session, takeover: bool) -> bool:
        if not self.pad.ownership_gate.allows_legacy_control():
            return False
        with self._lock:
            old = self._control
            if old is not None and old.alive and not takeover:
                return False
            self._control = session
        if old is not None and old.alive:
            self.log("takeover", old_session_id=old.session_id, new_session_id=session.session_id)
            old.close()
        return True

    def _session_closed(self, session: _Session) -> None:
        with self._lock:
            if session in self._sessions:
                self._sessions.remove(session)
            was_control = self._control is session
            if was_control:
                self._control = None
        if session.session_id is None:
            return
        if was_control:
            with self.pad.command_lock:
                active = has_active_input(self.pad)
                self.pad.neutral()
            self.log("session_closed_neutral", session_id=session.session_id, had_active_input=active)
        else:
            self.log("session_closed", session_id=session.session_id, role=session.role)

    def _finish(self, entry: _Entry, result: Json) -> None:
        entry.result = result
        for i, waiter in enumerate(entry.waiters):
            waiter.send(result if i == 0 else dict(result, duplicate=True))
        entry.waiters = []
        self.log(
            "command",
            id=result.get("id"),
            op=result.get("op"),
            status=result.get("status"),
            error=(result.get("error") or {}).get("code"),
            pi_recv_mono_ns=result.get("pi_recv_mono_ns"),
            pi_start_mono_ns=result.get("pi_start_mono_ns"),
            pi_end_mono_ns=result.get("pi_end_mono_ns"),
            first_report_seq=result.get("first_report_seq"),
            last_report_seq=result.get("last_report_seq"),
            stream=result.get("stream"),
            motion=result.get("motion"),
        )

    def _watchdog(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                s = self._control
            if s is None or s.busy or not s._queue.empty():
                continue
            idle_s = (time.monotonic_ns() - s.last_rx_ns) / 1e9
            if idle_s < self.idle_neutral_s:
                continue
            with self.pad.command_lock:
                if not has_active_input(self.pad):
                    continue
                self.pad.neutral()
            self.log("watchdog_neutral", session_id=s.session_id, idle_s=round(idle_s, 3))


# --- debug client (procon_send.py --v1, tests) -----------------------------


class V1Client:
    """Minimal synchronous client: one command in flight at a time."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        client: str = "procon_send",
        role: str = "control",
        takeover: bool = False,
        timeout: float = 5.0,
    ) -> None:
        self.host, self.port, self.token = host, port, token
        self.client, self.role, self.takeover, self.timeout = client, role, takeover, timeout
        self.session_id = uuid.uuid4().hex[:12]
        self.hello: Optional[Json] = None
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._seq = 0
        self.pending: Deque[Json] = collections.deque()

    def connect(self) -> Json:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self.send_raw(
            {
                "v": PROTOCOL_VERSION,
                "type": "hello",
                "token": self.token,
                "client": self.client,
                "session_id": self.session_id,
                "role": self.role,
                "takeover": self.takeover,
            }
        )
        self.hello = self.read()
        return self.hello

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "V1Client":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def send_raw(self, msg: Any) -> None:
        assert self._sock is not None
        data = msg if isinstance(msg, bytes) else (json.dumps(msg) + "\n").encode("utf-8")
        self._sock.sendall(data)

    def read(self) -> Json:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def next_id(self) -> Tuple[str, int]:
        self._seq += 1
        return "%s-%06d" % (self.session_id, self._seq), self._seq

    def make_cmd(self, op: str, args: Optional[Json] = None, cmd_id: Optional[str] = None, **extra: Any) -> Json:
        new_id, seq = self.next_id()
        msg: Json = {
            "v": PROTOCOL_VERSION,
            "type": "cmd",
            "id": cmd_id or new_id,
            "seq": seq,
            "op": op,
            "args": args or {},
            "mac_sent_mono_ns": time.monotonic_ns(),
        }
        msg.update(extra)
        return msg

    def command(self, op: str, args: Optional[Json] = None, **extra: Any) -> Tuple[Optional[Json], Json]:
        msg = self.make_cmd(op, args, **extra)
        self.send_raw(msg)
        return self.wait_for(msg["id"])

    def wait_for(self, cmd_id: str) -> Tuple[Optional[Json], Json]:
        ack: Optional[Json] = None
        while True:
            reply = self.read()
            if reply.get("id") != cmd_id:
                self.pending.append(reply)
                continue
            if reply.get("type") == "ack":
                ack = reply
            elif reply.get("type") == "result":
                return ack, reply


def words_to_cmd(words: List[str]) -> Tuple[str, Json]:
    """Debug CLI words -> (op, args): 'tap a 80', 'press a b', 'stick l 0 1', 'dpad clear'."""
    if not words:
        raise ValueError("empty command")
    op, rest = words[0].lower(), words[1:]
    if op in ("ping", "state", "neutral"):
        return op, {}
    if op in ("press", "release"):
        return op, {"buttons": rest}
    if op == "tap":
        if not rest:
            raise ValueError("tap BUTTON [hold_ms]")
        args: Json = {"button": rest[0]}
        if len(rest) > 1:
            args["hold_ms"] = int(rest[1])
        return op, args
    if op == "dpad":
        d = rest[0] if rest else "clear"
        return op, {"dir": None if d in ("clear", "none", "neutral") else d}
    if op == "stick":
        if len(rest) != 3:
            raise ValueError("stick l|r X Y   (floats -1..1)")
        return op, {"side": rest[0], "x": float(rest[1]), "y": float(rest[2])}
    if op == "rotate_pitch":
        if not 1 <= len(rest) <= 2:
            raise ValueError("rotate_pitch DELTA_DEG [duration_ms]")
        return op, {"delta_deg": float(rest[0]), "duration_ms": int(rest[1]) if len(rest) > 1 else 500}
    raise ValueError("unknown command %r" % op)
