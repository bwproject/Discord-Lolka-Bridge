from __future__ import annotations

import logging
import os
import signal
import socket
import struct
import threading
import time
from collections import defaultdict, deque

import numpy as np

try:
    from .secure_transport import SecureSocket
except ImportError:  # Running server.py directly from the Pterodactyl server root.
    from secure_transport import SecureSocket

LOG = logging.getLogger("voice-processor")
SAMPLE_RATE = 48_000
CHANNELS = 2
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = SAMPLES_PER_FRAME * CHANNELS * 2
HEADER = struct.Struct("!BBHI")
PACKET_AUDIO = 1
PACKET_MIXED = 2
MAX_SPEAKERS = 6
IDLE_SECONDS = 30
MAX_QUEUE_FRAMES = 4


def recv_exact(sock: SecureSocket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise ConnectionError("peer disconnected")
        data.extend(part)
    return bytes(data)


class Mixer:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.queues: dict[str, deque[bytes]] = {}
        self.last_seen: dict[str, float] = {}
        self.overflows = 0

    def push(self, speaker: str, pcm: bytes) -> None:
        if len(pcm) != FRAME_BYTES:
            return
        now = time.monotonic()
        with self.lock:
            stale = [key for key, seen in self.last_seen.items() if now - seen > IDLE_SECONDS]
            for key in stale:
                self.queues.pop(key, None)
                self.last_seen.pop(key, None)
            if speaker not in self.queues:
                if len(self.queues) >= MAX_SPEAKERS:
                    oldest = min(self.last_seen, key=self.last_seen.get)
                    self.queues.pop(oldest, None)
                    self.last_seen.pop(oldest, None)
                self.queues[speaker] = deque()
            self.last_seen[speaker] = now
            target = self.queues[speaker]
            if len(target) >= MAX_QUEUE_FRAMES:
                target.popleft()
                self.overflows += 1
            target.append(pcm)

    def mix(self) -> bytes:
        with self.lock:
            sources = list(self.queues.items())
            frames = []
            for speaker, pending in sources:
                if pending:
                    frames.append(pending.popleft())
        if not frames:
            return bytes(FRAME_BYTES)
        mixed = np.zeros(SAMPLES_PER_FRAME * CHANNELS, dtype=np.int32)
        for frame in frames:
            np.add(mixed, np.frombuffer(frame, dtype="<i2"), out=mixed, casting="unsafe")
        values = mixed.astype(np.float32)
        magnitude = np.abs(values)
        knee = 32767.0 * 0.75
        headroom = 32767.0 - knee
        over = magnitude > knee
        values[over] = np.sign(values[over]) * (
            knee + headroom * np.tanh((magnitude[over] - knee) / headroom)
        )
        return np.clip(values, -32768, 32767).astype("<i2").tobytes()


class ClientSession:
    def __init__(self, sock: SecureSocket, address: tuple) -> None:
        self.sock = sock
        self.address = address
        self.mixers = (Mixer(), Mixer())
        self.send_lock = threading.Lock()
        self.closed = threading.Event()

    def receive(self) -> None:
        while not self.closed.is_set():
            header = recv_exact(self.sock, HEADER.size)
            packet_type, bus, speaker_len, payload_len = HEADER.unpack(header)
            if packet_type != PACKET_AUDIO or bus not in (0, 1):
                raise ValueError("invalid client packet")
            if not 1 <= speaker_len <= 128 or payload_len != FRAME_BYTES:
                raise ValueError("invalid audio packet dimensions")
            speaker = recv_exact(self.sock, speaker_len).decode("utf-8", errors="replace")
            pcm = recv_exact(self.sock, payload_len)
            self.mixers[bus].push(speaker, pcm)

    def send_frames(self) -> None:
        next_tick = time.monotonic()
        while not self.closed.is_set():
            next_tick += FRAME_MS / 1000
            for bus, mixer in enumerate(self.mixers):
                frame = mixer.mix()
                packet = HEADER.pack(PACKET_MIXED, bus, 0, len(frame)) + frame
                with self.send_lock:
                    self.sock.sendall(packet)
            time.sleep(max(0, next_tick - time.monotonic()))

    def run(self) -> None:
        sender = threading.Thread(target=self.send_frames, name="processor-mixer", daemon=True)
        sender.start()
        try:
            self.receive()
        finally:
            self.closed.set()
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()


class VoiceProcessorServer:
    def __init__(self) -> None:
        # Pterodactyl exposes the allocation; bind on all container interfaces.
        self.host = os.getenv("PROCESSOR_IP", "0.0.0.0")
        self.port = int(os.getenv("PROCESSOR_PORT", "9443"))
        self.secret = os.environ["PROCESSOR_SECRET"]
        if len(self.secret.encode("utf-8")) < 32:
            raise RuntimeError("PROCESSOR_SECRET must contain at least 32 bytes")
        self.stop_event = threading.Event()
        self.server: socket.socket | None = None

    def handle(self, raw: socket.socket, address: tuple) -> None:
        conn: SecureSocket | None = None
        try:
            raw.settimeout(10)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            conn = SecureSocket.server(raw, self.secret)
            conn.settimeout(None)
            LOG.info("Authenticated encrypted client connected: %s", address[0])
            ClientSession(conn, address).run()
        except Exception as exc:
            LOG.warning("Client %s disconnected: %s", address[0], exc)
        finally:
            try:
                raw.close()
            except OSError:
                pass
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def serve(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            self.server = server
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            server.listen(16)
            server.settimeout(1)
            LOG.info("Encrypted voice processor listening on %s:%s", self.host, self.port)
            while not self.stop_event.is_set():
                try:
                    raw, address = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self.handle, args=(raw, address), daemon=True,
                    name=f"processor-client-{address[0]}",
                ).start()

    def stop(self, *_: object) -> None:
        self.stop_event.set()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    app = VoiceProcessorServer()
    signal.signal(signal.SIGTERM, app.stop)
    signal.signal(signal.SIGINT, app.stop)
    app.serve()
