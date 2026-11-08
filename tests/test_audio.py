from __future__ import annotations

import unittest

import numpy as np

from bridge.audio import (
    FRAME_BYTES,
    SAMPLES_PER_FRAME,
    PCMFrameMixer,
    _soft_limit,
)


class SoftLimiterTests(unittest.TestCase):
    def test_signal_below_knee_is_unchanged(self) -> None:
        samples = np.array([0, 8_000, -16_000, 24_000], dtype=np.int32)
        actual = _soft_limit(samples)
        np.testing.assert_array_equal(actual, samples.astype("<i2"))

    def test_signal_above_knee_is_compressed_without_hard_clipping(self) -> None:
        samples = np.array([40_000, -40_000], dtype=np.int32)
        actual = _soft_limit(samples)
        self.assertLess(abs(int(actual[0])), 32_767)
        self.assertEqual(int(actual[0]), -int(actual[1]))

    def test_output_stays_in_int16_range(self) -> None:
        samples = np.array([200_000, -200_000], dtype=np.int32)
        actual = _soft_limit(samples)
        self.assertLessEqual(int(np.max(actual)), 32_767)
        self.assertGreaterEqual(int(np.min(actual)), -32_768)


class MixerTests(unittest.TestCase):
    def test_two_speakers_are_mixed_without_int16_overflow(self) -> None:
        mixer = PCMFrameMixer()
        frame = np.full(SAMPLES_PER_FRAME * 2, 24_000, dtype="<i2").tobytes()
        self.assertEqual(len(frame), FRAME_BYTES)
        mixer.push("one", frame)
        mixer.push("two", frame)

        mixed, had_audio, has_signal = mixer.read_frame_with_status()
        result = np.frombuffer(mixed, dtype="<i2")

        self.assertTrue(had_audio)
        self.assertTrue(has_signal)
        self.assertEqual(result.size, SAMPLES_PER_FRAME * 2)
        self.assertTrue(np.all(result > 24_000))
        self.assertTrue(np.all(result < 32_767))


if __name__ == "__main__":
    unittest.main()
