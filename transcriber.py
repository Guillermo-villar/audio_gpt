"""Motor de transcripción con varios proveedores.

Proveedores soportados:
- openai           API de transcripción de OpenAI (gpt-4o-transcribe por
                   defecto; también mini y diarize con etiquetas de hablante).
- openai-realtime  Sesión de transcripción en tiempo real de la Realtime API:
                   audio PCM16 a 24 kHz por WebSocket, VAD en servidor y
                   transcripciones finales por turno de habla.
- groq             whisper-large-v3-turbo hospedado en Groq (API compatible
                   con OpenAI; ~$0.04/hora, muy rápido).
- local            faster-whisper en la propia máquina (gratis, offline).

Todos devuelven texto plano; el modelo diarize devuelve líneas
"Hablante A: ..." para conservar las etiquetas de hablante.
"""

import base64
import json
import os
import threading

import numpy as np

OPENAI_MODELS = [
    ("gpt-4o-transcribe", "GPT-4o Transcribe (mejor calidad)"),
    ("gpt-4o-mini-transcribe", "GPT-4o Mini Transcribe (barato)"),
    ("gpt-4o-transcribe-diarize", "GPT-4o Transcribe Diarize (etiqueta hablantes)"),
    ("whisper-1", "Whisper-1 (legacy)"),
]

GROQ_MODELS = [
    ("whisper-large-v3-turbo", "Whisper Large v3 Turbo (rápido y casi gratis)"),
    ("whisper-large-v3", "Whisper Large v3"),
]

LOCAL_MODELS = [
    ("large-v3-turbo", "Large v3 Turbo (mejor, ~1.6 GB)"),
    ("large-v3", "Large v3 (~3 GB)"),
    ("small", "Small (ligero)"),
    ("base", "Base (muy ligero)"),
]

REALTIME_MODELS = [
    ("gpt-4o-transcribe", "GPT-4o Transcribe (streaming)"),
    ("gpt-4o-transcribe-diarize", "GPT-4o Transcribe Diarize (streaming)"),
    ("gpt-live-transcribe", "GPT Live Transcribe (menor latencia)"),
]

PROVIDERS = {
    "openai": {
        "label": "OpenAI (nube)",
        "models": OPENAI_MODELS,
        "key_env": "OPENAI_API_KEY",
        "help": "Pago por uso (~$0.003–0.006/min). La mejor calidad sin instalar nada.",
    },
    "openai-realtime": {
        "label": "OpenAI Realtime (streaming)",
        "models": REALTIME_MODELS,
        "key_env": "OPENAI_API_KEY",
        "help": "Transcripción en directo por WebSocket con VAD en servidor.",
    },
    "groq": {
        "label": "Groq (whisper v3 turbo)",
        "models": GROQ_MODELS,
        "key_env": "GROQ_API_KEY",
        "help": "~$0.04/hora. Consigue la key en console.groq.com — API compatible con OpenAI.",
    },
    "local": {
        "label": "Local (faster-whisper, gratis)",
        "models": LOCAL_MODELS,
        "key_env": None,
        "help": "Gratis y privado. Descarga el modelo la primera vez; usa CPU o GPU.",
    },
}

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4o-transcribe"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
REALTIME_SAMPLE_RATE = 24000

# Texto guía para mejorar vocabulario técnico en ES/EN (solo modelos gpt-4o-*)
DEFAULT_PROMPT = (
    "Transcripción de una conversación o presentación técnica en español o inglés. "
    "Puede contener términos de programación, machine learning y entrevistas técnicas."
)


def _client(api_key, provider):
    from openai import OpenAI

    if provider == "groq":
        key = api_key or os.environ.get("GROQ_API_KEY")
        return OpenAI(api_key=key, base_url=GROQ_BASE_URL)
    return OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))


def _format_diarized(result):
    """TranscriptionDiarized -> texto con etiquetas de hablante."""
    lines = []
    for seg in getattr(result, "segments", []) or []:
        speaker = getattr(seg, "speaker", "?")
        text = getattr(seg, "text", "").strip()
        if text:
            lines.append(f"Hablante {speaker}: {text}")
    return "\n".join(lines) if lines else getattr(result, "text", "")


def transcribe_file(api_key, file_path, language=None, provider=DEFAULT_PROVIDER,
                    model=DEFAULT_MODEL, prompt=None):
    """Transcribe un archivo de audio. Devuelve texto o lanza excepción."""
    if provider == "local":
        return _transcribe_local(file_path, language, model)

    client = _client(api_key, provider)
    params = {"model": model, "file": open(file_path, "rb")}
    try:
        if language:
            params["language"] = language
        if model == "gpt-4o-transcribe-diarize":
            params["response_format"] = "diarized_json"
            result = client.audio.transcriptions.create(**params)
            return _format_diarized(result)
        if model.startswith("gpt-4o"):
            params["prompt"] = prompt or DEFAULT_PROMPT
        result = client.audio.transcriptions.create(**params)
        return result.text
    finally:
        params["file"].close()


