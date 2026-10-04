"""Clientes de API: transcripción (vía transcriber.py) y GPT.

Cambios respecto a la versión original:
- whisper-1 -> gpt-4o-transcribe (y más proveedores: groq, local, realtime).
- chat.completions + gpt-3.5-turbo -> Responses API con gpt-6-luna.
- La API key se puede dar por variable de entorno además de api_key.txt.
"""

import os
import json

from dotenv import load_dotenv
from PySide6.QtCore import QThread, Signal

import transcriber

load_dotenv()


def _key_file(provider):
    return "api_key.txt" if provider in ("openai", "openai-realtime") else f"{provider}_api_key.txt"


class ApiKeyManager:
    """Gestiona el almacenamiento y recuperación de API keys por proveedor."""

    @staticmethod
    def save_api_key(api_key, provider="openai"):
        """Guarda la API key en un archivo local (ignorado por git)."""
        try:
            with open(_key_file(provider), "w") as f:
                f.write(api_key)
            return True
        except Exception as e:
            print(f"Error al guardar API key: {e}")
            return False

    @staticmethod
    def load_api_key(provider="openai"):
        """Orden de búsqueda: variable de entorno -> archivo local."""
        env_name = transcriber.PROVIDERS.get(provider, {}).get("key_env")
        if env_name and os.environ.get(env_name):
            return os.environ[env_name].strip()
        try:
            path = _key_file(provider)
            if os.path.exists(path):
                with open(path, "r") as f:
                    return f.read().strip()
        except Exception as e:
            print(f"Error al cargar API key: {e}")
        return ""


class TranscriptionThread(QThread):
    """Hilo para transcribir un archivo de audio sin bloquear la UI."""

    transcription_complete = Signal(bool, str)

    def __init__(self, api_key, filename, language=None,
                 provider=transcriber.DEFAULT_PROVIDER, model=transcriber.DEFAULT_MODEL):
        super().__init__()
        self.api_key = api_key
        self.filename = filename
        self.language = language
        self.provider = provider
        self.model = model

    def run(self):
        try:
            text = transcriber.transcribe_file(
                self.api_key, self.filename, self.language,
                provider=self.provider, model=self.model,
            )
            self.transcription_complete.emit(True, text)
        except Exception as e:
            self.transcription_complete.emit(False, str(e))


class WhisperService:
    """Fachada de compatibilidad sobre transcriber.py."""

    @staticmethod
    def get_available_languages():
        return {
            "": "Auto-detectar",
            "es": "Español",
            "en": "Inglés",
            "fr": "Francés",
            "de": "Alemán",
            "it": "Italiano",
            "pt": "Portugués",
            "nl": "Holandés",
            "ru": "Ruso",
            "zh": "Chino",
            "ja": "Japonés",
            "ar": "Árabe",
        }

    @staticmethod
    def transcribe_file(api_key, file_path, language=None,
                        provider=transcriber.DEFAULT_PROVIDER,
                        model=transcriber.DEFAULT_MODEL):
        """Transcribe un archivo de forma síncrona (útil en scripts)."""
        try:
            return transcriber.transcribe_file(
                api_key, file_path, language, provider=provider, model=model
            )
        except Exception as e:
            return f"[Error: {str(e)}]"


