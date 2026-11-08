"""Disabled-by-default mTLS identity and rearm foundation; no control operations."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import uuid
from typing import Any

ROLES = frozenset({"executor", "stop", "human"})
MAX_LINE = 8192
MAX_CONNECTIONS = 8
MAX_HANDSHAKES = 4
MAX_NONSTOP = 2
MAX_STOP = 2
HANDSHAKE_TIMEOUT = 2.0
MESSAGE_TIMEOUT = 2.0
MAX_REQUESTS = 32


def parse_bind(value: str, allow_network: bool = False) -> tuple[str, int]:
    """Accept only an explicit IPv4 address and port; network exposure needs opt-in."""
    host, separator, port_text = value.rpartition(":")
    if not separator or not host or not port_text.isdecimal():
        raise ValueError("v2 bind requires an explicit IPv4 address and port")
    address = ipaddress.IPv4Address(host)
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("v2 port must be in 1..65535")
    if not address.is_loopback and not allow_network:
        raise ValueError("network binding requires --v2-allow-network")
    return str(address), port


def load_roles(path: str) -> dict[str, str]:
    """Read a pre-provisioned certificate fingerprint-to-role registry."""
    with open(path, encoding="utf-8") as handle:
        roles = json.load(handle)
    if not isinstance(roles, dict) or not roles:
        raise ValueError("role registry must be a non-empty object")
    for fingerprint, role in roles.items():
        if (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                or not isinstance(role, str) or role not in ROLES):
            raise ValueError("invalid fingerprint or role")
    return roles


def server_context(cert: str, key: str, ca: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert, key)
    context.load_verify_locations(cafile=ca)
    return context


class V2Server:
    """Independent per-connection authentication; never acquires controller ownership."""

    def __init__(self, pad: Any, host: str, port: int, context: ssl.SSLContext,
                 roles: dict[str, str]) -> None:
        self.pad, self.host, self.port = pad, host, port
        self.context, self.roles = context, dict(roles)
        self.boot_id = uuid.uuid4().hex  # daemon instance identity, not kernel boot ID
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._connections: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._handshakes = threading.BoundedSemaphore(MAX_HANDSHAKES)
        self._nonstop = threading.BoundedSemaphore(MAX_NONSTOP)
        self._stops = threading.BoundedSemaphore(MAX_STOP)

    @property
    def address(self) -> tuple[str, int]:
        assert self._socket is not None
        return self._socket.getsockname()[:2]

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(16)
            listener.settimeout(0.2)
        except Exception:
            listener.close()
            raise
        self._socket = listener
        threading.Thread(target=self._accept, daemon=True, name="v2-accept").start()

    def stop(self) -> None:
        self._stop.set()
        if self._socket:
            self._socket.close()
        with self._lock:
            connections = list(self._connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def _accept(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if len(self._connections) >= MAX_CONNECTIONS or not self._handshakes.acquire(False):
                    conn.close()
                    continue
                self._connections.add(conn)
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, raw: socket.socket) -> None:
        conn: socket.socket = raw
        role_slot = None
        handshake_slot = True
        try:
            raw.settimeout(HANDSHAKE_TIMEOUT)
            conn = self.context.wrap_socket(raw, server_side=True)
            with self._lock:
                self._connections.discard(raw)
                self._connections.add(conn)
            cert = conn.getpeercert(binary_form=True)
            role = self.roles.get(hashlib.sha256(cert).hexdigest()) if cert else None
            if role is None:
                return
            role_slot = self._stops if role == "stop" else self._nonstop
            if not role_slot.acquire(False):
                role_slot = None
                return
            self._handshakes.release()
            handshake_slot = False
            first = self._read_line(conn)
            if first:
                hello = json.loads(first)
                if (not isinstance(hello, dict) or hello.get("v") != 2 or
                        hello.get("type") != "hello" or hello.get("role") != role or
                        not isinstance(hello.get("session_id"), str) or
                        not 1 <= len(hello["session_id"]) <= 64):
                    return
                self._send(conn, {"v": 2, "type": "hello", "status": "ok",
                    "role": role, "session_id": hello["session_id"],
                    "daemon_instance_id": self.boot_id, "pi_mono_ns": time.monotonic_ns(),
                    "stop_latch": "requires_human_rearm" if self.pad.ownership_gate.state.requires_human_rearm else "clear",
                    "owner": self.pad.ownership_gate.owner.value,
                    "capabilities": ["mtls_identity_v1", "rearm_disabled_v1"],
                    "state": self.pad.state_dict()})
                for _ in range(MAX_REQUESTS):
                    if self._stop.is_set():
                        return
                    line = self._read_line(conn)
                    if not line:
                        return
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        return
                    request_id, operation = message.get("id"), message.get("type")
                    if message.get("v") != 2 or not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
                        return
                    code = "rearm_disabled" if role == "human" and operation == "rearm" else "role_forbidden"
                    if role == "stop" and operation == "estop":
                        code = "not_implemented"
                    self._send(conn, {"v": 2, "type": "result", "id": request_id,
                        "status": "rejected", "error": {"code": code}})
        except (OSError, ValueError, ssl.SSLError):
            pass
        finally:
            if handshake_slot:
                self._handshakes.release()
            if role_slot is not None:
                role_slot.release()
            with self._lock:
                self._connections.discard(raw)
                self._connections.discard(conn)
            conn.close()

    @staticmethod
    def _read_line(conn: socket.socket) -> bytes:
        deadline = time.monotonic() + MESSAGE_TIMEOUT
        data = bytearray()
        while len(data) <= MAX_LINE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            conn.settimeout(remaining)
            byte = conn.recv(1)
            if not byte:
                return b""
            data += byte
            if byte == b"\n":
                return bytes(data) if len(data) <= MAX_LINE else b""
        return b""

    @staticmethod
    def _send(conn: socket.socket, message: dict[str, Any]) -> None:
        conn.settimeout(MESSAGE_TIMEOUT)
        conn.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())