# ------------------------- motor local -------------------------

_local_model_cache = {}
_local_lock = threading.Lock()


def _transcribe_local(file_path, language, model_size):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "faster-whisper no está instalado. Instálalo con:\n"
            "  pip install faster-whisper"
        )

    with _local_lock:
        if model_size not in _local_model_cache:
            _local_model_cache[model_size] = WhisperModel(
                model_size, device="auto", compute_type="int8"
            )
        model = _local_model_cache[model_size]

    segments, _info = model.transcribe(
        file_path,
        language=language or None,
        vad_filter=True,
        beam_size=5,
    )
    return " ".join(seg.text.strip() for seg in segments if seg.text.strip())


# ------------------------- tiempo real (WebSocket) -------------------------

class RealtimeTranscriber:
    """Sesión de transcripción de la Realtime API por WebSocket.

    - Envía PCM16 mono a 24 kHz con `send_audio`.
    - Con server_vad, la API detecta turnos y emite transcripciones finales.
    - `on_transcript(texto, es_final)` recibe deltas y resultados finales.
    - `on_error(mensaje)` para errores; `on_status` opcional para eventos.
    """

    def __init__(self, api_key, model="gpt-4o-transcribe", language=None,
                 prompt=None, on_transcript=None, on_error=None, on_status=None):
        self.api_key = api_key
        self.model = model
        self.language = language
        self.prompt = prompt
        self.on_transcript = on_transcript or (lambda t, final: None)
        self.on_error = on_error or (lambda e: None)
        self.on_status = on_status or (lambda e: None)
        self._ws = None
        self._recv_thread = None
        self._running = False

    def start(self):
        import websocket  # websocket-client

        self._ws = websocket.create_connection(
            REALTIME_URL,
            header=[f"Authorization: Bearer {self.api_key}", "OpenAI-Beta: realtime=v1"],
            timeout=30,
        )
        self._running = True
        self._send_session_update()
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _send_session_update(self):
        turn_detection = {"type": "server_vad", "threshold": 0.5,
                          "prefix_padding_ms": 300, "silence_duration_ms": 500}
        if self.model == "gpt-live-transcribe":
            turn_detection = None  # este modelo no admite server_vad

        transcription = {"model": self.model}
        if self.language:
            transcription["language"] = self.language
        if self.prompt and self.model != "gpt-4o-transcribe-diarize":
            transcription["prompt"] = self.prompt

        event = {
            "type": "transcription_session.update",
            "session": {
                "type": "transcription",
                "input_audio_format": "pcm16",
                "input_audio_transcription": transcription,
                "turn_detection": turn_detection,
                "input_audio_noise_reduction": {"type": "near_field"},
            },
        }
        self._ws.send(json.dumps(event))

    def send_audio(self, pcm16_bytes):
        """Envía un bloque de audio PCM16 mono 24 kHz."""
        if self._ws is None:
            return
        self._ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm16_bytes).decode("ascii"),
        }))

    def commit(self):
        """Fuerza el fin del turno actual (útil con gpt-live-transcribe)."""
        if self._ws is not None:
            self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    def _recv_loop(self):
        try:
            while self._running:
                raw = self._ws.recv()
                if not raw:
                    break
                event = json.loads(raw)
                etype = event.get("type", "")

                if etype == "conversation.item.input_audio_transcription.delta":
                    self.on_transcript(event.get("delta", ""), False)
                elif etype == "conversation.item.input_audio_transcription.completed":
                    self.on_transcript(event.get("transcript", ""), True)
                elif etype == "error":
                    self.on_error(json.dumps(event.get("error", event)))
                elif etype in ("input_audio_buffer.speech_started",
                               "input_audio_buffer.speech_stopped",
                               "input_audio_buffer.committed"):
                    self.on_status(etype.rsplit(".", 1)[-1])
        except Exception as e:
            if self._running:
                self.on_error(str(e))

    def stop(self):
        self._running = False
        try:
            if self._ws is not None:
                self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                self._ws.close()
        except Exception:
            pass
        self._ws = None


def pcm16_for_realtime(audio_float, src_rate):
    """float32 (mono o estéreo) a cualquier rate -> PCM16 mono 24 kHz."""
    from vad import resample_linear

    audio = resample_linear(audio_float, src_rate, REALTIME_SAMPLE_RATE)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    audio = np.clip(audio, -1.0, 1.0)
    return (audio * 32767).astype(np.int16).tobytes()
