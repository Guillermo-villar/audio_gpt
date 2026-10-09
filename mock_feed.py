"""Guion de transcripción simulado (AUDIO_GPT_MOCK; por defecto, la llamada
exploratoria con Orbio): sustituye la captura
de audio y el STT por un QTimer que emite parciales y finales por la misma
señal `realtime_text` que usan Deepgram/OpenAI Realtime, así la app
recorre exactamente el mismo camino (_append_transcript → _ctx → prewarm)."""

import json
import os

from PySide6.QtCore import QObject, QTimer

E, T = "Entrevistador", "Tú"

# (segundos de espera antes de empezar a hablar, carril, texto)
DEFAULT_SCRIPT = [
    (2, E, "Hola Guillermo, ¿qué tal? Soy Aida, de Orbio. Gracias por hacer "
           "un hueco. Te cuento muy rápido quiénes somos y luego me cuentas "
           "tú, ¿vale?"),
    (5, E, "Somos una startup de Madrid: construimos agentes de IA para "
           "equipos de recursos humanos, selección, onboarding, insights. "
           "Cerramos una Serie A hace poco y estamos creciendo el equipo "
           "técnico. Cuéntame un poco de ti y qué estás haciendo ahora en "
           "AXA."),
    (10, T, "Pues en AXA estoy en el Tech Graduate Program, entre equipos "
            "técnicos y de negocio. He construido un agente de HR con su "
            "pipeline RAG que está en producción, y testing automatizado "
            "con IA."),
    (6, E, "Interesante lo del agente de HR, encaja mucho con lo nuestro. "
           "¿Qué parte construiste tú exactamente, y cuánta gente lo está "
           "usando?"),
    (10, T, "La parte del pipeline RAG y la integración con los equipos de "
            "negocio. La cifra exacta de usuarios no la tengo."),
    (6, E, "Vale. ¿Y por qué quieres salir de AXA ahora? El graduate "
           "program suena bastante bien."),
    (9, T, "Quiero construir IA como parte del producto, con más "
            "responsabilidad y un equipo técnico del que aprender."),
    (6, E, "Te explico un poco el rol: buscamos gente que esté cerca del "
           "cliente, desplegando y adaptando nuestros agentes. ¿Tienes "
           "alguna pregunta hasta aquí?"),
    (8, T, "Sí: ¿para qué rol concreto me estáis considerando, y qué "
            "construiría los primeros tres meses?"),
    (7, E, "Sería un perfil de AI engineer con parte de cliente. Los "
           "primeros meses, integraciones con el ATS del cliente y ajustar "
           "los agentes. ¿Cuáles son tus expectativas salariales?"),
    (9, T, "Prefiero saber primero la banda del rol; así veo si encaja."),
    (6, E, "Perfecto. Y en cuanto a ubicación, ¿estarías cómodo viniendo a "
           "la oficina de Madrid?"),
    (7, T, "Sí, estoy en Madrid. ¿Cuántos días sería, y es lo mismo para "
            "todo el equipo?"),
    (6, E, "Genial. Pues lo siguiente sería una llamada técnica con "
           "Antonio, nuestro CTO. ¿Algo más que quieras saber?"),
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
