"""Captura de audio del sistema y del micrófono.

En Windows se usa el loopback WASAPI vía `soundcard`, que permite grabar
directamente lo que suena por el altavoz/auriculares — sin instalar
VB-Cable ni cambiar el dispositivo de salida predeterminado.

`sounddevice` queda como respaldo para micrófono y para equipos donde
soundcard no esté disponible.
"""

import numpy as np

try:
    import soundcard as sc
    _SOUNDCARD_AVAILABLE = True
except Exception:
    _SOUNDCARD_AVAILABLE = False

import sounddevice as sd


DEFAULT_SAMPLERATE = 48000


def loopback_available():
    """True si hay un endpoint de loopback WASAPI usable (Windows)."""
    return _SOUNDCARD_AVAILABLE and default_loopback() is not None


def _loopback_microphones():
    """Endpoints de salida expuestos por WASAPI como micrófonos-loopback."""
    return [
        mic for mic in sc.all_microphones(include_loopback=True)
        if getattr(mic, "isloopback", False)
    ]


def _device_by_id(devices, device_id):
    """Localiza un endpoint por ID sin consultar nombres de todos los endpoints."""
    return next((device for device in devices if device.id == device_id), None)


def loopback_display_name(mic):
    """Nombre legible sin tocar `Microphone.name`, inestable en algunos drivers."""
    if mic is None:
        return ""
    try:
        if mic.id == sc.default_speaker().id:
            idx = sd.default.device[1]
            return sd.query_devices(idx)["name"]
    except Exception:
        pass
    return mic.id


def default_loopback():
    """Micrófono-loopback del altavoz predeterminado, o None."""
    if not _SOUNDCARD_AVAILABLE:
        return None
    try:
        speaker = sc.default_speaker()
        return _device_by_id(_loopback_microphones(), speaker.id)
    except Exception:
        return None


def list_loopback_devices():
    """Todos los dispositivos de salida capturables por loopback."""
    if not _SOUNDCARD_AVAILABLE:
        return []
    return [
        (f"Loopback WASAPI {i + 1}", mic)
        for i, mic in enumerate(_loopback_microphones())
    ]


def list_input_devices():
    """Dispositivos de entrada clásicos (micrófono, Stereo Mix, VB-Cable)."""
    return [
        (i, d["name"], d["max_input_channels"])
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


def find_device_by_name(name_fragment):
    """Índice sounddevice del primer dispositivo cuyo nombre coincida."""
    for idx, device in enumerate(sd.query_devices()):
        if name_fragment.lower() in device["name"].lower():
            return idx
    return None


class LoopbackRecorder:
    """Graba audio del sistema por loopback WASAPI en bloques.

    Con devolver float32 [-1, 1] a `samplerate` Hz con `channels` canales.
    Devuelve objetos compatibles con un generador `blocks()`.
    """

    def __init__(self, device=None, samplerate=DEFAULT_SAMPLERATE, channels=2, block_ms=100):
        self.device = device if device is not None else default_loopback()
        if self.device is None:
            raise RuntimeError(
                "No se encontró ningún dispositivo de loopback WASAPI. "
                "Necesitas Windows con salida de audio activa, o usa el modo micrófono."
            )
        self.samplerate = samplerate
        self.channels = channels
        self.block_frames = int(samplerate * block_ms / 1000)
        self._recorder = None

    def start(self):
        self._recorder = self.device.recorder(samplerate=self.samplerate, channels=self.channels)
        self._recorder.__enter__()

    def read(self):
        """Lee un bloque (float32). Bloqueante."""
        return self._recorder.record(numframes=self.block_frames)

    def blocks(self, running=lambda: True):
        """Generador de bloques mientras running() sea True."""
        self.start()
        try:
            while running():
                yield self.read()
        finally:
            self.close()

    def close(self):
        if self._recorder is not None:
            try:
                self._recorder.__exit__(None, None, None)
            except Exception:
                pass
            self._recorder = None


def normalize(audio, target_peak=0.9, silence_floor=0.005):
    """Normaliza el pico a target_peak; devuelve (audio, es_silencio)."""
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak < silence_floor:
        return audio, True
    return audio / peak * target_peak, False
