"""Búfer circular de PCM16 mono para re-transcribir turnos (sin Qt)."""

import io
import math
import threading
import wave


class PcmRing:
    def __init__(self, sample_rate, max_seconds=600):
        self.sample_rate = sample_rate
        self._max_bytes = int(max_seconds * sample_rate) * 2
        self._buf = bytearray()
        self._evicted = 0
        self._lock = threading.Lock()

    def append(self, pcm16):
        with self._lock:
            self._buf.extend(pcm16)
            if len(self._buf) > self._max_bytes * 1.25:
                drop = (len(self._buf) - self._max_bytes) // 2 * 2
                del self._buf[:drop]
                self._evicted += drop // 2

    @property
    def total_samples(self):
        with self._lock:
            return self._evicted + len(self._buf) // 2

    def slice(self, start_s, end_s, pad_s=0.25):
        sr = self.sample_rate
        with self._lock:
            total = self._evicted + len(self._buf) // 2
            first = max(0, math.floor((start_s - pad_s) * sr))
            last = min(total, math.ceil((end_s + pad_s) * sr))
            if last <= first or first >= total or first < self._evicted:
                return None
            lo = (first - self._evicted) * 2
            hi = (last - self._evicted) * 2
            return bytes(self._buf[lo:hi])


def wav_bytes(pcm16, sample_rate):
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return out.getvalue()