DEFAULT_GPT_CONFIG = {
    "model": "gpt-6-luna",
    "system_prompt": (
        "Eres un asistente virtual experto que ayuda a los usuarios a responder "
        "preguntas sobre conceptos técnicos y resolver problemas de programación "
        "típicos de entrevistas técnicas. Analiza el texto proporcionado (que viene "
        "de una transcripción de audio, por lo que puede tener errores) y busca en "
        "la transcripción preguntas, aunque no estén explícitamente formuladas. "
        "Por ejemplo, si el texto trata de un problema típico de entrevistas "
        "técnicas estilo leetcode, interpreta que es una pregunta técnica y "
        "devuelve código en Python que lo resuelva.\n\n"
        "1. PREGUNTAS CONCEPTUALES:\n"
        "- Explicaciones claras y concisas de estadística, machine learning o "
        "programación.\n"
        "- Definición, puntos clave y ejemplos cuando sea apropiado.\n"
        "- Responde directamente, sin introducciones largas.\n\n"
        "2. PREGUNTAS DE PROGRAMACIÓN:\n"
        "- Si la pregunta es sobre Python (o no especifica lenguaje), código en "
        "Python limpio y bien comentado.\n"
        "- Si la pregunta es claramente sobre SQL y se pide una consulta, código "
        "SQL optimizado.\n"
        "- Explica brevemente la lógica del código.\n\n"
        "Responde en el mismo idioma de la pregunta (español o inglés). Ignora "
        "texto en otros idiomas por posibles fallos de transcripción. Sé preciso "
        "y directo."
    ),
    "temperature": None,          # los modelos de razonamiento (gpt-5/6) no admiten temperature
    "reasoning_effort": "low",    # respuestas rápidas y baratas; sube a "medium" para más calidad
    "max_tokens": 2000,
}


class GptClient:
    """Cliente GPT usando la Responses API (la API recomendada actualmente)."""

    @staticmethod
    def load_config():
        """Carga gpt_config.json; si no existe lo crea con valores actuales."""
        config_path = os.path.join(os.path.dirname(__file__), "gpt_config.json")

        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_GPT_CONFIG, f, indent=2, ensure_ascii=False)
            return dict(DEFAULT_GPT_CONFIG)

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Rellenar claves nuevas que falten en configs antiguas
            merged = dict(DEFAULT_GPT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception as e:
            print(f"Error al cargar la configuración GPT: {e}")
            return None

    @staticmethod
    def send_to_gpt(api_key, transcription):
        """Envía la transcripción a GPT y devuelve (ok, respuesta_o_error)."""
        try:
            from openai import OpenAI, BadRequestError
        except ImportError:
            return False, "El paquete openai no está instalado"

        config = GptClient.load_config()
        if not config:
            return False, "Error al cargar la configuración de GPT"

        client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

        kwargs = {
            "model": config.get("model", DEFAULT_GPT_CONFIG["model"]),
            "instructions": config.get("system_prompt", "Eres un asistente útil."),
            "input": f"Transcription: {transcription}",
            "max_output_tokens": config.get("max_tokens", 2000),
        }
        if config.get("temperature") is not None:
            kwargs["temperature"] = config["temperature"]
        if config.get("top_p") is not None:
            kwargs["top_p"] = config["top_p"]
        if config.get("reasoning_effort"):
            kwargs["reasoning"] = {"effort": config["reasoning_effort"]}

        try:
            response = client.responses.create(**kwargs)
        except BadRequestError as e:
            # Los modelos de razonamiento rechazan temperature/top_p; los no
            # razonadores pueden rechazar `reasoning`. Reintentar sin ellos.
            msg = str(e).lower()
            if "unsupported" in msg or "unknown" in msg or "invalid" in msg:
                for key in ("temperature", "top_p", "reasoning"):
                    kwargs.pop(key, None)
                try:
                    response = client.responses.create(**kwargs)
                except Exception as e2:
                    return False, f"Error al comunicarse con GPT: {e2}"
            else:
                return False, f"Error al comunicarse con GPT: {e}"
        except Exception as e:
            return False, f"Error al comunicarse con GPT: {e}"

        text = getattr(response, "output_text", None)
        if not text:
            return False, "La API no devolvió texto"
        return True, text


class GptQueryThread(QThread):
    """Hilo para enviar consultas a GPT sin bloquear la interfaz."""

    query_complete = Signal(bool, str)

    def __init__(self, api_key, transcription):
        super().__init__()
        self.api_key = api_key
        self.transcription = transcription

    def run(self):
        success, result = GptClient.send_to_gpt(self.api_key, self.transcription)
        self.query_complete.emit(success, result)
