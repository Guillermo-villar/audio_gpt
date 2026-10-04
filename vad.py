"""Segmentación de audio por voz (VAD) con webrtcvad.

En lugar de cortar el audio en fragmentos de duración fija (que cortan
palabras por la mitad y desperdician llamadas API en silencio), este
módulo agrupa frames de 30 ms en segmentos de habla reales.
"""

import numpy as np

try:
    import webrtcvad
    _VAD_AVAILABLE = True
except ImportError:
    _VAD_AVAILABLE = False


VAD_SAMPLE_RATE = 16000          # webrtcvad solo admite 8/16/32/48 kHz
FRAME_MS = 30                    # duración de frame permitida: 10, 20 o 30 ms


def is_available():
    return _VAD_AVAILABLE


def _require_vad():
    if not _VAD_AVAILABLE:
        raise RuntimeError(
            "webrtcvad no está instalado. Instálalo con: pip install webrtcvad-wheels"
        )


def float_to_pcm16(audio):
    """Convierte audio float32 [-1, 1] a bytes PCM16 mono."""
    pcm = np.clip(audio, -1.0, 1.0)
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    return (pcm * 32767).astype(np.int16).tobytes()


def resample_linear(audio, orig_sr, target_sr):
    """Remuestreo por interpolación lineal (suficiente para ASR)."""
    if orig_sr == target_sr:
        return audio
    duration = audio.shape[0] / orig_sr
    n_target = int(duration * target_sr)
    if n_target <= 0:
        return np.zeros(0, dtype=audio.dtype)
    x_old = np.linspace(0.0, duration, num=audio.shape[0], endpoint=False)
    x_new = np.linspace(0.0, duration, num=n_target, endpoint=False)
    if audio.ndim > 1:
        return np.column_stack(
            [np.interp(x_new, x_old, audio[:, c]) for c in range(audio.shape[1])]
        )
    return np.interp(x_new, x_old, audio)


class VadSegmenter:
    """Acumula frames de audio y emite segmentos de habla completos.

    Uso:
        seg = VadSegmenter(sample_rate=16000)
        for chunk in audio_stream:
            for segment in seg.push(chunk):
                enviar_a_transcribir(segment)   # np.ndarray float32 mono
        for segment in seg.flush():
            enviar_a_transcribir(segment)
    """

    def __init__(
        self,
        sample_rate=VAD_SAMPLE_RATE,
        aggressiveness=2,          # 0 (permisivo) a 3 (agresivo)
        min_speech_ms=300,         # ignorar ruidos más cortos que esto
        max_segment_s=28,          # cortar segmentos más largos que esto
        trailing_silence_ms=500,   # silencio que marca el fin de un turno
        padding_ms=200,            # contexto que se conserva antes del habla
    ):
        _require_vad()
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError("sample_rate debe ser 8000, 16000, 32000 o 48000")
        self.sample_rate = sample_rate
        self.vad = webrtcvad.Vad(aggressiveness)
        self.frame_len = int(sample_rate * FRAME_MS / 1000)
        self.min_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = int(max_segment_s * 1000 / FRAME_MS)
        self.trailing_frames = max(1, trailing_silence_ms // FRAME_MS)
        self.padding_frames = max(0, padding_ms // FRAME_MS)

        self._pending = b""        # bytes PCM16 aún no agrupados en frames
        self._ring = []            # frames previos al inicio del habla
        self._current = []         # frames del segmento en curso
        self._voiced = 0           # frames con voz dentro del segmento
        self._silence_run = 0      # frames de silencio consecutivos

    def _frames_from_pending(self):
        while len(self._pending) >= self.frame_len * 2:
            frame = self._pending[: self.frame_len * 2]
            self._pending = self._pending[self.frame_len * 2 :]
            yield frame

    def push(self, audio, is_last=False):
        """Añade audio (float32 mono o estéreo) y devuelve segmentos completos."""
        self._pending += float_to_pcm16(audio)
        segments = []
        for frame in self._frames_from_pending():
            segment = self._consume_frame(frame)
            if segment is not None:
                segments.append(segment)
        if is_last:
            segments.extend(self.flush())
        return segments

    def _consume_frame(self, frame):
        voiced = self.vad.is_speech(frame, self.sample_rate)

        if self._current:
            self._current.append(frame)
            if not voiced:
                self._silence_run += 1
            else:
                self._silence_run = 0
                self._voiced += 1

            if self._silence_run >= self.trailing_frames or len(self._current) >= self.max_frames:
                segment = b"".join(self._current)
                enough_voice = self._voiced >= self.min_frames
                self._current = []
                self._voiced = 0
                self._silence_run = 0
                if not enough_voice:
                    return None  # ruido corto: no vale una llamada API
                return np.frombuffer(segment, dtype=np.int16).astype(np.float32) / 32768.0
            return None

        # En silencio: mantener un anillo de frames de contexto
        self._ring.append(frame)
        if len(self._ring) > self.padding_frames:
            self._ring.pop(0)

        if voiced:
            # El frame ya está en el anillo; arranca el segmento con el
            # contexto previo incluido.
            self._current = self._ring[:]
            self._ring = []
            self._voiced = 1
            self._silence_run = 0
        return None

    def flush(self):
        """Devuelve el segmento en curso (si tiene suficiente voz)."""
        segments = []
        if self._voiced >= self.min_frames:
            segment = b"".join(self._current)
            segments.append(
                np.frombuffer(segment, dtype=np.int16).astype(np.float32) / 32768.0
            )
        self._current = []
        self._ring = []
        self._voiced = 0
        self._silence_run = 0
        self._pending = b""
        return segments


def has_voice(audio, sample_rate, aggressiveness=2, min_voiced_ratio=0.1):
    """Devuelve True si el audio contiene suficiente habla para transcribir.

    Sustituye al antiguo verificar_audio() basado solo en amplitud: evita
    mandar a la API ruido constante (ventiladores, zumbidos) sin voz.
    """
    _require_vad()
    vad = webrtcvad.Vad(aggressiveness)
    pcm = float_to_pcm16(resample_linear(audio, sample_rate, VAD_SAMPLE_RATE))
    frame_len = VAD_SAMPLE_RATE * FRAME_MS // 1000 * 2
    voiced = total = 0
    for i in range(0, len(pcm) - frame_len, frame_len):
        total += 1
        if vad.is_speech(pcm[i : i + frame_len], VAD_SAMPLE_RATE):
            voiced += 1
    return total > 0 and (voiced / total) >= min_voiced_ratio
