"""Utilidades de línea de comandos para diagnosticar la captura de audio.

Ejecutar:  python recorder.py
Muestra dispositivos, comprueba el loopback WASAPI y graba una prueba.
"""

import os
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

import capture
import vad


def list_audio_devices():
    devices = sd.query_devices()
    print("\n=== DISPOSITIVOS DE AUDIO ===")
    if capture.sys.platform == "darwin":
        print("Audio del sistema (ScreenCaptureKit):")
        try:
            import capture_mac
            print("  " + capture_mac.status_text())
        except Exception as e:
            print(f"  no disponible: {e}")
    else:
        print("Loopback WASAPI (audio del sistema):")
        loopbacks = capture.list_loopback_devices()
        if loopbacks:
            for name, _ in loopbacks:
                print(f"  {name}")
        else:
            print("  (ninguno detectado)")

    print("\nEntradas (grabación):")
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']} (canales: {d['max_input_channels']})")

    print("\nSalidas (reproducción):")
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0:
            print(f"  [{i}] {d['name']} (canales: {d['max_output_channels']})")


def verificar_audio(filename):
    """True si el archivo contiene voz (no solo silencio/ruido)."""
    try:
        data, sr = sf.read(filename)
        if np.max(np.abs(data)) < 0.01:
            print("ADVERTENCIA: el archivo parece contener solo silencio.")
            return False
        if vad.is_available() and not vad.has_voice(data, sr):
            print("ADVERTENCIA: hay audio pero no se detecta voz.")
            return False
        print("El archivo contiene audio con voz.")
        return True
    except Exception as e:
        print(f"Error al verificar el audio: {e}")
        return False


def record_system_audio(filename, duration=5, samplerate=48000):
    """Graba `duration` segundos del audio del sistema (loopback WASAPI en
    Windows, ScreenCaptureKit en macOS)."""
    try:
        rec = capture.make_loopback_recorder(samplerate=samplerate,
                                             block_ms=100)
    except RuntimeError as e:
        print(f"Audio del sistema no disponible: {e}")
        return False

    print(f"Grabando {duration} s del audio del sistema…")
    rec.start()
    blocks = []
    total = int(duration * samplerate / rec.block_frames)
    for i in range(total):
        blocks.append(rec.read())
        print(f"  {i + 1}/{total}", end="\r")
    rec.close()

    audio = np.concatenate(blocks)
    audio, silent = capture.normalize(audio)
    sf.write(filename, audio, samplerate)
    print(f"\nAudio guardado en {filename}")
    if silent:
        print("ADVERTENCIA: solo silencio — ¿había algo sonando?")
        return False
    return True


def record_input(filename, duration=5, samplerate=48000, device_index=None):
    """Graba desde un dispositivo de entrada (micrófono o VB-Cable)."""
    inputs = capture.list_input_devices()
    if not inputs:
        print("No hay dispositivos de entrada.")
        return False
    if device_index is None:
        device_index = inputs[0][0]
        print("Dispositivos de entrada:")
        for idx, name, _ in inputs:
            print(f"  [{idx}] {name}")
        try:
            choice = input(f"Dispositivo [{device_index}]: ").strip()
            if choice:
                device_index = int(choice)
        except ValueError:
            pass

    print(f"Grabando {duration} s…")
    audio = sd.rec(int(duration * samplerate), samplerate=samplerate,
                   channels=1, device=device_index)
    for remaining in range(duration, 0, -1):
        print(f"  {remaining} s restantes", end="\r")
        time.sleep(1)
    sd.wait()
    audio, silent = capture.normalize(audio)
    sf.write(filename, audio, samplerate)
    print(f"\nAudio guardado en {filename}")
    return not silent


def reproducir_audio(filename):
    try:
        data, sr = sf.read(filename)
        print(f"Reproduciendo {filename}…")
        sd.play(data, sr)
        sd.wait()
        return True
    except Exception as e:
        print(f"Error al reproducir: {e}")
        return False


if __name__ == "__main__":
    list_audio_devices()

    out = "virtual_audio.wav"
    if capture.loopback_available():
        record_system_audio(out, duration=5)
    else:
        print("\nSin loopback; probando dispositivo de entrada…")
        record_input(out, duration=5)

    if os.path.exists(out):
        verificar_audio(out)
        reproducir_audio(out)
