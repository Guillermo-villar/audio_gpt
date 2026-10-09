import os
os.environ.setdefault("AUDIO_GPT_NO_LOG", "1")
import io
import struct
import unittest
import wave

import audio_ring

SR = 1000


def samples(values):
    return struct.pack(f"<{len(values)}h", *values)


def unpack(data):
    return list(struct.unpack(f"<{len(data) // 2}h", data))


class PcmRingTests(unittest.TestCase):
    def test_alignment_across_appends(self):
        ring = audio_ring.PcmRing(SR)
        values = list(range(5000))
        pos = 0
        for size in (1, 7, 250, 1000, 333, 3409):
            chunk = values[pos:pos + size]
            ring.append(samples(chunk))
            pos += len(chunk)
        self.assertEqual(ring.total_samples, pos)
        got = unpack(ring.slice(1.0, 2.0, pad_s=0))
        self.assertEqual(got, values[1000:2000])

    def test_pad_and_clamp(self):
        ring = audio_ring.PcmRing(SR)
        ring.append(samples(list(range(3000))))
        self.assertEqual(unpack(ring.slice(1.0, 2.0)), list(range(750, 2250)))
        self.assertEqual(unpack(ring.slice(0.0, 0.5)), list(range(0, 750)))
        self.assertEqual(unpack(ring.slice(2.5, 9.0)), list(range(2250, 3000)))

    def test_empty_and_beyond(self):
        ring = audio_ring.PcmRing(SR)
        self.assertIsNone(ring.slice(0, 1))
        ring.append(samples(list(range(1000))))
        self.assertIsNone(ring.slice(5.0, 6.0))
        self.assertIsNone(ring.slice(1.0, 0.5, pad_s=0))

    def test_trim_and_eviction(self):
        ring = audio_ring.PcmRing(SR, max_seconds=2)
        for i in range(10):
            ring.append(samples(list(range(i * 1000, (i + 1) * 1000))))
        self.assertEqual(ring.total_samples, 10000)
        self.assertLessEqual(len(ring._buf) // 2, 2500)
        self.assertGreaterEqual(len(ring._buf) // 2, 2000)
        self.assertIsNone(ring.slice(0.0, 1.0))
        tail = unpack(ring.slice(9.0, 10.0, pad_s=0))
        self.assertEqual(tail, list(range(9000, 10000)))

    def test_start_exactly_at_eviction_boundary(self):
        ring = audio_ring.PcmRing(SR, max_seconds=2)
        for i in range(10):
            ring.append(samples([0] * 1000))
        evicted = ring._evicted
        self.assertIsNotNone(ring.slice(evicted / SR, evicted / SR + 0.5,
                                        pad_s=0))
        self.assertIsNone(ring.slice((evicted - 1) / SR,
                                     evicted / SR + 0.5, pad_s=0))

    def test_wav_roundtrip(self):
        pcm = samples(list(range(-50, 50)))
        data = audio_ring.wav_bytes(pcm, 16000)
        with wave.open(io.BytesIO(data)) as wf:
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getnframes(), 100)
            self.assertEqual(wf.readframes(100), pcm)


if __name__ == "__main__":
    unittest.main()
