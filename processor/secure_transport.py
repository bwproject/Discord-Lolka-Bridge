from __future__ import annotations

import hashlib
import hmac
import secrets
import socket
import struct
import threading
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

_HANDSHAKE_NONCE: Final = 32
_TAG_SIZE: Final = 16
_RECORD_HEADER: Final = struct.Struct("!I")
_MAX_RECORD: Final = 64 * 1024
_INFO: Final = b"BW-Voice-Bridge secure transport v1"


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("peer closed during secure handshake/record")
        data.extend(chunk)
    return bytes(data)


def _derive(secret: bytes, client_nonce: bytes, server_nonce: bytes) -> tuple[bytes, ...]:
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=72,
        salt=client_nonce + server_nonce,
        info=_INFO,
    ).derive(secret)
    return (
        material[0:32], material[32:64],
        material[64:68], material[68:72],
    )


class SecureSocket:
    """PSK-authenticated encrypted TCP stream (AES-256-GCM, no certificates)."""

    def __init__(
        self,
        sock: socket.socket,
        send_key: bytes,
        recv_key: bytes,
        send_prefix: bytes,
        recv_prefix: bytes,
    ) -> None:
        self._sock = sock
        self._send_cipher = AESGCM(send_key)
        self._recv_cipher = AESGCM(recv_key)
        self._send_prefix = send_prefix
        self._recv_prefix = recv_prefix
        self._send_counter = 0
        self._recv_counter = 0
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._plain = bytearray()
        self._closed = False

    @classmethod
    def client(cls, sock: socket.socket, secret: str) -> "SecureSocket":
        key = secret.encode("utf-8")
        if len(key) < 32:
            raise ValueError("PROCESSOR_SECRET must contain at least 32 bytes")
        client_nonce = secrets.token_bytes(_HANDSHAKE_NONCE)
        sock.sendall(client_nonce)
        challenge = _recv_exact(sock, _HANDSHAKE_NONCE * 2)
        server_nonce, server_proof = challenge[:32], challenge[32:]
        expected = hmac.new(
            key, b"server" + client_nonce + server_nonce, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(server_proof, expected):
            raise PermissionError("Voice processor server authentication failed")
        client_proof = hmac.new(
            key, b"client" + client_nonce + server_nonce, hashlib.sha256
        ).digest()
        sock.sendall(client_proof)
        if _recv_exact(sock, 2) != b"OK":
            raise PermissionError("Voice processor rejected client authentication")
        c2s_key, s2c_key, c2s_prefix, s2c_prefix = _derive(
            key, client_nonce, server_nonce
        )
        return cls(sock, c2s_key, s2c_key, c2s_prefix, s2c_prefix)

    @classmethod
    def server(cls, sock: socket.socket, secret: str) -> "SecureSocket":
        key = secret.encode("utf-8")
        if len(key) < 32:
            raise ValueError("PROCESSOR_SECRET must contain at least 32 bytes")
        client_nonce = _recv_exact(sock, _HANDSHAKE_NONCE)
        server_nonce = secrets.token_bytes(_HANDSHAKE_NONCE)
        server_proof = hmac.new(
            key, b"server" + client_nonce + server_nonce, hashlib.sha256
        ).digest()
        sock.sendall(server_nonce + server_proof)
        client_proof = _recv_exact(sock, 32)
        expected = hmac.new(
            key, b"client" + client_nonce + server_nonce, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(client_proof, expected):
            raise PermissionError("Voice processor client authentication failed")
        sock.sendall(b"OK")
        c2s_key, s2c_key, c2s_prefix, s2c_prefix = _derive(
            key, client_nonce, server_nonce
        )
        return cls(sock, s2c_key, c2s_key, s2c_prefix, c2s_prefix)

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise OSError("secure socket is closed")
        view = memoryview(data)
        with self._send_lock:
            while view:
                chunk = bytes(view[:_MAX_RECORD])
                nonce = self._send_prefix + self._send_counter.to_bytes(8, "big")
                encrypted = self._send_cipher.encrypt(nonce, chunk, None)
                self._sock.sendall(_RECORD_HEADER.pack(len(encrypted)) + encrypted)
                self._send_counter += 1
                view = view[len(chunk):]

    def recv(self, size: int) -> bytes:
        if size <= 0:
            return b""
        with self._recv_lock:
            while not self._plain:
                length = _RECORD_HEADER.unpack(_recv_exact(self._sock, 4))[0]
                if not _TAG_SIZE <= length <= _MAX_RECORD + _TAG_SIZE:
                    raise ValueError("Invalid encrypted audio record length")
                encrypted = _recv_exact(self._sock, length)
                nonce = self._recv_prefix + self._recv_counter.to_bytes(8, "big")
                self._plain.extend(self._recv_cipher.decrypt(nonce, encrypted, None))
                self._recv_counter += 1
            result = bytes(self._plain[:size])
            del self._plain[:size]
            return result

    def settimeout(self, timeout: float | None) -> None:
        self._sock.settimeout(timeout)

    def setsockopt(self, *args: object) -> None:
        self._sock.setsockopt(*args)

    def shutdown(self, how: int) -> None:
        self._sock.shutdown(how)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._sock.close()
