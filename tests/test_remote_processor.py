from __future__ import annotations

import unittest

import numpy as np

from processor.server import FRAME_BYTES, MAX_SPEAKERS, Mixer, SAMPLES_PER_FRAME


class RemoteProcessorMixerTests(unittest.TestCase):
    def test_mixes_multiple_speakers_and_limits_peak(self) -> None:
        mixer = Mixer()
        frame = np.full(SAMPLES_PER_FRAME * 2, 24_000, dtype="<i2").tobytes()
        mixer.push("speaker-a", frame)
        mixer.push("speaker-b", frame)

        mixed = np.frombuffer(mixer.mix(), dtype="<i2")
        self.assertEqual(mixed.size, SAMPLES_PER_FRAME * 2)
        self.assertTrue(np.all(mixed > 24_000))
        self.assertTrue(np.all(mixed < 32_767))

    def test_silence_frame_when_no_audio_is_queued(self) -> None:
        self.assertEqual(Mixer().mix(), bytes(FRAME_BYTES))

    def test_active_speakers_are_bounded(self) -> None:
        mixer = Mixer()
        frame = bytes(FRAME_BYTES)
        for index in range(MAX_SPEAKERS + 3):
            mixer.push(f"speaker-{index}", frame)
        self.assertLessEqual(len(mixer.queues), MAX_SPEAKERS)


if __name__ == "__main__":
    unittest.main()
