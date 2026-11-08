"""Local mTLS tests with disposable credentials and an in-memory pad."""
from __future__ import annotations

import hashlib
import json
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from procon import ProController
from protocol_v2 import (MAX_HANDSHAKES, MAX_NONSTOP, MAX_REQUESTS, V2Server,
                         load_roles, parse_bind, server_context)
from safety import Owner, SafetyState


class ProtocolV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name)
        def openssl(*args: str) -> None:
            subprocess.run(["openssl", *args], cwd=cls.path, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=test-ca", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout", "ca.key", "-out", "ca.crt")
        for name in ("server", "human", "stop", "executor", "unknown"):
            openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=" + name,
                    "-keyout", name + ".key", "-out", name + ".csr")
            openssl("x509", "-req", "-in", name + ".csr", "-CA", "ca.crt", "-CAkey",
                    "ca.key", "-CAcreateserial", "-days", "1", "-out", name + ".crt")
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=wrong-ca", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout", "wrong.key", "-out", "wrong.crt")
        openssl("x509", "-req", "-in", "unknown.csr", "-CA", "ca.crt", "-CAkey",
                "ca.key", "-CAcreateserial", "-days", "0", "-out", "expired.crt")
        cls.roles = {}
        for name in ("human", "stop", "executor"):
            der = ssl.PEM_cert_to_DER_cert((cls.path / (name + ".crt")).read_text())
            cls.roles[hashlib.sha256(der).hexdigest()] = name

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.pad = ProController(reset_udc=False, verbose=False)
        self.server = V2Server(self.pad, "127.0.0.1", 0,
            server_context(str(self.path / "server.crt"), str(self.path / "server.key"),
                           str(self.path / "ca.crt")), self.roles)
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def connect(self, name: str, role: str):
        context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(self.path / (name + ".crt"), self.path / (name + ".key"))
        conn = context.wrap_socket(socket.create_connection(self.server.address), server_hostname="server")
        stream = conn.makefile("rwb")
        stream.write((json.dumps({"v": 2, "type": "hello", "role": role,
                                  "session_id": name}) + "\n").encode())
        stream.flush()
        return conn, stream

    def test_bind_requires_explicit_address_and_network_opt_in(self) -> None:
        self.assertEqual(parse_bind("127.0.0.1:47801"), ("127.0.0.1", 47801))
        for bind in (":47801", "0.0.0.0:47801", "localhost:47801", "127.0.0.1"):
            with self.subTest(bind=bind), self.assertRaises(ValueError):
                parse_bind(bind)
        self.assertEqual(parse_bind("0.0.0.0:47801", True), ("0.0.0.0", 47801))

    def test_server_identity_and_wrong_ca_are_rejected(self) -> None:
        for ca, hostname in (("ca.crt", "wrong-host"), ("wrong.crt", "server")):
            context = ssl.create_default_context(cafile=str(self.path / ca))
            context.load_cert_chain(self.path / "human.crt", self.path / "human.key")
            with self.subTest(ca=ca, hostname=hostname), self.assertRaises(ssl.SSLCertVerificationError):
                with socket.create_connection(self.server.address) as raw:
                    context.wrap_socket(raw, server_hostname=hostname)

    def test_client_certificate_is_required(self) -> None:
        context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
        with socket.create_connection(self.server.address) as raw:
            with context.wrap_socket(raw, server_hostname="server") as conn:
                with self.assertRaises((ssl.SSLError, ConnectionError, OSError)):
                    conn.sendall(b'{"v":2}\n')
                    conn.recv(1024)

    def test_every_role_rejects_rearm_and_controller_commands(self) -> None:
        before = self.pad.state_dict()
        latch = self.pad.ownership_gate.state
        for role in ("human", "stop", "executor"):
            conn, stream = self.connect(role, role)
            try:
                self.assertEqual(json.loads(stream.readline())["status"], "ok")
                for operation in ("rearm", "arm", "cmd", "lease_renew", "estop"):
                    request = {"v": 2, "type": operation, "id": operation,
                               "op": "press", "args": {"buttons": ["a"]}}
                    stream.write((json.dumps(request) + "\n").encode()); stream.flush()
                    result = json.loads(stream.readline())
                    self.assertEqual(result["status"], "rejected")
                    expected = ("rearm_disabled" if role == "human" and operation == "rearm"
                                else "not_implemented" if role == "stop" and operation == "estop"
                                else "role_forbidden")
                    self.assertEqual(result["error"]["code"], expected)
            finally:
                stream.close(); conn.close()
        self.assertEqual(self.pad.state_dict(), before)
        self.assertEqual(self.pad.ownership_gate.state, latch)

    def test_request_count_is_bounded(self) -> None:
        conn, stream = self.connect("human", "human")
        try:
            self.assertEqual(json.loads(stream.readline())["status"], "ok")
            for index in range(MAX_REQUESTS):
                request = {"v": 2, "type": "rearm", "id": str(index)}
                stream.write((json.dumps(request) + "\n").encode()); stream.flush()
                self.assertEqual(json.loads(stream.readline())["error"]["code"], "rearm_disabled")
            self.assertEqual(stream.readline(), b"")
        finally:
            stream.close(); conn.close()

    def test_expired_client_and_removed_fingerprint_rejected(self) -> None:
        time.sleep(1.1)  # -days 0 test certificate expires at issuance.
        der = ssl.PEM_cert_to_DER_cert((self.path / "expired.crt").read_text())
        self.server.roles[hashlib.sha256(der).hexdigest()] = "human"
        context = ssl.create_default_context(cafile=str(self.path / "ca.crt"))
        context.load_cert_chain(self.path / "expired.crt", self.path / "unknown.key")
        with socket.create_connection(self.server.address) as raw:
            with context.wrap_socket(raw, server_hostname="server") as conn:
                with self.assertRaises((ssl.SSLError, ConnectionError, OSError)):
                    conn.sendall(b'{"v":2}\n')
                    conn.recv(1024)
        fingerprint = next(key for key, role in self.server.roles.items() if role == "human" and key != hashlib.sha256(der).hexdigest())
        del self.server.roles[fingerprint]
        conn, stream = self.connect("human", "human")
        try:
            try:
                self.assertEqual(stream.readline(), b"")
            except ConnectionResetError:
                pass
        finally:
            stream.close(); conn.close()

    def test_stalled_executors_do_not_consume_stop_slots(self) -> None:
        clients = [self.connect("executor", "executor") for _ in range(MAX_NONSTOP)]
        try:
            for _, stream in clients:
                self.assertEqual(json.loads(stream.readline())["status"], "ok")
            stop, stream = self.connect("stop", "stop")
            try:
                self.assertEqual(json.loads(stream.readline())["role"], "stop")
            finally:
                stream.close(); stop.close()
        finally:
            for conn, stream in clients:
                stream.close(); conn.close()

    def test_handshake_slots_and_deadline_are_bounded(self) -> None:
        stalled = [socket.create_connection(self.server.address) for _ in range(MAX_HANDSHAKES)]
        try:
            time.sleep(0.2)
            self.assertLessEqual(len(self.server._connections), MAX_HANDSHAKES)
            time.sleep(2.2)
            self.assertEqual(len(self.server._connections), 0)
            conn, stream = self.connect("stop", "stop")
            self.assertEqual(json.loads(stream.readline())["role"], "stop")
            stream.close(); conn.close()
        finally:
            for conn in stalled:
                conn.close()

    def test_malformed_messages_close_connection_without_input(self) -> None:
        before = self.pad.state_dict()
        for message in (b'not-json\n', b'{"v":2,"type":"cmd"}\n', b'x' * 8193 + b'\n'):
            conn, stream = self.connect("executor", "executor")
            try:
                self.assertEqual(json.loads(stream.readline())["status"], "ok")
                try:
                    stream.write(message); stream.flush()
                    self.assertEqual(stream.readline(), b"")
                except (ConnectionResetError, BrokenPipeError):
                    pass
            finally:
                stream.close(); conn.close()
        self.assertEqual(self.pad.state_dict(), before)

    def test_rearm_always_rejected_and_latch_unchanged(self) -> None:
        self.pad.ownership_gate._state = SafetyState(9, True, "stop")
        conn, stream = self.connect("human", "human")
        try:
            self.assertEqual(json.loads(stream.readline())["stop_latch"], "requires_human_rearm")
            stream.write(b'{"v":2,"type":"rearm","id":"r1"}\n')
            stream.flush()
            self.assertEqual(json.loads(stream.readline())["error"]["code"], "rearm_disabled")
            self.assertEqual(self.pad.ownership_gate.state, SafetyState(9, True, "stop"))
        finally:
            stream.close(); conn.close()

    def test_stop_connects_while_executor_is_idle_and_owner_reserved(self) -> None:
        self.pad.ownership_gate._state = SafetyState(1, False, "test")
        self.pad.ownership_gate.reserve_autonomy()
        executor, executor_stream = self.connect("executor", "executor")
        self.assertEqual(json.loads(executor_stream.readline())["role"], "executor")
        stop, stop_stream = self.connect("stop", "stop")
        try:
            self.assertEqual(json.loads(stop_stream.readline())["role"], "stop")
            stop_stream.write(b'{"v":2,"type":"estop","id":"s1"}\n'); stop_stream.flush()
            self.assertEqual(json.loads(stop_stream.readline())["error"]["code"], "not_implemented")
            self.assertEqual(self.pad.ownership_gate.owner, Owner.AUTONOMOUS)
        finally:
            stop_stream.close(); stop.close(); executor_stream.close(); executor.close()

    def test_role_impersonation_and_unregistered_certificate_are_rejected(self) -> None:
        for name, role in (("executor", "human"), ("unknown", "stop")):
            conn, stream = self.connect(name, role)
            try:
                try:
                    self.assertEqual(stream.readline(), b"")
                except ConnectionResetError:
                    pass  # Immediate rejection may reset the TLS connection.
            finally:
                stream.close(); conn.close()

    def test_registry_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            path.write_text(json.dumps(self.roles))
            self.assertEqual(load_roles(str(path)), self.roles)
            path.write_text('{"bad":"human"}')
            with self.assertRaises(ValueError):
                load_roles(str(path))


if __name__ == "__main__":
    unittest.main()
