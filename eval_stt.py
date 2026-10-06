"""Eval harness: YouTube audio -> real Deepgram streaming -> WER + latency.

Usage: python _eval_stt.py <video_id_or_url> [--model flux-general-multi]
                              [--seconds 120] [--diarize]
Needs DEEPGRAM_API_KEY in env. Downloads audio + captions via yt-dlp.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
import soundfile as sf

import transcriber


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def download(url, out_dir, seconds):
    wav = os.path.join(out_dir, "audio.wav")
    vtt = os.path.join(out_dir, "subs.vtt")
    if "youtube" not in url and "youtu.be" not in url:
        # MP3/directo: descargar y convertir con ffmpeg
        raw = os.path.join(out_dir, "raw_audio")
        r = sh(["curl", "-sL", "-o", raw, url])
        r2 = sh(["ffmpeg", "-y", "-i", raw, "-ar", "48000", "-ac", "1",
                 "-f", "wav", wav])
        if not os.path.exists(wav):
            raise RuntimeError(f"ffmpeg falló: {r2.stderr[-300:]}")
        audio, rate = sf.read(wav, dtype="float32")
        if seconds:
            audio = audio[: int(seconds * rate)]
        return audio, rate, None
    cmd = [sys.executable, "-m", "yt_dlp", "-x", "--audio-format", "wav",
           "--audio-quality", "0", "-o", wav,
           "--write-auto-subs", "--write-subs",
           "--sub-langs", "en.*|es.*|-live_chat", "--sub-format", "vtt",
           "--convert-subs", "vtt", "-o", os.path.join(out_dir, "video"),
           url]
    r = sh(cmd)
    if not os.path.exists(wav):
        # descargar solo audio si subs fallan
        r = sh([sys.executable, "-m", "yt_dlp", "-x", "--audio-format", "wav",
                "-o", wav, url])
    if not os.path.exists(wav):
        raise RuntimeError(f"yt-dlp falló: {r.stderr[:300]}")
    # captions land alongside 'video' name
    for f in os.listdir(out_dir):
        if f.endswith(".vtt"):
            os.replace(os.path.join(out_dir, f), vtt)
    audio, rate = sf.read(wav, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if seconds:
        audio = audio[: int(seconds * rate)]
    return audio, rate, vtt if os.path.exists(vtt) else None


def vtt_text(path):
    """Referencia: texto limpio de un .vtt (sin timestamps ni tags)."""
    words = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"&\w+;", " ", line)
        if line and (not words or words[-1] != line):
            words.append(line)
    return " ".join(words)


def norm(t):
    return re.sub(r"[^a-z0-9ñáéíóúü' ]", " ", t.lower())


def wer(ref, hyp):
    r, h = norm(ref).split(), norm(hyp).split()
    if not r:
        return float("nan")
    d = np.zeros((len(r) + 1, len(h) + 1), dtype=int)
    d[:, 0], d[0, :] = range(len(r) + 1), range(len(h) + 1)
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1,
                          d[i - 1, j - 1] + (r[i - 1] != h[j - 1]))
    return d[-1, -1] / len(r)


def stream_eval(audio, src_rate, model, diarize=False, live_pace=True):
    """Alimenta DeepgramRealtime como si fuera tiempo real; mide latencias."""
    events = {"finals": [], "partials": [], "errors": [], "t_partial": None,
              "t_final": None, "speakers": set()}
    t0 = [None]

    def on_tx(text, final):
        t = time.monotonic() - t0[0]
        (events["finals"] if final else events["partials"]).append((t, text))
        if final and events["t_final"] is None:
            events["t_final"] = t
        elif not final and events["t_partial"] is None:
            events["t_partial"] = t

    rt = transcriber.DeepgramRealtime(
        os.environ["DEEPGRAM_API_KEY"], model=model,
        on_transcript=on_tx,
        on_error=lambda e: events["errors"].append(str(e)[:200]))
    if diarize:
        rt._extra_params = {"diarize": "true"}
    # hack: parchear query con diarize si se pidió
    orig_start = rt.start
    if diarize:
        def patched():
            import websocket, urllib.parse
            q = {"model": rt.model, "encoding": "linear16",
                 "sample_rate": transcriber.DEEPGRAM_SAMPLE_RATE,
                 "channels": 1, "punctuate": "true",
                 "interim_results": "true", "endpointing": 300,
                 "diarize": "true"}
            url = transcriber.DEEPGRAM_URL + "?" + urllib.parse.urlencode(q)
            rt._ws = websocket.create_connection(
                url, header=[f"Authorization: Token {rt.api_key}"], timeout=30)
            rt._running = True
            rt._recv_thread = threading.Thread(target=rt._recv_loop, daemon=True)
            rt._recv_thread.start()
        orig_start = patched
    orig_start()

    audio16 = transcriber.vad.resample_linear(audio, src_rate, 16000) \
        if src_rate != 16000 else audio
    pcm = (np.clip(audio16, -1, 1) * 32767).astype(np.int16).tobytes()
    blk = int(16000 * 0.04 * 2)
    t0[0] = time.monotonic()
    for i in range(0, len(pcm), blk):
        rt.send_audio(pcm[i:i + blk])
        if live_pace:
            time.sleep(0.04)
    rt.commit()
    time.sleep(4)
    rt.stop()
    hyp = " ".join(t for _, t in events["finals"])
    return events, hyp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--model", default="flux-general-multi")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--diarize", action="store_true")
    a = ap.parse_args()

    with tempfile.TemporaryDirectory() as d:
        print(f"[1/3] descargando {a.url} ...")
        audio, rate, vtt = download(a.url, d, a.seconds)
        print(f"      audio {len(audio)/rate:.0f}s @ {rate}Hz; subs: {bool(vtt)}")
        print(f"[2/3] streaming -> {a.model} ...")
        ev, hyp = stream_eval(audio, rate, a.model, a.diarize)
        print("[3/3] resultados")
        print(f"      parciales: {len(ev['partials'])}  "
              f"(1º a {ev['t_partial'] or -1:.2f}s)")
        print(f"      finales:   {len(ev['finals'])}  "
              f"(1º a {ev['t_final'] or -1:.2f}s)")
        if ev["errors"]:
            print(f"      errores:   {ev['errors']}")
        if vtt:
            print(f"      WER:       {wer(vtt_text(vtt), hyp):.1%}")
        print("      transcripción:")
        for t, txt in ev["finals"][:8]:
            print(f"        {t:6.2f}s  {txt[:110]}")


if __name__ == "__main__":
    import vad
    transcriber.vad = vad  # para resample en stream_eval
    main()
