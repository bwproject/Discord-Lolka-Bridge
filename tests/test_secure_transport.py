from __future__ import annotations

import socket
import threading
import unittest

from processor.secure_transport import SecureSocket


class SecureTransportTests(unittest.TestCase):
    def test_mutual_authentication_and_encrypted_stream(self) -> None:
        secret = "a" * 64
        client_raw, server_raw = socket.socketpair()
        errors: list[BaseException] = []

        def server_worker() -> None:
            try:
                server = SecureSocket.server(server_raw, secret)
                self.assertEqual(server.recv(5), b"hello")
                server.sendall(b"voice")
                server.sendall(b"bridge")
                server.close()
            except BaseException as exc:
                errors.append(exc)
                server_raw.close()

        worker = threading.Thread(target=server_worker)
        worker.start()
        client = SecureSocket.client(client_raw, secret)
        client.sendall(b"hello")
        self.assertEqual(client.recv(3), b"voi")
        self.assertEqual(client.recv(7), b"ce")
        self.assertEqual(client.recv(6), b"bridge")
        client.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_wrong_secret_is_rejected(self) -> None:
        good_secret = "b" * 64
        bad_secret = "c" * 64
        client_raw, server_raw = socket.socketpair()
        errors: list[BaseException] = []

        def server_worker() -> None:
            try:
                SecureSocket.server(server_raw, good_secret)
            except BaseException as exc:
                errors.append(exc)
            finally:
                server_raw.close()

        worker = threading.Thread(target=server_worker)
        worker.start()
        with self.assertRaises((PermissionError, ConnectionError, OSError)):
            SecureSocket.client(client_raw, bad_secret)
        client_raw.close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
