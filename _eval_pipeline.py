"""E2E pipeline eval: audio real -> Deepgram live -> gate -> draft/review LLM.

Simula exactamente la logica de gui.py (_append_transcript + _fire_gpt)
sin PySide6. Uso:
    python _eval_pipeline.py <url_mp3> [--model flux-general-multi] [--seconds 90]
Env: DEEPGRAM_API_KEY, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID
"""
import argparse
import os
import threading
import time
from collections import deque

import numpy as np

import eval_stt
import transcriber
from api_client import GptClient, looks_like_question

send_to_gpt = GptClient.send_to_gpt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--model", default="flux-general-multi")
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--engine", default="cloudflare")
    a = ap.parse_args()

    # --- mismos objetos de estado que gui.py ---
    ctx = deque(maxlen=16)          # _ctx
    draft_state = {}                # _draft_state
    results = []                    # (kind, latency_s, text)
    t0 = time.monotonic()

    def fire(text, kind, effort=None, max_tokens=None):
        t_fire = time.monotonic() - t0
        context = "\n".join(list(ctx)[-8:])
        print(f"  [{t_fire:6.2f}s] >>> {kind} disparado: \"{text[:80]}\"")

        def run():
            t_start = time.monotonic()
            try:
                ans = send_to_gpt(None, text, engine=a.engine,
                                  context=context, effort=effort,
                                  max_tokens=max_tokens)
                lat = time.monotonic() - t_start
                results.append((kind, lat, ans))
                print(f"  [{time.monotonic()-t0:6.2f}s] <<< {kind} "
                      f"({lat:.1f}s): {ans[:150]}")
            except Exception as e:
                print(f"  [{time.monotonic()-t0:6.2f}s] !!! {kind} ERROR: {e}")
        threading.Thread(target=run, daemon=True).start()

    def on_tx(text, is_final):
        t = time.monotonic() - t0
        if is_final:
            draft_state.pop("Entrevistador", None)
            print(f"[{t:6.2f}s] FINAL: {text[:100]}")
            ctx.append(f"Entrevistador: {text}")
            if looks_like_question(text):
                fire(text, "REVISION", effort="medium")
            return
        # interim: dedupe + gate (igual que gui.py)
        prev = draft_state.get("Entrevistador")
        if prev is not None and prev in text:
            return
        if looks_like_question(text):
            draft_state["Entrevistador"] = text
            print(f"[{t:6.2f}s] interim->borrador (gate ok): {text[:80]}")
            fire(text, "BORRADOR", max_tokens=200)

    os.makedirs("_test_audio", exist_ok=True)
    wav = "_test_audio/pipeline.wav"
    if not os.path.exists(wav):
        raw = "_test_audio/raw.mp3"
        eval_stt.sh(["curl", "-sL", "-o", raw, a.url])
        eval_stt.sh(["ffmpeg", "-y", "-i", raw, "-ar", "16000", "-ac", "1",
                     "-f", "wav", wav])
    import soundfile as sf
    audio, rate = sf.read(wav, dtype="float32")
    audio = audio[: a.seconds * rate]
    print(f"audio: {len(audio)/rate:.0f}s @ {rate}Hz")

    rt = transcriber.DeepgramRealtime(
        os.environ["DEEPGRAM_API_KEY"], model=a.model, on_transcript=on_tx,
        on_error=lambda e: print(f"!!! STT: {e}"))
    rt.start()

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    blk = int(16000 * 0.04 * 2)
    for i in range(0, len(pcm), blk):
        rt.send_audio(pcm[i:i + blk])
        time.sleep(0.04)
    rt.commit()
    print("audio enviado; esperando respuestas pendientes...")
    time.sleep(12)
    rt.stop()

    print("\n=== RESUMEN ===")
    print(f"llamadas LLM: {len(results)}")
    for kind, lat, ans in results:
        print(f"  {kind}: {lat:.1f}s | {ans[:120]}")


if __name__ == "__main__":
    main()
