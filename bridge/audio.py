from __future__ import annotations

import logging
import math
import queue
import struct
import threading
import time
from typing import Hashable

import discord
import lolka

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 2
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = SAMPLES_PER_FRAME * CHANNELS * 2

TONE_FREQUENCY = 440.0
TONE_AMPLITUDE = 6_000


def _tone_frame(phase: float) -> tuple[bytes, float]:
    samples: list[int] = []
    step = 2.0 * math.pi * TONE_FREQUENCY / SAMPLE_RATE
    for _ in range(SAMPLES_PER_FRAME):
        sample = int(TONE_AMPLITUDE * math.sin(phase))
        samples.append(sample)
        samples.append(sample)
        phase = (phase + step) % (2.0 * math.pi)
    return struct.pack("<" + "h" * len(samples), *samples), phase


class PCMFrameMixer:
    """Thread-safe per-speaker queues mixed into 20 ms stereo PCM frames."""

    def __init__(self, max_frames_per_speaker: int = 12) -> None:
        self._queues: dict[Hashable, queue.Queue[bytes]] = {}
        # Audio callbacks are not guaranteed to deliver exactly one 20 ms
        # frame per call. Keep partial PCM between calls instead of padding
        # every short packet with silence.
        self._pending: dict[Hashable, bytearray] = {}
        self._max_frames = max_frames_per_speaker
        self._lock = threading.Lock()

    def push(self, speaker_id: Hashable, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            target = self._queues.get(speaker_id)
            if target is None:
                target = queue.Queue(maxsize=self._max_frames)
                self._queues[speaker_id] = target
            pending = self._pending.setdefault(speaker_id, bytearray())
            pending.extend(pcm)

            # Only enqueue complete 20 ms frames. Padding each callback
            # independently would insert artificial silence whenever a
            # decoder/resampler returns a partial frame.
            while len(pending) >= FRAME_BYTES:
                frame = bytes(pending[:FRAME_BYTES])
                del pending[:FRAME_BYTES]
                try:
                    target.put_nowait(frame)
                except queue.Full:
                    try:
                        target.get_nowait()
                        target.put_nowait(frame)
                    except (queue.Empty, queue.Full):
                        pass

    def read_frame_with_status(self) -> tuple[bytes, bool, bool]:
        """Return PCM, whether any queued frame was consumed, and whether it contains non-zero samples."""
        with self._lock:
            queues = list(self._queues.values())
        mixed = [0] * (SAMPLES_PER_FRAME * CHANNELS)
        has_audio = False
        for source in queues:
            try:
                frame = source.get_nowait()
            except queue.Empty:
                continue
            has_audio = True
            samples = struct.unpack("<" + "h" * (len(frame) // 2), frame)
            for index, sample in enumerate(samples):
                mixed[index] += sample
        if not has_audio:
            return bytes(FRAME_BYTES), False, False
        mixed = [max(-32768, min(32767, sample)) for sample in mixed]
        has_signal = any(mixed)
        return struct.pack("<" + "h" * len(mixed), *mixed), True, has_signal

    def read_frame(self) -> bytes:
        # Keep the original public API for callers and tests.
        return self.read_frame_with_status()[0]


class _LoggedMixerSource:
    """Shared, rate-limited diagnostics for platform audio-player threads."""

    def _init_diagnostics(
        self, mixer: PCMFrameMixer, platform: str, diagnostic_tone: bool = False
    ) -> None:
        self._mixer = mixer
        self._platform = platform
        self._closed = False
        self._diagnostic_tone = diagnostic_tone
        self._tone_phase = 0.0
        self._read_calls = 0
        self._queued_frames = 0
        self._non_silent_frames = 0
        self._last_log_at = 0.0
        if diagnostic_tone:
            log.info(
                "%s: включён диагностический тон %.0f Гц (DIAGNOSTIC_TONE=true)",
                platform, TONE_FREQUENCY,
            )

    def _read_mixed_frame(self) -> bytes:
        if self._closed:
            return b""
        if self._diagnostic_tone:
            frame, self._tone_phase = _tone_frame(self._tone_phase)
            return frame
        frame, had_queued_audio, had_signal = self._mixer.read_frame_with_status()
        self._read_calls += 1
        self._queued_frames += int(had_queued_audio)
        self._non_silent_frames += int(had_signal)
        now = time.monotonic()
        if self._last_log_at == 0.0 or now - self._last_log_at >= 5.0:
            log.info(
                "%s: AudioSource.read() вызван; кадров прочитано=%s, "
                "кадров получено из очереди=%s, ненулевых PCM-кадров=%s",
                self._platform, self._read_calls, self._queued_frames,
                self._non_silent_frames,
            )
            self._last_log_at = now
            self._read_calls = 0
            self._queued_frames = 0
            self._non_silent_frames = 0
        return frame

    def cleanup(self) -> None:
        self._closed = True


class MixerAudioSource(_LoggedMixerSource, discord.AudioSource):
    """discord.py PCM source backed by a shared frame mixer."""

    def __init__(self, mixer: PCMFrameMixer, diagnostic_tone: bool = False) -> None:
        self._init_diagnostics(mixer, "Discord playback", diagnostic_tone)

    def read(self) -> bytes:
        return self._read_mixed_frame()

    def is_opus(self) -> bool:
        return False


class LolkaMixerAudioSource(_LoggedMixerSource, lolka.AudioSource):
    """lolka.py PCM source backed by a shared frame mixer."""

    def __init__(self, mixer: PCMFrameMixer, diagnostic_tone: bool = False) -> None:
        self._init_diagnostics(mixer, "Lolka playback", diagnostic_tone)

    def read(self) -> bytes:
        return self._read_mixed_frame()

    def is_opus(self) -> bool:
        return False
