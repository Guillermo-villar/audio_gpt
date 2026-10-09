"""Guion de transcripción simulado (AUDIO_GPT_MOCK): sustituye la captura
de audio y el STT por un QTimer que emite parciales y finales por la misma
señal `realtime_text` que usan Deepgram/OpenAI Realtime, así la app
recorre exactamente el mismo camino (_append_transcript → _ctx → prewarm)."""

import json
import os

from PySide6.QtCore import QObject, QTimer

E, T = "Entrevistador", "Tú"

# (segundos de espera antes de empezar a hablar, carril, texto)
DEFAULT_SCRIPT = [
    (2, E, "Hi, thanks for joining. Let's start with a classic one: design "
           "a distributed rate limiter for our public API. We have around "
           "fifty million daily active users and several thousand "
           "enterprise clients with different quotas. Take it wherever you "
           "want, but I care about accuracy at the edges and about what "
           "happens when things fail."),
    (8, T, "Vale, primero acoto el alcance: límite por API key y por IP, "
           "ventanas de un segundo y de un minuto, y quiero que la decisión "
           "se tome en menos de cinco milisegundos."),
    (6, E, "Fine. What algorithm would you pick and why? Walk me through "
           "the trade-offs between a sliding window log and a token bucket."),
    (9, T, "Token bucket en Redis con un script Lua para que la lectura y "
           "la escritura sean atómicas."),
    (7, E, "Okay, so now scale it. Redis is a single point of failure and "
           "you have fifty million users. How do you shard, what happens on "
           "a hot key like one huge customer hammering a single endpoint, "
           "and how do you keep the counters consistent across regions?"),
    (12, T, "Shardeo por API key con hashing consistente y acepto algo de "
            "sobre-admisión durante un failover."),
    (6, E, "Let's say Redis goes down completely for thirty seconds. Do you "
           "fail open or fail closed? Convince me."),
    (10, T, "Fail open con un límite local conservador en cada nodo."),
    (6, E, "Good. Switch gears. Write a function that, given a stream of "
           "request timestamps for one key, returns whether the request is "
           "allowed under a sliding window of N requests per minute. Python "
           "is fine, and tell me the complexity."),
    (14, T, "Uso un deque con los timestamps y voy sacando por la izquierda "
            "los que ya salieron de la ventana."),
    (6, E, "Last one before we wrap: tell me about a time you had to push "
           "back on a product requirement because of a reliability risk. "
           "What did you do and what was the outcome?"),
    (10, T, "Fue en un proyecto de pagos, propusimos un feature flag y un "
            "rollout gradual."),
    (8, E, "Great. And how would you monitor this rate limiter in "
           "production — what SLOs and which metrics would page you?"),
]


def load_script():
    path = os.environ.get("AUDIO_GPT_MOCK_SCRIPT")
    if not path:
        return list(DEFAULT_SCRIPT)
    with open(path, "r", encoding="utf-8") as f:
        return [tuple(item) for item in json.load(f)]


class ScriptFeeder(QObject):
    PARTIALS = 4          # parciales por frase antes del final
    PARTIAL_GAP_MS = 350

    def __init__(self, app, script, speed=1.0):
        super().__init__(app)
        self.app = app
        self.script = script
        self.speed = max(0.05, speed)
        self._i = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._speak)
        self._active = False

    def start(self):
        self._active = True
        self._schedule()

    def stop(self):
        self._active = False
        self._timer.stop()

    def _schedule(self):
        if not self._active or self._i >= len(self.script):
            return
        delay, _, _ = self.script[self._i]
        self._timer.start(int(delay * 1000 / self.speed))

    def _speak(self):
        if not self._active:
            return
        _, lane, text = self.script[self._i]
        self._i += 1
        words = text.split()
        step = max(1, len(words) // self.PARTIALS)
        cuts = [" ".join(words[:n]) for n in range(step, len(words), step)]
        gap = int(self.PARTIAL_GAP_MS / self.speed)
        for k, partial in enumerate(cuts):
            QTimer.singleShot(
                gap * k, lambda p=partial, l=lane:
                self._emit(p, False, l))
        QTimer.singleShot(
            gap * len(cuts), lambda t=text, l=lane: self._final(t, l))

    def _emit(self, text, final, lane):
        if self._active:
            self.app.realtime_text.emit(text, final, lane, None)

    def _final(self, text, lane):
        self._emit(text, True, lane)
        self._schedule()


def install():
    import capture
    import gui
    import transcriber
    import vad

    speed = float(os.environ.get("AUDIO_GPT_MOCK_SPEED") or 1.0)
    for meta in transcriber.PROVIDERS.values():
        meta["streaming"] = True
    vad.is_available = lambda: True
    capture.loopback_available = lambda: True

    def _start_realtime(self, language, model):
        self.realtimes = {}
        self._rings = {}
        feeder = ScriptFeeder(self, load_script(), speed)
        self._mock_feeder = feeder
        feeder.start()
        self.status_bar.showMessage(
            "MOCK: transcripción simulada, sin llamadas reales")
        return True

    original_stop = gui.WhisperApp.stop_continuous_mode

    def stop_continuous_mode(self):
        feeder = getattr(self, "_mock_feeder", None)
        if feeder is not None:
            feeder.stop()
            self._mock_feeder = None
        original_stop(self)

    gui.WhisperApp._start_realtime = _start_realtime
    gui.WhisperApp.stop_continuous_mode = stop_continuous_mode
