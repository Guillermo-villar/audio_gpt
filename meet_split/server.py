"""meet_split server — un WebSocket por audio track remoto.

Cada conexion: primer mensaje JSON {trackId, streamId, pcId, rate, page};
despues frames binarios PCM16 mono 16k. Por track: WAV en meet_split/out/
y (opcional --deepgram) transcripcion Deepgram en vivo por asistente.
"""
import asyncio
import io
import itertools
import json
import os
import sys
import time
import wave

import websockets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import transcriber  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
_track_idx = itertools.count(1)  # contador global: Meet-S1, Meet-S2…


class TrackPipe:
    def __init__(self, meta, idx, deepgram_key=None):
        self.meta = meta
        self.idx = idx
        self.label = f"Meet-S{idx}"
        self.bytes = 0
        self.t0 = time.time()
        os.makedirs(OUT_DIR, exist_ok=True)
        self.wav = wave.open(
            os.path.join(OUT_DIR, f"track_{idx}_{meta['trackId'][:8]}.wav"),
            "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(meta.get("rate", 16000))
        self.dg = None
        if deepgram_key:
            self.dg = transcriber.DeepgramRealtime(
                deepgram_key, model="nova-3", language="multi",
                on_transcript=self._on_text, diarize=False)
            try:
                self.dg.start()
            except Exception as e:
                print(f"[{self.label}] Deepgram error: {e}", flush=True)
                self.dg = None
        print(f"[meet-split] track {self.label} = {meta['trackId'][:12]} "
              f"stream={meta.get('streamId','')[:12]} pc={meta.get('pcId')}",
              flush=True)

    def _on_text(self, text, final):
        tag = "FINAL" if final else "int"
        print(f"[{self.label}] ({tag}) {text}", flush=True)

    def feed(self, pcm):
        self.bytes += len(pcm)
        self.wav.writeframesraw(pcm)
        if self.dg:
            self.dg.send_audio(pcm)

    def close(self):
        secs = time.time() - self.t0
        self.wav.close()
        if self.dg:
            try:
                self.dg.stop()
            except Exception:
                pass
        print(f"[meet-split] {self.label} closed: "
              f"{self.bytes/2/16000:.1f}s audio in {secs:.0f}s", flush=True)


async def handle(ws):
    pipes = {}
    try:
        async for msg in ws:
            if isinstance(msg, str):
                meta = json.loads(msg)
                pipes["main"] = TrackPipe(
                    meta, next(_track_idx),
                    deepgram_key=os.environ.get("DEEPGRAM_API_KEY"))
            elif "main" in pipes:
                pipes["main"].feed(msg)
    finally:
        for p in pipes.values():
            p.close()


async def main():
    print("[meet-split] ws://127.0.0.1:8765 listening", flush=True)
    async with websockets.serve(handle, "127.0.0.1", 8765,
                                max_size=2**22):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
