from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
from collections import defaultdict
from typing import Hashable

from .audio import FRAME_BYTES, PCMFrameMixer, SILENCE_FRAME
from processor.secure_transport import SecureSocket

log = logging.getLogger(__name__)

# Wire format: type(1), bus(1), speaker length(2), payload length(4), then bytes.
_HEADER = struct.Struct("!BBHI")
_PACKET_AUDIO = 1
_PACKET_MIXED = 2
_MAX_SPEAKER_BYTES = 128


class RemoteAudioClient:
    """PSK-authenticated encrypted transport shared by both directional buses."""

    def __init__(self, host: str, port: int, secret: str, connect_timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.connect_timeout = connect_timeout
        self._outgoing: queue.Queue[bytes] = queue.Queue(maxsize=256)
        self._incoming: dict[int, queue.Queue[bytes]] = {
            0: queue.Queue(maxsize=8),
            1: queue.Queue(maxsize=8),
        }
        self._socket: SecureSocket | None = None
        self._socket_lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread = threading.Thread(target=self._run, name="remote-audio-transport", daemon=True)
        self._pending: dict[tuple[int, str], bytearray] = defaultdict(bytearray)
        self._pending_lock = threading.Lock()
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def push(self, bus: int, speaker_id: Hashable, pcm: bytes) -> None:
        if bus not in (0, 1) or not pcm or self._stop.is_set():
            return
        speaker = str(speaker_id).encode("utf-8", errors="replace")[:_MAX_SPEAKER_BYTES]
        key = (bus, speaker.decode("utf-8", errors="replace"))
        with self._pending_lock:
            pending = self._pending[key]
            pending.extend(pcm)
            while len(pending) >= FRAME_BYTES:
                frame = bytes(pending[:FRAME_BYTES])
                del pending[:FRAME_BYTES]
                packet = _HEADER.pack(_PACKET_AUDIO, bus, len(speaker), len(frame)) + speaker + frame
                try:
                    self._outgoing.put_nowait(packet)
                except queue.Full:
                    try:
                        self._outgoing.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._outgoing.put_nowait(packet)
                    except queue.Full:
                        pass

    def read(self, bus: int) -> bytes | None:
        try:
            return self._incoming[bus].get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        self._stop.set()
        with self._socket_lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _set_socket(self, sock: SecureSocket | None) -> None:
        with self._socket_lock:
            self._socket = sock

    def _send_loop(self, sock: SecureSocket) -> None:
        while not self._stop.is_set():
            try:
                packet = self._outgoing.get(timeout=0.2)
            except queue.Empty:
                continue
            sock.sendall(packet)

    def _receive_loop(self, sock: SecureSocket) -> None:
        while not self._stop.is_set():
            header = self._recv_exact(sock, _HEADER.size)
            packet_type, bus, speaker_len, payload_len = _HEADER.unpack(header)
            if packet_type != _PACKET_MIXED or bus not in (0, 1):
                raise ValueError("Invalid audio processor packet")
            if speaker_len != 0 or payload_len != FRAME_BYTES:
                raise ValueError("Invalid mixed PCM frame size")
            frame = self._recv_exact(sock, payload_len)
            target = self._incoming[bus]
            try:
                target.put_nowait(frame)
            except queue.Full:
                try:
                    target.get_nowait()
                except queue.Empty:
                    pass
                try:
                    target.put_nowait(frame)
                except queue.Full:
                    pass

    @staticmethod
    def _recv_exact(sock: SecureSocket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = sock.recv(size - len(chunks))
            if not chunk:
                raise ConnectionError("Audio processor closed the connection")
            chunks.extend(chunk)
        return bytes(chunks)

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            sock: SecureSocket | None = None
            raw: socket.socket | None = None
            try:
                raw = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, "TCP_KEEPIDLE"):
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                sock = SecureSocket.client(raw, self.secret)
                sock.settimeout(None)
                while True:
                    try:
                        self._outgoing.get_nowait()
                    except queue.Empty:
                        break
                for pending in self._incoming.values():
                    while True:
                        try:
                            pending.get_nowait()
                        except queue.Empty:
                            break
                sock.settimeout(5.0)
                self._set_socket(sock)
                self._connected.set()
                delay = 1.0
                log.info("Remote audio processor connected: %s:%s (AES-256-GCM)", self.host, self.port)

                sender = threading.Thread(
                    target=self._send_loop, args=(sock,), name="remote-audio-sender", daemon=True
                )
                sender.start()
                self._receive_loop(sock)
            except Exception as exc:
                if not self._stop.is_set():
                    log.warning("Remote audio processor unavailable (%s); using local mixer", exc)
            finally:
                self._connected.clear()
                self._set_socket(None)
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                elif raw is not None:
                    try:
                        raw.close()
                    except OSError:
                        pass
            if not self._stop.wait(delay):
                delay = min(delay * 2, 15.0)


class RemotePCMFrameMixer:
    """Drop-in mixer facade: remote result when ready, local mixer as fallback."""

    def __init__(self, local: PCMFrameMixer, client: RemoteAudioClient, bus: int) -> None:
        self.local = local
        self.client = client
        self.bus = bus

    def push(self, speaker_id: Hashable, pcm: bytes) -> None:
        self.local.push(speaker_id, pcm)
        self.client.push(self.bus, speaker_id, pcm)

    def read_frame_with_status(self) -> tuple[bytes, bool, bool]:
        if self.client.connected:
            frame = self.client.read(self.bus)
            if frame is not None:
                return frame, True, frame != SILENCE_FRAME
        return self.local.read_frame_with_status()

    def read_frame(self) -> bytes:
        return self.read_frame_with_status()[0]

    def diagnostics(self) -> dict[str, int]:
        stats = self.local.diagnostics()
        stats["remote_connected"] = int(self.client.connected)
        return stats
